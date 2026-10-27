"""Тесты точечного управления правилами: генератор одной колонки и один инвариант."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import rules_state
from app.main import app
from app.models import TableRuleProfile


class TestGranularRulesApi(unittest.TestCase):
    def setUp(self):
        rules_state._rules_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.client = TestClient(app, raise_server_exceptions=False)
        profile = TableRuleProfile(
            columns={
                "client_id": {"kind": "sequence"},
                "status": {"kind": "choice", "values": ["Active", "Blocked"], "null_ratio": 0.05},
            },
            invariants=[],
        )
        with patch("app.rules_state.STATE_DIR", self.state):
            rules_state.set_table_rules("bank", "retail_bank.clients", profile)

    def tearDown(self):
        rules_state._rules_cache.clear()
        self.tmp.cleanup()

    def call(self, method: str, path: str, **kwargs):
        with patch("app.rules_state.STATE_DIR", self.state), \
             patch("app.main._assert_source", lambda source: None), \
             patch("app.main._wake", return_value={}):
            return getattr(self.client, method)(path, **kwargs)

    def test_put_column_sets_single_generator_and_keeps_rest(self):
        resp = self.call(
            "put",
            "/sources/bank/rules/retail_bank.clients/columns/email",
            json={"kind": "faker", "provider": "email", "null_ratio": 0.05},
        )
        self.assertEqual(resp.status_code, 200)
        profile = resp.json()
        self.assertEqual(profile["columns"]["email"]["kind"], "faker")
        self.assertEqual(profile["columns"]["client_id"]["kind"], "sequence")
        self.assertEqual(profile["columns"]["status"]["values"], ["Active", "Blocked"])

        stored = rules_state.get_table_rules("bank", "retail_bank.clients")
        self.assertEqual(stored.columns["email"].provider, "email")
        self.assertEqual(len(stored.columns), 3)

    def test_put_column_replaces_existing_generator(self):
        resp = self.call(
            "put",
            "/sources/bank/rules/retail_bank.clients/columns/status",
            json={"kind": "choice", "values": ["New"], "null_ratio": 0.0},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["columns"]["status"]["values"], ["New"])
        self.assertEqual(len(resp.json()["columns"]), 2)

    def test_put_column_rejects_bad_identifier(self):
        resp = self.call(
            "put",
            "/sources/bank/rules/retail_bank.clients/columns/bad-col;drop",
            json={"kind": "sequence"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_put_column_unknown_profile_is_404(self):
        resp = self.call(
            "put",
            "/sources/bank/rules/retail_bank.nosuch/columns/email",
            json={"kind": "sequence"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_column_removes_only_that_rule(self):
        resp = self.call("delete", "/sources/bank/rules/retail_bank.clients/columns/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(resp.json()["columns"]), ["client_id"])

        stored = rules_state.get_table_rules("bank", "retail_bank.clients")
        self.assertNotIn("status", stored.columns)

    def test_delete_unknown_column_is_404(self):
        resp = self.call("delete", "/sources/bank/rules/retail_bank.clients/columns/nosuch")
        self.assertEqual(resp.status_code, 404)

    def test_post_invariant_appends_one(self):
        invariant = {
            "kind": "cumulative_balance",
            "amount_column": "amount",
            "partition_by": ["account_id"],
            "min_balance": 0.0,
        }
        resp = self.call("post", "/sources/bank/rules/retail_bank.clients/invariants", json=invariant)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["invariants"]), 1)
        self.assertEqual(resp.json()["invariants"][0]["amount_column"], "amount")

        stored = rules_state.get_table_rules("bank", "retail_bank.clients")
        self.assertEqual(len(stored.invariants), 1)

    def test_post_duplicate_invariant_is_409(self):
        invariant = {"kind": "cumulative_balance", "amount_column": "amount", "min_balance": 0.0}
        self.assertEqual(
            self.call("post", "/sources/bank/rules/retail_bank.clients/invariants", json=invariant).status_code,
            200,
        )
        duplicate = self.call("post", "/sources/bank/rules/retail_bank.clients/invariants", json=invariant)
        self.assertEqual(duplicate.status_code, 409)

    def test_delete_invariant_by_index(self):
        invariant = {"kind": "cumulative_balance", "amount_column": "amount", "min_balance": 0.0}
        self.call("post", "/sources/bank/rules/retail_bank.clients/invariants", json=invariant)
        resp = self.call("delete", "/sources/bank/rules/retail_bank.clients/invariants/0")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["invariants"], [])

        out_of_range = self.call("delete", "/sources/bank/rules/retail_bank.clients/invariants/0")
        self.assertEqual(out_of_range.status_code, 404)

    def test_table_profile_route_still_matches_plain_fqn(self):
        """Старые маршруты с {entity_fqn:path} продолжают работать для обычных FQN."""
        resp = self.call("get", "/sources/bank/rules/retail_bank.clients")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("status", resp.json()["columns"])


if __name__ == "__main__":
    unittest.main()
