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
    ForeignKey,
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
        # Все идентификаторы Oracle квотируются: имя схемы сохраняется в нижнем регистре
        self.assertTrue(any('CREATE USER "demo" IDENTIFIED BY sydafa' in stmt for stmt in ddl))
        self.assertTrue(any('GRANT UNLIMITED TABLESPACE TO "demo"' in stmt for stmt in ddl))
        self.assertTrue(any('GRANT CREATE SESSION, CREATE TABLE TO "demo"' in stmt for stmt in ddl))
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
        # В Oracle все идентификаторы квотируются и создаются в нижнем регистре
        self.assertTrue(any('CREATE TABLE "demo"."_upd_customers"' in s for s in ddl))
        self.assertTrue(any('CREATE TABLE "demo"."_del_customers"' in s for s in ddl))

    def test_create_table_oracle_identifiers_lowercase(self):
        """Основная таблица, колонки и констрейнты в Oracle создаются квотированными (нижний регистр)."""
        self.live.schemas = ["demo"]
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string"),
            ],
        )
        ddl, _ = _build_ddl_and_update_state(table_op, "oracle", self.live, 0)
        create_sql = next(s for s in ddl if s.startswith('CREATE TABLE "demo"."customers"'))
        self.assertIn('"customer_id" NUMBER(19)', create_sql)
        self.assertIn('"name" VARCHAR2(400)', create_sql)
        self.assertIn('"_batch_id" NUMBER(19)', create_sql)
        self.assertIn('CONSTRAINT "pk_demo_customers" PRIMARY KEY ("customer_id")', create_sql)

    def test_create_table_oracle_fk_references_quoted(self):
        """Ссылка FOREIGN KEY в Oracle адресуется к квотированной родительской таблице."""
        self.live.schemas = ["demo"]
        parent_op = CreateTableOp(
            entity_fqn="demo.parents",
            primary_key=["parent_id"],
            columns=[ColumnSpec(name="parent_id", type="bigint", generator={"kind": "sequence"})],
        )
        _build_ddl_and_update_state(parent_op, "oracle", self.live, 0)
        child_op = CreateTableOp(
            entity_fqn="demo.children",
            primary_key=["child_id"],
            columns=[ColumnSpec(name="child_id", type="bigint", generator={"kind": "sequence"}), ColumnSpec(name="parent_id")],
            foreign_keys=[ForeignKey(columns=["parent_id"], ref_entity_fqn="demo.parents", ref_columns=["parent_id"])],
        )
        ddl, _ = _build_ddl_and_update_state(child_op, "oracle", self.live, 1)
        create_sql = next(s for s in ddl if s.startswith('CREATE TABLE "demo"."children"'))
        self.assertIn('REFERENCES "demo"."parents" ("parent_id")', create_sql)

    def test_drop_schema_oracle_user_quoted(self):
        _build_ddl_and_update_state(CreateSchemaOp(name="demo"), "oracle", self.live, 0)
        ddl, _ = _build_ddl_and_update_state(DropSchemaOp(name="demo"), "oracle", self.live, 1)
        self.assertIn('DROP USER "demo" CASCADE', ddl[0])

    def test_rename_table_and_columns_oracle_quoted(self):
        self.live.schemas = ["demo"]
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string"),
            ],
        )
        _build_ddl_and_update_state(table_op, "oracle", self.live, 0)

        ddl, _ = _build_ddl_and_update_state(
            RenameTableOp(entity_fqn="demo.customers", to_name="clients"), "oracle", self.live, 1
        )
        self.assertIn('ALTER TABLE "demo"."customers" RENAME TO "clients"', ddl[0])
        self.assertIn('ALTER TABLE "demo"."_upd_customers" RENAME TO "_upd_clients"', ddl[1])

        ddl, _ = _build_ddl_and_update_state(
            RenameColumnOp(entity_fqn="demo.clients", from_name="name", to_name="full_name"), "oracle", self.live, 2
        )
        self.assertIn('ALTER TABLE "demo"."clients" RENAME COLUMN "name" TO "full_name"', ddl[0])

    def test_add_drop_column_oracle_quoted(self):
        self.live.schemas = ["demo"]
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string"),
            ],
        )
        _build_ddl_and_update_state(table_op, "oracle", self.live, 0)

        add_op = AddColumnOp(entity_fqn="demo.customers", column=ColumnSpec(name="email", type="string"))
        ddl, _ = _build_ddl_and_update_state(add_op, "oracle", self.live, 1)
        self.assertIn('ALTER TABLE "demo"."customers" ADD ("email" VARCHAR2(400))', ddl[0])

        drop_op = DropColumnOp(entity_fqn="demo.customers", name="email")
        ddl, _ = _build_ddl_and_update_state(drop_op, "oracle", self.live, 2)
        self.assertIn('ALTER TABLE "demo"."customers" DROP COLUMN "email"', ddl[0])

    def test_transfer_table_oracle_quoted(self):
        self.live.schemas = ["demo", "archive"]
        table_op = CreateTableOp(
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"})],
        )
        _build_ddl_and_update_state(table_op, "oracle", self.live, 0)
        ddl, _ = _build_ddl_and_update_state(
            TransferTableOp(entity_fqn="demo.customers", to_schema="archive"), "oracle", self.live, 1
        )
        self.assertIn('CREATE TABLE "archive"."customers" AS SELECT * FROM "demo"."customers"', ddl[0])
        self.assertIn('DROP TABLE "demo"."customers" CASCADE CONSTRAINTS', ddl[1])

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
        """Валидация PK: отклонение колонок без явного генератора sequence или uuid."""
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

        # Невалидный PK: числовая колонка без явного генератора (нет неявного fallback)
        bad_int_op = CreateTableOp(
            entity_fqn="demo.num_tags",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint"),
                ColumnSpec(name="tag_name", type="string"),
            ],
        )
        with self.assertRaises(SchemaEvolutionError) as ctx:
            _build_ddl_and_update_state(bad_int_op, "mssql", self.live, 2)
        self.assertIn("Primary key", str(ctx.exception))

        # Валидный PK: колонка с явным генератором uuid
        uuid_table_op = CreateTableOp(
            entity_fqn="demo.events",
            primary_key=["event_id"],
            columns=[
                ColumnSpec(name="event_id", type="uuid", generator={"kind": "uuid"}),
                ColumnSpec(name="payload", type="string"),
            ],
        )
        ddl, desc = _build_ddl_and_update_state(uuid_table_op, "mssql", self.live, 3)
        self.assertIn("demo.events", self.live.tables)

        # Валидный PK: колонка с явным генератором sequence
        seq_table_op = CreateTableOp(
            entity_fqn="demo.orders",
            primary_key=["order_id"],
            columns=[
                ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="title", type="string"),
            ],
        )
        ddl, desc = _build_ddl_and_update_state(seq_table_op, "mssql", self.live, 4)
        self.assertIn("demo.orders", self.live.tables)

    def test_fk_column_type_inheritance_and_order(self):
        """FK-колонка без типа автоматически наследует тип из родительской таблицы, сохраняя порядок."""
        self.live.schemas = ["demo"]
        parent_op = CreateTableOp(
            entity_fqn="demo.parents",
            primary_key=["parent_id"],
            columns=[
                ColumnSpec(name="parent_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string"),
            ],
        )
        _build_ddl_and_update_state(parent_op, "mssql", self.live, 1)

        # Дочерняя таблица: parent_id идет ПЕРВОЙ, type не указан
        child_op = CreateTableOp(
            entity_fqn="demo.children",
            primary_key=["parent_id", "child_seq"],
            columns=[
                ColumnSpec(name="parent_id"),  # type отсутствует!
                ColumnSpec(name="child_seq", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="note", type="string"),
            ],
            foreign_keys=[
                ForeignKey(columns=["parent_id"], ref_entity_fqn="demo.parents", ref_columns=["parent_id"])
            ],
        )
        ddl, desc = _build_ddl_and_update_state(child_op, "mssql", self.live, 2)
        child_table = self.live.tables["demo.children"]

        # 1. Тип parent_id унаследован из родителя
        parent_id_col = next(c for c in child_table.columns if c.name == "parent_id")
        self.assertEqual(parent_id_col.type, "bigint")

        # 2. Порядок колонок строго сохранен
        self.assertEqual(
            [c.name for c in child_table.columns],
            ["parent_id", "child_seq", "note", "_batch_id"]
        )

        # 3. В DDL присутствует правильный тип BIGINT для parent_id
        create_sql = next(stmt for stmt in ddl if "CREATE TABLE [demo].[children]" in stmt)
        self.assertIn("[parent_id] BIGINT", create_sql)

    def test_fk_column_overwrites_wrong_type(self):
        """Даже если для FK-колонки указан неверный тип, он принудительно заменяется типом родителя."""
        self.live.schemas = ["demo"]
        parent_op = CreateTableOp(
            entity_fqn="demo.departments",
            primary_key=["dept_id"],
            columns=[
                ColumnSpec(name="dept_id", type="bigint", generator={"kind": "sequence"}),
            ],
        )
        _build_ddl_and_update_state(parent_op, "mssql", self.live, 1)

        # Ошибочно указан type="string" для FK dept_id
        emp_op = CreateTableOp(
            entity_fqn="demo.employees",
            primary_key=["emp_id"],
            columns=[
                ColumnSpec(name="emp_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="dept_id", type="string"),  # Противоречит родителю!
            ],
            foreign_keys=[
                ForeignKey(columns=["dept_id"], ref_entity_fqn="demo.departments", ref_columns=["dept_id"])
            ],
        )
        _build_ddl_and_update_state(emp_op, "mssql", self.live, 2)
        emp_table = self.live.tables["demo.employees"]
        dept_id_col = next(c for c in emp_table.columns if c.name == "dept_id")
        self.assertEqual(dept_id_col.type, "bigint")

    def test_non_fk_column_missing_type_raises_error(self):
        """Если колонка не входит в FK и не имеет типа, создание таблицы отклоняется."""
        self.live.schemas = ["demo"]
        bad_col_op = CreateTableOp(
            entity_fqn="demo.invalid",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="missing_type_col"),  # без типа и не FK
            ],
        )
        with self.assertRaises(SchemaEvolutionError) as ctx:
            _build_ddl_and_update_state(bad_col_op, "mssql", self.live, 1)
        self.assertIn("обязана иметь 'type'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
