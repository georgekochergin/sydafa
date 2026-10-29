"""Диалект MSSQL: квотирование, типы, бинды и DDL.

Переносится дословно из render_ddl.py (mssql-ветки) и schema_engine.py;
SQL-вывод обязан совпадать с прежним байт-в-байт (characterization-тесты).
"""
from __future__ import annotations

import re

from ....models import ColumnSpec, TableSchema
from ..contracts import TransactionSemantics

_TYPES = {
    "bigint": "BIGINT",
    "string": "NVARCHAR(400)",
    "date": "DATE",
    "timestamp": "DATETIME2",
    "uuid": "UNIQUEIDENTIFIER",
}

_DECIMAL_RE = re.compile(r"^decimal\((\d+),\s*(\d+)\)$")

# MSSQL: DML транзакционен; DDL допускается внутри транзакции и откатывается;
# TRUNCATE также откатывается (минимально протоколируется, но участвует в транзакции)
TRANSACTION_SEMANTICS = TransactionSemantics(
    transactional_dml=True,
    transactional_ddl=True,
    implicit_commit_ddl=False,
    transactional_hard_clear=True,
)


class MssqlDialect:
    engine_type = "mssql"
    transaction_semantics = TRANSACTION_SEMANTICS

    # --- Идентификаторы и типы ---

    def quote_identifier(self, ident: str) -> str:
        return f"[{ident}]"

    def qualify_table(self, schema: str, table: str) -> str:
        return f"[{schema}].[{table}]"

    def map_type(self, logical_type: str) -> str:
        m = _DECIMAL_RE.match(logical_type)
        if m:
            precision, scale = m.groups()
            return f"DECIMAL({precision},{scale})"
        try:
            return _TYPES[logical_type]
        except KeyError as exc:
            raise ValueError(
                f"Нет правила маппинга логического типа '{logical_type}' для диалекта 'mssql'"
            ) from exc

    def placeholder(self, index: int) -> str:
        return "%s"

    # --- DDL: схема ---

    def create_schema(self, name: str) -> list[str]:
        return [
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{name}') "
            f"EXEC('CREATE SCHEMA [{name}] AUTHORIZATION [dbo]')"
        ]

    def drop_schema(self, name: str) -> list[str]:
        return [f"DROP SCHEMA [{name}]"]

    def rename_schema(self, from_name: str, to_name: str, table_names: list[str]) -> list[str]:
        statements = self.create_schema(to_name)
        statements.extend(
            f"ALTER SCHEMA [{to_name}] TRANSFER [{from_name}].[{t}]" for t in table_names
        )
        statements.append(f"DROP SCHEMA [{from_name}]")
        return statements

    # --- DDL: таблица ---

    def create_table(self, table: TableSchema) -> str:
        qualified = self.qualify_table(table.schema_, table.name)
        col_lines = [f"  [{c.name}] {self.map_type(c.type)}" for c in table.columns]
        if table.primary_key:
            pk_cols = ", ".join(f"[{c}]" for c in table.primary_key)
            col_lines.append(
                f"  CONSTRAINT [pk_{table.schema_}_{table.name}] PRIMARY KEY ({pk_cols})"
            )
        for fk in table.foreign_keys:
            cols = ", ".join(f"[{c}]" for c in fk.columns)
            ref_schema = fk.ref_schema or table.schema_
            ref_cols = ", ".join(f"[{c}]" for c in fk.ref_columns)
            fk_name = f"fk_{table.schema_}_{table.name}_{'_'.join(fk.columns)}"
            col_lines.append(
                f"  CONSTRAINT [{fk_name}] FOREIGN KEY ({cols}) "
                f"REFERENCES [{ref_schema}].[{fk.ref_table}] ({ref_cols})"
            )
        body = ",\n".join(col_lines)
        return f"CREATE TABLE {qualified} (\n{body}\n)"

    def drop_table(self, schema: str, table: str) -> str:
        return f"DROP TABLE {self.qualify_table(schema, table)}"

    def clear_table(self, schema: str, table: str, *, hard: bool) -> str:
        qualified = self.qualify_table(schema, table)
        return f"TRUNCATE TABLE {qualified}" if hard else f"DELETE FROM {qualified}"

    def rename_table(self, schema: str, from_name: str, to_name: str) -> str:
        return f"EXEC sp_rename '[{schema}].[{from_name}]', '{to_name}'"

    def transfer_table(self, from_schema: str, to_schema: str, table: str) -> list[str]:
        return [f"ALTER SCHEMA [{to_schema}] TRANSFER [{from_schema}].[{table}]"]

    # --- DDL: колонка ---

    def add_column(self, schema: str, table: str, column: ColumnSpec) -> str:
        return (
            f"ALTER TABLE [{schema}].[{table}] "
            f"ADD [{column.name}] {self.map_type(column.type)} NULL"
        )

    def drop_column(self, schema: str, table: str, name: str) -> str:
        return f"ALTER TABLE [{schema}].[{table}] DROP COLUMN [{name}]"

    def rename_column(self, schema: str, table: str, from_name: str, to_name: str) -> str:
        return f"EXEC sp_rename '[{schema}].[{table}].[{from_name}]', '{to_name}', 'COLUMN'"
