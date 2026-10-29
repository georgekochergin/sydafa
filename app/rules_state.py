"""Хранение и CRUD профилей правил генерации (только персистентное состояние).

SQL-проверки инвариантов на данных вынесены в
`app/backends/rdbms/generation.py:validate_table_invariants` — хранилище правил
не открывает соединения с СУБД.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import STATE_DIR
from .models import TableRuleProfile, validate_entity_fqn

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


def get_table_rules(source: str, entity_fqn: str) -> TableRuleProfile | None:
    """Возвращает профиль правил для конкретной сущности schema.table или None."""
    validate_entity_fqn(entity_fqn)
    all_rules = get_all_rules(source)
    return all_rules.get(entity_fqn)


def set_table_rules(source: str, entity_fqn: str, profile: TableRuleProfile) -> None:
    """Устанавливает или перезаписывает профиль правил для сущности schema.table."""
    validate_entity_fqn(entity_fqn)
    all_rules = dict(get_all_rules(source))
    all_rules[entity_fqn] = profile
    save_rules(source, all_rules)


def delete_table_rules(source: str, entity_fqn: str) -> bool:
    """Удаляет профиль правил сущности. Возвращает True, если правило существовало."""
    validate_entity_fqn(entity_fqn)
    all_rules = dict(get_all_rules(source))
    if entity_fqn in all_rules:
        del all_rules[entity_fqn]
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
