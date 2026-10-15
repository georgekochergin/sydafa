"""Unit-тесты моделей и движка exec_engine в sydafa."""
import unittest
from pydantic import TypeAdapter

from app.models import (
    AppendOp,
    BATCH_ID_COLUMN,
    ColumnSpec,
    Condition,
    CreateSchemaOp,
    CreateTableOp,
    DeleteOp,
    EXEC_BATCH_ID_COLUMN,
    ExecOperation,
    ExecRequest,
    LiveSchema,
    TruncateOp,
    UpdateOp,
)
from app.generators import delete_rows_conn, truncate_table_conn, update_rows_conn
from app.schema_engine import _build_ddl_and_update_state, _del_table_name, _upd_table_name


class FakeCursor:
    """Захватывает SQL и параметры, имитирует успешное выполнение запроса."""
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


def _build_live_with_audit() -> LiveSchema:
    """Собирает live-схему demo с таблицей customers и пустыми _upd_/_del_ через schema_engine."""
    live = LiveSchema(schemas=[], tables={})
    _build_ddl_and_update_state(CreateSchemaOp(name="demo"), "oracle", live, 0)
    _build_ddl_and_update_state(
        CreateTableOp(
            schema="demo",
            name="customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string", generator={"kind": "faker", "provider": "company"}),
            ],
        ),
        "oracle",
        live,
        1,
    )
    return live


class TestExecModels(unittest.TestCase):
    def test_parse_exec_operations(self):
        adapter = TypeAdapter(list[ExecOperation])
        raw_ops = [
            {"op": "create_schema", "name": "demo"},
            {
                "op": "create_table",
                "schema": "demo",
                "name": "customers",
                "primary_key": ["id"],
                "columns": [{"name": "id", "type": "bigint"}],
            },
            {"op": "truncate", "schema": "demo", "table": "customers"},
            {"op": "append", "schema": "demo", "table": "customers", "rows": 5},
            {
                "op": "update",
                "schema": "demo",
                "table": "customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
                "set": {"name": "test"},
            },
            {
                "op": "delete",
                "schema": "demo",
                "table": "customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
            },
        ]
        ops = adapter.validate_python(raw_ops)
        self.assertEqual(len(ops), 6)
        self.assertIsInstance(ops[0], CreateSchemaOp)
        self.assertIsInstance(ops[1], CreateTableOp)
        self.assertIsInstance(ops[2], TruncateOp)
        self.assertIsInstance(ops[3], AppendOp)
        self.assertIsInstance(ops[4], UpdateOp)
        self.assertIsInstance(ops[5], DeleteOp)

        req = ExecRequest(operations=ops)
        self.assertEqual(len(req.operations), 6)


class TestBatchIdAudit(unittest.TestCase):
    def test_update_archives_snapshot_and_sets_batch_id(self):
        live = _build_live_with_audit()
        conn = FakeConn()
        n = update_rows_conn(
            conn, "oracle", live, "demo", "customers",
            conditions=[Condition(column="customer_id", operator="=", value=1)],
            set_values={"name": "NewName"},
            batch_id=7,
        )
        self.assertEqual(n, 1)
        # executed[0] — архивация снимка в _upd_customers ДО обновления
        archive_sql, archive_params = conn.cur.executed[0]
        self.assertIn(f'INSERT INTO demo."{_upd_table_name("customers")}"', archive_sql)
        self.assertIn(f'"{EXEC_BATCH_ID_COLUMN}"', archive_sql)
        self.assertEqual(archive_params[0], 7)  # _exec_batch_id = batch_id
        # executed[1] — UPDATE основной таблицы с _batch_id = batch_id
        update_sql, update_params = conn.cur.executed[1]
        self.assertIn("UPDATE demo.customers", update_sql)
        self.assertIn(f'"{BATCH_ID_COLUMN}" = :2', update_sql)
        self.assertEqual(update_params[1], 7)  # _batch_id = batch_id
        # Равенство main._batch_id == _upd._exec_batch_id
        self.assertEqual(archive_params[0], update_params[1])

    def test_update_ignores_system_columns_in_set(self):
        live = _build_live_with_audit()
        conn = FakeConn()
        update_rows_conn(
            conn, "oracle", live, "demo", "customers",
            conditions=[Condition(column="customer_id", operator="=", value=1)],
            set_values={"name": "NewName", BATCH_ID_COLUMN: 999, EXEC_BATCH_ID_COLUMN: 999},
            batch_id=7,
        )
        update_sql, update_params = conn.cur.executed[1]
        self.assertNotIn("999", str(update_params))
        self.assertIn(f'"{BATCH_ID_COLUMN}" = :2', update_sql)

    def test_delete_archives_to_del(self):
        live = _build_live_with_audit()
        conn = FakeConn()
        n = delete_rows_conn(
            conn, "oracle", live, "demo", "customers",
            conditions=[Condition(column="customer_id", operator="=", value=1)],
            cascade=False,
            batch_id=9,
        )
        self.assertEqual(n, 1)
        archive_sql, archive_params = conn.cur.executed[0]
        self.assertIn(f'INSERT INTO demo."{_del_table_name("customers")}"', archive_sql)
        self.assertEqual(archive_params[0], 9)  # _exec_batch_id = batch_id
        # executed[1] — DELETE из основной таблицы
        self.assertIn("DELETE FROM demo.customers", conn.cur.executed[1][0])

    def test_truncate_archives_all_to_del(self):
        live = _build_live_with_audit()
        conn = FakeConn()
        truncate_table_conn(conn, "oracle", live, "demo", "customers", batch_id=5)
        archive_sql, archive_params = conn.cur.executed[0]
        self.assertIn(f'INSERT INTO demo."{_del_table_name("customers")}"', archive_sql)
        self.assertEqual(archive_params[0], 5)  # _exec_batch_id = batch_id
        self.assertNotIn(" WHERE ", archive_sql)  # архивируются все строки
        self.assertIn("TRUNCATE TABLE demo.customers", conn.cur.executed[1][0])


if __name__ == "__main__":
    unittest.main()
