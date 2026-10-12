"""Генерация и мутация синтетических данных (append / update / delete) с FK-каскадом."""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from faker import Faker

from .models import ColumnSpec, Condition, ForeignKey, LiveSchema, TableSchema
from .sources import get_source

_fake = Faker()


class GeneratorError(ValueError):
    pass


class PredicateError(ValueError):
    pass


def _qualify(dialect: str, schema: str, table_name: str) -> str:
    if dialect == "mssql":
        return f"[{schema}].[{table_name}]"
    return f"{schema}.{table_name}"


def _placeholder(dialect: str, index: int) -> str:
    return "%s" if dialect == "mssql" else f":{index}"


def _generate_value(col: ColumnSpec) -> Any:
    gen = col.generator
    if gen is None:
        return None

    kind = getattr(gen, "kind", None)
    if kind == "faker":
        provider = getattr(gen, "provider", "word")
        return getattr(_fake, provider, _fake.word)()
    if kind == "choice":
        values = getattr(gen, "values", [])
        return random.choice(values) if values else None
    if kind == "random_decimal":
        min_v = getattr(gen, "min", 0.0)
        max_v = getattr(gen, "max", 100000.0)
        return round(random.uniform(min_v, max_v), 2)
    if kind == "random_date":
        start = date.today() - timedelta(days=365)
        return start + timedelta(days=random.randint(0, 365))
    return None


def _fk_for_column(table: TableSchema, column_name: str) -> ForeignKey | None:
    for fk in table.foreign_keys:
        if column_name in fk.columns:
            return fk
    return None


def _next_sequence_value(conn, dialect: str, qualified: str, pk_col: str) -> int:
    cur = conn.cursor()
    col_expr = f"[{pk_col}]" if dialect == "mssql" else f"{pk_col}"
    cur.execute(f"SELECT MAX({col_expr}) FROM {qualified}")
    (current_max,) = cur.fetchone()
    return int(current_max) + 1 if current_max is not None else 1


def _pick_existing_pk(conn, dialect: str, qualified: str, pk_cols: list[str]) -> dict[str, Any] | None:
    cols = ", ".join(f"[{c}]" if dialect == "mssql" else f"{c}" for c in pk_cols)
    cur = conn.cursor()
    cur.execute(f"SELECT {cols} FROM {qualified}")
    rows = cur.fetchall()
    if not rows:
        return None
    row = random.choice(rows)
    return dict(zip(pk_cols, row))


def _insert_row(conn, dialect: str, qualified: str, values: dict[str, Any]) -> None:
    col_names = list(values.keys())
    cols = ", ".join(f"[{c}]" if dialect == "mssql" else f"{c}" for c in col_names)
    placeholders = ", ".join(_placeholder(dialect, i) for i in range(1, len(col_names) + 1))
    cur = conn.cursor()
    cur.execute(f"INSERT INTO {qualified} ({cols}) VALUES ({placeholders})", list(values.values()))


def _find_table(live: LiveSchema, schema: str, table_name: str) -> TableSchema:
    key = f"{schema}.{table_name}"
    if key in live.tables:
        return live.tables[key]
    for t in live.tables.values():
        if t.name == table_name:
            return t
    raise GeneratorError(f"Таблица '{key}' не найдена в схеме источника")


def _resolve_fk_values(
    conn, source: str, dialect: str, live: LiveSchema, fk: ForeignKey, current_schema: str, new_key_ratio: float
) -> dict[str, Any]:
    ref_schema = fk.ref_schema or current_schema
    parent_table = _find_table(live, ref_schema, fk.ref_table)
    parent_qualified = _qualify(dialect, parent_table.schema_, parent_table.name)
    existing = _pick_existing_pk(conn, dialect, parent_qualified, parent_table.primary_key)
    if existing is not None and random.random() >= new_key_ratio:
        parent_pk_values = existing
    else:
        parent_pk_values = _mint_new_row(conn, source, dialect, live, parent_table, new_key_ratio)
    return dict(zip(fk.columns, [parent_pk_values[c] for c in fk.ref_columns]))


def _mint_new_row(
    conn, source: str, dialect: str, live: LiveSchema, table: TableSchema, new_key_ratio: float
) -> dict[str, Any]:
    qualified = _qualify(dialect, table.schema_, table.name)
    values: dict[str, Any] = {}

    for fk in table.foreign_keys:
        values.update(_resolve_fk_values(conn, source, dialect, live, fk, table.schema_, new_key_ratio))

    for col in table.columns:
        if col.name in table.primary_key or _fk_for_column(table, col.name):
            continue
        values[col.name] = _generate_value(col)

    for pk_col in table.primary_key:
        if pk_col not in values:
            values[pk_col] = _next_sequence_value(conn, dialect, qualified, pk_col)

    _insert_row(conn, dialect, qualified, values)
    return {pk: values[pk] for pk in table.primary_key}


