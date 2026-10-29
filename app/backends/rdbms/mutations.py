"""Общие workflow мутаций: предикаты, архивация, FK-каскады, unlimited DML и truncate.

Владение аудит-таблицами, системными колонками и порядком операций — здесь;
вендорские стратегии ограниченного случайного выбора (MSSQL CTE+OUTPUT,
Oracle ROWID) — в mutations реализаций движков. SQL строится через диалект:
unlimited-ветки update/delete одинаковы для всех движков семейства.
"""
from __future__ import annotations

import logging
from typing import Any

from ...generation.values import GeneratorError
from ...models import (
    BATCH_ID_COLUMN,
    Condition,
    EXEC_BATCH_ID_COLUMN,
    ForeignKey,
    LiveSchema,
    TableSchema,
)
from .generation import find_table

logger = logging.getLogger("sydafa.backends.rdbms.mutations")


class PredicateError(ValueError):
    pass


def validate_column(table: TableSchema, column: str) -> None:
    if column not in table.column_names:
        raise PredicateError(f"Неизвестная колонка '{column}' в таблице '{table.schema_}.{table.name}'")


def build_where(
    table: TableSchema, conditions: list[Condition], dialect, start_index: int = 1
) -> tuple[str, list[Any]]:
    if not conditions:
        return "1=1", []
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        validate_column(table, condition.column)
        col_expr = dialect.quote_identifier(condition.column)
        index = start_index + len(params)
        clauses.append(f"{col_expr} {condition.operator} {dialect.placeholder(index)}")
        params.append(condition.value)
    return " AND ".join(clauses), params


def archive_rows(
    conn: Any,
    dialect,
    table: TableSchema,
    src_qualified: str,
    archive_qualified: str,
    batch_id: int,
    where: str | None = None,
    where_params: list[Any] | None = None,
) -> None:
    """Архивирует строки из src-таблицы в audit-таблицу с _exec_batch_id = batch_id.

    Ожидается, что `where` использует плейсхолдеры, начинающиеся с позиции 2
    (позиция 1 занята batch_id в SELECT); для позиционных диалектов (MSSQL %s)
    нумерация не влияет на вывод.
    """
    archive_cols = [c.name for c in table.columns if c.name != EXEC_BATCH_ID_COLUMN]
    ins_cols = ", ".join(dialect.quote_identifier(c) for c in [EXEC_BATCH_ID_COLUMN] + archive_cols)
    sel_cols = dialect.placeholder(1) + ", " + ", ".join(
        dialect.quote_identifier(c) for c in archive_cols
    )
    params = [batch_id] + (list(where_params) if where_params else [])
    sql = f"INSERT INTO {archive_qualified} ({ins_cols}) SELECT {sel_cols} FROM {src_qualified}"
    if where:
        sql += f" WHERE {where}"
    cur = conn.cursor()
    cur.execute(sql, params)


def cascade_delete_dependents(
    conn: Any,
    cur: Any,
    dialect,
    live: LiveSchema,
    table: TableSchema,
    dependents: list[tuple[TableSchema, ForeignKey]],
    target_pks: list[Any],
    batch_id: int,
) -> None:
    """Архивирует и удаляет строки зависимых таблиц для выбранных PK родителя."""
    for dep_table, fk in dependents:
        dep_qualified = dialect.qualify_table(dep_table.schema_, dep_table.name)
        dep_del_name = f"_del_{dep_table.name}"
        dep_del_qualified = dialect.qualify_table(dep_table.schema_, dep_del_name)
        ref_idx = {c: i for i, c in enumerate(table.primary_key)}
        for pk_row in target_pks:
            dep_where = " AND ".join(
                f"{dialect.quote_identifier(fk_col)} = {dialect.placeholder(i)}"
                for i, fk_col in enumerate(fk.columns, start=1)
            )
            values = [pk_row[ref_idx[rc]] for rc in fk.ref_columns]
            if f"{dep_table.schema_}.{dep_del_name}" in live.tables:
                dep_where_archive = " AND ".join(
                    f"{dialect.quote_identifier(fk_col)} = {dialect.placeholder(i)}"
                    for i, fk_col in enumerate(fk.columns, start=2)
                )
                archive_rows(
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


def _clean_set_values(table: TableSchema, set_values: dict[str, Any]) -> dict[str, Any]:
    # Системные колонки _batch_id / _exec_batch_id нельзя обновлять напрямую из запроса
    clean_set = {k: v for k, v in set_values.items() if k not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)}
    for column in clean_set:
        validate_column(table, column)
    return clean_set


