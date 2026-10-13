"""Unit-тесты однократного применения init.json."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import app.exec_engine
from app.init_apply import apply_init_for_source, is_initialized
from app.models import LiveSchema, TableSchema, ColumnSpec


class TestInitApply(unittest.TestCase):
    def test_is_initialized_empty(self):
        with patch("app.init_apply.get_live_schema", return_value=LiveSchema()):
            self.assertFalse(is_initialized("mssql"))

    def test_is_initialized_nonempty(self):
        live = LiveSchema(schemas=["demo"], tables={})
        with patch("app.init_apply.get_live_schema", return_value=live):
            self.assertTrue(is_initialized("mssql"))

    def test_skip_when_initialized(self):
        live = LiveSchema(
            schemas=["demo"],
            tables={
                "demo.t": TableSchema(
                    name="t",
                    schema="demo",
                    primary_key=["id"],
                    columns=[ColumnSpec(name="id", type="bigint")],
                )
            },
        )
        with patch("app.init_apply.get_live_schema", return_value=live), \
             patch("app.init_apply.load_init_ops") as load, \
             patch("app.exec_engine.execute_source_operations") as exe:
            apply_init_for_source("mssql")
            load.assert_not_called()
            exe.assert_not_called()

    def test_apply_when_empty(self):
        with patch("app.init_apply.get_live_schema", return_value=LiveSchema()), \
             patch("app.init_apply.load_init_ops", return_value={"mssql": [{"op": "create_schema", "name": "demo"}]}), \
             patch("app.exec_engine.execute_source_operations") as exe:
            exe.return_value.executed = 1
            apply_init_for_source("mssql")
            exe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
