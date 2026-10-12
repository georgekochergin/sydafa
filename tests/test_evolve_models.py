"""Unit-тесты Pydantic-моделей генераторов данных, DML и DSL эволюции схемы."""
from __future__ import annotations

import unittest
from pydantic import ValidationError

from app.models import (
    AddColumnOp,
    AppendRequest,
    ChoiceGenerator,
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    DeleteRequest,
    DropColumnOp,
    DropSchemaOp,
    DropTableOp,
    EvolveRequest,
    FakerGenerator,
    ForeignKey,
    RandomDateGenerator,
    RandomDecimalGenerator,
    RenameColumnOp,
    RenameSchemaOp,
    RenameTableOp,
    SequenceGenerator,
    SourceConfig,
    TransferTableOp,
    UpdateRequest,
)


class TestEvolveModels(unittest.TestCase):
    def test_source_config(self):
        conf = SourceConfig(type="mssql", host="mssql-source", port=1433, reader="reader")
        self.assertEqual(conf.type, "mssql")
        self.assertEqual(conf.host, "mssql-source")
        self.assertEqual(conf.port, 1433)
        self.assertEqual(conf.reader, "reader")

        with self.assertRaises(ValidationError):
            SourceConfig(type="postgres", host="h", port=5432)

    def test_generators_validation(self):
        seq = SequenceGenerator()
        self.assertEqual(seq.kind, "sequence")

        fk = FakerGenerator(provider="company")
        self.assertEqual(fk.provider, "company")

        ch = ChoiceGenerator(values=["Retail", "VIP"])
        self.assertEqual(len(ch.values), 2)

        rd = RandomDecimalGenerator(min=10, max=50)
        self.assertEqual(rd.min, 10.0)

        rdate = RandomDateGenerator()
        self.assertEqual(rdate.kind, "random_date")

    def test_column_spec(self):
        col = ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"})
        self.assertEqual(col.name, "customer_id")
        self.assertEqual(col.generator.kind, "sequence")

        # Invalid identifier with spaces
        with self.assertRaises(ValidationError):
            ColumnSpec(name="invalid name", type="string")

        # Invalid identifier with special characters
        with self.assertRaises(ValidationError):
            ColumnSpec(name="bad-name!", type="string")

    def test_all_evolve_operations_in_request(self):
        payload = {
            "operations": [
                {"op": "create_schema", "name": "demo"},
                {
                    "op": "create_table",
                    "schema": "demo",
                    "name": "customers",
                    "primary_key": ["customer_id"],
                    "columns": [
                        {"name": "customer_id", "type": "bigint", "generator": {"kind": "sequence"}},
                        {"name": "name", "type": "string", "generator": {"kind": "faker", "provider": "company"}},
                        {"name": "segment", "type": "string", "generator": {"kind": "choice", "values": ["A", "B"]}},
                    ],
                    "foreign_keys": [],
                },
                {
                    "op": "add_column",
                    "schema": "demo",
                    "table": "customers",
                    "column": {"name": "created_at", "type": "date", "generator": {"kind": "random_date"}},
                },
                {
                    "op": "rename_column",
                    "schema": "demo",
                    "table": "customers",
                    "from_name": "name",
                    "to_name": "full_name",
                },
                {
                    "op": "rename_table",
                    "schema": "demo",
                    "from_name": "customers",
                    "to_name": "clients",
                },
                {
                    "op": "drop_column",
                    "schema": "demo",
                    "table": "clients",
                    "name": "segment",
                },
                {
                    "op": "transfer_table",
                    "table": "clients",
                    "from_schema": "demo",
                    "to_schema": "core",
                },
                {
                    "op": "drop_table",
                    "schema": "core",
                    "name": "clients",
                },
                {
                    "op": "rename_schema",
                    "from_name": "demo",
                    "to_name": "staging",
                },
                {
                    "op": "drop_schema",
                    "name": "staging",
                },
            ]
        }
        req = EvolveRequest(**payload)
        self.assertEqual(len(req.operations), 10)
        self.assertIsInstance(req.operations[0], CreateSchemaOp)
        self.assertIsInstance(req.operations[1], CreateTableOp)
        self.assertIsInstance(req.operations[2], AddColumnOp)
        self.assertIsInstance(req.operations[3], RenameColumnOp)
        self.assertIsInstance(req.operations[4], RenameTableOp)
        self.assertIsInstance(req.operations[5], DropColumnOp)
        self.assertIsInstance(req.operations[6], TransferTableOp)
        self.assertIsInstance(req.operations[7], DropTableOp)
        self.assertIsInstance(req.operations[8], RenameSchemaOp)
        self.assertIsInstance(req.operations[9], DropSchemaOp)

    def test_invalid_operation_rejected(self):
        with self.assertRaises(ValidationError):
            EvolveRequest(operations=[{"op": "unknown_op", "foo": "bar"}])

        with self.assertRaises(ValidationError):
            CreateSchemaOp(name="schema with space")

        with self.assertRaises(ValidationError):
            RenameColumnOp(schema="demo", table="customers", from_name="col", to_name="bad-col")

    def test_dml_models(self):
        append_req = AppendRequest(rows=10, new_key_ratio=0.5)
        self.assertEqual(append_req.rows, 10)

        update_req = UpdateRequest(
            predicate=[{"column": "id", "operator": "=", "value": 1}],
            set={"name": "updated"},
        )
        self.assertEqual(len(update_req.predicate), 1)

        delete_req = DeleteRequest(
            predicate=[{"column": "id", "operator": ">=", "value": 100}],
            cascade=True,
        )
        self.assertTrue(delete_req.cascade)


if __name__ == "__main__":
    unittest.main()
