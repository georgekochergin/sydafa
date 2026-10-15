"""Генерация и мутация синтетических данных (append / update / delete) с FK-каскадом."""
from __future__ import annotations

import calendar
import random
from datetime import date, datetime, timedelta
from typing import Any

from faker import Faker

from .models import (
    BATCH_ID_COLUMN,
    ColumnSpec,
    Condition,
    EXEC_BATCH_ID_COLUMN,
    ForeignKey,
    LiveSchema,
    TableSchema,
)
from .render_ddl import qualify_table, quote_ident
from .sources import get_source

_faker_cache: dict[str, Faker] = {}


def _get_faker(locale: str) -> Faker:
    if locale not in _faker_cache:
        _faker_cache[locale] = Faker(locale)
    return _faker_cache[locale]


class GeneratorError(ValueError):
    pass


class PredicateError(ValueError):
    pass


def _placeholder(dialect: str, index: int) -> str:
    return "%s" if dialect == "mssql" else f":{index}"


def _resolve_anchor_date(anchor: str) -> date:
    today = date.today()
    if anchor == "start_of_year":
        return date(today.year, 1, 1)
    if anchor == "end_of_year":
        return date(today.year, 12, 31)
    if anchor == "today":
        return today
    if anchor == "start_of_month":
        return date(today.year, today.month, 1)
    if anchor == "start_of_quarter":
        q_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, q_month, 1)
    raise GeneratorError(f"Неизвестная опорная точка anchor: '{anchor}'")


def _shift_date(base: date, dim: str, offset: int) -> date:
    if dim == "d":
        return base + timedelta(days=offset)
    if dim == "m":
        total_months = base.year * 12 + (base.month - 1) + offset
        target_year = total_months // 12
        target_month = (total_months % 12) + 1
        max_days = calendar.monthrange(target_year, target_month)[1]
        target_day = min(base.day, max_days)
        return date(target_year, target_month, target_day)
    if dim == "y":
        target_year = base.year + offset
        if base.month == 2 and base.day == 29:
            target_day = 29 if calendar.isleap(target_year) else 28
        else:
            target_day = base.day
        return date(target_year, base.month, target_day)
    raise GeneratorError(f"Неизвестная размерность dim: '{dim}'. Разрешены 'y', 'm', 'd'")


def _generate_value(col: ColumnSpec, row_values: dict[str, Any] | None = None) -> Any:
    gen = col.generator
    if gen is None:
        return None

    null_ratio = getattr(gen, "null_ratio", None)
    if null_ratio is not None and random.random() < null_ratio:
        return None

    kind = getattr(gen, "kind", None)
    if kind == "faker":
        locale = getattr(gen, "locale", "ru_RU")
        provider = getattr(gen, "provider", "word")
        fk = _get_faker(locale)
        func = getattr(fk, provider, None)
        if func is None:
            func = fk.word
        return func()

    if kind == "choice":
        values = getattr(gen, "values", [])
        if not values:
            return None
        weights = getattr(gen, "weights", None)
        if weights:
            return random.choices(values, weights=weights, k=1)[0]
        return random.choice(values)

    if kind == "random_decimal":
        min_v = getattr(gen, "min", 0.0)
        max_v = getattr(gen, "max", 100000.0)
        return round(random.uniform(min_v, max_v), 2)

    if kind == "random_date":
        anchor = getattr(gen, "anchor", "start_of_year")
        dim = getattr(gen, "dim", "d")
        min_offset = getattr(gen, "min_offset", None)
        max_offset = getattr(gen, "max_offset", None)

        if min_offset is None and max_offset is None:
            if anchor == "start_of_year" and dim == "d":
                year = date.today().year
                start = date(year, 1, 1)
                end = date(year, 12, 31)
                days = (end - start).days
                return start + timedelta(days=random.randint(0, days))
            min_o, max_o = 0, 0
        else:
            min_o = min_offset if min_offset is not None else 0
            max_o = max_offset if max_offset is not None else min_o

        if min_o > max_o:
            min_o, max_o = max_o, min_o

        offset = random.randint(min_o, max_o)
        base = _resolve_anchor_date(anchor)
        return _shift_date(base, dim, offset)

    if kind == "date_offset":
        ref_column = getattr(gen, "ref_column", None)
        if not ref_column:
            raise GeneratorError(f"Для генератора date_offset не указан обязательный параметр ref_column")

        base_val = row_values.get(ref_column) if row_values is not None else None
        if base_val is None:
            return None

        if isinstance(base_val, str):
            base_date = date.fromisoformat(base_val)
        elif isinstance(base_val, datetime):
            base_date = base_val.date()
        elif isinstance(base_val, date):
            base_date = base_val
        else:
            raise GeneratorError(f"Значение колонки '{ref_column}' ({base_val}) не является корректной датой")

        dim = getattr(gen, "dim", "y")
        min_o = getattr(gen, "min_offset", 1)
        max_o = getattr(gen, "max_offset", 5)
        if min_o > max_o:
            min_o, max_o = max_o, min_o

        offset = random.randint(min_o, max_o)
        return _shift_date(base_date, dim, offset)

    return None


