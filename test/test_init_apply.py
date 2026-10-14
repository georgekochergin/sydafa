"""Unit-тесты фиксации флага init_state.json, API bootstrap и self-healing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.init_apply import (
    apply_init_for_source,
    check_and_heal_orphaned_state,
    get_init_timestamp,
    is_init_applied,
    mark_init_applied,
    seed_all_sources_on_first_startup,
)


class TestInitApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_lifecycle(self):
        with patch("app.init_apply.STATE_DIR", self.state):
            self.assertFalse(is_init_applied())
            self.assertIsNone(get_init_timestamp())
            mark_init_applied()
            self.assertTrue(is_init_applied())
            self.assertIsNotNone(get_init_timestamp())

    def test_skip_when_flag_is_true(self):
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.main.exec_source") as mock_exec, \
             patch("app.main.create_source") as mock_create:
            mark_init_applied()
            apply_init_for_source("mssql")
            mock_create.assert_not_called()
            mock_exec.assert_not_called()

    def test_seed_all_sources_on_first_startup_fresh(self):
        init_data = {
            "mssql": {"type": "mssql", "operations": []},
            "crm": {"type": "oracle", "operations": []},
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.init_apply.load_init_data", return_value=init_data), \
             patch("app.container_manager.wake_up") as mock_wake, \
             patch("app.container_manager.deactivate") as mock_deactivate:
            seed_all_sources_on_first_startup()
            self.assertEqual(mock_wake.call_count, 2)
            mock_wake.assert_any_call("mssql")
            mock_wake.assert_any_call("crm")
            self.assertEqual(mock_deactivate.call_count, 2)
            mock_deactivate.assert_any_call("mssql")
            mock_deactivate.assert_any_call("crm")
            self.assertTrue(is_init_applied())
            self.assertIsNotNone(get_init_timestamp())

    def test_seed_all_sources_on_first_startup_already_applied(self):
        init_data = {
            "mssql": {"type": "mssql", "operations": []},
        }
        with patch("app.init_apply.STATE_DIR", self.state), \
             patch("app.init_apply.load_init_data", return_value=init_data), \
             patch("app.container_manager.wake_up") as mock_wake, \
             patch("app.container_manager.deactivate") as mock_deactivate:
            mark_init_applied()
            seed_all_sources_on_first_startup()
            mock_wake.assert_not_called()
            mock_deactivate.assert_not_called()

    def test_apply_via_api_when_flag_is_false(self):
        init_data = {
            "mssql": {
                "type": "mssql",
                "operations": [{"op": "create_schema", "name": "demo"}]
            }
        }
        with patch("app.init_apply.STATE_DIR", self.state),              patch("app.init_apply.load_init_data", return_value=init_data),              patch("app.sources.source_names", return_value=[]),              patch("app.main.create_source") as mock_create,              patch("app.exec_engine.execute_source_operations") as mock_exec:
            apply_init_for_source("mssql")
            mock_create.assert_called_once()
            mock_exec.assert_called_once()
            self.assertTrue(is_init_applied())

    def test_self_healing_orphaned_resources(self):
        mock_client = MagicMock()
        mock_container = MagicMock(name="sydafa-mssql-mssql-source", labels={})
        mock_container.name = "sydafa-mssql-mssql-source"
        mock_client.containers.list.return_value = [mock_container]

        mock_volume = MagicMock()
        mock_volume.name = "sydafa-mssql-mssql-data"
        mock_volume.attrs = {"Labels": {}}
        mock_client.volumes.list.return_value = [mock_volume]

        with patch("app.init_apply.STATE_DIR", self.state),              patch("app.container_manager._get_docker_client", return_value=mock_client):
            check_and_heal_orphaned_state()
            mock_container.remove.assert_called_once_with(force=True, v=True)
            mock_volume.remove.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
