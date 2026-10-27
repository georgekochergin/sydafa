"""Unit-тесты для подсистемы генерации данных: random_date, date_offset, weights, locale, null_ratio и DAG."""
import calendar
from datetime import date, datetime
import unittest

from pydantic import ValidationError

from app.generators import (
    GenerationSession,
    GeneratorError,
    _generate_value,
    _mint_new_row,
    _resolve_anchor_date,
    _shift_date,
)
from app.models import (
    ChoiceGenerator,
    ColumnSpec,
    CumulativeBalanceInvariant,
    DateOffsetGenerator,
    FakerGenerator,
    ForeignKey,
    LiveSchema,
    PatternGenerator,
    RandomDateGenerator,
    RandomDecimalGenerator,
    SequenceGenerator,
    TableRuleProfile,
    TableSchema,
    UuidGenerator,
)


class FakeCursor:
    def __init__(self):
        self.executed = []
        self._max_id = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        self._max_id += 1
        return (self._max_id,)

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur


class TestGenerators(unittest.TestCase):
    def test_random_date_default_is_current_year(self):
        """random_date без параметров всегда возвращает дату строго в рамках текущего года."""
        current_year = date.today().year
        for _ in range(50):
            val = _generate_value("dt", gen=RandomDateGenerator(), col_type="date")
            self.assertIsInstance(val, date)
            self.assertEqual(val.year, current_year)
            self.assertGreaterEqual(val, date(current_year, 1, 1))
            self.assertLessEqual(val, date(current_year, 12, 31))

    def test_random_date_with_anchors_and_dim(self):
        """random_date с anchor и размерностями y, m, d."""
        today = date.today()

        # Anchor today + dim d [-5, 5]
        gen_d = RandomDateGenerator(anchor="today", dim="d", min_offset=-5, max_offset=5)
        val_d = _generate_value("dt_d", gen=gen_d, col_type="date")
        self.assertIsInstance(val_d, date)
        self.assertLessEqual(abs((val_d - today).days), 5)

        # Anchor start_of_month
        gen_m = RandomDateGenerator(anchor="start_of_month", dim="d", min_offset=0, max_offset=0)
        val_m = _generate_value("dt_m", gen=gen_m, col_type="date")
        self.assertEqual(val_m, date(today.year, today.month, 1))

        # Anchor start_of_quarter
        gen_q = RandomDateGenerator(anchor="start_of_quarter", dim="d", min_offset=0, max_offset=0)
        val_q = _generate_value("dt_q", gen=gen_q, col_type="date")
        expected_q_month = ((today.month - 1) // 3) * 3 + 1
        self.assertEqual(val_q, date(today.year, expected_q_month, 1))

    def test_shift_date_edge_cases(self):
        """Проверка безопасного смещения дат: високосные года и концы месяцев."""
        # 29 февраля 2024 (високосный) + 1 год -> 28 февраля 2025 (невисокосный)
        base_leap = date(2024, 2, 29)
        shifted_y = _shift_date(base_leap, "y", 1)
        self.assertEqual(shifted_y, date(2025, 2, 28))

        # 29 февраля 2024 + 4 года -> 29 февраля 2028 (високосный)
        shifted_4y = _shift_date(base_leap, "y", 4)
        self.assertEqual(shifted_4y, date(2028, 2, 29))

        # 31 января 2026 + 1 месяц -> 28 февраля 2026
        base_jan31 = date(2026, 1, 31)
        shifted_m = _shift_date(base_jan31, "m", 1)
        self.assertEqual(shifted_m, date(2026, 2, 28))

        # 31 августа 2026 - 1 месяц -> 31 июля 2026
        base_aug31 = date(2026, 8, 31)
        shifted_prev_m = _shift_date(base_aug31, "m", -1)
        self.assertEqual(shifted_prev_m, date(2026, 7, 31))

        # Неизвестная размерность
        with self.assertRaises(GeneratorError):
            _shift_date(date(2026, 1, 1), "unknown", 1)

    def test_date_offset_generator(self):
        """date_offset вычисляет дату со сдвигом от ref_column."""
        gen = DateOffsetGenerator(ref_column="start_date", dim="y", min_offset=1, max_offset=3)
        row_context = {"start_date": date(2026, 5, 10)}
        for _ in range(20):
            val = _generate_value("end_date", row_values=row_context, gen=gen, col_type="date")
            self.assertIsInstance(val, date)
            self.assertEqual(val.month, 5)
            self.assertEqual(val.day, 10)
            self.assertIn(val.year, [2027, 2028, 2029])
            self.assertGreater(val, row_context["start_date"])

        # Базовая дата None -> возвращает None
        self.assertIsNone(_generate_value("end_date", row_values={"start_date": None}, gen=gen, col_type="date"))

    def test_null_ratio(self):
        """null_ratio=1.0 всегда дает None, null_ratio=0.0 никогда не дает None."""
        gen_null = FakerGenerator(provider="word", null_ratio=1.0)
        self.assertIsNone(_generate_value("txt", gen=gen_null, col_type="string"))

        gen_not_null = FakerGenerator(provider="word", null_ratio=0.0)
        self.assertIsNotNone(_generate_value("txt", gen=gen_not_null, col_type="string"))

    def test_faker_locale(self):
        """Faker с локалью ru_RU генерирует данные с кириллицей."""
        gen = FakerGenerator(provider="name", locale="ru_RU")
        val = _generate_value("name", gen=gen, col_type="string")
        self.assertIsInstance(val, str)
        self.assertTrue(len(val) > 0)

    def test_pattern_generator_format(self):
        """pattern подставляет символы по плейсхолдерам и сохраняет литералы шаблона."""
        gen = PatternGenerator(template="40817810############")
        for _ in range(25):
            val = _generate_value("account_number", gen=gen, col_type="string")
            self.assertRegex(val, r"^40817810\d{12}$")

    def test_pattern_generator_placeholder_classes(self):
        """'#' — цифры, '@' — заглавные, 'a' — строчные, '?' — буквоцифры."""
        gen = PatternGenerator(template="AAA-####-@@@-aaa-???-ZZZ")
        val = _generate_value("code", gen=gen, col_type="string")
        digits, upper_rand, lower_rand, alnum = val.split("-")[1:5]
        self.assertTrue(digits.isdigit() and len(digits) == 4)
        self.assertTrue(upper_rand.isalpha() and upper_rand.isupper() and len(upper_rand) == 3)
        self.assertTrue(lower_rand.isalpha() and lower_rand.islower() and len(lower_rand) == 3)
        self.assertTrue(alnum.isalnum() and len(alnum) == 3)
        self.assertTrue(val.startswith("AAA-") and val.endswith("-ZZZ"))

    def test_pattern_generator_requires_template(self):
        """Пустой template отклоняется валидацией модели."""
        with self.assertRaises(ValidationError):
            PatternGenerator(template="")

    def test_pattern_generator_null_ratio(self):
        """null_ratio=1.0 для pattern дает None."""
        gen = PatternGenerator(template="##", null_ratio=1.0)
        self.assertIsNone(_generate_value("code", gen=gen, col_type="string"))

    def test_choice_with_weights(self):
        """ChoiceGenerator валидирует веса и соблюдает распределение."""
        # Несовпадение длины weights и values
        with self.assertRaises(ValidationError):
            ChoiceGenerator(values=["A", "B"], weights=[1.0])

        # Отрицательные веса
        with self.assertRaises(ValidationError):
            ChoiceGenerator(values=["A", "B"], weights=[-1.0, 2.0])

        # Сумма весов 0
        with self.assertRaises(ValidationError):
            ChoiceGenerator(values=["A", "B"], weights=[0.0, 0.0])

        # Вес 100% на один элемент
        gen = ChoiceGenerator(values=["FIRST", "SECOND"], weights=[1.0, 0.0])
        for _ in range(20):
            self.assertEqual(_generate_value("status", gen=gen, col_type="string"), "FIRST")

    def test_dag_generation_with_inverted_column_order(self):
        """Колонка end_date объявлена ПЕРЕД start_date в схеме таблицы.

        Движок должен автоматически разрешить зависимость и корректно сгенерировать обе даты.
        """
        table = TableSchema(
            name="contracts",
            schema="dbo",
            primary_key=["contract_id"],
            columns=[
                ColumnSpec(name="contract_id", type="bigint"),
                ColumnSpec(name="contract_end_date", type="date"),
                ColumnSpec(name="contract_start_date", type="date"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={
                "contract_id": SequenceGenerator(),
                "contract_end_date": DateOffsetGenerator(ref_column="contract_start_date", dim="y", min_offset=1, max_offset=3),
                "contract_start_date": RandomDateGenerator(),
            }
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        _mint_new_row(conn, "test_source", "mssql", live, table, batch_id=1, session=session)

        self.assertEqual(len(conn.cur.executed), 1)
        insert_sql, params = conn.cur.executed[0]
        self.assertIn("INSERT INTO", insert_sql)

        cols_part = insert_sql.split("(")[1].split(")")[0]
        cols_inserted = [c.strip().strip("[]\"") for c in cols_part.split(",")]
        inserted_dict = dict(zip(cols_inserted, params))

        start_date = inserted_dict["contract_start_date"]
        end_date = inserted_dict["contract_end_date"]

        self.assertIsInstance(start_date, date)
        self.assertIsInstance(end_date, date)
        self.assertGreater(end_date, start_date)
        self.assertIn(end_date.year - start_date.year, [1, 2, 3])

    def test_dag_generation_cycle_detection(self):
        """При циклической зависимости колонок выбрасывается GeneratorError."""
        table = TableSchema(
            name="contracts",
            schema="dbo",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint"),
                ColumnSpec(name="col_a", type="date"),
                ColumnSpec(name="col_b", type="date"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={
                "id": SequenceGenerator(),
                "col_a": DateOffsetGenerator(ref_column="col_b", dim="y", min_offset=1, max_offset=2),
                "col_b": DateOffsetGenerator(ref_column="col_a", dim="y", min_offset=1, max_offset=2),
            }
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        with self.assertRaises(GeneratorError) as ctx:
            _mint_new_row(conn, "test_source", "mssql", live, table, batch_id=1, session=session)
        self.assertIn("циклическая", str(ctx.exception).lower())

    def test_dag_unknown_ref_column(self):
        """Если ref_column ссылается на несуществующую колонку, выбрасывается GeneratorError."""
        table = TableSchema(
            name="contracts",
            schema="dbo",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint"),
                ColumnSpec(name="end_date", type="date"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={
                "id": SequenceGenerator(),
                "end_date": DateOffsetGenerator(ref_column="non_existent", dim="y", min_offset=1, max_offset=2),
            }
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        with self.assertRaises(GeneratorError) as ctx:
            _mint_new_row(conn, "test_source", "mssql", live, table, batch_id=1, session=session)
        self.assertIn("неизвестную ref_column", str(ctx.exception))

    def test_cumulative_balance_invariant(self):
        """Инвариант cumulative_balance: баланс счета никогда не опускается ниже min_balance=0.0."""
        table = TableSchema(
            name="txs",
            schema="dbo",
            primary_key=["tx_id"],
            columns=[
                ColumnSpec(name="tx_id", type="bigint"),
                ColumnSpec(name="account_id", type="bigint"),
                ColumnSpec(name="amount", type="decimal(12,2)"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={
                "tx_id": SequenceGenerator(),
                "account_id": ChoiceGenerator(values=[1, 2]),
            },
            invariants=[
                CumulativeBalanceInvariant(
                    partition_by=["account_id"],
                    amount_column="amount",
                    min_balance=0.0,
                    deposit_prob=0.4,
                    deposit_range=(100.0, 500.0),
                    withdrawal_range=(10.0, 200.0),
                )
            ],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.txs": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        running_balances = {1: 0.0, 2: 0.0}
        for _ in range(50):
            _mint_new_row(conn, "test_src", "mssql", live, table, batch_id=1, session=session)
            last_ins, params = conn.cur.executed[-1]
            cols = [c.strip().strip("[]\"") for c in last_ins.split("(")[1].split(")")[0].split(",")]
            row_dict = dict(zip(cols, params))
            acc = row_dict["account_id"]
            amt = float(row_dict["amount"])

            running_balances[acc] = round(running_balances[acc] + amt, 2)
            self.assertGreaterEqual(
                running_balances[acc],
                0.0,
                f"Отрицательный остаток {running_balances[acc]} на счете {acc} после транзакции {amt}",
            )

    def test_cumulative_balance_global_no_partition(self):
        """Инвариант cumulative_balance без указания partition_by (глобальный баланс всей таблицы)."""
        table = TableSchema(
            name="vault",
            schema="dbo",
            primary_key=["tx_id"],
            columns=[
                ColumnSpec(name="tx_id", type="bigint"),
                ColumnSpec(name="amount", type="decimal(12,2)"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={"tx_id": SequenceGenerator()},
            invariants=[
                CumulativeBalanceInvariant(
                    amount_column="amount",
                    min_balance=0.0,
                    deposit_prob=0.5,
                    deposit_range=(50.0, 300.0),
                    withdrawal_range=(10.0, 150.0),
                )
            ],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.vault": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        global_balance = 0.0
        for _ in range(30):
            _mint_new_row(conn, "test_src", "mssql", live, table, batch_id=1, session=session)
            last_ins, params = conn.cur.executed[-1]
            cols = [c.strip().strip("[]\"") for c in last_ins.split("(")[1].split(")")[0].split(",")]
            row_dict = dict(zip(cols, params))
            amt = float(row_dict["amount"])

            global_balance = round(global_balance + amt, 2)
            self.assertGreaterEqual(
                global_balance,
                0.0,
                f"Отрицательный глобальный остаток {global_balance} после транзакции {amt}",
            )

    def test_append_rebalances_negative_balance(self):
        """Если после ручного update/delete счет ушел в минус, append форсирует пополнения."""
        table = TableSchema(
            name="txs",
            schema="dbo",
            primary_key=["tx_id"],
            columns=[
                ColumnSpec(name="tx_id", type="bigint"),
                ColumnSpec(name="account_id", type="bigint"),
                ColumnSpec(name="amount", type="decimal(12,2)"),
            ],
            foreign_keys=[],
        )
        profile = TableRuleProfile(
            columns={
                "tx_id": SequenceGenerator(),
                "account_id": ChoiceGenerator(values=[999]),
            },
            invariants=[
                CumulativeBalanceInvariant(
                    partition_by=["account_id"],
                    amount_column="amount",
                    min_balance=0.0,
                    deposit_prob=0.1,  # даже при 10% вероятности депозита, при отрицательном балансе депозит форсируется 100%!
                    deposit_range=(100.0, 500.0),
                    withdrawal_range=(50.0, 200.0),
                )
            ],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.txs": table})
        conn = FakeConn()
        session = GenerationSession(conn, "mssql", live, table, profile)

        # Имитируем, что в результате предыдущих update/delete баланс счета 999 ушел в глубокий минус (-1000.00)
        session.balances[0] = {(999,): -1000.0}

        # Все первые транзакции ОБЯЗАНЫ быть пополнениями (> 0), пока баланс не выйдет из дефицита!
        for _ in range(3):
            _mint_new_row(conn, "test_src", "mssql", live, table, batch_id=1, session=session)
            last_ins, params = conn.cur.executed[-1]
            cols = [c.strip().strip("[]\"") for c in last_ins.split("(")[1].split(")")[0].split(",")]
            row_dict = dict(zip(cols, params))
            amt = float(row_dict["amount"])

            self.assertGreater(amt, 0.0, "При отрицательном остатке списания должны быть заблокированы!")

    def test_monotonic_sequence_no_sql_read_and_no_resurrection(self):
        """Проверка строгой монотонности сиквенса без SQL SELECT MAX и отсутствия переиспользования ID."""
        from app.generators import append_rows_conn
        from app import schema_state

        source = "test_seq_src"
        schema_state.reset_memory_cache(source)

        table = TableSchema(
            name="orders",
            schema="dbo",
            primary_key=["order_id"],
            columns=[
                ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="title", type="string"),
            ],
            foreign_keys=[],
        )
        live = LiveSchema(schemas=["dbo"], tables={"dbo.orders": table})
        conn = FakeConn()

        # Первый батч: 3 строки
        append_rows_conn(conn, source, "mssql", live, "dbo", "orders", rows=3, batch_id=1)

        # Проверяем, что не было ни одного SELECT MAX
        for sql, _ in conn.cur.executed:
            self.assertNotIn("SELECT MAX", sql)

        # Проверяем значения order_id: 1, 2, 3
        executed_inserts = [e for e in conn.cur.executed if "INSERT INTO" in e[0]]
        self.assertEqual(len(executed_inserts), 3)

        ids_batch1 = []
        for insert_sql, params in executed_inserts:
            cols = [c.strip().strip("[]\"") for c in insert_sql.split("(")[1].split(")")[0].split(",")]
            row_dict = dict(zip(cols, params))
            ids_batch1.append(row_dict["order_id"])
        self.assertEqual(ids_batch1, [1, 2, 3])

        # Второй батч: 2 строки (имитируем, что строки 2 и 3 могли быть удалены в БД)
        conn.cur.executed.clear()
        append_rows_conn(conn, source, "mssql", live, "dbo", "orders", rows=2, batch_id=2)

        # Новые строки обязаны получить 4 и 5 (никакого возврата к 2 или 3)
        ids_batch2 = []
        for insert_sql, params in conn.cur.executed:
            if "INSERT INTO" in insert_sql:
                cols = [c.strip().strip("[]\"") for c in insert_sql.split("(")[1].split(")")[0].split(",")]
                row_dict = dict(zip(cols, params))
                ids_batch2.append(row_dict["order_id"])
        self.assertEqual(ids_batch2, [4, 5])

        schema_state.reset_memory_cache(source)

    def test_sequence_generator_on_non_pk_column(self):
        """Генератор kind=sequence корректно работает на не-PK колонках."""
        from app.generators import append_rows_conn
        from app import schema_state, rules_state

        source = "test_seq_non_pk"
        schema_state.reset_memory_cache(source)

        table = TableSchema(
            name="docs",
            schema="dbo",
            primary_key=["doc_id"],
            columns=[
                ColumnSpec(name="doc_id", type="bigint", generator={"kind": "sequence"}),
                ColumnSpec(name="reg_number", type="bigint"),
            ],
            foreign_keys=[],
        )
        rules_state.set_table_rules(
            source,
            "dbo.docs",
            TableRuleProfile(columns={"reg_number": SequenceGenerator()}),
        )
        live = LiveSchema(schemas=["dbo"], tables={"dbo.docs": table})
        conn = FakeConn()

        append_rows_conn(conn, source, "mssql", live, "dbo", "docs", rows=3, batch_id=1)

        reg_numbers = []
        doc_ids = []
        for insert_sql, params in conn.cur.executed:
            if "INSERT INTO" in insert_sql:
                cols = [c.strip().strip("[]\"") for c in insert_sql.split("(")[1].split(")")[0].split(",")]
                row_dict = dict(zip(cols, params))
                doc_ids.append(row_dict["doc_id"])
                reg_numbers.append(row_dict["reg_number"])

        self.assertEqual(doc_ids, [1, 2, 3])
        self.assertEqual(reg_numbers, [1, 2, 3])

        schema_state.reset_memory_cache(source)

    def test_uuid_generator(self):
        """Генератор uuid возвращает валидную строку UUIDv4."""
        import uuid as uuid_lib
        val = _generate_value("id_col", gen=UuidGenerator(), col_type="string")
        self.assertIsInstance(val, str)
        parsed = uuid_lib.UUID(val)
        self.assertEqual(parsed.version, 4)

        # Автоматическая генерация для колонки типа uuid без явного генератора
        val_auto = _generate_value("uuid_col", gen=None, col_type="uuid")
        self.assertIsInstance(val_auto, str)
        parsed_auto = uuid_lib.UUID(val_auto)
        self.assertEqual(parsed_auto.version, 4)

    def test_fk_empty_parent_raises_generator_error(self):
        """Попытка append в дочернюю таблицу при пустом родителе падает с GeneratorError."""
        from app.generators import append_rows_conn
        from app import schema_state

        source = "test_fk_empty"
        schema_state.reset_memory_cache(source)

        parent = TableSchema(
            name="orders",
            schema="dbo",
            primary_key=["order_id"],
            columns=[ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[],
        )
        child = TableSchema(
            name="order_items",
            schema="dbo",
            primary_key=["order_id", "item_seq"],
            columns=[
                ColumnSpec(name="order_id", type="bigint"),
                ColumnSpec(name="item_seq", type="bigint", generator={"kind": "sequence"}),
            ],
            foreign_keys=[
                ForeignKey(
                    columns=["order_id"],
                    ref_entity_fqn="dbo.orders",
                    ref_columns=["order_id"],
                )
            ],
        )
        live = LiveSchema(schemas=["dbo"], tables={"dbo.orders": parent, "dbo.order_items": child})
        conn = FakeConn()  # fetchall() возвращает [], т.е. родитель пуст

        with self.assertRaises(GeneratorError) as ctx:
            append_rows_conn(conn, source, "mssql", live, "dbo", "order_items", rows=1, batch_id=1)

        self.assertIn("пуста", str(ctx.exception))
        self.assertIn("orders", str(ctx.exception))
        schema_state.reset_memory_cache(source)

    def test_composite_pk_fk_plus_sequence(self):
        """Составной PK [order_id (FK), item_seq (sequence)] корректно наполняется."""
        from app.generators import append_rows_conn
        from app import schema_state

        source = "test_composite_pk"
        schema_state.reset_memory_cache(source)

        parent = TableSchema(
            name="orders",
            schema="dbo",
            primary_key=["order_id"],
            columns=[ColumnSpec(name="order_id", type="bigint", generator={"kind": "sequence"})],
            foreign_keys=[],
        )
        child = TableSchema(
            name="order_items",
            schema="dbo",
            primary_key=["order_id", "item_seq"],
            columns=[
                ColumnSpec(name="order_id", type="bigint"),
                ColumnSpec(name="item_seq", type="bigint", generator={"kind": "sequence"}),
            ],
            foreign_keys=[
                ForeignKey(
                    columns=["order_id"],
                    ref_entity_fqn="dbo.orders",
                    ref_columns=["order_id"],
                )
            ],
        )
        live = LiveSchema(schemas=["dbo"], tables={"dbo.orders": parent, "dbo.order_items": child})

        # Создаем соединение, где родитель возвращает строку с order_id = 100
        class PopulatedParentConn(FakeConn):
            def __init__(self):
                super().__init__()
            def cursor(self):
                cur = super().cursor()
                cur.fetchall = lambda: [(100,)]
                return cur

        conn = PopulatedParentConn()
        append_rows_conn(conn, source, "mssql", live, "dbo", "order_items", rows=2, batch_id=1)

        inserts = [e for e in conn.cur.executed if "INSERT INTO" in e[0]]
        self.assertEqual(len(inserts), 2)

        row1 = dict(zip([c.strip("[]\"") for c in inserts[0][0].split("(")[1].split(")")[0].split(", ")], inserts[0][1]))
        row2 = dict(zip([c.strip("[]\"") for c in inserts[1][0].split("(")[1].split(")")[0].split(", ")], inserts[1][1]))

        # order_id взят из родителя (100)
        self.assertEqual(row1["order_id"], 100)
        self.assertEqual(row2["order_id"], 100)
        # item_seq сгенерирован как монотонный сиквенс (1, 2)
        self.assertEqual(row1["item_seq"], 1)
        self.assertEqual(row2["item_seq"], 2)

        schema_state.reset_memory_cache(source)

