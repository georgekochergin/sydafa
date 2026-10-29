"""Контракты RDBMS-семейства: диалект SQL, физическая очистка и границы транзакций.

Диалект — не только CREATE TABLE: квотирование, маппинг типов, стиль
бинд-параметров, все DDL-действия над схемой/таблицами/колонками и физическая
команда очистки. Валидация операций относительно LiveSchema и обновление
модели — обязанность общего слоя (`rdbms/ddl.py`), а не диалекта.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...models import ColumnSpec, TableSchema


@dataclass(frozen=True)
class TransactionSemantics:
    """Честные границы транзакционности движка по классам операций.

    Описывает фактическую семантику, не обещая All-or-Nothing там, где её нет
    (SIDE-01/backlog №4: безусловная гарантия отката всего батча Oracle DDL/TRUNCATE
    не обеспечивается; исправление контракта — отдельная задача).
    """

    transactional_dml: bool          # INSERT/UPDATE/DELETE откатываются rollback
    transactional_ddl: bool          # DDL-операции откатываются rollback
    implicit_commit_ddl: bool        # DDL выполняет неявный commit (Oracle)
    transactional_hard_clear: bool   # физический TRUNCATE откатывается rollback


class RdbmsDialect(Protocol):
    """SQL-диалект конкретного движка; общие алгоритмы не содержат ветвлений по типу."""

    engine_type: str
    transaction_semantics: TransactionSemantics

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

    def clear_table(self, schema: str, table: str, *, hard: bool) -> str:
        """Физическая команда очистки таблицы: hard=True — TRUNCATE-семантика,
        hard=False — построчное удаление (используется при наличии зависимостей).
        Предметная семантика очистки (audit/cascade/порядок) — общий слой."""

    def rename_table(self, schema: str, from_name: str, to_name: str) -> str: ...

    def transfer_table(self, from_schema: str, to_schema: str, table: str) -> list[str]:
        """Перенос таблицы между схемами; может требовать несколько statements."""

    # --- DDL: колонка ---
    def add_column(self, schema: str, table: str, column: ColumnSpec) -> str: ...
    def drop_column(self, schema: str, table: str, name: str) -> str: ...
    def rename_column(self, schema: str, table: str, from_name: str, to_name: str) -> str: ...
