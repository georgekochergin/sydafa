"""Контракты RDBMS-семейства: диалект SQL и границы транзакций.

Диалект — не только CREATE TABLE: квотирование, маппинг типов, стиль
бинд-параметров и все DDL-действия над схемой/таблицами/колонками.
Валидация операций относительно LiveSchema и обновление модели —
обязанность общего слоя (`rdbms/ddl.py`), а не диалекта.
"""
from __future__ import annotations

from typing import Protocol

from ...models import ColumnSpec, TableSchema


class RdbmsDialect(Protocol):
    """SQL-диалект конкретного движка; общие алгоритмы не содержат ветвлений по типу."""

    engine_type: str

    # --- Идентификаторы и типы ---
    def quote_identifier(self, ident: str) -> str:
        """Квотирование колонки/таблицы; Oracle — всегда двойные кавычки (нижний регистр)."""

    def qualify_table(self, schema: str, table: str) -> str:
        """Полное квалифицированное имя schema.table."""

    def map_type(self, logical_type: str) -> str:
        """Логический тип sydafa -> физический тип движка."""

    def placeholder(self, index: int) -> str:
        """Плейсхолдер бинд-параметра с позицией index (MSSQL — позиционный %s)."""

    # --- DDL: схема ---
    def create_schema(self, name: str) -> list[str]:
        """Создание схемы (Oracle: пользователи-схемы с грантами)."""

    def drop_schema(self, name: str) -> list[str]: ...

    def rename_schema(self, from_name: str, to_name: str, table_names: list[str]) -> list[str]:
        """Перенос всех таблиц схемы; MSSQL — ALTER SCHEMA TRANSFER, Oracle — CTAS."""

    # --- DDL: таблица ---
    def create_table(self, table: TableSchema) -> str:
        """CREATE TABLE с PK/FK и именами констрейнтов движка."""

    def drop_table(self, schema: str, table: str) -> str:
        """DROP TABLE с учётом зависимостей (Oracle — CASCADE CONSTRAINTS)."""

    def rename_table(self, schema: str, from_name: str, to_name: str) -> str: ...

    def transfer_table(self, from_schema: str, to_schema: str, table: str) -> list[str]:
        """Перенос таблицы между схемами; может требовать несколько statements."""

    # --- DDL: колонка ---
    def add_column(self, schema: str, table: str, column: ColumnSpec) -> str: ...
    def drop_column(self, schema: str, table: str, name: str) -> str: ...
    def rename_column(self, schema: str, table: str, from_name: str, to_name: str) -> str: ...
