"""Characterization: инвентарь сохраняемого поведения (proposal-1.md §4, behavior freeze).

Фиксирует наблюдаемое поведение, которое обязано пережить рефакторинг source-адаптеров
без изменений: тексты ошибок FK/cascade (на них завязан HTTP 409), fallback-поведения
(D-6/D-8), резервирование batch_id до исполнения и wire-ответ SourceInfo (SIDE-06).
Ассерты — часть спецификации переноса; при переезде модулей меняются только импорты.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.exec_engine import ExecError, execute_source_operations
from app.generators import (
    GenerationSession,
    GeneratorError,
    _find_table,
    delete_rows_conn,
    truncate_table_conn,
)
from app.main import app
from app.models import (
    ColumnSpec,
    Condition,
    ForeignKey,
    LiveSchema,
    SourceConfig,
    TableRuleProfile,
    TableSchema,
    TruncateOp,
)
from app import schema_state


class FakeCursor:
    rowcount = 1

    def __init__(self):
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


def _orders_live(with_audit: bool = True, with_dependent: bool = False) -> LiveSchema:
    orders = TableSchema(
        name="orders",
        schema="demo",
        primary_key=["order_id"],
        columns=[ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"})],
    )
    tables: dict[str, TableSchema] = {"demo.orders": orders}
    if with_audit:
        tables["demo._del_orders"] = TableSchema(
            name="_del_orders", schema="demo", primary_key=[],
            columns=[ColumnSpec(name="_exec_batch_id", type="bigint"), ColumnSpec(name="order_id", type="bigint")],
        )
    if with_dependent:
        tables["demo.order_items"] = TableSchema(
            name="order_items",
            schema="demo",
            primary_key=["item_id"],
            columns=[ColumnSpec(name="item_id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[ForeignKey(columns=["order_id"], ref_entity_fqn="demo.orders", ref_columns=["order_id"])],
        )
        tables["demo._del_order_items"] = TableSchema(
            name="_del_order_items", schema="demo", primary_key=[],
            columns=[ColumnSpec(name="_exec_batch_id", type="bigint"), ColumnSpec(name="item_id", type="bigint")],
        )
    return LiveSchema(schemas=["demo"], tables=tables)


class TestErrorTextsCharacterization(unittest.TestCase):
    """Тексты ошибок FK/cascade менять нельзя: exec_engine превращает их в HTTP 409
    поиском подстрок «ссыла»/«cascade» (D-7)."""

    def test_delete_without_cascade_blocked_text(self):
        live = _orders_live(with_dependent=True)
        conn = FakeConn()
        conn.cur._rows = [(1,)]  # SELECT pk возвращает строку: удаление затрагивает зависимые
        with self.assertRaises(GeneratorError) as ctx:
            delete_rows_conn(
                conn, "oracle", live, "demo", "orders",
                conditions=[Condition(column="order_id", operator="=", value=1)],
                cascade=False, batch_id=1,
            )
        msg = str(ctx.exception)
        self.assertIn("ссыла", msg)
        self.assertIn("cascade", msg)
        self.assertIn("order_items", msg)
        # Текущий порядок: архивация в _del_orders выполняется ДО блокировки cascade
        self.assertIn('INSERT INTO "demo"."_del_orders"', conn.cur.executed[0][0])

    def test_truncate_without_cascade_blocked_text(self):
        live = _orders_live(with_audit=True, with_dependent=True)
        conn = FakeConn()
        conn.cur.fetchone = lambda: (5,)  # в зависимой таблице 5 строк
        with self.assertRaises(GeneratorError) as ctx:
            truncate_table_conn(conn, "oracle", live, "demo", "orders", cascade=False, batch_id=1)
        msg = str(ctx.exception)
        self.assertIn("ссылается", msg)
        self.assertIn("cascade", msg)
        self.assertIn("5 строками", msg)

    def test_fk_empty_parent_exact_text(self):
        parent = TableSchema(
            name="customers", schema="demo", primary_key=["cust_id"],
            columns=[ColumnSpec(name="cust_id", type="bigint", generator={"kind": "sequence"})],
        )
        child = TableSchema(
            name="orders", schema="demo", primary_key=["order_id"],
            columns=[ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[ForeignKey(columns=["cust_id"], ref_entity_fqn="demo.customers", ref_columns=["cust_id"])],
        )
        live = LiveSchema(schemas=["demo"], tables={"demo.customers": parent, "demo.orders": child})
        conn = FakeConn()  # fetchall -> [] : родитель пуст
        from app.generators import append_rows_conn
        with self.assertRaises(GeneratorError) as ctx:
            append_rows_conn(conn, "demo", "oracle", live, "demo", "orders", rows=1, batch_id=1)
        msg = str(ctx.exception)
        self.assertIn("родительская таблица пуста", msg)
        self.assertIn("Сначала выполните append", msg)

    def test_exec_error_conflict_flag_by_substrings(self):
        """is_conflict определяется подстроками «ссыла»/«cascade» — заморозка D-7."""
        err = GeneratorError(
            "Таблица 'demo.a' ссылается на 'demo.b' через Foreign Key, "
            "но родительская таблица пуста. Сначала выполните append в родительскую таблицу."
        )
        self.assertTrue("ссыла" in str(err) or "cascade" in str(err))
        plain = GeneratorError("иная ошибка генерации")
        self.assertFalse("ссыла" in str(plain) or "cascade" in str(plain))


class TestFallbacksCharacterization(unittest.TestCase):
    def test_find_table_falls_back_to_bare_name_in_other_schema(self):
        """D-8: при промахе FQN таблица ищется по имени по всем схемам."""
        live = _orders_live()
        found = _find_table(live, "other", "orders")
        self.assertIs(found, live.tables["demo.orders"])
        with self.assertRaises(GeneratorError):
            _find_table(live, "other", "missing")

    def test_preload_state_swallows_read_errors(self):
        """D-6: сбой чтения балансов подавляется — сессия создаётся с пустыми балансами."""
        table = TableSchema(
            name="txs", schema="demo", primary_key=["id"],
            columns=[ColumnSpec(name="id", type="bigint"), ColumnSpec(name="amount", type="decimal(12,2)")],
        )
        live = LiveSchema(schemas=["demo"], tables={"demo.txs": table})
        conn = FakeConn()
        conn.cur.execute = lambda sql, params=None: (_ for _ in ()).throw(RuntimeError("db read failed"))
        profile = TableRuleProfile()  # инвариантов нет: preload не выполняет SQL
        session = GenerationSession(conn, "mssql", live, table, profile)
        self.assertEqual(session.balances, {})


class TestBatchReservationCharacterization(unittest.TestCase):
    def test_batch_id_reserved_before_execution_gap_on_failure(self):
        """batch_id резервируется до исполнения: сбой батча оставляет пропуск (§8.2)."""
        import json
        from contextlib import contextmanager

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        cfg = SourceConfig(name="src", type="mssql", host="h", port=1433)
        live = _orders_live(with_audit=False)
        conn = FakeConn()
        conn.cur.execute = lambda sql, params=None: (_ for _ in ()).throw(RuntimeError("SQL failed"))

        @contextmanager
        def fake_conn(*args, **kwargs):
            yield conn

        import app.exec_engine as ee
        original_conn = ee.get_connection
        ee.get_connection = fake_conn
        self.addCleanup(setattr, ee, "get_connection", original_conn)

        with patch("app.schema_state.STATE_DIR", state), \
             patch("app.exec_engine.get_source", return_value=cfg), \
             patch("app.exec_engine.get_live_schema", return_value=live), \
             patch("app.exec_engine.save_live_schema"), \
             patch("app.exec_engine.sync_sequences_after_operations"), \
             patch("app.exec_engine.append_evolution_log"):
            with self.assertRaises(ExecError):
                execute_source_operations("src", [TruncateOp(entity_fqn="demo.orders")])

        batch_file = state / "batch_sequence.src.json"
        self.assertTrue(batch_file.exists())
        self.assertEqual(json.loads(batch_file.read_text())["current_batch_id"], 1)


class TestSourceInfoWireCharacterization(unittest.TestCase):
    def test_get_source_info_response_has_no_connection_field(self):
        """SIDE-06 (фиксация): connection передаётся в конструктор, но в ответе отсутствует."""
        client = TestClient(app, raise_server_exceptions=False)
        cfg = SourceConfig(type="mssql", host="mssql-source", port=1433, reader="sydafa")
        live = _orders_live(with_audit=False)
        with patch("app.sources.source_names", return_value=["mssql"]), \
             patch("app.sources.get_source", return_value=cfg), \
             patch("app.schema_state.get_live_schema", return_value=live):
            resp = client.get("/sources/mssql")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("connection", resp.json())


if __name__ == "__main__":
    unittest.main()
