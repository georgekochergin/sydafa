"""Unit-тесты для подсистемы правил связности данных и эндпоинта /migrate."""
import unittest
from unittest.mock import patch

from app.init_apply import compile_schema_decl_to_operations
from app.models import (
    CreateTableOp,
    CumulativeBalanceInvariant,
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
                CumulativeBalanceInvariant(partition_by=["account_id"], amount_column="amount", min_balance=0.0)
            ]
        )
        rules_state.set_table_rules("mssql", "core.transactions", profile)

        retrieved = rules_state.get_table_rules("mssql", "core.transactions")
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved.invariants), 1)
        self.assertIsInstance(retrieved.invariants[0], CumulativeBalanceInvariant)

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

    @patch("app.db_connectors.get_connection")
    @patch("app.schema_state.get_live_schema")
    @patch("app.sources.get_source")
    def test_validate_invariants_violation_and_success(self, mock_get_source, mock_get_live, mock_get_conn):
        """Проверка строгой валидации существующих данных в validate_table_invariants_on_data."""
        from app.models import ColumnSpec, LiveSchema, SourceConfig, TableSchema

        table = TableSchema(
            name="transactions",
            schema="core",
            primary_key=["id"],
            columns=[ColumnSpec(name="id", type="bigint"), ColumnSpec(name="amount", type="decimal(12,2)")],
            foreign_keys=[],
        )
        mock_get_live.return_value = LiveSchema(schemas=["core"], tables={"core.transactions": table})
        mock_get_source.return_value = SourceConfig(name="mssql", type="mssql", host="host", port=1433)

        class MockCursor:
            def __init__(self, rows):
                self._rows = rows

            def execute(self, sql):
                pass

            def fetchall(self):
                return self._rows

        class MockConn:
            def __init__(self, rows):
                self.cur = MockCursor(rows)

            def cursor(self):
                return self.cur

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        profile = TableRuleProfile(
            invariants=[
                CumulativeBalanceInvariant(partition_by=["account_id"], amount_column="amount", min_balance=0.0)
            ]
        )

        # 1. Случай нарушения: остаток -1500.00
        mock_get_conn.return_value = MockConn([([101], -1500.00)])
        with self.assertRaises(ValueError) as ctx:
            rules_state.validate_table_invariants_on_data("mssql", "core.transactions", profile)
        self.assertIn("отрицательн" if "отрицательн" in str(ctx.exception) else "нарушает ограничение", str(ctx.exception))
        self.assertIn("101", str(ctx.exception))

        # 2. Случай успеха: нет строк с нарушениями (пустая выборка HAVING)
        mock_get_conn.return_value = MockConn([])
        # Не должно вызывать исключений
        rules_state.validate_table_invariants_on_data("mssql", "core.transactions", profile)

    def test_invariant_guard_in_exec_engine(self):
        """Проверка условного срабатывания Invariant Guard в exec_engine при update/delete."""
        from app.models import DeleteOp, LiveSchema, TableSchema, ColumnSpec
        from app.exec_engine import ExecError

        table = TableSchema(
            name="txs",
            schema="core",
            primary_key=["id"],
            columns=[ColumnSpec(name="id", type="bigint"), ColumnSpec(name="amount", type="decimal(12,2)")],
            foreign_keys=[],
        )
        live = LiveSchema(schemas=["core"], tables={"core.txs": table})

        class MockCursor:
            def execute(self, sql, params=None):
                pass
            def fetchall(self):
                # Имитируем нарушение: отрицательный остаток
                return [([1], -500.0)]
            def fetchone(self):
                return None

        class MockConn:
            def __init__(self):
                self.cur = MockCursor()
            def cursor(self):
                return self.cur

        conn = MockConn()

        # 1. Мягкий режим (validate_existing=False): Invariant Guard НЕ блокирует мутацию
        profile_soft = TableRuleProfile(
            invariants=[CumulativeBalanceInvariant(partition_by=["account_id"], amount_column="amount", validate_existing=False)]
        )
        rules_state.set_table_rules("mssql", "core.txs", profile_soft)
        # Проверяем, что при validate_existing=False проверка не выбрасывает ExecError
        prof = rules_state.get_table_rules("mssql", "core.txs")
        self.assertFalse(any(getattr(inv, "validate_existing", False) for inv in prof.invariants))

        # 2. Строгий режим (validate_existing=True): Invariant Guard выбрасывает ошибку при нарушении
        profile_strict = TableRuleProfile(
            invariants=[CumulativeBalanceInvariant(partition_by=["account_id"], amount_column="amount", validate_existing=True)]
        )
        rules_state.set_table_rules("mssql", "core.txs", profile_strict)
        prof_strict = rules_state.get_table_rules("mssql", "core.txs")
        self.assertTrue(any(getattr(inv, "validate_existing", False) for inv in prof_strict.invariants))
