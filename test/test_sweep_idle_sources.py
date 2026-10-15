"""Тесты фонового процесса sweep_idle_sources в container_manager."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.container_manager import sweep_idle_sources
from app.models import SourceConfig


class TestSweepIdleSources(unittest.TestCase):
    def setUp(self):
        self.cfg = SourceConfig(
            name="mssql",
            type="mssql",
            host="sydafa-src-mssql-mssql",
            port=1433,
            external_port=14330,
        )

    @patch("app.container_manager.deactivate")
    @patch("app.source_status.is_expired", return_value=False)
    @patch("app.container_manager._get_container")
    @patch("app.source_status.load_status")
    @patch("app.sources.get_source")
    @patch("app.sources.source_names", return_value=["mssql"])
    @patch("app.container_manager._get_docker_client")
    def test_sweep_stops_unauthorized_running_container_without_ttl(
        self,
        mock_docker_client,
        mock_source_names,
        mock_get_source,
        mock_load_status,
        mock_get_container,
        mock_is_expired,
        mock_deactivate,
    ):
        mock_get_source.return_value = self.cfg
        # Статус в /state указывает, что контейнер idle и нет active TTL
        mock_load_status.return_value = {"state": "idle", "activated_until": None}

        # Физический контейнер в Docker запущен кем-то вручную
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_get_container.return_value = mock_container

        sweep_idle_sources()

        # Контейнер должен быть немедленно остановлен
        mock_deactivate.assert_called_once_with("mssql")

    @patch("app.container_manager.deactivate")
    @patch("app.source_status.is_expired", return_value=True)
    @patch("app.container_manager._get_container")
    @patch("app.source_status.load_status")
    @patch("app.sources.get_source")
    @patch("app.sources.source_names", return_value=["mssql"])
    @patch("app.container_manager._get_docker_client")
    def test_sweep_stops_expired_container(
        self,
        mock_docker_client,
        mock_source_names,
        mock_get_source,
        mock_load_status,
        mock_get_container,
        mock_is_expired,
        mock_deactivate,
    ):
        mock_get_source.return_value = self.cfg
        mock_load_status.return_value = {"state": "running", "activated_until": "2026-01-01T00:00:00Z"}

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_get_container.return_value = mock_container

        sweep_idle_sources()

        mock_deactivate.assert_called_once_with("mssql")

    @patch("app.container_manager.deactivate")
    @patch("app.source_status.is_expired", return_value=False)
    @patch("app.container_manager._get_container")
    @patch("app.source_status.load_status")
    @patch("app.sources.get_source")
    @patch("app.sources.source_names", return_value=["mssql"])
    @patch("app.container_manager._get_docker_client")
    def test_sweep_leaves_legitimately_running_container(
        self,
        mock_docker_client,
        mock_source_names,
        mock_get_source,
        mock_load_status,
        mock_get_container,
        mock_is_expired,
        mock_deactivate,
    ):
        mock_get_source.return_value = self.cfg
        mock_load_status.return_value = {"state": "running", "activated_until": "2026-12-31T23:59:59Z"}

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_get_container.return_value = mock_container

        sweep_idle_sources()

        mock_deactivate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
