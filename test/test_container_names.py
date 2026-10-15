"""Имена контейнеров и volume с префиксом."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.container_manager import container_name_for, sanitize_user_token, volume_name_for
from app.models import SourceConfig


class TestContainerNames(unittest.TestCase):
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

    def test_source_name_with_spaces_or_dashes(self):
        cfg = SourceConfig(name="  -billing-  ", type="mssql", host="sydafa-src-mssql-billing", port=1433)
        with patch("app.container_manager.CONTAINER_PREFIX", "inst1-"):
            self.assertEqual(container_name_for(cfg), "sydafa-inst1-src-mssql-billing")
            self.assertEqual(volume_name_for(cfg), "sydafa-inst1-data-mssql-billing")


if __name__ == "__main__":
    unittest.main()
