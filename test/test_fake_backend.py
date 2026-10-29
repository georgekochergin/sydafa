"""Доказательство границы расширения: fake RDBMS-движок через общий execution.

Fake-движок со своим стилем квотирования (обратные кавычки), биндов ($N) и
SQL-маркерами проходит репрезентативный сценарий schema + DML целиком через
общие алгоритмы (`rdbms/ddl`, `rdbms/execution`, `rdbms/generation`,
`rdbms/mutations`) — без единой правки общего движка. Fake регистрируется
только внутри теста и удаляется после (production-реестр не меняется).

Каждый выполняемый SQL валидируется FakeCursor на принадлежность fake-диалекту
(запрещены маркеры MSSQL/Oracle), а точные ожидания SQL/params делают тест
чувствительным к ошибкам биндов/квотирования/типов (review R3).
"""
from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.backends import registry
from app.backends.rdbms import mutations as common_mutations
from app.backends.rdbms.execution import execute_source_operations
from app.backends.rdbms.oracle.dialect import OracleDialect
from app.backends.rdbms.contracts import TransactionSemantics
from app.backends.registry import EngineFamilyError
from app.main import app
from app.models import (
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    ForeignKey,
    LiveSchema,
    SourceConfig,
)


class FakeDialect:
    """Диалект с максимально непохожим на MSSQL/Oracle выводом."""

    engine_type = "fakedb"
    transaction_semantics = TransactionSemantics(
        transactional_dml=True,
        transactional_ddl=True,
        implicit_commit_ddl=False,
        transactional_hard_clear=True,
    )

    def quote_identifier(self, ident): return f"`{ident}`"

    def qualify_table(self, schema, table): return f"`{schema}`.`{table}`"

    def map_type(self, logical_type): return f"FAKE::{logical_type}"

    def placeholder(self, index): return f"${index}"

    def create_schema(self, name): return [f"FAKE CREATE SCHEMA `{name}`"]

    def drop_schema(self, name): return [f"FAKE DROP SCHEMA `{name}`"]

    def rename_schema(self, from_name, to_name, table_names):
        return [f"FAKE RENAME SCHEMA `{from_name}` TO `{to_name}`"]

    def create_table(self, table):
        cols = ", ".join(f"`{c.name}` {self.map_type(c.type)}" for c in table.columns)
        return f"FAKE CREATE TABLE {self.qualify_table(table.schema_, table.name)} ({cols})"

    def drop_table(self, schema, table):
        return f"FAKE DROP TABLE {self.qualify_table(schema, table)}"

    def clear_table(self, schema, table, *, hard: bool):
        mode = "HARD" if hard else "BY ROWS"
        return f"FAKE CLEAR TABLE {self.qualify_table(schema, table)} {mode}"

    def rename_table(self, schema, from_name, to_name):
        return f"FAKE RENAME TABLE {self.qualify_table(schema, from_name)} TO `{to_name}`"

    def transfer_table(self, from_schema, to_schema, table):
        return [f"FAKE TRANSFER {self.qualify_table(from_schema, table)} TO `{to_schema}`"]

    def add_column(self, schema, table, column):
        return f"FAKE ADD COLUMN `{column.name}` TO {self.qualify_table(schema, table)}"

    def drop_column(self, schema, table, name):
        return f"FAKE DROP COLUMN `{name}` FROM {self.qualify_table(schema, table)}"

    def rename_column(self, schema, table, from_name, to_name):
        return f"FAKE RENAME COLUMN `{from_name}` TO `{to_name}`"


_ORACLE_BIND = re.compile(r":\d+")


