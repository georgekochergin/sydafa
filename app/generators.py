"""Мутации строк (update / delete / truncate) с архивацией и FK-каскадом.

Переходный модуль этапа 4: чистая генерация значений переехала в
`app/generation/values.py`, GenerationSession/FK-оркестрация — в
`app/backends/rdbms/generation.py`. Здесь остаётся вендорный DML-исполнитель,
который на этапе 5 разделяется на общие workflows и vendor-мутации.
"""
from __future__ import annotations

from typing import Any

from .backends.rdbms.generation import find_table as _find_table
from .generation.values import GeneratorError
from .models import (
    BATCH_ID_COLUMN,
    Condition,
    EXEC_BATCH_ID_COLUMN,
    ForeignKey,
    LiveSchema,
    TableSchema,
)
from .render_ddl import qualify_table, quote_ident
from .sources import get_source


class PredicateError(ValueError):
    pass


def _placeholder(dialect: str, index: int) -> str:
    return "%s" if dialect == "mssql" else f":{index}"


def _validate_column(table: TableSchema, column: str) -> None:
    if column not in table.column_names:
        raise PredicateError(f"Неизвестная колонка '{column}' в таблице '{table.schema_}.{table.name}'")


def _validate_set(table: TableSchema, set_values: dict[str, Any]) -> None:
    for column in set_values:
        _validate_column(table, column)


def _build_where(
    table: TableSchema, conditions: list[Condition], dialect: str, start_index: int = 1
) -> tuple[str, list[Any]]:
    if not conditions:
        return "1=1", []
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        _validate_column(table, condition.column)
        col_expr = quote_ident(condition.column, dialect)
        index = start_index + len(params)
        clauses.append(f"{col_expr} {condition.operator} {_placeholder(dialect, index)}")
        params.append(condition.value)
    return " AND ".join(clauses), params


def _build_rowid_where(rowids: list[Any], start_index: int = 1) -> tuple[str, list[Any]]:
    chunk_size = 1000
    clauses: list[str] = []
    params: list[Any] = []
    idx = start_index
    for i in range(0, len(rowids), chunk_size):
        chunk = rowids[i : i + chunk_size]
        placeholders = ", ".join(f":{idx + j}" for j in range(len(chunk)))
        clauses.append(f"ROWID IN ({placeholders})")
        params.extend(chunk)
        idx += len(chunk)
    return " OR ".join(clauses), params


def _cascade_delete_dependents(
    conn: Any,
    cur: Any,
    dialect: str,
    live: LiveSchema,
    table: TableSchema,
    dependents: list[tuple[TableSchema, ForeignKey]],
    target_pks: list[Any],
    batch_id: int,
) -> None:
    for dep_table, fk in dependents:
        dep_qualified = qualify_table(dep_table.schema_, dep_table.name, dialect)
        dep_del_name = f"_del_{dep_table.name}"
        dep_del_qualified = qualify_table(dep_table.schema_, dep_del_name, dialect)
        ref_idx = {c: i for i, c in enumerate(table.primary_key)}
        for pk_row in target_pks:
            dep_where = " AND ".join(
                f"{quote_ident(fk_col, dialect)} = {_placeholder(dialect, i)}"
                for i, fk_col in enumerate(fk.columns, start=1)
            )
            values = [pk_row[ref_idx[rc]] for rc in fk.ref_columns]
            if f"{dep_table.schema_}.{dep_del_name}" in live.tables:
                dep_where_archive = " AND ".join(
                    f"{quote_ident(fk_col, dialect)} = {_placeholder(dialect, i)}"
                    for i, fk_col in enumerate(fk.columns, start=2)
                )
                _archive_rows(
                    conn,
                    dialect,
                    dep_table,
                    dep_qualified,
                    dep_del_qualified,
                    batch_id,
                    where=dep_where_archive,
                    where_params=values,
                )
            cur.execute(f"DELETE FROM {dep_qualified} WHERE {dep_where}", values)


