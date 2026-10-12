"""Unit-тесты генерации DDL и валидации движка эволюции схемы."""
from __future__ import annotations

import unittest
from app.models import (
    AddColumnOp,
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    DropColumnOp,
    DropSchemaOp,
    DropTableOp,
    LiveSchema,
    RenameColumnOp,
    RenameSchemaOp,
    RenameTableOp,
    TransferTableOp,
)
from app.schema_engine import SchemaEvolutionError, _build_ddl_and_update_state


class TestSchemaEngine(unittest.TestCase):
    def setUp(self):
        self.live = LiveSchema(schemas=[], tables={})

    def test_create_schema_mssql(self):
        op = CreateSchemaOp(name="demo")
        ddl, desc = _build_ddl_and_update_state(op, "mssql", self.live, 0)
        self.assertIn("CREATE SCHEMA [demo]", ddl[0])
        self.assertIn("demo", self.live.schemas)

        # Duplicate schema gives error
        with self.assertRaises(SchemaEvolutionError):
            _build_ddl_and_update_state(op, "mssql", self.live, 1)

    def test_create_schema_oracle(self):
        op = CreateSchemaOp(name="demo")
        ddl, desc = _build_ddl_and_update_state(op, "oracle", self.live, 0)
        self.assertTrue(any("CREATE USER demo" in stmt for stmt in ddl))
        self.assertIn("demo", self.live.schemas)

    def test_create_table_and_columns(self):
        # Create schema first
        _build_ddl_and_update_state(CreateSchemaOp(name="demo"), "mssql", self.live, 0)

        # Create table
        table_op = CreateTableOp(
            schema="demo",
            name="customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string", generator={"kind": "faker", "provider": "company"}),
            ],
        )
        ddl, desc = _build_ddl_and_update_state(table_op, "mssql", self.live, 1)
        self.assertTrue(any("CREATE TABLE [demo].[customers]" in stmt for stmt in ddl))
        self.assertIn("demo.customers", self.live.tables)

        # Rename column
        rename_col_op = RenameColumnOp(
            schema="demo",
            table="customers",
            from_name="name",
            to_name="full_name",
        )
        ddl, desc = _build_ddl_and_update_state(rename_col_op, "mssql", self.live, 2)
        self.assertIn("sp_rename '[demo].[customers].[name]', 'full_name', 'COLUMN'", ddl[0])
        self.assertIn("full_name", self.live.tables["demo.customers"].column_names)
        self.assertNotIn("name", self.live.tables["demo.customers"].column_names)

        # Rename table
        rename_table_op = RenameTableOp(
            schema="demo",
            from_name="customers",
            to_name="clients",
        )
        ddl, desc = _build_ddl_and_update_state(rename_table_op, "mssql", self.live, 3)
        self.assertIn("sp_rename '[demo].[customers]', 'clients'", ddl[0])
        self.assertIn("demo.clients", self.live.tables)
        self.assertNotIn("demo.customers", self.live.tables)


if __name__ == "__main__":
    unittest.main()
