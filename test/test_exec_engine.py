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
            entity_fqn="demo.customers",
            primary_key=["customer_id"],
            columns=[
                ColumnSpec(name="customer_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="name", type="string"),
            ],
        ),
        "mssql",
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
                "entity_fqn": "demo.customers",
                "primary_key": ["id"],
                "columns": [{"name": "id", "type": "bigint"}],
            },
            {"op": "truncate", "entity_fqn": "demo.customers"},
            {"op": "append", "entity_fqn": "demo.customers", "rows": 5},
            {
                "op": "update",
                "entity_fqn": "demo.customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
                "set": {"name": "test"},
                "limit": 5,
            },
            {
                "op": "delete",
                "entity_fqn": "demo.customers",
                "predicate": [{"column": "id", "operator": "=", "value": 1}],
                "limit": 5,
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
        self.assertEqual(ops[1].entity_fqn, "demo.customers")
        self.assertEqual(ops[1].schema_, "demo")
        self.assertEqual(ops[1].table, "customers")
        self.assertEqual(ops[4].limit, 5)
        self.assertEqual(ops[5].limit, 5)

        req = ExecRequest(operations=ops)
        self.assertEqual(len(req.operations), 6)

    def test_parse_limit_in_ops(self):
        adapter = TypeAdapter(list[ExecOperation])
        raw_ops = [
            {
                "op": "delete",
                "entity_fqn": "demo.customers",
                "predicate": [{"column": "amount", "operator": "<=", "value": 0}],
                "limit": 10,
            },
            {
                "op": "update",
                "entity_fqn": "demo.customers",
                "predicate": [{"column": "status", "operator": "=", "value": "INACTIVE"}],
                "set": {"status": "ARCHIVED"},
                "limit": 5,
            },
        ]
        ops = adapter.validate_python(raw_ops)
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0].limit, 10)
        self.assertEqual(len(ops[0].predicate), 1)
        self.assertEqual(ops[0].predicate[0].column, "amount")
        self.assertEqual(ops[1].limit, 5)
        self.assertEqual(ops[1].set, {"status": "ARCHIVED"})


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
        self.assertIn(f'INSERT INTO "demo"."{_upd_table_name("customers")}"', archive_sql)
        self.assertIn(f'"{EXEC_BATCH_ID_COLUMN}"', archive_sql)
        self.assertEqual(archive_params[0], 7)  # _exec_batch_id = batch_id
        # executed[1] — UPDATE основной таблицы с _batch_id = batch_id
        update_sql, update_params = conn.cur.executed[1]
        self.assertIn('UPDATE "demo"."customers"', update_sql)
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
        self.assertIn(f'INSERT INTO "demo"."{_del_table_name("customers")}"', archive_sql)
        self.assertEqual(archive_params[0], 9)  # _exec_batch_id = batch_id
        # executed[1] — DELETE из основной таблицы
        self.assertIn('DELETE FROM "demo"."customers"', conn.cur.executed[1][0])

    def test_truncate_archives_all_to_del(self):
        live = _build_live_with_audit()
        conn = FakeConn()
        truncate_table_conn(conn, "oracle", live, "demo", "customers", batch_id=5)
        archive_sql, archive_params = conn.cur.executed[0]
        self.assertIn(f'INSERT INTO "demo"."{_del_table_name("customers")}"', archive_sql)
        self.assertEqual(archive_params[0], 5)  # _exec_batch_id = batch_id
        self.assertNotIn(" WHERE ", archive_sql)  # архивируются все строки
        self.assertIn('TRUNCATE TABLE "demo"."customers"', conn.cur.executed[1][0])

    def test_interleaved_ddl_and_dml_flow(self):
        """Проверка возможности чередования DDL и DML в едином эволюционном сценарии."""
        live = LiveSchema(schemas=["demo"], tables={})
        # 1. create_table
        create_op = CreateTableOp(
            entity_fqn="demo.contracts",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="amount", type="decimal(10,2)", generator={"kind": "random_decimal", "min": 100, "max": 200}),
            ],
            foreign_keys=[],
        )
        stmts, _ = _build_ddl_and_update_state(create_op, "mssql", live, 0)
        self.assertTrue(len(stmts) > 0)
        self.assertIn("demo.contracts", live.tables)

        # 2. add_column (DDL после создания)
        from app.models import AddColumnOp
        add_col_op = AddColumnOp(
            entity_fqn="demo.contracts",
            column=ColumnSpec(name="status", type="string", generator={"kind": "choice", "values": ["NEW"]}),
        )
        stmts2, _ = _build_ddl_and_update_state(add_col_op, "mssql", live, 1)
        self.assertTrue(len(stmts2) > 0)
        self.assertIn("status", live.tables["demo.contracts"].column_names)

        # 3. update_rows (DML для добавленной колонки)
        conn = FakeConn()
        updated = update_rows_conn(
            conn,
            "mssql",
            live,
            "demo",
            "contracts",
            conditions=[Condition(column="id", operator="=", value=1)],
            set_values={"status": "ACTIVE"},
            batch_id=2,
        )
        self.assertEqual(updated, 1)
        update_sql, _ = conn.cur.executed[1]
        self.assertIn("[status] = %s", update_sql)

    def test_mssql_random_rows_without_pk(self):
        """Проверка PK-независимого UPDATE и DELETE в MSSQL (Updatable CTE + OUTPUT INTO)."""
        live = LiveSchema(schemas=["demo"], tables={})
        create_op = CreateTableOp(
            entity_fqn="demo.logs",
            primary_key=[],  # Таблица БЕЗ первичного ключа
            columns=[
                ColumnSpec(name="message", type="string", generator={"kind": "faker", "provider": "sentence"}),
                ColumnSpec(name="level", type="string", generator={"kind": "choice", "values": ["INFO", "ERROR"]}),
            ],
        )
        _build_ddl_and_update_state(create_op, "mssql", live, 0)
        self.assertIn("demo.logs", live.tables)
        self.assertEqual(live.tables["demo.logs"].primary_key, [])

        # 1. Update N random rows without PK
        conn = FakeConn()
        conn.cur.rowcount = 10
        updated = update_rows_conn(
            conn,
            "mssql",
            live,
            "demo",
            "logs",
            conditions=[],  # Без предиката: обновляем 10 случайных строк таблицы
            set_values={"level": "WARN"},
            batch_id=11,
            limit=10,
        )
        self.assertEqual(updated, 10)
        self.assertEqual(len(conn.cur.executed), 1)
        upd_sql, upd_params = conn.cur.executed[0]
        self.assertIn("WITH cte AS (SELECT TOP (10) * FROM [demo].[logs] WHERE 1=1 ORDER BY NEWID())", upd_sql)
        self.assertIn("UPDATE cte SET [level] = %s", upd_sql)
        self.assertIn("OUTPUT DELETED.* INTO #upd_logs", upd_sql)
        self.assertIn("INSERT INTO [demo].[_upd_logs]", upd_sql)
        self.assertIn(11, upd_params)

        # 2. Delete N random rows without PK
        conn_del = FakeConn()
        conn_del.cur.rowcount = 5
        deleted = delete_rows_conn(
            conn_del,
            "mssql",
            live,
            "demo",
            "logs",
            conditions=[],
            cascade=False,
            batch_id=12,
            limit=5,
        )
        self.assertEqual(deleted, 5)
        self.assertEqual(len(conn_del.cur.executed), 1)
        del_sql, del_params = conn_del.cur.executed[0]
        self.assertIn("WITH cte AS (SELECT TOP (5) * FROM [demo].[logs] WHERE 1=1 ORDER BY NEWID())", del_sql)
        self.assertIn("DELETE FROM cte", del_sql)
        self.assertIn("OUTPUT DELETED.* INTO #del_logs", del_sql)
        self.assertIn("INSERT INTO [demo].[_del_logs]", del_sql)
        self.assertIn(12, del_params)

    def test_oracle_random_rows_without_pk(self):
        """Проверка PK-независимого UPDATE и DELETE в Oracle через системный ROWID."""
        live = LiveSchema(schemas=["demo"], tables={})
        create_op = CreateTableOp(
            entity_fqn="demo.logs",
            primary_key=[],  # Таблица БЕЗ первичного ключа
            columns=[
                ColumnSpec(name="message", type="string", generator={"kind": "faker", "provider": "sentence"}),
                ColumnSpec(name="level", type="string", generator={"kind": "choice", "values": ["INFO", "ERROR"]}),
            ],
        )
        _build_ddl_and_update_state(create_op, "oracle", live, 0)
        self.assertEqual(live.tables["demo.logs"].primary_key, [])

        # 1. Update N random rows without PK
        conn = FakeConn()
        conn.cur.rowcount = 2
        conn.cur._rows = [("AAARowid1",), ("AAARowid2",)]
        updated = update_rows_conn(
            conn,
            "oracle",
            live,
            "demo",
            "logs",
            conditions=[Condition(column="level", operator="=", value="ERROR")],
            set_values={"level": "RESOLVED"},
            batch_id=21,
            limit=2,
        )
        self.assertEqual(updated, 2)
        self.assertEqual(len(conn.cur.executed), 3)

        # executed[0]: выборка ROWID со случайной сортировкой
        rowid_sql, rowid_params = conn.cur.executed[0]
        self.assertIn('SELECT ROWID FROM "demo"."logs"', rowid_sql)
        self.assertIn("ORDER BY dbms_random.value FETCH FIRST :2 ROWS ONLY", rowid_sql)
        self.assertEqual(rowid_params, ["ERROR", 2])

        # executed[1]: архивация снимка в _upd_logs по выбранным ROWID
        arch_sql, arch_params = conn.cur.executed[1]
        self.assertIn('INSERT INTO "demo"."_upd_logs"', arch_sql)
        self.assertIn("WHERE ROWID IN (:2, :3)", arch_sql)
        self.assertEqual(arch_params, [21, "AAARowid1", "AAARowid2"])

        # executed[2]: UPDATE по выбранным ROWID
        upd_sql, upd_params = conn.cur.executed[2]
        self.assertIn('UPDATE "demo"."logs" SET "level" = :1, "_batch_id" = :2 WHERE ROWID IN (:3, :4)', upd_sql)
        self.assertEqual(upd_params, ["RESOLVED", 21, "AAARowid1", "AAARowid2"])

        # 2. Delete N random rows without PK
        conn_del = FakeConn()
        conn_del.cur.rowcount = 2
        conn_del.cur._rows = [("AAARowid1",), ("AAARowid2",)]
        deleted = delete_rows_conn(
            conn_del,
            "oracle",
            live,
            "demo",
            "logs",
            conditions=[],  # Без предиката
            cascade=False,
            batch_id=22,
            limit=2,
        )
        self.assertEqual(deleted, 2)
        self.assertEqual(len(conn_del.cur.executed), 3)

        # executed[0]: выборка ROWID
        del_rowid_sql, del_rowid_params = conn_del.cur.executed[0]
        self.assertIn("WHERE 1=1 ORDER BY dbms_random.value FETCH FIRST :1 ROWS ONLY", del_rowid_sql)
        self.assertEqual(del_rowid_params, [2])

        # executed[1]: архивация снимка в _del_logs
        del_arch_sql, del_arch_params = conn_del.cur.executed[1]
        self.assertIn('INSERT INTO "demo"."_del_logs"', del_arch_sql)
        self.assertIn("WHERE ROWID IN (:2, :3)", del_arch_sql)
        self.assertEqual(del_arch_params, [22, "AAARowid1", "AAARowid2"])

        # executed[2]: DELETE по выбранным ROWID
        del_sql, del_params = conn_del.cur.executed[2]
        self.assertIn('DELETE FROM "demo"."logs" WHERE ROWID IN (:1, :2)', del_sql)
        self.assertEqual(del_params, ["AAARowid1", "AAARowid2"])

    def test_oracle_random_rows_empty_table(self):
        """Проверка Oracle, когда строк 0 — операция завершается без вызова архива и мутации."""
        live = LiveSchema(schemas=["demo"], tables={})
        create_op = CreateTableOp(
            entity_fqn="demo.empty_tbl",
            primary_key=[],
            columns=[ColumnSpec(name="v", type="bigint")],
        )
        _build_ddl_and_update_state(create_op, "oracle", live, 0)
        conn = FakeConn()
        conn.cur._rows = []  # Ничего не найдено

        updated = update_rows_conn(conn, "oracle", live, "demo", "empty_tbl", conditions=[], set_values={"v": 1}, limit=5)
        self.assertEqual(updated, 0)
        self.assertEqual(len(conn.cur.executed), 1)  # Только запрос ROWID

        conn_del = FakeConn()
        conn_del.cur._rows = []
        deleted = delete_rows_conn(conn_del, "oracle", live, "demo", "empty_tbl", conditions=[], cascade=False, limit=5)
        self.assertEqual(deleted, 0)
        self.assertEqual(len(conn_del.cur.executed), 1)  # Только запрос ROWID


if __name__ == "__main__":
    unittest.main()