def update_rows_unlimited(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], set_values: dict[str, Any], batch_id: int,
) -> int:
    """UPDATE по предикату с архивацией ДО изменения (общая ветка всех движков)."""
    table = find_table(live, schema, table_name)
    qualified = dialect.qualify_table(table.schema_, table.name)

    clean_set = _clean_set_values(table, set_values)
    set_cols = list(clean_set.keys())
    # В основной таблице всегда обновляем _batch_id актуальным идентификатором батча
    assign_cols = set_cols + [BATCH_ID_COLUMN]
    assignments = ", ".join(
        f"{dialect.quote_identifier(col)} = {dialect.placeholder(i)}"
        for i, col in enumerate(assign_cols, start=1)
    )
    cur = conn.cursor()
    upd_name = f"_upd_{table.name}"

    where, where_params = build_where(table, conditions, dialect, start_index=len(assign_cols) + 1)
    params = list(clean_set.values()) + [batch_id] + where_params

    # Архивация строк ДО обновления в _upd_<таблица>
    if f"{table.schema_}.{upd_name}" in live.tables:
        where_archive, _ = build_where(table, conditions, dialect, start_index=2)
        archive_rows(
            conn,
            dialect,
            table,
            qualified,
            dialect.qualify_table(table.schema_, upd_name),
            batch_id,
            where=where_archive,
            where_params=where_params,
        )

    cur.execute(f"UPDATE {qualified} SET {assignments} WHERE {where}", params)
    return cur.rowcount if cur.rowcount != -1 else len(params)


def delete_rows_unlimited(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], cascade: bool, batch_id: int,
) -> int:
    """DELETE по предикату с архивацией и блокировкой cascade (общая ветка)."""
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

    # Архивация удаляемых строк в _del_<таблица> перед удалением
    if f"{table.schema_}.{del_name}" in live.tables:
        where_archive, _ = build_where(table, conditions, dialect, start_index=2)
        archive_rows(
            conn,
            dialect,
            table,
            qualified,
            dialect.qualify_table(table.schema_, del_name),
            batch_id,
            where=where_archive,
            where_params=where_params,
        )

    if dependents:
        pk_cols = ", ".join(dialect.quote_identifier(c) for c in table.primary_key)
        cur.execute(f"SELECT {pk_cols} FROM {qualified} WHERE {where}", where_params)
        target_pks = cur.fetchall()
        if target_pks:
            if not cascade:
                raise GeneratorError(
                    f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                    "удаление заблокировано без cascade=true"
                )
            cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)

    cur.execute(f"DELETE FROM {qualified} WHERE {where}", where_params)
    return cur.rowcount


def truncate_table(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    cascade: bool = False, batch_id: int = 1,
) -> int:
    """Предметная очистка таблицы: архивация всех строк, каскад зависимых,
    физическую команду выбирает диалект (hard=True — TRUNCATE-семантика; при
    наличии зависимостей очистка идёт построчным DELETE). Рекурсивные шаги
    каскада проходят через эту же функцию — то есть тоже через диалект."""
    table = find_table(live, schema, table_name)
    qualified = dialect.qualify_table(table.schema_, table.name)
    current_fqn = f"{table.schema_}.{table.name}"
    dependents = [
        t for t in live.tables.values()
        if t != table and any(fk.ref_entity_fqn == current_fqn for fk in t.foreign_keys)
    ]
    cur = conn.cursor()

    # Архивация всех текущих строк в _del_<таблица> перед очисткой
    del_name = f"_del_{table.name}"
    if f"{table.schema_}.{del_name}" in live.tables:
        archive_rows(
            conn,
            dialect,
            table,
            qualified,
            dialect.qualify_table(table.schema_, del_name),
            batch_id,
        )

    if dependents:
        if not cascade:
            for dep in dependents:
                dep_q = dialect.qualify_table(dep.schema_, dep.name)
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
                truncate_table(conn, dialect, live, dep.schema_, dep.name, cascade=True, batch_id=batch_id)

    cur.execute(dialect.clear_table(table.schema_, table.name, hard=not dependents))
    return 1