def _fk_for_column(table: TableSchema, column_name: str) -> ForeignKey | None:
    for fk in table.foreign_keys:
        if column_name in fk.columns:
            return fk
    return None


def _next_sequence_value(conn, dialect: str, qualified: str, pk_col: str) -> int:
    cur = conn.cursor()
    col_expr = quote_ident(pk_col, dialect)
    cur.execute(f"SELECT MAX({col_expr}) FROM {qualified}")
    (current_max,) = cur.fetchone()
    return int(current_max) + 1 if current_max is not None else 1


def _pick_existing_pk(conn, dialect: str, qualified: str, pk_cols: list[str]) -> dict[str, Any] | None:
    cols = ", ".join(quote_ident(c, dialect) for c in pk_cols)
    cur = conn.cursor()
    cur.execute(f"SELECT {cols} FROM {qualified}")
    rows = cur.fetchall()
    if not rows:
        return None
    row = random.choice(rows)
    return dict(zip(pk_cols, row))


def _insert_row(conn, dialect: str, qualified: str, values: dict[str, Any]) -> None:
    col_names = list(values.keys())
    cols = ", ".join(quote_ident(c, dialect) for c in col_names)
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
    conn, source: str, dialect: str, live: LiveSchema, fk: ForeignKey, current_schema: str, new_key_ratio: float, batch_id: int
) -> dict[str, Any]:
    ref_schema = fk.ref_schema or current_schema
    parent_table = _find_table(live, ref_schema, fk.ref_table)
    parent_qualified = qualify_table(parent_table.schema_, parent_table.name, dialect)
    existing = _pick_existing_pk(conn, dialect, parent_qualified, parent_table.primary_key)
    if existing is not None and random.random() >= new_key_ratio:
        parent_pk_values = existing
    else:
        parent_pk_values = _mint_new_row(conn, source, dialect, live, parent_table, new_key_ratio, batch_id)
    return dict(zip(fk.columns, [parent_pk_values[c] for c in fk.ref_columns]))


def _mint_new_row(
    conn, source: str, dialect: str, live: LiveSchema, table: TableSchema, new_key_ratio: float, batch_id: int = 1
) -> dict[str, Any]:
    qualified = qualify_table(table.schema_, table.name, dialect)
    values: dict[str, Any] = {}

    for fk in table.foreign_keys:
        values.update(_resolve_fk_values(conn, source, dialect, live, fk, table.schema_, new_key_ratio, batch_id))

    candidates: dict[str, ColumnSpec] = {}
    dependencies: dict[str, set[str]] = {}
    all_table_column_names = {c.name for c in table.columns}

    for col in table.columns:
        if col.name in table.primary_key or _fk_for_column(table, col.name):
            continue
        if col.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            continue
        candidates[col.name] = col

        deps: set[str] = set()
        gen = col.generator
        if gen is not None and getattr(gen, "kind", None) == "date_offset":
            ref = getattr(gen, "ref_column", None)
            if not ref or ref not in all_table_column_names:
                raise GeneratorError(
                    f"Колонка '{col.name}' таблицы '{table.name}' ссылается на неизвестную ref_column='{ref}'"
                )
            deps.add(ref)
        dependencies[col.name] = deps

    remaining = set(candidates.keys())
    while remaining:
        ready = [
            c_name for c_name in remaining
            if all(dep in values for dep in dependencies[c_name])
        ]
        if not ready:
            raise GeneratorError(
                f"Обнаружена циклическая или неразрешимая зависимость между колонками в таблице '{table.name}': {sorted(remaining)}"
            )

        for c_name in ready:
            col = candidates[c_name]
            values[c_name] = _generate_value(col, values)
            remaining.remove(c_name)

    for pk_col in table.primary_key:
        if pk_col not in values:
            values[pk_col] = _next_sequence_value(conn, dialect, qualified, pk_col)

    values[BATCH_ID_COLUMN] = batch_id

    _insert_row(conn, dialect, qualified, values)
    return {pk: values[pk] for pk in table.primary_key}


