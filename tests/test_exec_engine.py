"""Unit-тесты моделей и движка exec_engine в sydafa."""
import unittest
from pydantic import TypeAdapter

from app.models import (
    AppendOp,
    CreateSchemaOp,
    CreateTableOp,
    DeleteOp,
    ExecOperation,
    ExecRequest,
    TruncateOp,
    UpdateOp,
)


class TestExecModels(unittest.TestCase):
    def test_parse_exec_operations(self):
        adapter = TypeAdapter(list[ExecOperation])
        raw_ops = [
            {"op": "create_schema", "name": "demo"},
            {
                "op": "create_table",
                "schema": "demo",
                "name": "customers",
                "primary_key": ["id"],
                "columns": [{"name": "id", "type": "bigint"}],
            },
            {"op": "truncate", "schema": "demo", "table": "customers"},
            {"op": "append", "schema": "demo", "table": "customers", "rows": 5},
            {
                "op": "update",
                "schema": "demo",
                "table": "customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
                "set": {"name": "test"},
            },
            {
                "op": "delete",
                "schema": "demo",
                "table": "customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
            },
        ]
        ops = adapter.validate_python(raw_ops)
        self.assertEqual(len(ops), 6)
        self.assertIsInstance(ops[0], CreateSchemaOp)
        self.assertIsInstance(ops[1], CreateTableOp)
        self.assertIsInstance(ops[2], TruncateOp)
        self.assertIsInstance(ops[3], AppendOp)
        self.assertIsInstance(ops[4], UpdateOp)
        self.assertIsInstance(ops[5], DeleteOp)

        req = ExecRequest(operations=ops)
        self.assertEqual(len(req.operations), 6)


if __name__ == "__main__":
    unittest.main()
