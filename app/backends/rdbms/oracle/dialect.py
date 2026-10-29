"""Диалект Oracle: квотирование в нижнем регистре, NUMBER, пользователи-схемы.

Переносится дословно из render_ddl.py (oracle-ветки) и schema_engine.py;
SQL-вывод обязан совпадать с прежним байт-в-байт (characterization-тесты).
"""
from __future__ import annotations

import re

from ....models import ColumnSpec, TableSchema
from ..contracts import TransactionSemantics

_TYPES = {
    "bigint": "NUMBER(19)",
    "string": "VARCHAR2(400)",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "uuid": "VARCHAR2(36)",
}

_DECIMAL_RE = re.compile(r"^decimal\((\d+),\s*(\d+)\)$")

# Oracle: DML транзакционен; DDL и TRUNCATE выполняют неявный commit и не
# откатываются последующим rollback (SIDE-01/backlog №4 — честная граница)
TRANSACTION_SEMANTICS = TransactionSemantics(
    transactional_dml=True,
    transactional_ddl=False,
    implicit_commit_ddl=True,
    transactional_hard_clear=False,
)


class OracleDialect:
    engine_type = "oracle"
    transaction_semantics = TRANSACTION_SEMANTICS

    # --- Идентификаторы и типы ---

    def quote_identifier(self, ident: str) -> str:
        """Oracle: всегда двойные кавычки — без квотирования имена уходят в верхний регистр."""
        return f'"{ident}"'

    def qualify_table(self, schema: str, table: str) -> str:
        return f'"{schema}"."{table}"'

    def map_type(self, logical_type: str) -> str:
        m = _DECIMAL_RE.match(logical_type)
        if m:
            precision, scale = m.groups()
            return f"NUMBER({precision},{scale})"
        try:
            return _TYPES[logical_type]
        except KeyError as exc:
            raise ValueError(
                f"Нет правила маппинга логического типа '{logical_type}' для диалекта 'oracle'"
            ) from exc

    def placeholder(self, index: int) -> str:
        return f":{index}"

    # --- DDL: схема (пользователи-схемы) ---

    def create_schema(self, name: str) -> list[str]:
        return [
            f"CREATE USER {self.quote_identifier(name)} IDENTIFIED BY sydafa",
            f"GRANT UNLIMITED TABLESPACE TO {self.quote_identifier(name)}",
            f"GRANT CREATE SESSION, CREATE TABLE TO {self.quote_identifier(name)}",
        ]

    def drop_schema(self, name: str) -> list[str]:
        return [f"DROP USER {self.quote_identifier(name)} CASCADE"]

    def rename_schema(self, from_name: str, to_name: str, table_names: list[str]) -> list[str]:
        statements = self.create_schema(to_name)
        # Oracle не умеет переносить таблицы между схемами ALTER-командой:
        # копируем данные CTAS в новую схему, затем удаляем старого пользователя
        # вместе с его таблицами.
        statements.extend(
            f"CREATE TABLE {self.qualify_table(to_name, t)} "
            f"AS SELECT * FROM {self.qualify_table(from_name, t)}"
            for t in table_names
        )
        statements.append(f"DROP USER {self.quote_identifier(from_name)} CASCADE")
        return statements

    # --- DDL: таблица ---

    def create_table(self, table: TableSchema) -> str:
        qualified = self.qualify_table(table.schema_, table.name)
        col_lines = [f"  {self.quote_identifier(c.name)} {self.map_type(c.type)}" for c in table.columns]
        if table.primary_key:
            pk_cols = ", ".join(self.quote_identifier(c) for c in table.primary_key)
            pk_name = self.quote_identifier(f"pk_{table.schema_}_{table.name}")
            col_lines.append(f"  CONSTRAINT {pk_name} PRIMARY KEY ({pk_cols})")
        for fk in table.foreign_keys:
            cols = ", ".join(self.quote_identifier(c) for c in fk.columns)
            ref_schema = fk.ref_schema or table.schema_
            ref_cols = ", ".join(self.quote_identifier(c) for c in fk.ref_columns)
            fk_name = f"fk_{table.schema_}_{table.name}_{'_'.join(fk.columns)}"[:30]
            col_lines.append(
                f"  CONSTRAINT {self.quote_identifier(fk_name)} FOREIGN KEY ({cols})"
                f" REFERENCES {self.qualify_table(ref_schema, fk.ref_table)} ({ref_cols})"
            )
        body = ",\n".join(col_lines)
        return f"CREATE TABLE {qualified} (\n{body}\n)"

    def drop_table(self, schema: str, table: str) -> str:
        return f"DROP TABLE {self.qualify_table(schema, table)} CASCADE CONSTRAINTS"

    def clear_table(self, schema: str, table: str, *, hard: bool) -> str:
        qualified = self.qualify_table(schema, table)
        return f"TRUNCATE TABLE {qualified}" if hard else f"DELETE FROM {qualified}"

    def rename_table(self, schema: str, from_name: str, to_name: str) -> str:
        return (
            f"ALTER TABLE {self.qualify_table(schema, from_name)} "
            f"RENAME TO {self.quote_identifier(to_name)}"
        )

    def transfer_table(self, from_schema: str, to_schema: str, table: str) -> list[str]:
        return [
            f"CREATE TABLE {self.qualify_table(to_schema, table)} "
            f"AS SELECT * FROM {self.qualify_table(from_schema, table)}",
            f"DROP TABLE {self.qualify_table(from_schema, table)} CASCADE CONSTRAINTS",
        ]

    # --- DDL: колонка ---

    def add_column(self, schema: str, table: str, column: ColumnSpec) -> str:
        return (
            f"ALTER TABLE {self.qualify_table(schema, table)} "
            f"ADD ({self.quote_identifier(column.name)} {self.map_type(column.type)})"
        )

    def drop_column(self, schema: str, table: str, name: str) -> str:
        return (
            f"ALTER TABLE {self.qualify_table(schema, table)} "
            f"DROP COLUMN {self.quote_identifier(name)}"
        )

    def rename_column(self, schema: str, table: str, from_name: str, to_name: str) -> str:
        return (
            f"ALTER TABLE {self.qualify_table(schema, table)} "
            f"RENAME COLUMN {self.quote_identifier(from_name)} TO {self.quote_identifier(to_name)}"
        )
