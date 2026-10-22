"""Unit-тесты генерации DDL и валидации движка эволюции схемы."""
from __future__ import annotations

import unittest
from app.models import (
    AddColumnOp,
    BATCH_ID_COLUMN,
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    DropColumnOp,
    DropSchemaOp,
    DropTableOp,
    EXEC_BATCH_ID_COLUMN,
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

    def _create_schema_and_table(self, dialect="mssql"):
        # Create schema first
        _build_ddl_and_update_state(CreateSchemaOp(name="demo"), dialect, self.live, 0)

        # Create table
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string", generator={"kind": "faker", "provider": "company"}),
            ],
        )
        ddl, desc = _build_ddl_and_update_state(table_op, dialect, self.live, 1)
        return ddl, desc

    def test_create_table_injects_batch_id_and_audit_tables(self):
        ddl, _ = self._create_schema_and_table("mssql")
        customers = self.live.tables["demo.customers"]
        # Системная колонка _batch_id внедрена в основную таблицу
        self.assertIn(BATCH_ID_COLUMN, customers.column_names)
        self.assertEqual(customers.column_names.count(BATCH_ID_COLUMN), 1)
        # Созданы пустые таблицы аудита _upd_ и _del_
        self.assertIn("demo._upd_customers", self.live.tables)
        self.assertIn("demo._del_customers", self.live.tables)
        self.assertTrue(any("CREATE TABLE [demo].[_upd_customers]" in s for s in ddl))
        self.assertTrue(any("CREATE TABLE [demo].[_del_customers]" in s for s in ddl))
        upd = self.live.tables["demo._upd_customers"]
        self.assertIn(EXEC_BATCH_ID_COLUMN, upd.column_names)
        self.assertIn(BATCH_ID_COLUMN, upd.column_names)
        self.assertEqual(upd.primary_key, [])

    def test_create_table_audit_tables_oracle_quoted(self):
        ddl, _ = self._create_schema_and_table("oracle")
        self.assertIn("demo._upd_customers", self.live.tables)
        # В Oracle имена аудит-таблиц с подчеркиванием квотируются
        self.assertTrue(any('CREATE TABLE demo."_upd_customers"' in s for s in ddl))
        self.assertTrue(any('CREATE TABLE demo."_del_customers"' in s for s in ddl))

    def test_drop_system_columns_forbidden(self):
        self._create_schema_and_table("mssql")
        for col in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            with self.subTest(column=col):
                op = DropColumnOp(entity_fqn="demo.customers", name=col)
                with self.assertRaises(SchemaEvolutionError):
                    _build_ddl_and_update_state(op, "mssql", self.live, 2)

    def test_rename_and_add_system_columns_forbidden(self):
        self._create_schema_and_table("mssql")
        # Переименование системной колонки
        op = RenameColumnOp(entity_fqn="demo.customers", from_name=BATCH_ID_COLUMN, to_name="batch")
        with self.assertRaises(SchemaEvolutionError):
            _build_ddl_and_update_state(op, "mssql", self.live, 2)
        # Переименование колонки в системное имя
        op2 = RenameColumnOp(entity_fqn="demo.customers", from_name="name", to_name=EXEC_BATCH_ID_COLUMN)
        with self.assertRaises(SchemaEvolutionError):
            _build_ddl_and_update_state(op2, "mssql", self.live, 3)
        # Ручное добавление системной колонки
        op3 = AddColumnOp(entity_fqn="demo.customers", column=ColumnSpec(name=EXEC_BATCH_ID_COLUMN, type="bigint"))
        with self.assertRaises(SchemaEvolutionError):
            _build_ddl_and_update_state(op3, "mssql", self.live, 4)

    def test_recreate_audit_tables_on_add_column(self):
        self._create_schema_and_table("mssql")
        add_op = AddColumnOp(
            entity_fqn="demo.customers",
            column=ColumnSpec(name="email", type="string"),
        )
        ddl, _ = _build_ddl_and_update_state(add_op, "mssql", self.live, 2)
        # Аудит-таблицы пересозданы (DROP + CREATE)
        self.assertIn("DROP TABLE [demo].[_upd_customers]", ddl)
        self.assertIn("DROP TABLE [demo].[_del_customers]", ddl)
        self.assertTrue(any("CREATE TABLE [demo].[_upd_customers]" in s for s in ddl))
        self.assertTrue(any("CREATE TABLE [demo].[_del_customers]" in s for s in ddl))
        # Пересозданные аудит-таблицы содержат новую колонку и системные колонки
        upd = self.live.tables["demo._upd_customers"]
        self.assertIn("email", upd.column_names)
        self.assertIn(BATCH_ID_COLUMN, upd.column_names)
        self.assertIn(EXEC_BATCH_ID_COLUMN, upd.column_names)

    def test_create_table_and_columns(self):
        # Create schema first
        _build_ddl_and_update_state(CreateSchemaOp(name="demo"), "mssql", self.live, 0)

        # Create table
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
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
            entity_fqn="demo.customers",
            from_name="name",
            to_name="full_name",
        )
        ddl, desc = _build_ddl_and_update_state(rename_col_op, "mssql", self.live, 2)
        self.assertIn("sp_rename '[demo].[customers].[name]', 'full_name', 'COLUMN'", ddl[0])
        self.assertIn("full_name", self.live.tables["demo.customers"].column_names)
        self.assertNotIn("name", self.live.tables["demo.customers"].column_names)

        # Rename table
        rename_table_op = RenameTableOp(
            entity_fqn="demo.customers",
            to_name="clients",
        )
        ddl, desc = _build_ddl_and_update_state(rename_table_op, "mssql", self.live, 3)
        self.assertIn("sp_rename '[demo].[customers]', 'clients'", ddl[0])
        self.assertIn("demo.clients", self.live.tables)
        self.assertNotIn("demo.customers", self.live.tables)

    def test_primary_key_validation(self):
        """Валидация PK: отклонение не уникальных колонок (например, string без sequence/uuid)."""
        self.live.schemas = ["demo"]
        # Невалидный PK: строковая колонка без sequence/uuid
        bad_table_op = CreateTableOp(
            entity_fqn="demo.tags",
            primary_key=["tag_code"],
            columns=[
                ColumnSpec(name="tag_code", type="string"),
                ColumnSpec(name="tag_name", type="string"),
            ],
        )
        with self.assertRaises(SchemaEvolutionError) as ctx:
            _build_ddl_and_update_state(bad_table_op, "mssql", self.live, 1)
        self.assertIn("Primary key", str(ctx.exception))

        # Валидный PK: строковая колонка с типом uuid
        uuid_table_op = CreateTableOp(
            entity_fqn="demo.events",
            primary_key=["event_id"],
            columns=[
                ColumnSpec(name="event_id", type="uuid"),
                ColumnSpec(name="payload", type="string"),
            ],
        )
        ddl, desc = _build_ddl_and_update_state(uuid_table_op, "mssql", self.live, 2)
        self.assertIn("demo.events", self.live.tables)


if __name__ == "__main__":
    unittest.main()
