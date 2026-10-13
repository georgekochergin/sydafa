"""Тесты эндпоинтов FastAPI sydafa с использованием TestClient."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.models import LiveSchema, TableSchema, ColumnSpec, SourceConfig


class TestSydafaAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_health_not_ready(self):
        with patch("app.main._is_ready", False):
            resp = self.client.get("/health")
            self.assertEqual(resp.status_code, 503)

    def test_health_ready(self):
        with patch("app.main._is_ready", True), patch("app.sources.source_names", return_value=["mssql", "oracle"]):
            resp = self.client.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["database"], "sydafa")
            self.assertEqual(data["sources"], ["mssql", "oracle"])

    def test_get_source_info(self):
        fake_cfg = SourceConfig(type="mssql", host="mssql-source", port=1433, reader="reader")
        fake_live = LiveSchema(
            schemas=["demo"],
            tables={
                "demo.customers": TableSchema(
                    name="customers",
                    schema="demo",
                    primary_key=["customer_id"],
                    columns=[ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"})],
                )
            },
        )
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.sources.get_source", return_value=fake_cfg), \
             patch("app.schema_state.get_live_schema", return_value=fake_live):
            resp = self.client.get("/sources/mssql")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["source"], "mssql")
            self.assertEqual(data["database"], "sydafa")
            self.assertEqual(data["reader"], "reader")
            self.assertEqual(data["schemas"], ["demo"])
            self.assertEqual(len(data["tables"]), 1)
            self.assertEqual(data["tables"][0]["name"], "customers")

    def test_exec_source_endpoint(self):
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.main._wake", return_value={}), \
             patch("app.exec_engine.execute_source_operations") as mock_exec:
            mock_exec.return_value = MagicMock(status="ok", executed=1, applied=[{"op": "create_schema"}])
            resp = self.client.post("/sources/mssql/exec", json={
                "operations": [{"op": "create_schema", "name": "demo"}]
            })
            self.assertEqual(resp.status_code, 200)
            mock_exec.assert_called_once()

    def test_activate_source(self):
        status = {
            "source": "mssql",
            "state": "running",
            "last_used": "2026-01-01T00:00:00+00:00",
            "activated_until": "2026-01-01T00:15:00+00:00",
            "ttl_minutes": 15,
            "host_port": 32768,
            "published": True,
        }
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.container_manager.wake_up", return_value=status):
            resp = self.client.post("/sources/mssql/activate?ttl_minutes=15")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["host_port"], 32768)
            self.assertTrue(data["published"])

    def test_source_status(self):
        status = {
            "source": "mssql",
            "state": "idle",
            "last_used": None,
            "activated_until": None,
            "ttl_minutes": 15,
            "host_port": None,
            "published": False,
        }
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.container_manager.get_runtime_status", return_value=status):
            resp = self.client.get("/sources/mssql/status")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["state"], "idle")


if __name__ == "__main__":
    unittest.main()
