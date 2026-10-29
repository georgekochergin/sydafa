"""Генерация строк RDBMS: GenerationSession, FK-оркестрация и проверка инвариантов.

Чистые генераторы значений — в `app/generation/values.py`; SQL-чтения — в
`rdbms/data.py`. Здесь — сессия генерации (резервирование сиквенсов, кэш
балансов), граф зависимостей колонок, Strict FK Integrity (значения только из
существующих родителей) и валидация инвариантов на данных.
"""
from __future__ import annotations

import logging
import random
import uuid
from typing import Any

from ...generation.values import GeneratorError, generate_value
from ...models import (
    BATCH_ID_COLUMN,
    EXEC_BATCH_ID_COLUMN,
    CumulativeBalanceInvariant,
    ForeignKey,
    LiveSchema,
    TableRuleProfile,
    TableSchema,
)
from ... import rules_state
from ... import schema_state
from .data import load_existing_balances, fetch_invariant_violations, pick_existing_pk

logger = logging.getLogger("sydafa.backends.rdbms.generation")


def find_table(live: LiveSchema, schema: str, table_name: str) -> TableSchema:
    """Поиск таблицы по FQN с сохраняемым fallback по имени (backlog №15)."""
    key = f"{schema}.{table_name}"
    if key in live.tables:
        return live.tables[key]
    for t in live.tables.values():
        if t.name == table_name:
            return t
    raise GeneratorError(f"Таблица '{key}' не найдена в схеме источника")


def _fk_for_column(table: TableSchema, column_name: str) -> ForeignKey | None:
    for fk in table.foreign_keys:
        if column_name in fk.columns:
            return fk
    return None


def _insert_row(conn, dialect, qualified: str, values: dict[str, Any]) -> None:
    col_names = list(values.keys())
    cols = ", ".join(dialect.quote_identifier(c) for c in col_names)
    placeholders = ", ".join(dialect.placeholder(i) for i in range(1, len(col_names) + 1))
    cur = conn.cursor()
    cur.execute(f"INSERT INTO {qualified} ({cols}) VALUES ({placeholders})", list(values.values()))


