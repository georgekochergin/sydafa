"""Управление персистентным состоянием правил генерации и инвариантов данных."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .constants import STATE_DIR
from .models import TableRuleProfile, validate_table_fqn

logger = logging.getLogger("sydafa.rules_state")

_rules_cache: dict[str, dict[str, TableRuleProfile]] = {}


def _rules_file(source: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"rules.{source}.json"


def load_rules(source: str) -> dict[str, TableRuleProfile]:
    """Загружает профили правил для указанного источника из /state/rules.{source}.json."""
    global _rules_cache
    rf = _rules_file(source)
    if rf.exists():
        try:
            raw = json.loads(rf.read_text(encoding="utf-8"))
            profiles = {fqn: TableRuleProfile(**p_data) for fqn, p_data in raw.items()}
            _rules_cache[source] = profiles
            return profiles
        except Exception as e:
            logger.warning("Не удалось прочитать rules для '%s': %s. Инициализируется пустой реестр.", source, e)

    _rules_cache[source] = {}
    return _rules_cache[source]


def save_rules(source: str, rules: dict[str, TableRuleProfile]) -> None:
    """Сохраняет словарь профилей правил источника на диск."""
    global _rules_cache
    _rules_cache[source] = rules
    rf = _rules_file(source)
    serialized = {fqn: profile.model_dump(by_alias=True) for fqn, profile in rules.items()}
    rf.write_text(json.dumps(serialized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_all_rules(source: str) -> dict[str, TableRuleProfile]:
    """Возвращает все зарегистрированные профили правил для источника."""
    if source not in _rules_cache:
        load_rules(source)
    return _rules_cache[source]


def get_table_rules(source: str, table_fqn: str) -> TableRuleProfile | None:
    """Возвращает профиль правил для конкретной таблицы schema.table или None."""
    validate_table_fqn(table_fqn)
    all_rules = get_all_rules(source)
    return all_rules.get(table_fqn)


def set_table_rules(source: str, table_fqn: str, profile: TableRuleProfile) -> None:
    """Устанавливает или перезаписывает профиль правил для таблицы schema.table."""
    validate_table_fqn(table_fqn)
    all_rules = dict(get_all_rules(source))
    all_rules[table_fqn] = profile
    save_rules(source, all_rules)


def delete_table_rules(source: str, table_fqn: str) -> bool:
    """Удаляет профиль правил таблицы. Возвращает True, если правило существовало."""
    validate_table_fqn(table_fqn)
    all_rules = dict(get_all_rules(source))
    if table_fqn in all_rules:
        del all_rules[table_fqn]
        save_rules(source, all_rules)
        return True
    return False


def reset_rules_cache(source: str) -> None:
    """Сбрасывает кэш правил в памяти и удаляет файл состояния."""
    global _rules_cache
    if source in _rules_cache:
        del _rules_cache[source]
    rf = _rules_file(source)
    if rf.exists():
        try:
            rf.unlink()
        except Exception:
            pass


def validate_table_invariants_on_data(
    source: str,
    table_fqn: str,
    profile: TableRuleProfile,
    conn: Any = None,
) -> None:
    """Проверяет соответствие существующих данных в СУБД правилам инвариантов profile.

    Если обнаружены нарушения, выбрасывает ValueError с подробным описанием нарушений.
    Если таблица пуста или нарушений нет, завершается успешно.
    """
    from .db_connectors import get_connection
    from .models import CumulativeBalanceInvariant
    from .render_ddl import qualify_table, quote_ident
    from .schema_state import get_live_schema
    from .sources import get_source

    schema_name, table_name = validate_table_fqn(table_fqn)
    live = get_live_schema(source)
    if f"{schema_name}.{table_name}" not in live.tables:
        return

    cfg = get_source(source)
    dialect = cfg.type
    qualified = qualify_table(schema_name, table_name, dialect)

    def _check_on_connection(active_conn):
        cur = active_conn.cursor()
        for inv in profile.invariants:
            if isinstance(inv, CumulativeBalanceInvariant):
                amt_col = quote_ident(inv.amount_column, dialect)
                if inv.partition_by:
                    part_cols = ", ".join(quote_ident(c, dialect) for c in inv.partition_by)
                    sql = f"SELECT {part_cols}, SUM({amt_col}) FROM {qualified} GROUP BY {part_cols} HAVING SUM({amt_col}) < {inv.min_balance}"
                else:
                    sql = f"SELECT SUM({amt_col}) FROM {qualified} HAVING SUM({amt_col}) < {inv.min_balance}"
                try:
                    cur.execute(sql)
                    violations = cur.fetchall()
                except Exception as e:
                    logger.warning("Не удалось выполнить проверочный запрос для '%s': %s", table_fqn, e)
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
                        f"Невозможно применить правило cumulative_balance к таблице '{table_fqn}': "
                        f"{part_desc}текущий накопленный баланс равен {sum_val}, "
                        f"что нарушает ограничение min_balance={inv.min_balance}"
                    )

    if conn is not None:
        _check_on_connection(conn)
    else:
        with get_connection(source) as fresh_conn:
            _check_on_connection(fresh_conn)
