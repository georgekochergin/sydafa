"""Тесты выдачи привилегий публичному читателю Oracle (runtime backend)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.backends.rdbms.oracle import runtime as oracle_runtime
from app.models import SourceConfig
from app.secrets import AppSecrets


class TestOracleReaderGrants(unittest.TestCase):
    def _run_ensure(self, mock_oracledb):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = ("SYDAFA",)
        conn.cursor.return_value = cur
        executed = []

        def record_execute(sql):
            executed.append(sql)

        cur.execute.side_effect = record_execute
        config = SourceConfig(name="oracle", type="oracle", host="sydafa-src-oracle-oracle", port=1521)
        secrets = AppSecrets(
            dba_user="SYDAFA_ADMIN_TEST", dba_password="pw",
            mssql_sa_password="pw", oracle_sys_password="pw",
        )
        with patch.dict("sys.modules", {"oracledb": mock_oracledb}), \
             patch.object(oracle_runtime, "_connect", return_value=conn) as conn_factory:
            oracle_runtime.OracleRuntime._ensure_reader("sydafa-src-oracle-oracle", 1521, secrets, "sydafa")

        return conn_factory, cur, executed

    def test_reader_gets_dictionary_grant_via_sysdba_session(self):
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        conn_factory, cur, executed = self._run_ensure(mock_oracledb)

        conn_factory.assert_called_once()
        self.assertEqual(conn_factory.call_args.args[1], "SYS")
        self.assertEqual(conn_factory.call_args.kwargs.get("mode"), mock_oracledb.AUTH_MODE_SYSDBA)
        self.assertIn("GRANT CREATE SESSION TO sydafa", executed)
        self.assertIn("GRANT SELECT ANY TABLE TO sydafa", executed)
        self.assertIn("GRANT SELECT ANY DICTIONARY TO sydafa", executed)

    def test_existing_reader_is_not_recreated(self):
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        _, cur, executed = self._run_ensure(mock_oracledb)

        self.assertFalse(any(sql.startswith("CREATE USER") for sql in executed))


if __name__ == "__main__":
    unittest.main()
