"""Переходный адаптер диалектного DDL поверх backend-диалектов.

Канонические реализации живут в `app/backends/rdbms/{mssql,oracle}/dialect.py`;
этот модуль останется только до перевода оставшихся потребителей
(generators/rules_state) на диалекты напрямую. Неизвестный диалект — явный
отказ через реестр (fallback «не MSSQL -> Oracle» удалён).
"""
from __future__ import annotations

from .backends import registry
from .models import TableSchema


def _dialect(dialect: str):
    return registry.require(dialect).dialect


def map_type(logical_type: str, dialect: str) -> str:
    return _dialect(dialect).map_type(logical_type)


def quote_ident(ident: str, dialect: str) -> str:
    """Экранирует имя колонки или таблицы в зависимости от диалекта СУБД.

    Для Oracle имя всегда заключается в двойные кавычки: без квотирования
    Oracle нормализует идентификаторы в верхний регистр, а с квотированием
    имена создаются в точности как в спецификации (нижний регистр).
    """
    return _dialect(dialect).quote_identifier(ident)


def qualify_table(schema: str, table: str, dialect: str) -> str:
    """Формирует полное квалифицированное имя таблицы schema.table с учетом диалекта."""
    return _dialect(dialect).qualify_table(schema, table)


def render_create_table(table: TableSchema, dialect: str) -> str:
    """Генерирует CREATE TABLE для таблицы с учётом её схемы и диалекта СУБД."""
    return _dialect(dialect).create_table(table)
