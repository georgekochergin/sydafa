"""Тесты эндпоинтов FastAPI sydafa с использованием TestClient."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
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
        with patch("app.main._is_ready", True), \
             patch("app.sources.source_names", return_value=["mssql", "oracle"]), \
             patch("app.init_apply.get_init_timestamp", return_value="2026-10-14T12:00:00Z"):
            resp = self.client.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["database"], "sydafa")
            self.assertEqual(data["sources"], ["mssql", "oracle"])
            self.assertEqual(data["initialized_at"], "2026-10-14T12:00:00Z")

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
             patch("app.main._wake", return_value={}), \
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
            "connection": {
                "type": "mssql",
                "host": "localhost",
                "port": 14330,
                "database": "sydafa",
                "user": "sydafa",
                "password": "sydafa",
                "jdbc_url": "jdbc:sqlserver://localhost:14330;databaseName=sydafa;trustServerCertificate=true;encrypt=false",
            },
            "host_port": 14330,
            "published": True,
        }
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.container_manager.wake_up", return_value=status):
            resp = self.client.post("/sources/mssql/activate?ttl_minutes=15")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["host_port"], 14330)
            self.assertTrue(data["published"])
            self.assertEqual(data["connection"]["user"], "sydafa")
            self.assertEqual(data["connection"]["port"], 14330)

    def test_activate_source_ttl_cap_and_fallback(self):
        status = {
            "source": "mssql",
            "state": "running",
            "last_used": "2026-01-01T00:00:00Z",
            "activated_until": "2026-01-01T00:30:00Z",
            "ttl_minutes": 30,
            "connection": None,
            "host_port": 14330,
            "published": True,
        }
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.container_manager.wake_up", return_value=status) as mock_wake:
            # При запросе > MAX_TTL_MINUTES (30) значение ограничивается 30 минутами
            resp = self.client.post("/sources/mssql/activate?ttl_minutes=45")
            self.assertEqual(resp.status_code, 200)
            mock_wake.assert_called_once()
            self.assertEqual(mock_wake.call_args[1]["ttl_minutes"], 30)

        mock_wake.reset_mock()
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.container_manager.wake_up", return_value=status) as mock_wake:
            # При запросе <= 0 или None значение сбрасывается на дефолт (15)
            resp = self.client.post("/sources/mssql/activate?ttl_minutes=0")
            self.assertEqual(resp.status_code, 200)
            mock_wake.assert_called_once()
            self.assertEqual(mock_wake.call_args[1]["ttl_minutes"], 15)

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

    def test_reinit_source_endpoint(self):
        reinit_res = {
            "status": "reinitialized",
            "source": "mssql",
            "applied_at": "2026-10-15T12:00:00Z",
            "operations_executed": 2,
            "message": "Источник 'mssql' успешно зачищен и реинициализирован с 2 операциями.",
        }
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.init_apply.reinit_source", return_value=reinit_res) as mock_reinit:
            resp = self.client.post("/sources/mssql/reinit")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "reinitialized")
            self.assertEqual(data["source"], "mssql")
            self.assertEqual(data["operations_executed"], 2)
            mock_reinit.assert_called_once_with("mssql")

    def test_list_sources(self):
        with patch("app.sources.source_names", return_value=["billing", "crm"]):
            resp = self.client.get("/sources")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"sources": ["billing", "crm"]})

    def test_lifespan_startup_and_seed(self):
        """Интеграционный тест: запуск lifespan FastAPI с первичным сидингом источников."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg_file = tmp_path / "sources.json"
            cfg_file.write_text(json.dumps({
                "sources": {
                    "mssql": {"type": "mssql", "operations": []}
                }
            }))
            with patch.dict("os.environ", {"SYDAFA_SOURCES_FILE": str(cfg_file)}), \
                 patch("app.init_apply.STATE_DIR", tmp_path), \
                 patch("app.container_manager.wake_up", autospec=True) as mock_wake, \
                 patch("app.container_manager.deactivate", autospec=True) as mock_deact:
                with TestClient(app) as client:
                    resp = client.get("/health")
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(resp.json()["status"], "ok")
                    self.assertIn("mssql", resp.json()["sources"])
                    mock_wake.assert_called_once()
                    mock_deact.assert_called_once()


if __name__ == "__main__":
    unittest.main()