def assert_all_fake_sql(statements):
    """Все выражения построены fake-диалектом: свои квотирование/бинды/маркеры.

    Чувствительность (review R3): сломанный диалект (mssql-бинды %s, oracle
    :N, чужое квотирование) падает здесь или раньше — в FakeCursor.execute.
    """
    for sql, _params in statements:
        assert "`" in sql, f"нет fake-квотирования: {sql!r}"
        assert "%s" not in sql, f"мssql-бинды в fake-сценарии: {sql!r}"
        assert not _ORACLE_BIND.search(sql), f"oracle-бинды в fake-сценарии: {sql!r}"
        assert "[" not in sql and '"' not in sql, f"чужое квотирование: {sql!r}"


class FakeCursor:
    rowcount = 1

    def __init__(self, rows):
        self.executed = []
        self._rows = rows

    def execute(self, sql, params=None):
        # Валидация на границе исполнения: неправильный SQL от общего движка
        # (чужие бинды/квотирование) обязан уронить сценарий немедленно
        assert_all_fake_sql([(sql, params)])
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return (self._rows[0][0],) if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeConnections:
    opened = 0
    last_conn: FakeConn | None = None

    @contextmanager
    def open_working(self, cfg, secrets, endpoint_host, autocommit=False):
        FakeConnections.opened += 1
        conn = FakeConn(rows=[(100,)])  # родительский PK всегда существует
        FakeConnections.last_conn = conn
        yield conn


class FakeMutations:
    """Fake использует ОБЩИЕ unlimited-ветки и truncate — как любой новый движок.

    Валидация исполняемого SQL происходит в FakeCursor; counts возвращаются
    из фактического rowcount (не заглушка «успеха»).
    """

    @staticmethod
    def update_rows(conn, dialect, live, schema, table_name, conditions, set_values, batch_id=1, limit=None):
        return common_mutations.update_rows_unlimited(
            conn, dialect, live, schema, table_name, conditions, set_values, batch_id
        )

    @staticmethod
    def delete_rows(conn, dialect, live, schema, table_name, conditions, cascade, batch_id=1, limit=None):
        return common_mutations.delete_rows_unlimited(
            conn, dialect, live, schema, table_name, conditions, cascade, batch_id
        )

    @staticmethod
    def truncate_table(conn, dialect, live, schema, table_name, cascade=False, batch_id=1):
        return common_mutations.truncate_table(conn, dialect, live, schema, table_name, cascade, batch_id)


class FakeRdbmsBackend:
    engine_type = "fakedb"
    family = "rdbms"
    internal_port = 9999

    def __init__(self):
        self.dialect = FakeDialect()
        self.connections = FakeConnections()
        self.mutations = FakeMutations

    def describe_connection(self, host, port):
        raise NotImplementedError


class FakeStreamBackend:
    engine_type = "fakestream"
    family = "stream"
    internal_port = 9092


def _register(backend):
    registry._register(backend)
    return lambda: registry._BACKENDS.pop(backend.engine_type, None)


