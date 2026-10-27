"""Имена контейнеров и volume с префиксом."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.container_manager import container_name_for, sanitize_user_token, volume_name_for
from app.models import SourceConfig


class TestContainerNames(unittest.TestCase):
    def test_reset_compose_volume_preserves_mount_and_clears_all_entries(self):
        """Reinit clears mounted data including hidden files without following symlinks."""
        from pathlib import Path
        import tempfile
        from app.container_manager import reset_data_volume

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested/data").write_text("data")
            (root / ".hidden").write_text("data")
            (root / "link").symlink_to("/nonexistent")
            volume = MagicMock()
            volume.name = "source-data"
            client = MagicMock()
            client.containers.get.return_value.attrs = {"Mounts": [{
                "Type": "volume", "Name": volume.name, "Destination": "/source-volumes/bank",
            }]}
            with patch("app.container_manager.Path", return_value=root):
                reset_data_volume(client, volume)
            self.assertEqual(list(root.iterdir()), [])
            volume.remove.assert_not_called()
            client.containers.get.return_value.attrs = {"Mounts": []}
            reset_data_volume(client, volume)
            volume.remove.assert_called_once_with(force=True)
            from docker.errors import NotFound

            volume.reset_mock()
            client.containers.get.side_effect = NotFound("parent not found")
            reset_data_volume(client, volume)
            volume.remove.assert_called_once_with(force=True)

    def test_compose_labels_only_when_parent_belongs_to_compose(self):
        """Compose discovers managed orphans; standalone runs stay independent."""
        from app.container_manager import _run_container

        config = SourceConfig(name="bank", type="mssql", host="sydafa-src-mssql-bank", port=1433)
        for project in ("cleanup-test", ""):
            with self.subTest(project=project), patch("app.container_manager.CONTAINER_PREFIX", ""), \
                    patch("app.container_manager._get_context", return_value=("bridge", project)), \
                    patch("app.container_manager.get_secrets"):
                client = MagicMock()
                _run_container(client, "bank", config)
                labels = client.containers.run.call_args.kwargs["labels"]
                self.assertEqual(labels["sydafa.managed"], "true")
                self.assertEqual(labels["sydafa.source"], "bank")
                if project:
                    self.assertEqual(labels["com.docker.compose.project"], project)
                    self.assertTrue(labels["com.docker.compose.config-hash"])
                    self.assertEqual(labels["com.docker.compose.oneoff"], "False")
                else:
                    self.assertFalse(any(key.startswith("com.docker.compose.") for key in labels))

    def test_context_does_not_invent_compose_ownership(self):
        """Missing introspection or standalone parent never claims a Compose project."""
        from app.container_manager import _get_context

        client = MagicMock()
        client.containers.get.return_value.labels = {}
        self.assertEqual(_get_context(client)[1], "")
        client.containers.get.side_effect = RuntimeError("unavailable")
        self.assertEqual(_get_context(client), ("bridge", ""))

    def test_sanitize_user_token(self):
        self.assertEqual(sanitize_user_token("inst1-"), "inst1")
        self.assertEqual(sanitize_user_token("inst1"), "inst1")
        self.assertEqual(sanitize_user_token("  --inst1--  "), "inst1")
        self.assertEqual(sanitize_user_token("inst_1"), "inst_1")
        self.assertEqual(sanitize_user_token("my.prefix/"), "my.prefix")
        self.assertEqual(sanitize_user_token(""), "")
        self.assertEqual(sanitize_user_token(None), "")

    def test_default_names(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", ""):
            self.assertEqual(container_name_for(cfg), "sydafa-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-data-mssql-billing")

    def test_prefixed_names_various_formats(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        for prefix in ["inst1-", "inst1", "  inst1  ", "--inst1--"]:
            with self.subTest(prefix=prefix):
                with patch("app.container_manager.CONTAINER_PREFIX", prefix):
                    self.assertEqual(container_name_for(cfg), "sydafa-inst1-src-mssql-billing")
                    self.assertEqual(volume_name_for(cfg), "sydafa-inst1-data-mssql-billing")

    def test_prefix_starting_with_sydafa_does_not_duplicate(self):
        cfg = SourceConfig(name="db1", type="oracle", host="sydafa-src-oracle-db1", port=1521)
        with patch("app.container_manager.CONTAINER_PREFIX", "sydafa-test-"):
            self.assertEqual(container_name_for(cfg), "sydafa-test-src-oracle-db1")
            self.assertEqual(volume_name_for(cfg), "sydafa-test-data-oracle-db1")

    def test_source_name_with_spaces_or_dashes(self):
        cfg = SourceConfig(name="  -billing-  ", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "inst1-"):
            self.assertEqual(container_name_for(cfg), "sydafa-inst1-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-inst1-data-mssql-billing")

    def test_build_connection_details_respects_container_prefix(self):
        from app.container_manager import build_connection_details

        cfg = SourceConfig(name="bank", type="mssql", host="sydafa-src-mssql-bank", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "test-"):
            conn = build_connection_details("bank", cfg)
            self.assertEqual(conn.host, "sydafa-test-src-mssql-bank")
            self.assertIn("sydafa-test-src-mssql-bank", conn.jdbc_url)


if __name__ == "__main__":
    unittest.main()
