"""Unit-тесты фиксации флага init_state.json, API bootstrap и self-healing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.constants import DEFAULT_TTL_MINUTES
from app.init_apply import (
    apply_init_for_source,
    check_and_heal_orphaned_state,
    get_init_timestamp,
    get_source_init_timestamp,
    is_source_initialized,
    mark_source_initialized,
    reinit_source,
    reset_source_init,
    seed_all_sources_on_first_startup,
)


class TestInitApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_init_lifecycle(self):
        with patch("app.init_apply.STATE_DIR", self.state):
            self.assertFalse(is_source_initialized("mssql"))
            self.assertIsNone(get_source_init_timestamp("mssql"))
            mark_source_initialized("mssql")
            self.assertTrue(is_source_initialized("mssql"))
            self.assertFalse(is_source_initialized("oracle"))
            self.assertIsNotNone(get_source_init_timestamp("mssql"))
            self.assertIsNotNone(get_init_timestamp())

    def test_skip_when_source_already_initialized(self):
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.exec_engine.execute_source_operations") as mock_exec:
            mark_source_initialized("mssql")
            apply_init_for_source("mssql")
            mock_exec.assert_not_called()

    def test_seed_all_sources_on_first_startup_fresh(self):
        manifest_data = {
            "mssql": {"type": "mssql", "operations": []},
            "crm": {"type": "oracle", "operations": []},
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.init_apply.load_manifest_sources", return_value=manifest_data), \
             patch("app.container_manager.wake_up", autospec=True) as mock_wake, \
             patch("app.container_manager.deactivate", autospec=True) as mock_deactivate:
            seed_all_sources_on_first_startup()
            self.assertEqual(mock_wake.call_count, 2)
            mock_wake.assert_any_call("mssql", ttl_minutes=DEFAULT_TTL_MINUTES)
            mock_wake.assert_any_call("crm", ttl_minutes=DEFAULT_TTL_MINUTES)
            self.assertEqual(mock_deactivate.call_count, 2)
            mock_deactivate.assert_any_call("mssql")
            mock_deactivate.assert_any_call("crm")
            self.assertTrue(is_source_initialized("mssql"))
            self.assertTrue(is_source_initialized("crm"))

    def test_seed_all_sources_skips_initialized_and_seeds_new(self):
        manifest_data = {
            "mssql": {"type": "mssql", "operations": []},
            "erp": {"type": "mssql", "operations": []},
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.init_apply.load_manifest_sources", return_value=manifest_data), \
             patch("app.container_manager.wake_up", autospec=True) as mock_wake, \
             patch("app.container_manager.deactivate", autospec=True) as mock_deactivate:
            mark_source_initialized("mssql")
            seed_all_sources_on_first_startup()
            # mssql уже инициализирован, должен запуститься только erp
            mock_wake.assert_called_once_with("erp", ttl_minutes=DEFAULT_TTL_MINUTES)
            mock_deactivate.assert_called_once_with("erp")
            self.assertTrue(is_source_initialized("erp"))

    def test_apply_for_source_with_operations(self):
        manifest_data = {
            "mssql": {
                "type": "mssql",
                "operations": [{"op": "create_schema", "name": "demo"}]
            }
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.init_apply.load_manifest_sources", return_value=manifest_data), \
             patch("app.exec_engine.execute_source_operations") as mock_exec:
            apply_init_for_source("mssql")
            mock_exec.assert_called_once()
            self.assertTrue(is_source_initialized("mssql"))

    def test_self_healing_orphaned_resources(self):
        mock_client = MagicMock()
        mock_container = MagicMock(name="sydafa-src-mssql-mssql", labels={})
        mock_container.name = "sydafa-src-mssql-mssql"
        mock_client.containers.list.return_value = [mock_container]

        mock_volume = MagicMock()
        mock_volume.name = "sydafa-data-mssql-mssql"
        mock_volume.attrs = {"Labels": {}}
        mock_client.volumes.list.return_value = [mock_volume]

        with patch("app.init_apply.STATE_DIR", self.state),              patch("app.container_manager._get_docker_client", return_value=mock_client):
            check_and_heal_orphaned_state()
            mock_container.remove.assert_called_once_with(force=True, v=True)
            mock_volume.remove.assert_called_once_with(force=True)


    def test_reinit_source_flow(self):
        manifest_data = {
            "mssql": {
                "type": "mssql",
                "operations": [{"op": "create_schema", "name": "fresh"}]
            }
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.container_manager.purge_source_container_and_volume", autospec=True) as mock_purge, \
             patch("app.init_apply.load_manifest_sources", return_value=manifest_data), \
             patch("app.sources.load_sources") as mock_load_sources, \
             patch("app.container_manager.wake_up", autospec=True) as mock_wake, \
             patch("app.container_manager.deactivate", autospec=True) as mock_deactivate:
            mark_source_initialized("mssql")
            self.assertTrue(is_source_initialized("mssql"))

            res = reinit_source("mssql")
            self.assertEqual(res["status"], "reinitialized")
            self.assertEqual(res["source"], "mssql")
            self.assertEqual(res["operations_executed"], 1)

            mock_purge.assert_called_once_with("mssql")
            mock_load_sources.assert_called_once()
            mock_wake.assert_called_once_with("mssql", ttl_minutes=DEFAULT_TTL_MINUTES)
            mock_deactivate.assert_called_once_with("mssql")
            self.assertTrue(is_source_initialized("mssql"))


if __name__ == "__main__":
    unittest.main()
