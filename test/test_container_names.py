"""Имена контейнеров и volume с префиксом."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.container_manager import container_name_for, volume_name_for
from app.models import SourceConfig


class TestContainerNames(unittest.TestCase):
    def test_default_names(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-mssql-billing-source", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", ""):
            self.assertEqual(container_name_for(cfg), "sydafa-mssql-billing-source")
            self.assertEqual(volume_name_for(cfg), "sydafa-mssql-billing-data")

    def test_prefixed_names(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-mssql-billing-source", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "sydafa-test-"):
            self.assertEqual(container_name_for(cfg), "sydafa-test-mssql-billing-source")
            self.assertEqual(volume_name_for(cfg), "sydafa-test-mssql-billing-data")


if __name__ == "__main__":
    unittest.main()
