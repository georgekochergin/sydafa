"""Управление персистентным состоянием правил генерации и инвариантов данных."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .constants import STATE_DIR
from .models import TableRuleProfile

logger = logging.getLogger("sydafa.rules_state")

_TABLE_FQN_REGEX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")

_rules_cache: dict[str, dict[str, TableRuleProfile]] = {}


def validate_table_fqn(table_fqn: str) -> tuple[str, str]:
    """Проверяет формат schema.table и возвращает (schema, table)."""
    match = _TABLE_FQN_REGEX.match(table_fqn.strip())
    if not match:
        raise ValueError(
            f"Некорректный идентификатор таблицы '{table_fqn}'. Ожидается формат 'schema.table', "
            "где имена схемы и таблицы содержат только латиницу, цифры и символ '_'"
        )
    return match.group(1), match.group(2)


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
