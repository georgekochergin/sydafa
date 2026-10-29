"""Мутации Oracle: ограниченный случайный выбор через ROWID + dbms_random.

Один selected set для архива и мутации: сначала выбираются ROWID (FETCH FIRST
со случайной сортировкой), затем тот же набор используется для архивации в
_upd_/_del_ и для самой мутации. Unlimited-ветки и truncate — общие.
"""
from __future__ import annotations

from typing import Any

from ....generation.values import GeneratorError
from ....models import BATCH_ID_COLUMN, Condition, LiveSchema
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


def _rowid_in(rowids: list[Any], start_index: int = 1) -> tuple[str, list[Any]]:
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


def _select_rowids(cur, qualified: str, where: str, where_params: list[Any], top_n: int) -> list[Any]:
    rowid_sql = (
        f"SELECT ROWID FROM {qualified} WHERE {where} "
        f"ORDER BY dbms_random.value FETCH FIRST :{len(where_params) + 1} ROWS ONLY"
    )
    cur.execute(rowid_sql, where_params + [top_n])
    rows = cur.fetchall()
    return [r[0] for r in rows if r]


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
    rowids = _select_rowids(cur, qualified, where, where_params, top_n)
    if not rowids:
        return 0
    if f"{table.schema_}.{upd_name}" in live.tables:
        archive_where, archive_params = _rowid_in(rowids, start_index=2)
        archive_rows(
            conn,
            dialect,
            table,
            qualified,
            dialect.qualify_table(table.schema_, upd_name),
            batch_id,
            where=archive_where,
            where_params=archive_params,
        )
    upd_where, upd_params = _rowid_in(rowids, start_index=len(assign_cols) + 1)
    cur.execute(
        f"UPDATE {qualified} SET {assignments} WHERE {upd_where}",
        list(clean_set.values()) + [batch_id] + upd_params,
    )
    return cur.rowcount if cur.rowcount != -1 else len(rowids)


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
    rowids = _select_rowids(cur, qualified, where, where_params, top_n)
    if not rowids:
        return 0

    if dependents:
        if not cascade:
            raise GeneratorError(
                f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                "удаление заблокировано без cascade=true"
            )
        pk_cols = ", ".join(dialect.quote_identifier(c) for c in table.primary_key)
        dep_pk_where, dep_pk_params = _rowid_in(rowids, start_index=1)
        cur.execute(f"SELECT {pk_cols} FROM {qualified} WHERE {dep_pk_where}", dep_pk_params)
        target_pks = cur.fetchall()
        if target_pks:
            cascade_delete_dependents(conn, cur, dialect, live, table, dependents, target_pks, batch_id)

    if f"{table.schema_}.{del_name}" in live.tables:
        archive_where, archive_params = _rowid_in(rowids, start_index=2)
        archive_rows(
            conn,
            dialect,
            table,
            qualified,
            dialect.qualify_table(table.schema_, del_name),
            batch_id,
            where=archive_where,
            where_params=archive_params,
        )

    del_where, del_params = _rowid_in(rowids, start_index=1)
    cur.execute(f"DELETE FROM {qualified} WHERE {del_where}", del_params)
    return cur.rowcount if cur.rowcount != -1 else len(rowids)


def delete_rows(
    conn: Any, dialect, live: LiveSchema, schema: str, table_name: str,
    conditions: list[Condition], cascade: bool, batch_id: int = 1,
    limit: int | None = None,
) -> int:
    if limit is not None:
        return _delete_limited(conn, dialect, live, schema, table_name, conditions, cascade, batch_id, int(limit))
    return delete_rows_unlimited(conn, dialect, live, schema, table_name, conditions, cascade, batch_id)
