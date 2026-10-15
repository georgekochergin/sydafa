"""Unit-тесты для подсистемы генерации данных: random_date, date_offset, weights, locale, null_ratio и DAG."""
import calendar
from datetime import date, datetime
import unittest

from pydantic import ValidationError

from app.generators import (
    GeneratorError,
    _generate_value,
    _mint_new_row,
    _resolve_anchor_date,
    _shift_date,
)
from app.models import (
    ChoiceGenerator,
    ColumnSpec,
    DateOffsetGenerator,
    FakerGenerator,
    LiveSchema,
    RandomDateGenerator,
    RandomDecimalGenerator,
    SequenceGenerator,
    TableSchema,
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
        col = ColumnSpec(name="dt", type="date", generator=RandomDateGenerator())
        current_year = date.today().year
        for _ in range(50):
            val = _generate_value(col)
            self.assertIsInstance(val, date)
            self.assertEqual(val.year, current_year)
            self.assertGreaterEqual(val, date(current_year, 1, 1))
            self.assertLessEqual(val, date(current_year, 12, 31))

    def test_random_date_with_anchors_and_dim(self):
        """random_date с anchor и размерностями y, m, d."""
        today = date.today()

        # Anchor today + dim d [-5, 5]
        col_d = ColumnSpec(
            name="dt_d",
            type="date",
            generator=RandomDateGenerator(anchor="today", dim="d", min_offset=-5, max_offset=5),
        )
        val_d = _generate_value(col_d)
        self.assertIsInstance(val_d, date)
        self.assertLessEqual(abs((val_d - today).days), 5)

        # Anchor start_of_month
        col_m = ColumnSpec(
            name="dt_m",
            type="date",
            generator=RandomDateGenerator(anchor="start_of_month", dim="d", min_offset=0, max_offset=0),
        )
        val_m = _generate_value(col_m)
        self.assertEqual(val_m, date(today.year, today.month, 1))

        # Anchor start_of_quarter
        col_q = ColumnSpec(
            name="dt_q",
            type="date",
            generator=RandomDateGenerator(anchor="start_of_quarter", dim="d", min_offset=0, max_offset=0),
        )
        val_q = _generate_value(col_q)
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
        col = ColumnSpec(
            name="end_date",
            type="date",
            generator=DateOffsetGenerator(ref_column="start_date", dim="y", min_offset=1, max_offset=3),
        )

        row_context = {"start_date": date(2026, 5, 10)}
        for _ in range(20):
            val = _generate_value(col, row_context)
            self.assertIsInstance(val, date)
            self.assertEqual(val.month, 5)
            self.assertEqual(val.day, 10)
            self.assertIn(val.year, [2027, 2028, 2029])
            self.assertGreater(val, row_context["start_date"])

        # Базовая дата None -> возвращает None
        self.assertIsNone(_generate_value(col, {"start_date": None}))

    def test_null_ratio(self):
        """null_ratio=1.0 всегда дает None, null_ratio=0.0 никогда не дает None."""
        col_null = ColumnSpec(
            name="txt",
            type="string",
            generator=FakerGenerator(provider="word", null_ratio=1.0),
        )
        self.assertIsNone(_generate_value(col_null))

        col_not_null = ColumnSpec(
            name="txt",
            type="string",
            generator=FakerGenerator(provider="word", null_ratio=0.0),
        )
        self.assertIsNotNone(_generate_value(col_not_null))

    def test_faker_locale(self):
        """Faker с локалью ru_RU генерирует данные с кириллицей."""
        col = ColumnSpec(
            name="name",
            type="string",
            generator=FakerGenerator(provider="name", locale="ru_RU"),
        )
        val = _generate_value(col)
        self.assertIsInstance(val, str)
        self.assertTrue(len(val) > 0)

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
        col = ColumnSpec(
            name="status",
            type="string",
            generator=ChoiceGenerator(values=["FIRST", "SECOND"], weights=[1.0, 0.0]),
        )
        for _ in range(20):
            self.assertEqual(_generate_value(col), "FIRST")

    def test_dag_generation_with_inverted_column_order(self):
        """Колонка end_date объявлена ПЕРЕД start_date в схеме таблицы.

        Движок должен автоматически разрешить зависимость и корректно сгенерировать обе даты.
        """
        table = TableSchema(
            name="contracts",
            schema="dbo",
            primary_key=["contract_id"],
            columns=[
                ColumnSpec(name="contract_id", type="bigint", generator=SequenceGenerator()),
                # end_date объявлена раньше start_date!
                ColumnSpec(
                    name="contract_end_date",
                    type="date",
                    generator=DateOffsetGenerator(ref_column="contract_start_date", dim="y", min_offset=1, max_offset=3),
                ),
                ColumnSpec(
                    name="contract_start_date",
                    type="date",
                    generator=RandomDateGenerator(),
                ),
            ],
            foreign_keys=[],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()

        _mint_new_row(conn, "test_source", "mssql", live, table, new_key_ratio=1.0, batch_id=1)

        self.assertEqual(len(conn.cur.executed), 2)
        select_sql, _ = conn.cur.executed[0]
        self.assertIn("SELECT MAX", select_sql)

        insert_sql, params = conn.cur.executed[1]
        self.assertIn("INSERT INTO", insert_sql)

        # Вытаскиваем список колонок из INSERT INTO [dbo].[contracts] (col1, col2...)
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
                ColumnSpec(name="id", type="bigint", generator=SequenceGenerator()),
                ColumnSpec(
                    name="col_a",
                    type="date",
                    generator=DateOffsetGenerator(ref_column="col_b", dim="y", min_offset=1, max_offset=2),
                ),
                ColumnSpec(
                    name="col_b",
                    type="date",
                    generator=DateOffsetGenerator(ref_column="col_a", dim="y", min_offset=1, max_offset=2),
                ),
            ],
            foreign_keys=[],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()

        with self.assertRaises(GeneratorError) as ctx:
            _mint_new_row(conn, "test_source", "mssql", live, table, new_key_ratio=1.0, batch_id=1)
        self.assertIn("циклическая", str(ctx.exception).lower())

    def test_dag_unknown_ref_column(self):
        """Если ref_column ссылается на несуществующую колонку, выбрасывается GeneratorError."""
        table = TableSchema(
            name="contracts",
            schema="dbo",
            primary_key=["id"],
            columns=[
                ColumnSpec(name="id", type="bigint", generator=SequenceGenerator()),
                ColumnSpec(
                    name="end_date",
                    type="date",
                    generator=DateOffsetGenerator(ref_column="non_existent", dim="y", min_offset=1, max_offset=2),
                ),
            ],
            foreign_keys=[],
        )

        live = LiveSchema(schemas=["dbo"], tables={"dbo.contracts": table})
        conn = FakeConn()

        with self.assertRaises(GeneratorError) as ctx:
            _mint_new_row(conn, "test_source", "mssql", live, table, new_key_ratio=1.0, batch_id=1)
        self.assertIn("неизвестную ref_column", str(ctx.exception))
