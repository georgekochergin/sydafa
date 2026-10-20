"""Тестирование устойчивости проверки готовности СУБД (_wait_for_db_ready) и fallback-логики."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.container_manager import _wait_for_db_ready
from app.models import SourceConfig


class TestDbReadyFallback(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mssql_fallback_to_sa_when_dba_user_not_provisioned(self):
        mock_pymssql = MagicMock()
        mock_conn = MagicMock()
        mock_pymssql.connect.side_effect = [Exception("(18456, Login failed for user)"), mock_conn]

        cfg = SourceConfig(
            name="bank",
            type="mssql",
            host="sydafa-src-mssql-bank",
            port=1433,
        )

        with patch("app.secrets.STATE_DIR", self.state_path), \
             patch("app.constants.STATE_DIR", self.state_path), \
             patch("app.init_apply.STATE_DIR", self.state_path), \
             patch.dict("sys.modules", {"pymssql": mock_pymssql}):
            _wait_for_db_ready(cfg, timeout=5)

        self.assertEqual(mock_pymssql.connect.call_count, 2)
        # Первый вызов был под dba_user
        first_call_user = mock_pymssql.connect.call_args_list[0].kwargs.get("user")
        self.assertTrue(first_call_user.startswith("SYDAFA_ADMIN_"))
        # Второй вызов fallback был под sa
        second_call_user = mock_pymssql.connect.call_args_list[1].kwargs.get("user")
        self.assertEqual(second_call_user, "sa")
        self.assertEqual(mock_pymssql.connect.call_args_list[1].kwargs.get("database"), "master")
        mock_conn.close.assert_called_once()

    def test_oracle_fallback_to_sys_when_dba_user_not_provisioned(self):
        mock_oracledb = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("READ WRITE",)
        mock_conn.cursor.return_value = mock_cur
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        mock_oracledb.makedsn.return_value = "dummy_dsn"

        # Первый вызов (dba_user к sydafa PDB) падает
        # Второй вызов (SYS к FREE CDB) успешен
        mock_oracledb.connect.side_effect = [Exception("ORA-01017: invalid username/password"), mock_conn]

        cfg = SourceConfig(
            name="broker",
            type="oracle",
            host="sydafa-src-oracle-broker",
            port=1521,
        )

        with patch("app.secrets.STATE_DIR", self.state_path), \
             patch("app.constants.STATE_DIR", self.state_path), \
             patch("app.init_apply.STATE_DIR", self.state_path), \
             patch.dict("sys.modules", {"oracledb": mock_oracledb}):
            _wait_for_db_ready(cfg, timeout=5)

        self.assertEqual(mock_oracledb.connect.call_count, 2)
        # Второй вызов был под SYS в режиме SYSDBA
        self.assertEqual(mock_oracledb.connect.call_args_list[1].kwargs.get("user"), "SYS")
        mock_cur.execute.assert_called_with("SELECT open_mode FROM v$database")
        mock_conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
