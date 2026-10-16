"""Unit tests for Sydafa Airflow operators."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from airflow.exceptions import AirflowException

from sydafa_airflow.operators.sydafa import (
    SydafaActivateOperator,
    SydafaDeactivateOperator,
    SydafaMigrateOperator,
)


class TestSydafaOperators(unittest.TestCase):
    def test_conn_id_is_required_on_all_operators(self):
        """Проверка, что conn_id строго обязателен без значений по умолчанию."""
        with self.assertRaises((TypeError, AirflowException)):
            SydafaActivateOperator(task_id="act", source="mssql")  # type: ignore[call-arg]

        with self.assertRaises((TypeError, AirflowException)):
            SydafaMigrateOperator(task_id="mig", source="mssql", operations=[])  # type: ignore[call-arg]

        with self.assertRaises((TypeError, AirflowException)):
            SydafaDeactivateOperator(task_id="deact", source="mssql")  # type: ignore[call-arg]

        with self.assertRaises(ValueError):
            SydafaActivateOperator(task_id="act", source="mssql", conn_id="")

        with self.assertRaises(ValueError):
            SydafaMigrateOperator(task_id="mig", source="mssql", operations=[], conn_id="")

        with self.assertRaises(ValueError):
            SydafaDeactivateOperator(task_id="deact", source="mssql", conn_id="")

    def test_template_fields(self):
        """Проверка наличия conn_id в template_fields всех операторов."""
        self.assertIn("conn_id", SydafaActivateOperator.template_fields)
        self.assertIn("conn_id", SydafaMigrateOperator.template_fields)
        self.assertIn("conn_id", SydafaDeactivateOperator.template_fields)

    @patch("sydafa_airflow.operators.sydafa.SydafaHook")
    def test_activate_operator_execute(self, mock_hook_cls):
        mock_hook = MagicMock()
        mock_hook.activate.return_value = {"state": "running"}
        mock_hook_cls.return_value = mock_hook

        op = SydafaActivateOperator(
            task_id="test_activate",
            conn_id="custom_sydafa",
            source="mssql",
            ttl_minutes=45,
            timeout_seconds=90,
        )
        context = MagicMock()
        result = op.execute(context)

        mock_hook_cls.assert_called_once_with(conn_id="custom_sydafa")
        mock_hook.activate.assert_called_once_with(
            source="mssql",
            ttl_minutes=45,
            timeout_seconds=90,
        )
        self.assertEqual(result, {"state": "running"})

    @patch("sydafa_airflow.operators.sydafa.SydafaHook")
    def test_migrate_operator_execute(self, mock_hook_cls):
        mock_hook = MagicMock()
        mock_hook.migrate.return_value = {"executed": 1}
        mock_hook_cls.return_value = mock_hook

        ops = [{"op": "truncate", "entity_fqn": "demo.orders"}]
        op = SydafaMigrateOperator(
            task_id="test_migrate",
            conn_id="custom_sydafa",
            source="oracle",
            operations=ops,
            timeout_seconds=200,
        )
        context = MagicMock()
        result = op.execute(context)

        mock_hook_cls.assert_called_once_with(conn_id="custom_sydafa")
        mock_hook.migrate.assert_called_once_with(
            source="oracle",
            operations=ops,
            timeout_seconds=200,
        )
        self.assertEqual(result, {"executed": 1})

    @patch("sydafa_airflow.operators.sydafa.SydafaHook")
    def test_deactivate_operator_execute(self, mock_hook_cls):
        mock_hook = MagicMock()
        mock_hook.deactivate.return_value = {"state": "idle"}
        mock_hook_cls.return_value = mock_hook

        op = SydafaDeactivateOperator(
            task_id="test_deactivate",
            conn_id="custom_sydafa",
            source="mssql",
            timeout_seconds=40,
        )
        context = MagicMock()
        result = op.execute(context)

        mock_hook_cls.assert_called_once_with(conn_id="custom_sydafa")
        mock_hook.deactivate.assert_called_once_with(
            source="mssql",
            timeout_seconds=40,
        )
        self.assertEqual(result, {"state": "idle"})


if __name__ == "__main__":
    unittest.main()
