"""Unit-тесты для персистентного реестра сиквенсов (schema_state.py)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import schema_state
from app.models import DropTableOp, RenameColumnOp, RenameSchemaOp, RenameTableOp, TransferTableOp


class TestSchemaStateSequences(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher = patch.object(schema_state, "STATE_DIR", Path(self.tmp_dir))
        self.patcher.start()
        schema_state.reset_memory_cache("test_src")

    def tearDown(self):
        schema_state.reset_memory_cache("test_src")
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reserve_sequence_range_starts_from_zero(self):
        """Счетчик сиквенса стартует с 0 (первое возвращенное значение 1) и монотонно возрастает."""
        start_1 = schema_state.reserve_sequence_range("test_src", "demo.orders", "order_id", count=5)
        self.assertEqual(start_1, 1)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.orders", "order_id"), 5)

        start_2 = schema_state.reserve_sequence_range("test_src", "demo.orders", "order_id", count=3)
        self.assertEqual(start_2, 6)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.orders", "order_id"), 8)

    def test_persistence_on_disk(self):
        """Значения сиквенсов сохраняются на диск в sequences.{source}.json."""
        schema_state.reserve_sequence_range("test_src", "demo.items", "item_id", count=10)
        seq_file = Path(self.tmp_dir) / "sequences.test_src.json"
        self.assertTrue(seq_file.exists())
        data = json.loads(seq_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("demo.items.item_id"), 10)

        # Очищаем кэш в памяти и проверяем считывание с диска
        schema_state._sequences_cache.clear()
        start = schema_state.reserve_sequence_range("test_src", "demo.items", "item_id", count=5)
        self.assertEqual(start, 11)

    def test_reset_table_sequences(self):
        """Удаление таблицы сбрасывает связанные с ней сиквенсы."""
        schema_state.reserve_sequence_range("test_src", "demo.users", "user_id", count=5)
        schema_state.reserve_sequence_range("test_src", "demo.users", "seq_num", count=3)
        schema_state.reserve_sequence_range("test_src", "demo.orders", "order_id", count=2)

        schema_state.reset_table_sequences("test_src", "demo.users")

        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.users", "user_id"), 0)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.users", "seq_num"), 0)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.orders", "order_id"), 2)

    def test_rename_table_sequences(self):
        """Переименование таблицы сохраняет текущие значения сиквенсов под новым именем."""
        schema_state.reserve_sequence_range("test_src", "demo.users", "user_id", count=42)
        schema_state.rename_table_sequences("test_src", "demo.users", "demo.clients")

        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.users", "user_id"), 0)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.clients", "user_id"), 42)

    def test_rename_column_sequence(self):
        """Переименование колонки переносит значение счетчика на новое имя колонки."""
        schema_state.reserve_sequence_range("test_src", "demo.items", "old_id", count=99)
        schema_state.rename_column_sequence("test_src", "demo.items", "old_id", "new_id")

        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.items", "old_id"), 0)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.items", "new_id"), 99)

    def test_sync_sequences_after_operations(self):
        """Синхронизация счетчиков после пачки DDL-операций."""
        schema_state.reserve_sequence_range("test_src", "demo.t1", "id", count=10)
        schema_state.reserve_sequence_range("test_src", "demo.t2", "id", count=20)
        schema_state.reserve_sequence_range("test_src", "demo.t3", "id", count=30)
        schema_state.reserve_sequence_range("test_src", "other.t4", "id", count=40)

        ops = [
            DropTableOp(entity_fqn="demo.t1"),
            RenameTableOp(entity_fqn="demo.t2", to_name="t2_renamed"),
            TransferTableOp(entity_fqn="demo.t3", to_schema="archive"),
            RenameSchemaOp(from_name="other", to_name="legacy"),
        ]
        schema_state.sync_sequences_after_operations("test_src", ops)

        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.t1", "id"), 0)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "demo.t2_renamed", "id"), 20)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "archive.t3", "id"), 30)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "legacy.t4", "id"), 40)
        self.assertEqual(schema_state.get_current_sequence_value("test_src", "other.t4", "id"), 0)