class TestFakeRdbmsBackendThroughCommonEngine(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_register(FakeRdbmsBackend()))
        FakeConnections.opened = 0
        FakeConnections.last_conn = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.cfg = SourceConfig.model_construct(
            name="fake_src", type="fakedb", host="sydafa-src-fakedb-fake_src",
            port=9999, external_port=None, reader="sydafa",
        )

    def _run_scenario(self):
        from pydantic import TypeAdapter
        from app.models import MigrateOperation

        ops = [
            CreateSchemaOp(name="demo"),
            CreateTableOp(
                entity_fqn="demo.parents",
                primary_key=["parent_id"],
                columns=[ColumnSpec(name="parent_id", type="bigint", generator={"kind": "sequence"})],
            ),
            CreateTableOp(
                entity_fqn="demo.children",
                primary_key=["child_id"],
                columns=[
                    ColumnSpec(name="child_id", type="bigint", generator={"kind": "sequence"}),
                    ColumnSpec(name="parent_id"),
                    ColumnSpec(name="note", type="string", generator={"kind": "choice", "values": ["NOTE"]}),
                ],
                foreign_keys=[ForeignKey(columns=["parent_id"], ref_entity_fqn="demo.parents", ref_columns=["parent_id"])],
            ),
            {"op": "append", "entity_fqn": "demo.children", "rows": 2},
            {"op": "update", "entity_fqn": "demo.children",
             "predicate": [{"column": "note", "operator": "=", "value": "old"}],
             "set": {"note": "new"}},
            {"op": "delete", "entity_fqn": "demo.children",
             "predicate": [{"column": "note", "operator": "=", "value": "new"}]},
            {"op": "truncate", "entity_fqn": "demo.parents", "cascade": True},
        ]
        parsed = TypeAdapter(list[MigrateOperation]).validate_python(ops)

        with patch("app.backends.rdbms.execution.get_source", return_value=self.cfg), \
             patch("app.backends.rdbms.execution.get_live_schema", return_value=LiveSchema(schemas=[], tables={})), \
             patch("app.backends.rdbms.execution.save_live_schema"), \
             patch("app.backends.rdbms.execution.sync_sequences_after_operations"), \
             patch("app.backends.rdbms.execution.append_evolution_log"):
            result = execute_source_operations("fake_src", parsed)
        return result

    def test_scenario_sql_and_results(self):
        result = self._run_scenario()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.executed, 7)
        self.assertEqual(FakeConnections.opened, 1, "один сценарий — одна рабочая session")

        sqls = [sql for sql, _ in FakeConnections.last_conn.cur.executed]
        by_sql = dict(FakeConnections.last_conn.cur.executed)

        # --- DDL построен fake-диалектом с fake-маппингом типов ---
        self.assertIn("FAKE CREATE SCHEMA `demo`", sqls)
        self.assertIn(
            "FAKE CREATE TABLE `demo`.`parents` (`parent_id` FAKE::bigint, `_batch_id` FAKE::bigint)",
            sqls,
        )
        self.assertIn(
            "FAKE CREATE TABLE `demo`.`children` "
            "(`child_id` FAKE::bigint, `parent_id` FAKE::bigint, `note` FAKE::string, `_batch_id` FAKE::bigint)",
            sqls,
        )
        # аудит-таблицы созданы общим слоем через fake-диалект
        self.assertIn(
            "FAKE CREATE TABLE `demo`.`_upd_children` "
            "(`_exec_batch_id` FAKE::bigint, `child_id` FAKE::bigint, `parent_id` FAKE::bigint, "
            "`note` FAKE::string, `_batch_id` FAKE::bigint)",
            sqls,
        )

        # --- Append: FK из существующего родителя, свои бинды ---
        fk_select = "SELECT `parent_id` FROM `demo`.`parents`"
        self.assertIn(fk_select, sqls)
        insert = (
            "INSERT INTO `demo`.`children` (`parent_id`, `note`, `child_id`, `_batch_id`) "
            "VALUES ($1, $2, $3, $4)"
        )
        self.assertIn(insert, sqls)
        insert_params = by_sql[insert]
        self.assertEqual(insert_params[0], 100)      # FK из родителя
        self.assertEqual(insert_params[1], "NOTE")        # choice-генератор (детерминирован)
        self.assertIn(insert_params[2], (1, 2))      # монотонный sequence
        self.assertEqual(insert_params[3], 1)        # _batch_id батча

        # --- Update: архивация ДО мутации со смещением биндов $1/$2 ---
        archive_upd = (
            "INSERT INTO `demo`.`_upd_children` (`_exec_batch_id`, `child_id`, `parent_id`, `note`, `_batch_id`) "
            "SELECT $1, `child_id`, `parent_id`, `note`, `_batch_id` FROM `demo`.`children` WHERE `note` = $2"
        )
        self.assertIn(archive_upd, sqls)
        self.assertEqual(by_sql[archive_upd], [1, "old"])
        update = "UPDATE `demo`.`children` SET `note` = $1, `_batch_id` = $2 WHERE `note` = $3"
        self.assertIn(update, sqls)
        self.assertEqual(by_sql[update], ["new", 1, "old"])
        # counts из rowcount курсора, а не заглушка
        update_record = next(r for r in result.applied if r.get("op") == "update")
        self.assertEqual(update_record["updated"], 1)

        # --- Delete: архив + удаление по предикату ---
        archive_del = archive_upd.replace("`_upd_children`", "`_del_children`")
        self.assertIn(archive_del, sqls)
        self.assertEqual(by_sql[archive_del], [1, "new"])
        self.assertIn("DELETE FROM `demo`.`children` WHERE `note` = $1", sqls)
        self.assertEqual(by_sql["DELETE FROM `demo`.`children` WHERE `note` = $1"], ["new"])

        # --- Truncate cascade: физическую очистку выражает fake-диалект,
        # включая дочернюю таблицу рекурсивного шага (review R6) ---
        self.assertIn("FAKE CLEAR TABLE `demo`.`children` HARD", sqls)
        self.assertIn("FAKE CLEAR TABLE `demo`.`parents` BY ROWS", sqls)
        self.assertLess(
            sqls.index("FAKE CLEAR TABLE `demo`.`children` HARD"),
            sqls.index("FAKE CLEAR TABLE `demo`.`parents` BY ROWS"),
        )

        # Весь поток — fake-диалект (никаких mssql/oracle следов)
        assert_all_fake_sql(FakeConnections.last_conn.cur.executed)

    def test_sql_assertions_are_sensitive(self):
        """Sensitivity (review R3): ассерты падают на сломанном/чужом диалекте."""
        broken_samples = [
            ("UPDATE cte SET [note] = %s WHERE [note] = %s", None),      # mssql-стиль
            ('UPDATE "demo"."children" SET "note" = :1 WHERE "note" = :2', None),  # oracle-стиль
            ("TRUNCATE TABLE demo.parents", None),                        # нет fake-квотирования
        ]
        for sample in broken_samples:
            with self.subTest(sql=sample[0]):
                with self.assertRaises(AssertionError):
                    assert_all_fake_sql([sample])

    def test_fake_dialect_declares_transaction_semantics(self):
        """Контракт transaction-semantics обязателен для диалекта движка."""
        self.assertIsInstance(FakeDialect.transaction_semantics, TransactionSemantics)
        self.assertTrue(FakeDialect.transaction_semantics.transactional_dml)
        # production-диалекты тоже декларируют семантику
        self.assertTrue(OracleDialect.transaction_semantics.implicit_commit_ddl)