def _archive_rows(
    conn: Any,
    dialect: str,
    table: TableSchema,
    src_qualified: str,
    archive_qualified: str,
    batch_id: int,
    where: str | None = None,
    where_params: list[Any] | None = None,
) -> None:
    """Архивирует строки из src-таблицы в audit-таблицу с _exec_batch_id = batch_id.

    Ожидается, что `where` для Oracle использует плейсхолдеры, начинающиеся с :2
    (плейсхолдер :1 занят batch_id в SELECT), для MSSQL — позиционные %s.
    """
    archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
    ins_cols = ", ".join(quote_ident(c, dialect) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
    if dialect == "mssql":
        sel_cols = _placeholder(dialect, 1) + ", " + ", ".join(quote_ident(c, dialect) for c in archive_cols)
    else:
        sel_cols = ":1, " + ", ".join(quote_ident(c, dialect) for c in archive_cols)
    params = [batch_id] + (list(where_params) if where_params else [])
    sql = f"INSERT INTO {archive_qualified} ({ins_cols}) SELECT {sel_cols} FROM {src_qualified}"
    if where:
        sql += f" WHERE {where}"
    cur = conn.cursor()
    cur.execute(sql, params)


def update_rows_conn(
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], set_values: dict[str, Any], batch_id: int = 1, limit: int | None = None
) -> int:
    table = _find_table(live, schema, table_name)
    qualified = qualify_table(table.schema_, table.name, dialect)

    # Системные колонки _batch_id / _exec_batch_id нельзя обновлять напрямую из запроса
    clean_set = {k: v for k, v in set_values.items() if k not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)}
    _validate_set(table, clean_set)

    set_cols = list(clean_set.keys())
    # В основной таблице всегда обновляем _batch_id актуальным идентификатором батча
    assign_cols = set_cols + [BATCH_ID_COLUMN]
    assignments = ", ".join(
        f"{quote_ident(col, dialect)} = {_placeholder(dialect, i)}"
        for i, col in enumerate(assign_cols, start=1)
    )
    cur = conn.cursor()
    upd_name = f"_upd_{table.name}"

    if limit is not None and dialect == "mssql":
        top_n = int(limit)
        where, where_params = _build_where(table, conditions, dialect)
        if f"{table.schema_}.{upd_name}" in live.tables:
            archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
            archive_cols_quoted = ", ".join(quote_ident(c, dialect) for c in archive_cols)
            ins_cols = ", ".join(quote_ident(c, dialect) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
            sql = (
                f"SELECT TOP 0 * INTO #upd_{table.name} FROM {qualified};\n"
                f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
                f"UPDATE cte SET {assignments}\n"
                f"OUTPUT DELETED.* INTO #upd_{table.name};\n"
                f"INSERT INTO {qualify_table(table.schema_, upd_name, dialect)} ({ins_cols}) "
                f"SELECT %s, {archive_cols_quoted} FROM #upd_{table.name};\n"
                f"DROP TABLE #upd_{table.name};"
            )
            all_params = list(where_params) + list(clean_set.values()) + [batch_id] + [batch_id]
        else:
            sql = (
                f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
                f"UPDATE cte SET {assignments};"
            )
            all_params = list(where_params) + list(clean_set.values()) + [batch_id]
        cur.execute(sql, all_params)
        return cur.rowcount if cur.rowcount != -1 else top_n

    if limit is not None and dialect == "oracle":
        top_n = int(limit)
        where, where_params = _build_where(table, conditions, dialect)
        rowid_sql = f"SELECT ROWID FROM {qualified} WHERE {where} ORDER BY dbms_random.value FETCH FIRST :{len(where_params) + 1} ROWS ONLY"
        cur.execute(rowid_sql, where_params + [top_n])
        rows = cur.fetchall()
        rowids = [r[0] for r in rows if r]
        if not rowids:
            return 0
        if f"{table.schema_}.{upd_name}" in live.tables:
            archive_where, archive_params = _build_rowid_where(rowids, start_index=2)
            _archive_rows(
                conn,
                dialect,
                table,
                qualified,
                qualify_table(table.schema_, upd_name, dialect),
                batch_id,
                where=archive_where,
                where_params=archive_params,
            )
        upd_where, upd_params = _build_rowid_where(rowids, start_index=len(assign_cols) + 1)
        cur.execute(f"UPDATE {qualified} SET {assignments} WHERE {upd_where}", list(clean_set.values()) + [batch_id] + upd_params)
        return cur.rowcount if cur.rowcount != -1 else len(rowids)

    where, where_params = _build_where(table, conditions, dialect, start_index=len(assign_cols) + 1)
    params = list(clean_set.values()) + [batch_id] + where_params

    # Архивация строк ДО обновления в _upd_<таблица>
    if f"{table.schema_}.{upd_name}" in live.tables:
        where_archive, _ = _build_where(table, conditions, dialect, start_index=2)
        _archive_rows(
            conn,
            dialect,
            table,
            qualified,
            qualify_table(table.schema_, upd_name, dialect),
            batch_id,
            where=where_archive,
            where_params=where_params,
        )

    cur.execute(f"UPDATE {qualified} SET {assignments} WHERE {where}", params)
    return cur.rowcount if cur.rowcount != -1 else len(params)


def update_rows(
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], set_values: dict[str, Any], limit: int | None = None
) -> int:
    cfg = get_source(source)
    from .db_connectors import get_connection
    with get_connection(source) as conn:
        updated = update_rows_conn(conn, cfg.type, live, schema, table_name, conditions, set_values, limit=limit)
        conn.commit()
        return updated


