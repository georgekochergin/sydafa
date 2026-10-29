"""Characterization: диалектный SQL рендеринга DDL для MSSQL и Oracle.

Закрепляет текущий SQL-вывод идентификаторов, типов и констрейнтов до/при переносе
в backends/rdbms/{mssql,oracle}/dialect.py (proposal-1.md, §4 behavior freeze).
При переезде модуля меняются только импорты — ассерты остаются неизменными.
"""
from __future__ import annotations

import unittest

from app.models import ColumnSpec, ForeignKey, TableSchema
from app.backends.rdbms.mssql.dialect import MssqlDialect
from app.backends.rdbms.oracle.dialect import OracleDialect

MSSQL = MssqlDialect()
ORACLE = OracleDialect()


class TestMapTypeCharacterization(unittest.TestCase):
    def test_all_logical_types_mssql(self):
        expected = {
            "bigint": "BIGINT",
            "string": "NVARCHAR(400)",
            "date": "DATE",
            "timestamp": "DATETIME2",
            "uuid": "UNIQUEIDENTIFIER",
        }
        for logical, sql_type in expected.items():
            self.assertEqual(MSSQL.map_type(logical), sql_type)

    def test_all_logical_types_oracle(self):
        expected = {
            "bigint": "NUMBER(19)",
            "string": "VARCHAR2(400)",
            "date": "DATE",
            "timestamp": "TIMESTAMP",
            "uuid": "VARCHAR2(36)",
        }
        for logical, sql_type in expected.items():
            self.assertEqual(ORACLE.map_type(logical), sql_type)

    def test_decimal_variants(self):
        self.assertEqual(MSSQL.map_type("decimal(10,2)"), "DECIMAL(10,2)")
        self.assertEqual(ORACLE.map_type("decimal(12, 0)"), "NUMBER(12,0)")
        self.assertEqual(ORACLE.map_type("decimal(38,9)"), "NUMBER(38,9)")

    def test_unknown_logical_type_raises_in_both_dialects(self):
        for dialect in (MSSQL, ORACLE):
            with self.subTest(dialect=dialect.engine_type):
                with self.assertRaises(ValueError):
                    dialect.map_type("money")


class TestIdentifierQuotingCharacterization(unittest.TestCase):
    def test_mssql_bracket_quoting(self):
        self.assertEqual(MSSQL.quote_identifier("order_id"), "[order_id]")
        self.assertEqual(MSSQL.qualify_table("dbo", "orders"), "[dbo].[orders]")

    def test_oracle_double_quote_quoting(self):
        self.assertEqual(ORACLE.quote_identifier("order_id"), '"order_id"')
        self.assertEqual(ORACLE.qualify_table("dbo", "orders"), '"dbo"."orders"')


class TestRenderCreateTableCharacterization(unittest.TestCase):
    def _table(self, name: str = "orders", fk_columns: list[str] | None = None) -> TableSchema:
        return TableSchema(
            name=name,
            schema="dbo",
            primary_key=["order_id"],
            columns=[
                ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="title", type="string"),
                ColumnSpec(name="amount", type="decimal(10,2)"),
            ],
            foreign_keys=[
                ForeignKey(
                    columns=fk_columns or ["cust_id"],
                    ref_entity_fqn="dbo.customers",
                    ref_columns=["cust_id"],
                )
            ] if name != "customers" else [],
        )

    def test_mssql_create_table_full_sql(self):
        sql = MSSQL.create_table(self._table())
        self.assertEqual(
            sql,
            "CREATE TABLE [dbo].[orders] (\n"
            "  [order_id] BIGINT,\n"
            "  [title] NVARCHAR(400),\n"
            "  [amount] DECIMAL(10,2),\n"
            "  CONSTRAINT [pk_dbo_orders] PRIMARY KEY ([order_id]),\n"
            "  CONSTRAINT [fk_dbo_orders_cust_id] FOREIGN KEY ([cust_id]) "
            "REFERENCES [dbo].[customers] ([cust_id])\n"
            ")",
        )

    def test_oracle_create_table_full_sql(self):
        sql = ORACLE.create_table(self._table())
        self.assertEqual(
            sql,
            'CREATE TABLE "dbo"."orders" (\n'
            '  "order_id" NUMBER(19),\n'
            '  "title" VARCHAR2(400),\n'
            '  "amount" NUMBER(10,2),\n'
            '  CONSTRAINT "pk_dbo_orders" PRIMARY KEY ("order_id"),\n'
            '  CONSTRAINT "fk_dbo_orders_cust_id" FOREIGN KEY ("cust_id") '
            'REFERENCES "dbo"."customers" ("cust_id")\n'
            ")",
        )

    def test_oracle_fk_constraint_name_truncated_to_30_chars(self):
        long_table = TableSchema(
            name="very_long_sales_transaction_fact_table",
            schema="dbo",
            primary_key=["id"],
            columns=[ColumnSpec(name="id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[
                ForeignKey(
                    columns=["extremely_long_counterparty_identifier_column"],
                    ref_entity_fqn="dbo.partners",
                    ref_columns=["partner_id"],
                )
            ],
        )
        sql = ORACLE.create_table(long_table)
        fk_line = next(line for line in sql.splitlines() if "FOREIGN KEY" in line)
        constraint_name = fk_line.split("CONSTRAINT ")[1].split(" FOREIGN")[0]
        self.assertLessEqual(len(constraint_name.strip('"')), 30)
        # MSSQL не усекает имя констрейнта
        sql_mssql = MSSQL.create_table(long_table)
        fk_line_mssql = next(line for line in sql_mssql.splitlines() if "FOREIGN KEY" in line)
        constraint_mssql = fk_line_mssql.split("CONSTRAINT [")[1].split("] FOREIGN")[0]
        self.assertGreater(len(constraint_mssql), 30)

    def test_oracle_fk_without_explicit_ref_schema_defaults_to_own_schema(self):
        table = TableSchema(
            name="items",
            schema="core",
            primary_key=["item_id"],
            columns=[ColumnSpec(name="item_id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[
                ForeignKey(columns=["order_id"], ref_entity_fqn="core.orders", ref_columns=["order_id"])
            ],
        )
        sql = ORACLE.create_table(table)
        self.assertIn('REFERENCES "core"."orders" ("order_id")', sql)
        sql_mssql = MSSQL.create_table(table)
        self.assertIn("REFERENCES [core].[orders] ([order_id])", sql_mssql)


if __name__ == "__main__":
    unittest.main()
