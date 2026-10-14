"""Unit-тесты генерации и чтения секретов."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.secrets import get_secrets, reset_cache


class TestSecrets(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        reset_cache()
        self.tmp.cleanup()

    def test_generate_and_persist(self):
        with patch("app.secrets.STATE_DIR", self.state):
            first = get_secrets()
            self.assertTrue(first.sydafa_password.startswith("Sy"))
            self.assertTrue(first.mssql_sa_password.startswith("Sa1!"))
            self.assertTrue(first.oracle_sys_password.startswith("Or"))
            path = self.state / "secrets.json"
            self.assertTrue(path.is_file())
            reset_cache()
            second = get_secrets()
            self.assertEqual(first, second)

    def test_read_existing(self):
        payload = {
            "sydafa_password": "Syfixed",
            "mssql_sa_password": "Sa1!fixed",
            "oracle_sys_password": "Orfixed",
        }
        (self.state / "secrets.json").write_text(json.dumps(payload), encoding="utf-8")
        with patch("app.secrets.STATE_DIR", self.state):
            got = get_secrets()
            self.assertEqual(got.sydafa_password, "Syfixed")
            self.assertEqual(got.mssql_sa_password, "Sa1!fixed")
            self.assertEqual(got.oracle_sys_password, "Orfixed")


if __name__ == "__main__":
    unittest.main()