def delete_rows_conn(
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], cascade: bool, batch_id: int = 1, limit: int | None = None
) -> int:
    table = _find_table(live, schema, table_name)
    qualified = qualify_table(table.schema_, table.name, dialect)

    current_fqn = f"{table.schema_}.{table.name}"
    dependents = [
        (t, fk) for t in live.tables.values() for fk in t.foreign_keys
        if fk.ref_entity_fqn == current_fqn
    ]
    cur = conn.cursor()
    del_name = f"_del_{table.name}"

    if limit is not None and dialect == "mssql":
        top_n = int(limit)
        where, where_params = _build_where(table, conditions, dialect)
        if dependents:
            if not cascade:
                raise GeneratorError(
                    f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                    "удаление заблокировано без cascade=true"
                )
            pk_cols = ", ".join(quote_ident(c, dialect) for c in table.primary_key)
            cur.execute(f"SELECT TOP ({top_n}) {pk_cols} FROM {qualified} WHERE {where} ORDER BY NEWID()", where_params)
            target_pks = cur.fetchall()
            if not target_pks:
                return 0
            _cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)
            if len(table.primary_key) == 1:
                pk_col = quote_ident(table.primary_key[0], dialect)
                del_where = f"{pk_col} IN ({', '.join(['%s'] * len(target_pks))})"
                del_params = [r[0] for r in target_pks]
            else:
                del_where = " OR ".join(
                    f"({' AND '.join(f'{quote_ident(c, dialect)} = %s' for c in table.primary_key)})"
                    for _ in target_pks
                )
                del_params = [val for r in target_pks for val in r]

            if f"{table.schema_}.{del_name}" in live.tables:
                _archive_rows(
                    conn,
                    dialect,
                    table,
                    qualified,
                    qualify_table(table.schema_, del_name, dialect),
                    batch_id,
                    where=del_where,
                    where_params=del_params,
                )
            cur.execute(f"DELETE FROM {qualified} WHERE {del_where}", del_params)
            return cur.rowcount if cur.rowcount != -1 else len(target_pks)

        # Без внешних зависимостей — полностью PK-независимое удаление через CTE + OUTPUT
        if f"{table.schema_}.{del_name}" in live.tables:
            archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
            archive_cols_quoted = ", ".join(quote_ident(c, dialect) for c in archive_cols)
            ins_cols = ", ".join(quote_ident(c, dialect) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
            sql = (
                f"SELECT TOP 0 * INTO #del_{table.name} FROM {qualified};\n"
                f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
                f"DELETE FROM cte\n"
                f"OUTPUT DELETED.* INTO #del_{table.name};\n"
                f"INSERT INTO {qualify_table(table.schema_, del_name, dialect)} ({ins_cols}) "
                f"SELECT %s, {archive_cols_quoted} FROM #del_{table.name};\n"
                f"DROP TABLE #del_{table.name};"
            )
            all_params = list(where_params) + [batch_id]
        else:
            sql = (
                f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
                f"DELETE FROM cte;"
            )
            all_params = list(where_params)
        cur.execute(sql, all_params)
        return cur.rowcount if cur.rowcount != -1 else top_n

    if limit is not None and dialect == "oracle":
        top_n = int(limit)
        where, where_params = _build_where(table, conditions, dialect)
        rowid_sql = f"SELECT ROWID FROM {qualified} WHERE {where} ORDER BY dbms_random.value FETCH FIRST :{len(where_params) + 1} ROWS ONLY"
        cur.execute(rowid_sql, where_params + [top_n])
        rows = cur.fetchall()
        rowids = [r[0] for r in rows if r]
        if not rowids:
            return 0

        if dependents:
            if not cascade:
                raise GeneratorError(
                    f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                    "удаление заблокировано без cascade=true"
                )
            pk_cols = ", ".join(quote_ident(c, dialect) for c in table.primary_key)
            dep_pk_where, dep_pk_params = _build_rowid_where(rowids, start_index=1)
            cur.execute(f"SELECT {pk_cols} FROM {qualified} WHERE {dep_pk_where}", dep_pk_params)
            target_pks = cur.fetchall()
            if target_pks:
                _cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)

        if f"{table.schema_}.{del_name}" in live.tables:
            archive_where, archive_params = _build_rowid_where(rowids, start_index=2)
            _archive_rows(
                conn,
                dialect,
                table,
                qualified,
                qualify_table(table.schema_, del_name, dialect),
                batch_id,
                where=archive_where,
                where_params=archive_params,
            )

        del_where, del_params = _build_rowid_where(rowids, start_index=1)
        cur.execute(f"DELETE FROM {qualified} WHERE {del_where}", del_params)
        return cur.rowcount if cur.rowcount != -1 else len(rowids)

    where, where_params = _build_where(table, conditions, dialect)

    # Архивация удаляемых строк в _del_<таблица> перед удалением
    if f"{table.schema_}.{del_name}" in live.tables:
        where_archive, _ = _build_where(table, conditions, dialect, start_index=2)
        _archive_rows(
            conn,
            dialect,
            table,
            qualified,
            qualify_table(table.schema_, del_name, dialect),
            batch_id,
            where=where_archive,
            where_params=where_params,
        )

    if dependents:
        pk_cols = ", ".join(quote_ident(c, dialect) for c in table.primary_key)
        cur.execute(f"SELECT {pk_cols} FROM {qualified} WHERE {where}", where_params)
        target_pks = cur.fetchall()
        if target_pks:
            if not cascade:
                raise GeneratorError(
                    f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                    "удаление заблокировано без cascade=true"
                )
            _cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)

    cur.execute(f"DELETE FROM {qualified} WHERE {where}", where_params)
    return cur.rowcount