def append_rows_conn(
    conn: Any, source: str, dialect: str, live: LiveSchema, schema: str, table_name: str, rows: int, new_key_ratio: float, batch_id: int = 1
) -> int:
    table = _find_table(live, schema, table_name)
    for _ in range(rows):
        _mint_new_row(conn, source, dialect, live, table, new_key_ratio, batch_id)
    return rows


def append_rows(source: str, live: LiveSchema, schema: str, table_name: str, rows: int, new_key_ratio: float) -> int:
    table = _find_table(live, schema, table_name)
    cfg = get_source(source)
    dialect = cfg.type

    from .db_connectors import get_connection
    with get_connection(source) as conn:
        res = append_rows_conn(conn, source, dialect, live, schema, table_name, rows, new_key_ratio)
        conn.commit()
    return res


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
        col_expr = quote_ident(condition.column, dialect)
        index = start_index + len(params)
        clauses.append(f"{col_expr} {condition.operator} {_placeholder(dialect, index)}")
        params.append(condition.value)
    return " AND ".join(clauses), params


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
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], set_values: dict[str, Any], batch_id: int = 1
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
    where, where_params = _build_where(table, conditions, dialect, start_index=len(assign_cols) + 1)
    params = list(clean_set.values()) + [batch_id] + where_params
    cur = conn.cursor()

    # Архивация строк ДО обновления в _upd_<таблица>
    upd_name = f"_upd_{table.name}"
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
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], set_values: dict[str, Any]
) -> int:
    cfg = get_source(source)
    from .db_connectors import get_connection
    with get_connection(source) as conn:
        updated = update_rows_conn(conn, cfg.type, live, schema, table_name, conditions, set_values)
        conn.commit()
        return updated


def delete_rows_conn(
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], cascade: bool, batch_id: int = 1
) -> int:
    table = _find_table(live, schema, table_name)
    qualified = qualify_table(table.schema_, table.name, dialect)

    dependents = [
        (t, fk) for t in live.tables.values() for fk in t.foreign_keys
        if fk.ref_table == table.name and (fk.ref_schema or t.schema_) == table.schema_
    ]
    where, where_params = _build_where(table, conditions, dialect)
    cur = conn.cursor()

    # Архивация удаляемых строк в _del_<таблица> перед удалением
    del_name = f"_del_{table.name}"
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
                    # Каскадная архивация дочерних строк перед их удалением
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

    cur.execute(f"DELETE FROM {qualified} WHERE {where}", where_params)
    return cur.rowcount


def delete_rows(
    source: str, live: LiveSchema, schema: str, table_name: str, conditions: list[Condition], cascade: bool
) -> int:
    cfg = get_source(source)
    from .db_connectors import get_connection
    with get_connection(source) as conn:
        deleted = delete_rows_conn(conn, cfg.type, live, schema, table_name, conditions, cascade)
        conn.commit()
        return deleted


def truncate_table_conn(
    conn: Any, dialect: str, live: LiveSchema, schema: str, table_name: str, cascade: bool = False, batch_id: int = 1
) -> int:
    table = _find_table(live, schema, table_name)
    qualified = qualify_table(table.schema_, table.name, dialect)
    dependents = [
        t for t in live.tables.values()
        if t != table and any(fk.ref_table == table.name and (fk.ref_schema or t.schema_) == table.schema_ for fk in t.foreign_keys)
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
