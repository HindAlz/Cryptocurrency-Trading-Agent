"""Checks the simulation harness and evidence labels. No network or keys."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import security_simulations as sim


class SecuritySimulationTests(unittest.TestCase):
    def test_positive_control_executes_only_in_stub(self):
        result = sim.run_case("baseline_valid_order")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["observed"]["guardian_results"][0]["status"], "EXECUTED")
        self.assertEqual(result["real_ledger_transaction_ids"], [])

    def test_oversized_buy_is_blocked_before_model(self):
        result = sim.run_case("oversized_buy")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["observed"]["model_stub_calls"], 0)
        self.assertIn("TRANSACTION_LIMIT_EXCEEDED", result["observed"]["submitted_reviews_to_http_stub"][0]["rules_triggered"])

    def test_unsupported_sale_is_not_confounded_by_size(self):
        result = sim.run_case("unsupported_sale")
        self.assertEqual(result["result"], "PASS")
        review = result["observed"]["submitted_reviews_to_http_stub"][0]
        self.assertIn("INSUFFICIENT_HOLDINGS", review["rules_triggered"])
        self.assertNotIn("TRANSACTION_LIMIT_EXCEEDED", review["rules_triggered"])

    def test_malformed_output_reaches_adapter_and_escalates(self):
        result = sim.run_case("malformed_model")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["observed"]["model_stub_calls"], 1)
        self.assertEqual(result["observed"]["persisted_local_status"], "ESCALATED")
        self.assertTrue(result["checks"]["no_authorizing_review"])

    def test_each_negative_case_preserves_cash_and_holdings(self):
        for name in sim.CASES:
            with self.subTest(case=name):
                result = sim.run_case(name)
                self.assertEqual(result["inputs"]["portfolio_before"], result["observed"]["portfolio_after"])
                self.assertEqual(result["real_ledger_transaction_ids"], [])

    def test_cli_writes_evidence_and_scope(self):
        with tempfile.TemporaryDirectory() as folder, patch("builtins.print"):
            self.assertEqual(sim.main(["--member", "Test member", "--output", folder]), 0)
            document = json.loads(next(Path(folder).glob("*/results.json")).read_text())
            self.assertTrue(document["all_passed"])
            self.assertEqual(len(document["results"]), 4)
            self.assertTrue(next(Path(folder).glob("*/summary.md")).exists())

    def test_unexpected_harness_error_is_not_reported_as_pass(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sim, "run_case", side_effect=RuntimeError("private")), patch("builtins.print"):
            self.assertEqual(sim.main(["--member", "Test", "--output", folder]), 1)
            raw = next(Path(folder).glob("*/results.json")).read_text()
            self.assertNotIn("private", raw)
            self.assertFalse(json.loads(raw)["all_passed"])


if __name__ == "__main__":
    unittest.main()