def delete_rows(
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], cascade: bool, limit: int | None = None
) -> int:
    cfg = get_source(source)
    from .db_connectors import get_connection
    with get_connection(source) as conn:
        deleted = delete_rows_conn(conn, cfg.type, live, schema, table_name, conditions, cascade, limit=limit)
        conn.commit()
        return deleted


def truncate_table_conn(
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, cascade: bool = False, batch_id: int = 1
) -> int:
    table = _find_table(live, schema, table_name)
    qualified = qualify_table(table.schema_, table.name, dialect)
    current_fqn = f"{table.schema_}.{table.name}"
    dependents = [
        t for t in live.tables.values()
        if t != table and any(fk.ref_entity_fqn == current_fqn for fk in t.foreign_keys)
    ]
    cur = conn.cursor()

    # Архивация всех текущих строк в _del_<таблица> перед очисткой
    del_name = f"_del_{table.name}"
    if f"{table.schema_}.{del_name}" in live.tables:
        _archive_rows(
            conn,
            dialect,
            table,
            qualified,
            qualify_table(table.schema_, del_name, dialect),
            batch_id,
        )

    if dependents:
        if not cascade:
            for dep in dependents:
                dep_q = qualify_table(dep.schema_, dep.name, dialect)
                cur.execute(f"SELECT COUNT(*) FROM {dep_q}")
                row = cur.fetchone()
                cnt = row[0] if row else 0
                if cnt > 0:
                    raise GeneratorError(
                        f"На таблицу '{table.schema_}.{table.name}' ссылается '{dep.schema_}.{dep.name}' с {cnt} строками; "
                        "truncate заблокирован без cascade=true"
                    )
        else:
            for dep in dependents:
                truncate_table_conn(conn, dialect, live, dep.schema_, dep.name, cascade=True, batch_id=batch_id)

    if dependents:
        cur.execute(f"DELETE FROM {qualified}")
    else:
        cur.execute(f"TRUNCATE TABLE {qualified}")
    return 1