class GenerationSession:
    """Сессия генерации данных с кэшированием накопительных балансов и диапазонов сиквенсов."""

    def __init__(
        self,
        conn: Any,
        dialect,
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
            src = self.source or "default"
            entity_fqn = f"{self.table.schema_}.{self.table.name}"
            return schema_state.reserve_sequence_range(src, entity_fqn, column, 1)

    def sequence_supplier(self):
        """Поставщик значений sequence для чистых генераторов (values.generate_value)."""
        return self.next_sequence_value

    def _preload_state(self) -> None:
        qualified = self.dialect.qualify_table(self.table.schema_, self.table.name)

        for idx, inv in enumerate(self.profile.invariants):
            if isinstance(inv, CumulativeBalanceInvariant):
                # Инкрементальное наполнение с подавлением исключений (backlog №12):
                # сбой чтения/преобразования поздней строки не затирает уже
                # заполненную часть балансов — в точности как до переноса.
                self.balances[idx] = {}
                try:
                    load_existing_balances(self.conn, self.dialect, qualified, inv, self.balances[idx])
                except Exception:
                    pass


def _resolve_fk_values(
    conn, dialect, live: LiveSchema, fk: ForeignKey, current_schema: str, table_name: str
) -> dict[str, Any]:
    """Strict FK Integrity: значения только из существующих строк родителя."""
    ref_schema = fk.ref_schema or current_schema
    parent_table = find_table(live, ref_schema, fk.ref_table)
    parent_qualified = dialect.qualify_table(parent_table.schema_, parent_table.name)
    existing = pick_existing_pk(conn, dialect, parent_qualified, parent_table.primary_key)
    if existing is None:
        raise GeneratorError(
            f"Таблица '{current_schema}.{table_name}' ссылается на '{parent_table.schema_}.{parent_table.name}' "
            f"через Foreign Key, но родительская таблица пуста. Сначала выполните append в родительскую таблицу."
        )
    return dict(zip(fk.columns, [existing[c] for c in fk.ref_columns]))


def mint_new_row(
    conn,
    source: str,
    dialect,
    live: LiveSchema,
    table: TableSchema,
    batch_id: int = 1,
    session: GenerationSession | None = None,
) -> dict[str, Any]:
    """Генерирует и вставляет одну строку; возвращает значения PK."""
    qualified = dialect.qualify_table(table.schema_, table.name)
    values: dict[str, Any] = {}

    if session is None:
        entity_fqn = f"{table.schema_}.{table.name}"
        profile = rules_state.get_table_rules(source, entity_fqn) or TableRuleProfile()
        session = GenerationSession(conn, dialect, live, table, profile, source=source, total_rows=1)

    for fk in table.foreign_keys:
        values.update(_resolve_fk_values(conn, dialect, live, fk, table.schema_, table.name))

    candidates: dict[str, Any] = {}
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

            values[c_name] = generate_value(
                col, values, gen=gen, sequence=session.sequence_supplier()
            )
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


def append_rows(
    conn, source: str, dialect, live: LiveSchema, schema: str, table_name: str, rows: int, batch_id: int = 1
) -> int:
    table = find_table(live, schema, table_name)
    entity_fqn = f"{schema}.{table_name}"
    profile = rules_state.get_table_rules(source, entity_fqn) or TableRuleProfile()
    session = GenerationSession(conn, dialect, live, table, profile, source=source, total_rows=rows)
    for _ in range(rows):
        mint_new_row(conn, source, dialect, live, table, batch_id, session=session)
    return rows


def validate_table_invariants(
    source: str,
    entity_fqn: str,
    profile: TableRuleProfile,
    conn=None,
    backend=None,
) -> None:
    """Проверяет соответствие существующих данных правилам инвариантов profile.

    При обнаружении нарушений выбрасывает ValueError с прежним текстом.
    Сохраняемое поведение (backlog №7/№12): ошибка проверочного запроса
    трактуется как отсутствие нарушений; отсутствие таблицы в live-схеме —
    тихий успех. backend — выбранный на границе сценария контекст (dialect и
    connections берутся из него без повторного lookup); если не передан,
    выбирается из реестра — это самостоятельная входная граница. Если conn
    не передан, открывается одна рабочая session через этот же backend.
    """
    from ...models import validate_entity_fqn
    from ...schema_state import get_live_schema

    schema_name, table_name = validate_entity_fqn(entity_fqn)
    live = get_live_schema(source)
    if f"{schema_name}.{table_name}" not in live.tables:
        return

    if backend is None:
        from ...sources import get_source
        from .. import registry

        backend = registry.require_rdbms(get_source(source).type)
    dialect = backend.dialect
    qualified = dialect.qualify_table(schema_name, table_name)

    def _check_on_connection(active_conn):
        from ...models import CumulativeBalanceInvariant as _CBI

        for inv in profile.invariants:
            if isinstance(inv, _CBI):
                try:
                    violations = fetch_invariant_violations(active_conn, dialect, qualified, inv)
                except Exception as e:
                    logger.warning("Не удалось выполнить проверочный запрос для '%s': %s", entity_fqn, e)
                    violations = []

                if violations:
                    sample = violations[0]
                    sum_val = sample[-1]
                    if inv.partition_by:
                        part_vals = list(sample[: len(inv.partition_by)])
                        part_desc = f"для партиции {part_vals} "
                    else:
                        part_desc = "глобальный "
                    raise ValueError(
                        f"Невозможно применить правило cumulative_balance к сущности '{entity_fqn}': "
                        f"{part_desc}текущий накопленный баланс равен {sum_val}, "
                        f"что нарушает ограничение min_balance={inv.min_balance}"
                    )

    if conn is not None:
        _check_on_connection(conn)
    else:
        from ...sources import get_source

        with _open_working_session(get_source(source), backend) as fresh_conn:
            _check_on_connection(fresh_conn)


def _open_working_session(cfg, backend):
    """Открытие рабочей session для валидации через уже выбранный backend.

    Один сценарий — одно подключение: wake уже выполнен вызывающим эндпоинтом,
    повторный registry lookup не выполняется — контекст передан вызывающим.
    """
    from ...naming import container_name_for
    from ...secrets import get_secrets

    return backend.connections.open_working(cfg, get_secrets(), container_name_for(cfg))
