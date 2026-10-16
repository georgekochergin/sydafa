"""Unit-тесты для подсистемы правил связности данных и эндпоинта /migrate."""
import unittest
from unittest.mock import patch

from app.init_apply import compile_schema_decl_to_operations
from app.models import (
    CreateTableOp,
    CumulativeBalanceInvariant,
    FunctionalDependencyInvariant,
    MigrateRequest,
    TableRuleProfile,
)
from app import rules_state


class TestRulesAndMigrate(unittest.TestCase):
    def setUp(self):
        rules_state._rules_cache.clear()

    def test_rules_state_crud(self):
        """Проверка операций CRUD в rules_state."""
        profile = TableRuleProfile(
            invariants=[
                FunctionalDependencyInvariant(determinant=["account_id"], dependent=["client_id"])
            ]
        )
        rules_state.set_table_rules("mssql", "core.transactions", profile)

        retrieved = rules_state.get_table_rules("mssql", "core.transactions")
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved.invariants), 1)
        self.assertIsInstance(retrieved.invariants[0], FunctionalDependencyInvariant)

        # Некорректный FQN
        with self.assertRaises(ValueError):
            rules_state.get_table_rules("mssql", "invalid_fqn")

        # Удаление
        self.assertTrue(rules_state.delete_table_rules("mssql", "core.transactions"))
        self.assertIsNone(rules_state.get_table_rules("mssql", "core.transactions"))

    def test_compile_schema_decl_to_operations(self):
        """Проверка трансляции декларативного schema в батч DDL операций."""
        schema_data = {
            "tables": {
                "core.accounts": {
                    "primary_key": ["account_id"],
                    "columns": {"account_id": "bigint", "name": "string"},
                },
                "core.transactions": {
                    "primary_key": ["tx_id"],
                    "columns": {"tx_id": "bigint", "account_id": "bigint", "amount": "decimal(12,2)"},
                    "foreign_keys": [
                        {
                            "columns": ["account_id"],
                            "ref_schema": "core",
                            "ref_table": "accounts",
                            "ref_columns": ["account_id"],
                        }
                    ],
                },
            }
        }

        ops = compile_schema_decl_to_operations(schema_data)
        self.assertEqual(len(ops), 3)  # create_schema + create_table accounts + create_table transactions
        self.assertEqual(ops[0].op, "create_schema")
        self.assertEqual(ops[0].name, "core")

        # Таблица accounts должна идти ДО transactions из-за Foreign Key!
        self.assertEqual(ops[1].op, "create_table")
        self.assertEqual(ops[1].name, "accounts")

        self.assertEqual(ops[2].op, "create_table")
        self.assertEqual(ops[2].name, "transactions")

    def test_migrate_request_parsing(self):
        """Проверка разбора MigrateRequest."""
        req = MigrateRequest(
            operations=[
                {"op": "create_schema", "name": "test"},
                {"op": "truncate", "schema": "test", "table": "users"},
            ]
        )
        self.assertEqual(len(req.operations), 2)
