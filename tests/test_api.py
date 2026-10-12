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


if __name__ == "__main__":
    unittest.main()