class TestNonRdbmsRejection(unittest.TestCase):
    def test_stream_family_rejected_before_connection(self):
        self.addCleanup(_register(FakeStreamBackend()))
        cfg = SourceConfig.model_construct(
            name="stream_src", type="fakestream", host="sydafa-src-fakestream-stream_src",
            port=9092, external_port=None, reader="sydafa",
        )

        with patch("app.backends.rdbms.execution.get_source", return_value=cfg), \
             patch("app.backends.rdbms.execution._open_working") as mock_open, \
             patch("app.backends.rdbms.execution.get_next_batch_id") as mock_batch:
            with self.assertRaises(EngineFamilyError):
                execute_source_operations("stream_src", [CreateSchemaOp(name="demo")])

        mock_open.assert_not_called()
        mock_batch.assert_not_called()

    def test_migrate_http_rejects_stream_family_before_wake(self):
        """HTTP-граница (review R2): не-RDBMS отклоняется ДО wake контейнера."""
        self.addCleanup(_register(FakeStreamBackend()))
        cfg = SourceConfig.model_construct(
            name="stream_src", type="fakestream", host="sydafa-src-fakestream-stream_src",
            port=9092, external_port=None, reader="sydafa",
        )
        client = TestClient(app, raise_server_exceptions=False)
        with patch("app.sources.source_names", return_value=["stream_src"]), \
             patch("app.sources.get_source", return_value=cfg), \
             patch("app.main._wake") as mock_wake:
            resp = client.post(
                "/sources/stream_src/migrate",
                json={"operations": [{"op": "create_schema", "name": "demo"}]},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("rdbms", resp.json()["detail"])
        mock_wake.assert_not_called()


class TestSelectedBackendContext(unittest.TestCase):
    """Regression review-2: выбранный на границе backend — единственный источник
    dialect/connections/mutations; вложенные registry lookup запрещены."""

    def setUp(self):
        self.addCleanup(_register(FakeRdbmsBackend()))
        FakeConnections.opened = 0
        FakeConnections.last_conn = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SourceConfig.model_construct(
            name="fake_src", type="fakedb", host="sydafa-src-fakedb-fake_src",
            port=9999, external_port=None, reader="sydafa",
        )

    def _forbid_registry_lookups(self):
        from app.backends import registry as registry_module

        def _boom(*args, **kwargs):
            raise AssertionError("вложенный registry lookup при переданном backend")

        return patch.object(registry_module, "require_rdbms", _boom), \
               patch.object(registry_module, "require", _boom)

    def test_execution_uses_only_selected_backend(self):
        """Сценарий schema+DML с backend=selected: ни одного обращения к реестру."""
        from pydantic import TypeAdapter
        from app.models import MigrateOperation

        selected = registry.require_rdbms("fakedb")  # первичный выбор на границе сценария
        ops = TypeAdapter(list[MigrateOperation]).validate_python([
            CreateSchemaOp(name="demo"),
            CreateTableOp(
                entity_fqn="demo.parents",
                primary_key=["parent_id"],
                columns=[ColumnSpec(name="parent_id", type="bigint", generator={"kind": "sequence"})],
            ),
            {"op": "append", "entity_fqn": "demo.parents", "rows": 1},
        ])
        patch_require, patch_require_rdbms = self._forbid_registry_lookups()
        with patch_require, patch_require_rdbms, \
             patch("app.backends.rdbms.execution.get_source", return_value=self.cfg), \
             patch("app.backends.rdbms.execution.get_live_schema", return_value=LiveSchema(schemas=[], tables={})), \
             patch("app.backends.rdbms.execution.save_live_schema"), \
             patch("app.backends.rdbms.execution.sync_sequences_after_operations"), \
             patch("app.backends.rdbms.execution.append_evolution_log"):
            result = execute_source_operations("fake_src", ops, backend=selected)

        self.assertEqual(result.status, "ok")
        self.assertEqual(FakeConnections.opened, 1, "session открыта connections выбранного backend")
        assert_all_fake_sql(FakeConnections.last_conn.cur.executed)

    def test_invariant_validator_uses_passed_backend_and_conn(self):
        """Валидатор с conn+backend: dialect выбранного объекта, ноль обращений к реестру."""
        from app.models import CumulativeBalanceInvariant, TableRuleProfile, TableSchema

        selected = registry.require_rdbms("fakedb")
        children = TableSchema(
            name="children", schema="demo", primary_key=["child_id"],
            columns=[ColumnSpec(name="child_id", type="bigint")],
        )
        live = LiveSchema(schemas=["demo"], tables={"demo.children": children})
        conn = FakeConn(rows=[])  # HAVING не возвращает нарушений
        profile = TableRuleProfile(
            invariants=[CumulativeBalanceInvariant(amount_column="amount", min_balance=0.0)]
        )

        from app.backends.rdbms.generation import validate_table_invariants

        patch_require, patch_require_rdbms = self._forbid_registry_lookups()
        with patch_require, patch_require_rdbms, \
             patch("app.schema_state.get_live_schema", return_value=live):
            validate_table_invariants("fake_src", "demo.children", profile, conn=conn, backend=selected)

        sqls = [sql for sql, _ in conn.cur.executed]
        self.assertIn("SELECT SUM(`amount`) FROM `demo`.`children` HAVING SUM(`amount`) < 0.0", sqls)
        assert_all_fake_sql(conn.cur.executed)


if __name__ == "__main__":
    unittest.main()
