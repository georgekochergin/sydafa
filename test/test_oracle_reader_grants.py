"""Тесты выдачи привилегий публичному читателю Oracle (runtime backend).

Behavior freeze (review R1): выдача выполняется сессией внутреннего DBA
в режиме SYSDBA — как до рефакторинга; SYS-учётка в этом пути не участвует,
поэтому доступности DBA достаточно и при недоступном SYS.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.backends.rdbms.oracle import runtime as oracle_runtime
from app.models import SourceConfig
from app.secrets import AppSecrets

DBA_USER = "SYDAFA_ADMIN_TEST"


def _secrets() -> AppSecrets:
    return AppSecrets(
        dba_user=DBA_USER, dba_password="DbaPass",
        mssql_sa_password="pw", oracle_sys_password="SysPass",
    )


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
        with patch.dict("sys.modules", {"oracledb": mock_oracledb}), \
             patch.object(oracle_runtime, "_connect", return_value=conn) as conn_factory:
            oracle_runtime.OracleRuntime._ensure_reader(
                "sydafa-src-oracle-oracle", 1521, _secrets(), "sydafa"
            )
        return conn_factory, cur, executed

    def test_reader_grants_via_dba_sysdba_session(self):
        """Гранты выдаёт внутренний DBA (dba_user/dba_password) в режиме SYSDBA."""
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        conn_factory, cur, executed = self._run_ensure(mock_oracledb)

        conn_factory.assert_called_once()
        self.assertEqual(conn_factory.call_args.args[1], DBA_USER)
        self.assertEqual(conn_factory.call_args.args[2], "DbaPass")
        self.assertEqual(conn_factory.call_args.kwargs.get("mode"), mock_oracledb.AUTH_MODE_SYSDBA)
        self.assertIn("GRANT CREATE SESSION TO sydafa", executed)
        self.assertIn("GRANT SELECT ANY TABLE TO sydafa", executed)
        self.assertIn("GRANT SELECT ANY DICTIONARY TO sydafa", executed)

    def test_sys_account_is_not_used_for_reader_grants(self):
        """Сценарий «DBA доступен, SYS недоступен»: путь не зависит от SYS-учётки."""
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        conn_factory, cur, executed = self._run_ensure(mock_oracledb)

        users = [call.args[1] for call in conn_factory.call_args_list]
        self.assertTrue(users, "должен быть хотя бы один вызов подключения")
        self.assertTrue(all(u == DBA_USER for u in users), users)
        self.assertFalse(any(u == "SYS" for u in users))

    def test_existing_reader_is_not_recreated(self):
        mock_oracledb = MagicMock()
        mock_oracledb.AUTH_MODE_SYSDBA = 2
        _, cur, executed = self._run_ensure(mock_oracledb)

        self.assertFalse(any(sql.startswith("CREATE USER") for sql in executed))


if __name__ == "__main__":
    unittest.main()
