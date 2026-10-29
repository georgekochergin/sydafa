"""Мутации MSSQL: ограниченный случайный выбор через CTE TOP + OUTPUT во temp-table.

Один selected set для архива и мутации: OUTPUT DELETED пишется во временную
таблицу и переиспользуется для INSERT в _upd_/_del_. Unlimited-ветки и truncate —
общие (`rdbms/mutations.py`).
"""
from __future__ import annotations

from typing import Any

from ....generation.values import GeneratorError
from ....models import (
    BATCH_ID_COLUMN,
    Condition,
    EXEC_BATCH_ID_COLUMN,
    LiveSchema,
)
from ..generation import find_table
from ..mutations import (
    _clean_set_values,
    build_where,
    cascade_delete_dependents,
    archive_rows,
    delete_rows_unlimited,
    truncate_table,
    update_rows_unlimited,
)

__all__ = ["delete_rows", "truncate_table", "update_rows"]


def _update_limited(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], set_values: dict[str, Any], batch_id: int, top_n: int,
) -> int:
    table = find_table(live, schema, table_name)
    qualified = dialect.qualify_table(table.schema_, table.name)

    clean_set = _clean_set_values(table, set_values)
    set_cols = list(clean_set.keys())
    assign_cols = set_cols + [BATCH_ID_COLUMN]
    assignments = ", ".join(
        f"{dialect.quote_identifier(col)} = {dialect.placeholder(i)}"
        for i, col in enumerate(assign_cols, start=1)
    )
    cur = conn.cursor()
    upd_name = f"_upd_{table.name}"

    where, where_params = build_where(table, conditions, dialect)
    if f"{table.schema_}.{upd_name}" in live.tables:
        archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
        archive_cols_quoted = ", ".join(dialect.quote_identifier(c) for c in archive_cols)
        ins_cols = ", ".join(dialect.quote_identifier(c) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
        sql = (
            f"SELECT TOP 0 * INTO #upd_{table.name} FROM {qualified};\n"
            f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
            f"UPDATE cte SET {assignments}\n"
            f"OUTPUT DELETED.* INTO #upd_{table.name};\n"
            f"INSERT INTO {dialect.qualify_table(table.schema_, upd_name)} ({ins_cols}) "
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


def update_rows(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], set_values: dict[str, Any], batch_id: int = 1,
    limit: int | None = None,
) -> int:
    if limit is not None:
        return _update_limited(conn, dialect, live, schema, table_name, conditions, set_values, batch_id, int(limit))
    return update_rows_unlimited(conn, dialect, live, schema, table_name, conditions, set_values, batch_id)


def _delete_limited(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], cascade: bool, batch_id: int, top_n: int,
) -> int:
    table = find_table(live, schema, table_name)
    qualified = dialect.qualify_table(table.schema_, table.name)

    current_fqn = f"{table.schema_}.{table.name}"
    dependents = [
        (t, fk) for t in live.tables.values() for fk in t.foreign_keys
        if fk.ref_entity_fqn == current_fqn
    ]
    cur = conn.cursor()
    del_name = f"_del_{table.name}"

    where, where_params = build_where(table, conditions, dialect)
    if dependents:
        if not cascade:
            raise GeneratorError(
                f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                "удаление заблокировано без cascade=true"
            )
        pk_cols = ", ".join(dialect.quote_identifier(c) for c in table.primary_key)
        cur.execute(f"SELECT TOP ({top_n}) {pk_cols} FROM {qualified} WHERE {where} ORDER BY NEWID()", where_params)
        target_pks = cur.fetchall()
        if not target_pks:
            return 0
        cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)
        if len(table.primary_key) == 1:
            pk_col = dialect.quote_identifier(table.primary_key[0])
            del_where = f"{pk_col} IN ({', '.join(['%s'] * len(target_pks))})"
            del_params = [r[0] for r in target_pks]
        else:
            del_where = " OR ".join(
                f"({' AND '.join(f'{dialect.quote_identifier(c)} = %s' for c in table.primary_key)})"
                for _ in target_pks
            )
            del_params = [val for r in target_pks for val in r]

        if f"{table.schema_}.{del_name}" in live.tables:
            archive_rows(
                conn,
                dialect,
                table,
                qualified,
                dialect.qualify_table(table.schema_, del_name),
                batch_id,
                where=del_where,
                where_params=del_params,
            )
        cur.execute(f"DELETE FROM {qualified} WHERE {del_where}", del_params)
        return cur.rowcount if cur.rowcount != -1 else len(target_pks)

    # Без внешних зависимостей — полностью PK-независимое удаление через CTE + OUTPUT
    if f"{table.schema_}.{del_name}" in live.tables:
        archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
        archive_cols_quoted = ", ".join(dialect.quote_identifier(c) for c in archive_cols)
        ins_cols = ", ".join(dialect.quote_identifier(c) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
        sql = (
            f"SELECT TOP 0 * INTO #del_{table.name} FROM {qualified};\n"
            f"WITH cte AS (SELECT TOP ({top_n}) * FROM {qualified} WHERE {where} ORDER BY NEWID())\n"
            f"DELETE FROM cte\n"
            f"OUTPUT DELETED.* INTO #del_{table.name};\n"
            f"INSERT INTO {dialect.qualify_table(table.schema_, del_name)} ({ins_cols}) "
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


def delete_rows(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], cascade: bool, batch_id: int = 1,
    limit: int | None = None,
) -> int:
    if limit is not None:
        return _delete_limited(conn, dialect, live, schema, table_name, conditions, cascade, batch_id, int(limit))
    return delete_rows_unlimited(conn, dialect, live, schema, table_name, conditions, cascade, batch_id)
