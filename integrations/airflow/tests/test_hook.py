"""Unit tests for SydafaHook."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from airflow.exceptions import AirflowException
from airflow.models import Connection

from sydafa_airflow.hooks.sydafa import SydafaHook


class TestSydafaHook(unittest.TestCase):
    def test_conn_id_is_required(self):
        """Проверка строго обязательного аргумента conn_id."""
        with self.assertRaises(TypeError):
            SydafaHook()  # type: ignore[call-arg]

        with self.assertRaises(ValueError):
            SydafaHook(conn_id="")

    @patch.object(SydafaHook, "get_connection")
    def test_get_base_url_variations(self, mock_get_conn):
        """Проверка нормализации URL из различных форматов Airflow Connection."""
        # 1. Host с схемой и портом
        mock_get_conn.return_value = Connection(conn_id="test1", host="http://sydafa:8000")
        hook = SydafaHook(conn_id="test1")
        self.assertEqual(hook.get_base_url(), "http://sydafa:8000")

        # 2. Host без схемы, с портом в поле port
        mock_get_conn.return_value = Connection(conn_id="test2", host="sydafa", port=8000, schema="http")
        hook2 = SydafaHook(conn_id="test2")
        self.assertEqual(hook2.get_base_url(), "http://sydafa:8000")

        # 3. Host с trailing slash
        mock_get_conn.return_value = Connection(conn_id="test3", host="https://sydafa.internal:8443/")
        hook3 = SydafaHook(conn_id="test3")
        self.assertEqual(hook3.get_base_url(), "https://sydafa.internal:8443")

        # 4. Host отсутствует
        mock_get_conn.return_value = Connection(conn_id="test4", host="")
        hook4 = SydafaHook(conn_id="test4")
        with self.assertRaises(AirflowException):
            hook4.get_base_url()

    @patch("requests.post")
    @patch.object(SydafaHook, "get_connection")
    def test_activate_success(self, mock_get_conn, mock_post):
        mock_get_conn.return_value = Connection(conn_id="sydafa_default", host="http://sydafa:8000")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"state": "running", "activated_until": "2026-10-16T18:00:00Z"}
        mock_post.return_value = mock_resp

        hook = SydafaHook(conn_id="sydafa_default")
        result = hook.activate(source="mssql", ttl_minutes=30, timeout_seconds=120)

        self.assertEqual(result["state"], "running")
        mock_post.assert_called_once_with(
            "http://sydafa:8000/sources/mssql/activate",
            params={"ttl_minutes": 30},
            timeout=120,
        )

    @patch("requests.post")
    @patch.object(SydafaHook, "get_connection")
    def test_activate_http_error(self, mock_get_conn, mock_post):
        mock_get_conn.return_value = Connection(conn_id="sydafa_default", host="http://sydafa:8000")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        hook = SydafaHook(conn_id="sydafa_default")
        with self.assertRaises(AirflowException) as ctx:
            hook.activate(source="mssql")
        self.assertIn("HTTP 500", str(ctx.exception))

    @patch("requests.post")
    @patch.object(SydafaHook, "get_connection")
    def test_migrate_success(self, mock_get_conn, mock_post):
        mock_get_conn.return_value = Connection(conn_id="sydafa_default", host="http://sydafa:8000")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"executed": 2}
        mock_post.return_value = mock_resp

        hook = SydafaHook(conn_id="sydafa_default")
        ops = [
            {"op": "truncate", "entity_fqn": "demo.users"},
            {"op": "append", "entity_fqn": "demo.users", "rows": 10},
        ]
        result = hook.migrate(source="mssql", operations=ops)

        self.assertEqual(result["executed"], 2)
        mock_post.assert_called_once_with(
            "http://sydafa:8000/sources/mssql/migrate",
            json={"operations": ops},
            timeout=300,
        )

    @patch("requests.post")
    @patch.object(SydafaHook, "get_connection")
    def test_deactivate_success(self, mock_get_conn, mock_post):
        mock_get_conn.return_value = Connection(conn_id="sydafa_default", host="http://sydafa:8000")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"state": "idle"}
        mock_post.return_value = mock_resp

        hook = SydafaHook(conn_id="sydafa_default")
        result = hook.deactivate(source="oracle")

        self.assertEqual(result["state"], "idle")
        mock_post.assert_called_once_with(
            "http://sydafa:8000/sources/oracle/deactivate",
            timeout=60,
        )


if __name__ == "__main__":
    unittest.main()
