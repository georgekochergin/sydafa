"""Тесты выдачи привилегий публичному читателю Oracle."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import provisioning


class TestOracleReaderGrants(unittest.TestCase):
    def _run_ensure(self, mock_oracledb):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = ("SYDAFA",)
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        executed = []

        def record_execute(sql):
            executed.append(sql)

        cur.execute.side_effect = record_execute
        with patch.dict("sys.modules", {"oracledb": mock_oracledb}), \
             patch.object(provisioning, "oracle_connection", return_value=ctx) as conn_factory:
            provisioning._ensure_oracle_reader("oracle")

        return conn_factory, cur, executed

    def test_reader_gets_dictionary_grant_via_sysdba_session(self):
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        conn_factory, cur, executed = self._run_ensure(mock_oracledb)

        conn_factory.assert_called_once_with(
            source_name="oracle", service_name="sydafa", mode=mock_oracledb.AUTH_MODE_SYSDBA
        )
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
