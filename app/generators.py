"""Генерация и мутация синтетических данных (append / update / delete) с FK-каскадом."""
from __future__ import annotations

import calendar
import random
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from faker import Faker

from .models import (
    BATCH_ID_COLUMN,
    ColumnSpec,
    Condition,
    CumulativeBalanceInvariant,
    EXEC_BATCH_ID_COLUMN,
    ForeignKey,
    GeneratorSpec,
    LiveSchema,
    TableRuleProfile,
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


def _generate_value(
    col: ColumnSpec | str,
    row_values: dict[str, Any] | None = None,
    gen: GeneratorSpec | None = None,
    col_type: str | None = None,
    session: GenerationSession | None = None,
) -> Any:
    if isinstance(col, ColumnSpec):
        col_name = col.name
        col_type = col.type
        if gen is None:
            gen = getattr(col, "generator", None)
    else:
        col_name = str(col)
        col_type = col_type or "string"

    # Если явный генератор не задан, генерируем синтетическое значение по типу колонки
    if gen is None:
        t_lower = col_type.lower()
        if "uuid" in t_lower:
            return str(uuid.uuid4())
        if "int" in t_lower:
            return random.randint(1, 10000)
        if "decimal" in t_lower or "numeric" in t_lower or "float" in t_lower:
            return round(random.uniform(10.0, 5000.0), 2)
        if "date" in t_lower or "time" in t_lower:
            today = date.today()
            return today - timedelta(days=random.randint(0, 365))
        if "char" in t_lower or "str" in t_lower or "text" in t_lower:
            return _get_faker("ru_RU").word()
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

    if kind == "sequence":
        if session is not None:
            return session.next_sequence_value(col_name)
        from . import schema_state
        return schema_state.reserve_sequence_range("default", "default.default", col_name, 1)

    if kind == "uuid":
        return str(uuid.uuid4())

    return None


def _fk_for_column(table: TableSchema, column_name: str) -> ForeignKey | None:
    for fk in table.foreign_keys:
        if column_name in fk.columns:
            return fk
    return None


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


class GenerationSession:
    """Сессия генерации данных с кэшированием накопительных балансов и диапазонов сиквенсов."""

    def __init__(
        self,
        conn: Any,
        dialect: str,
        live: LiveSchema,
        table: TableSchema,
        profile: TableRuleProfile,
        source: str = "",
        total_rows: int = 1,
    ):
        self.conn = conn
        self.dialect = dialect
        self.live = live
        self.table = table
        self.profile = profile
        self.source = source
        self.total_rows = max(total_rows, 1)
        self.balances: dict[int, dict[tuple[Any, ...], float]] = {}
        self._sequences: dict[str, dict[str, int]] = {}
        self._preload_state()

    def next_sequence_value(self, column: str) -> int:
        """Возвращает следующее монотонное значение сиквенса из зарезервированного диапазона."""
        if column not in self._sequences:
            from . import schema_state
            src = self.source or "default"
            entity_fqn = f"{self.table.schema_}.{self.table.name}"
            start_val = schema_state.reserve_sequence_range(
                src, entity_fqn, column, self.total_rows
            )
            self._sequences[column] = {
                "curr": start_val,
                "limit": start_val + self.total_rows - 1,
            }

        seq_info = self._sequences[column]
        if seq_info["curr"] <= seq_info["limit"]:
            val = seq_info["curr"]
            seq_info["curr"] += 1
            return val
        else:
            from . import schema_state
            src = self.source or "default"
            entity_fqn = f"{self.table.schema_}.{self.table.name}"
            val = schema_state.reserve_sequence_range(src, entity_fqn, column, 1)
            return val

    def _preload_state(self) -> None:
        qualified = qualify_table(self.table.schema_, self.table.name, self.dialect)
        cur = self.conn.cursor()

        for idx, inv in enumerate(self.profile.invariants):
            if isinstance(inv, CumulativeBalanceInvariant):
                self.balances[idx] = {}
                amt_col = quote_ident(inv.amount_column, self.dialect)
                if inv.partition_by:
                    part_cols = ", ".join(quote_ident(c, self.dialect) for c in inv.partition_by)
                    sql = f"SELECT {part_cols}, SUM({amt_col}) FROM {qualified} GROUP BY {part_cols}"
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                        for r in rows:
                            part_vals = tuple(r[: len(inv.partition_by)])
                            sum_val = float(r[-1]) if r[-1] is not None else 0.0
                            self.balances[idx][part_vals] = sum_val
                    except Exception:
                        pass
                else:
                    sql = f"SELECT SUM({amt_col}) FROM {qualified}"
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                        if rows and rows[0] and rows[0][0] is not None:
                            self.balances[idx][()] = float(rows[0][0])
                        else:
                            self.balances[idx][()] = 0.0
                    except Exception:
                        pass


def _resolve_fk_values(
    conn, dialect: str, live: LiveSchema, fk: ForeignKey, current_schema: str, table_name: str
) -> dict[str, Any]:
    ref_schema = fk.ref_schema or current_schema
    parent_table = _find_table(live, ref_schema, fk.ref_table)
    parent_qualified = qualify_table(parent_table.schema_, parent_table.name, dialect)
    existing = _pick_existing_pk(conn, dialect, parent_qualified, parent_table.primary_key)
    if existing is None:
        raise GeneratorError(
            f"Таблица '{current_schema}.{table_name}' ссылается на '{parent_table.schema_}.{parent_table.name}' "
            f"через Foreign Key, но родительская таблица пуста. Сначала выполните append в родительскую таблицу."
        )
    return dict(zip(fk.columns, [existing[c] for c in fk.ref_columns]))


def _mint_new_row(
    conn,
    source: str,
    dialect: str,
    live: LiveSchema,
    table: TableSchema,
    batch_id: int = 1,
    session: GenerationSession | None = None,
) -> dict[str, Any]:
    qualified = qualify_table(table.schema_, table.name, dialect)
    values: dict[str, Any] = {}

    if session is None:
        from . import rules_state
        entity_fqn = f"{table.schema_}.{table.name}"
        profile = rules_state.get_table_rules(source, entity_fqn) or TableRuleProfile()
        session = GenerationSession(conn, dialect, live, table, profile, source=source, total_rows=1)

    for fk in table.foreign_keys:
        values.update(_resolve_fk_values(conn, dialect, live, fk, table.schema_, table.name))

    candidates: dict[str, ColumnSpec] = {}
    dependencies: dict[str, set[str]] = {}
    all_table_column_names = {c.name for c in table.columns}

    # Построение графа зависимостей
    for col in table.columns:
        if col.name in table.primary_key or _fk_for_column(table, col.name):
            continue
        if col.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            continue
        candidates[col.name] = col

        deps: set[str] = set()
        gen = session.profile.columns.get(col.name) or getattr(col, "generator", None)
        if gen is not None and getattr(gen, "kind", None) == "date_offset":
            ref = getattr(gen, "ref_column", None)
            if not ref or ref not in all_table_column_names:
                raise GeneratorError(
                    f"Колонка '{col.name}' таблицы '{table.name}' ссылается на неизвестную ref_column='{ref}'"
                )
            deps.add(ref)
        dependencies[col.name] = deps

    # Добавляем зависимости из инвариантов
    for inv in session.profile.invariants:
        if isinstance(inv, CumulativeBalanceInvariant):
            if inv.amount_column in dependencies and inv.partition_by:
                dependencies[inv.amount_column].update(inv.partition_by)

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
            gen = session.profile.columns.get(c_name) or getattr(col, "generator", None)

            # Проверяем CumulativeBalanceInvariant
            inv_handled = False
            for idx, inv in enumerate(session.profile.invariants):
                if isinstance(inv, CumulativeBalanceInvariant) and c_name == inv.amount_column:
                    if inv.amount_column in values:
                        # Уже вычислено в текущей строке
                        inv_handled = True
                        break

                    part_vals = tuple(values.get(c) for c in inv.partition_by) if inv.partition_by else ()
                    curr_bal = session.balances.get(idx, {}).get(part_vals, inv.initial_balance)

                    max_withdrawal = round(curr_bal - inv.min_balance, 2)
                    can_withdraw = max_withdrawal >= inv.withdrawal_range[0]

                    if not can_withdraw or random.random() < inv.deposit_prob:
                        # Пополнение (Credit/Deposit)
                        amt = round(random.uniform(inv.deposit_range[0], inv.deposit_range[1]), 2)
                        new_bal = round(curr_bal + amt, 2)
                        values[inv.amount_column] = amt
                    else:
                        # Списание (Debit/Withdrawal)
                        w_hi = min(inv.withdrawal_range[1], max_withdrawal)
                        w_amt = round(random.uniform(inv.withdrawal_range[0], w_hi), 2)
                        new_bal = round(curr_bal - w_amt, 2)
                        values[inv.amount_column] = -w_amt

                    if idx not in session.balances:
                        session.balances[idx] = {}
                    session.balances[idx][part_vals] = new_bal

                    remaining.discard(inv.amount_column)
                    inv_handled = True
                    break

            if inv_handled:
                continue

            values[c_name] = _generate_value(col, values, gen=gen, session=session)
            remaining.remove(c_name)

    for pk_col in table.primary_key:
        if pk_col not in values:
            col_spec = next((c for c in table.columns if c.name == pk_col), None)
            gen = session.profile.columns.get(pk_col) or getattr(col_spec, "generator", None)
            gen_kind = getattr(gen, "kind", None)
            if gen_kind == "uuid":
                values[pk_col] = str(uuid.uuid4())
            elif gen_kind == "sequence":
                values[pk_col] = session.next_sequence_value(pk_col)
            else:
                raise GeneratorError(
                    f"Колонка первичного ключа '{pk_col}' таблицы '{table.schema_}.{table.name}' "
                    f"не имеет обязательного уникального генератора ('sequence' или 'uuid')"
                )

    values[BATCH_ID_COLUMN] = batch_id

    _insert_row(conn, dialect, qualified, values)
    return {pk: values[pk] for pk in table.primary_key}


def append_rows_conn(
    conn: Any, source: str, dialect: str, live: LiveSchema, schema: str, table_name: str, rows: int, batch_id: int = 1
) -> int:
    table = _find_table(live, schema, table_name)
    from . import rules_state
    entity_fqn = f"{schema}.{table_name}"
    profile = rules_state.get_table_rules(source, entity_fqn) or TableRuleProfile()
    session = GenerationSession(conn, dialect, live, table, profile, source=source, total_rows=rows)
    for _ in range(rows):
        _mint_new_row(conn, source, dialect, live, table, batch_id, session=session)
    return rows


def append_rows(source: str, live: LiveSchema, schema: str, table_name: str, rows: int) -> int:
    table = _find_table(live, schema, table_name)
    cfg = get_source(source)
    dialect = cfg.type

    from .db_connectors import get_connection
    with get_connection(source) as conn:
        res = append_rows_conn(conn, source, dialect, live, schema, table_name, rows)
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
