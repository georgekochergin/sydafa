"""Имена контейнеров и volume с префиксом."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.container_manager import container_name_for, volume_name_for
from app.models import SourceConfig


class TestContainerNames(unittest.TestCase):
    def test_default_names(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", ""):
            self.assertEqual(container_name_for(cfg), "sydafa-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-data-mssql-billing")

    def test_prefixed_names(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "inst1-"):
            self.assertEqual(container_name_for(cfg), "sydafa-inst1-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-inst1-data-mssql-billing")

    def test_prefixed_names_without_trailing_dash(self):
        cfg = SourceConfig(name="billing", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "inst1"):
            self.assertEqual(container_name_for(cfg), "sydafa-inst1-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-inst1-data-mssql-billing")


if __name__ == "__main__":
    unittest.main()