def append_rows(source: str, live: LiveSchema, schema: str, table_name: str, rows: int, new_key_ratio: float) -> int:
    table = _find_table(live, schema, table_name)
    cfg = get_source(source)
    dialect = cfg.type

    from .db_connectors import get_connection
    with get_connection(source) as conn:
        for _ in range(rows):
            _mint_new_row(conn, source, dialect, live, table, new_key_ratio)
        conn.commit()
    return rows


def _validate_column(table: TableSchema, column: str) -> None:
    if column not in table.column_names:
        raise PredicateError(f"Неизвестная колонка '{column}' в таблице '{table.schema_}.{table.name}'")


def _validate_set(table: TableSchema, set_values: dict[str, Any]) -> None:
    for column in set_values:
        _validate_column(table, column)


def _build_where(
    table: TableSchema, conditions: list[Condition], dialect: str, start_index: int = 1
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        _validate_column(table, condition.column)
        col_expr = f"[{condition.column}]" if dialect == "mssql" else f"{condition.column}"
        index = start_index + len(params)
        clauses.append(f"{col_expr} {condition.operator} {_placeholder(dialect, index)}")
        params.append(condition.value)
    return " AND ".join(clauses), params


def update_rows(
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], set_values: dict[str, Any]
) -> int:
    table = _find_table(live, schema, table_name)
    cfg = get_source(source)
    dialect = cfg.type
    qualified = _qualify(dialect, table.schema_, table.name)
    _validate_set(table, set_values)

    from .db_connectors import get_connection
    with get_connection(source) as conn:
        set_cols = list(set_values.keys())
        assignments = ", ".join(
            f"{f'[{col}]' if dialect == 'mssql' else col} = {_placeholder(dialect, i)}"
            for i, col in enumerate(set_cols, start=1)
        )
        where, where_params = _build_where(table, conditions, dialect, start_index=len(set_cols) + 1)
        params = list(set_values.values()) + where_params
        cur = conn.cursor()
        cur.execute(f"UPDATE {qualified} SET {assignments} WHERE {where}", params)
        updated = cur.rowcount if cur.rowcount != -1 else len(params)
        conn.commit()
        return updated


def delete_rows(
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], cascade: bool
) -> int:
    table = _find_table(live, schema, table_name)
    cfg = get_source(source)
    dialect = cfg.type
    qualified = _qualify(dialect, table.schema_, table.name)

    dependents = [
        (t, fk) for t in live.tables.values() for fk in t.foreign_keys
        if fk.ref_table == table.name and (fk.ref_schema or t.schema_) == table.schema_
    ]
    where, where_params = _build_where(table, conditions, dialect)

    from .db_connectors import get_connection
    with get_connection(source) as conn:
        cur = conn.cursor()
        if dependents:
            pk_cols = ", ".join(f"[{c}]" if dialect == "mssql" else f"{c}" for c in table.primary_key)
            cur.execute(f"SELECT {pk_cols} FROM {qualified} WHERE {where}", where_params)
            target_pks = cur.fetchall()
            if target_pks:
                if not cascade:
                    raise GeneratorError(
                        f"На таблицу '{table.schema_}.{table.name}' ссылаются {[t.name for t, _ in dependents]}; "
                        "удаление заблокировано без cascade=true"
                    )
                for dep_table, fk in dependents:
                    dep_qualified = _qualify(dialect, dep_table.schema_, dep_table.name)
                    ref_idx = {c: i for i, c in enumerate(table.primary_key)}
                    for pk_row in target_pks:
                        dep_where = " AND ".join(
                            f"{f'[{fk_col}]' if dialect == 'mssql' else fk_col} = {_placeholder(dialect, i)}"
                            for i, fk_col in enumerate(fk.columns, start=1)
                        )
                        values = [pk_row[ref_idx[rc]] for rc in fk.ref_columns]
                        cur.execute(f"DELETE FROM {dep_qualified} WHERE {dep_where}", values)

        cur.execute(f"DELETE FROM {qualified} WHERE {where}", where_params)
        deleted = cur.rowcount
        conn.commit()
        return deleted
