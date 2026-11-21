"""Offline runner/recovery tests using the real shared modules.

Uses the trader integration test's external-boundary fixture. No real keys/network.
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import test_trader as fixtures
from config import load_ledger_config
from main import (run_cycle, RoleLock, Deadline, SessionEnding, protect_client,
                  arguments, cooldown_seconds, summarize, log_event)
from recovery import reconcile_known, bind_confirmed_transaction, unresolved_summary
from state_store import StateStore


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TraderContractTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def cycle(self, mode="both", submit=False):
        return run_cycle(mode, self.f.trader, self.f.guardian, self.f.llm, self.f.state,
                         submit=submit, flat_entry=True, clock=lambda: fixtures.NOW)

    def main_once(self, submit=False):
        import main as runner
        config = Path(self.f.tmp.name) / "config.json"
        log = Path(self.f.tmp.name) / "runner.jsonl"
        config.write_text(json.dumps({"LEDGER_BASE_URL": "https://ledger.chanyeun.com/api/v1",
                                     "TRADER_API_KEY": "offline-trader-key",
                                     "GUARDIAN_API_KEY": "offline-guardian-key"}), encoding="utf-8")
        argv = ["--account", "hind-btc", "--config", str(config), "--state", str(self.f.db),
                "--log", str(log), "--mode", "both", "--flat-entry", "--once"]
        if submit:
            argv.append("--submit")
        def with_test_clock(*args, **kwargs):
            return run_cycle(*args, **kwargs, clock=lambda: fixtures.NOW)
        with patch.object(runner, "LedgerClient", side_effect=[self.f.trader, self.f.guardian]), \
             patch.object(runner, "create_adapter", return_value=self.f.llm), \
             patch.object(runner, "run_cycle", side_effect=with_test_clock), patch("builtins.print"):
            result = runner.main(argv)
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return result, records

    def test_main_once_preview_wires_real_components_and_logs(self):
        result, records = self.main_once()
        self.assertEqual(result, 0)
        self.assertEqual(records[0]["event"], "session_start")
        self.assertEqual(records[-1]["event"], "session_end")
        self.assertEqual(records[1]["result"]["trader"]["status"], "PREVIEW")
        self.assertEqual(self.f.boundary.posts, [])

    def test_main_once_submit_wires_real_components_and_logs(self):
        result, records = self.main_once(submit=True)
        self.assertEqual(result, 0)
        self.assertEqual(records[1]["result"]["guardian_after"][0]["status"], "EXECUTED")
        self.assertEqual(len(self.f.boundary.posts), 2)
        self.assertEqual(records[-1]["unresolved"], [])

    def seed(self, tid=None):
        f = self.f
        f.state.save_theory_snapshot(fixtures.THEORY)
        order = {"symbol": "BTC", "action": "BUY", "quantity": "0.001", "theory_id": 7,
                 "reasoning": "Saved proposal"}
        intent = f.state.save_proposal_intent(order, 2)
        f.state.mark_submitting(intent)
        f.state.mark_uncertain(intent)
        if tid is not None:
            f.state.attach_transaction_id(intent, tid)
            f.state.mark_uncertain(intent)
        return intent, order

    def row(self, order, tid=42, status="EXECUTED"):
        return {"transaction_id": tid, "status": status, "symbol": order["symbol"],
                "action": order["action"], "requested_quantity": order["quantity"],
                "theory_id": order["theory_id"], "theory_version": 2, "reasoning": order["reasoning"],
                "executed_quantity": .001, "executed_price": 80000., "executed_total": 80.}

    def test_both_preview_does_not_write(self):
        result = self.cycle()
        self.assertEqual(result["trader"]["status"], "PREVIEW")
        self.assertEqual(self.f.boundary.posts, [])
        self.assertEqual(self.f.state.get_unresolved_intents(), [])

    def test_both_live_proposes_then_reviews_then_resolves(self):
        result = self.cycle(submit=True)
        self.assertEqual(result["trader"]["status"], "PROPOSED")
        self.assertEqual(result["guardian_after"][0]["status"], "EXECUTED")
        self.assertEqual([x[0] for x in self.f.boundary.posts], ["trade_propose.php", "trade_review.php"])
        self.assertEqual(self.f.state.get_unresolved_intents(), [])

    def test_trader_only_leaves_pending_for_separate_guardian(self):
        result = self.cycle(mode="trader", submit=True)
        self.assertEqual(result["trader"]["status"], "PROPOSED")
        self.assertEqual([x[0] for x in self.f.boundary.posts], ["trade_propose.php"])
        result = self.cycle(mode="guardian", submit=True)
        self.assertEqual(result["guardian_before"][0]["status"], "EXECUTED")
        self.assertEqual(self.f.state.get_unresolved_intents(), [])

    def test_outstanding_order_reviewed_before_another_proposal(self):
        self.f.cycle()
        result = self.cycle(submit=True)
        self.assertEqual(result["guardian_before"][0]["status"], "EXECUTED")
        # The resulting holding prevents Hind's additional BUY.
        self.assertEqual(result["trader"]["status"], "HOLD")
        self.assertEqual(len(self.f.boundary.posts), 2)

    def test_unknown_transaction_id_blocks_proposals_after_restart(self):
        intent, _ = self.seed()
        self.f.state.close()
        self.f.state = StateStore(self.f.db, "hind-btc")
        self.f.boundary.state = self.f.state
        result = self.cycle(submit=True)
        self.assertEqual(result["trader"]["status"], "HOLD")
        self.assertEqual(self.f.state.get_intent(intent)["status"], "UNCERTAIN")
        self.assertEqual(self.f.boundary.posts, [])

    def test_known_final_history_reconciles_without_reproposal(self):
        intent, order = self.seed(tid=42)
        self.f.boundary.history = [self.row(order)]
        result = self.cycle(mode="recover")
        self.assertEqual(result["recovery"], [])
        self.assertEqual(self.f.state.get_intent(intent)["status"], "EXECUTED")
        self.assertEqual(self.f.boundary.posts, [])

    def test_missing_history_stays_unresolved(self):
        intent, _ = self.seed(tid=42)
        self.cycle(mode="recover")
        self.assertEqual(self.f.state.get_intent(intent)["status"], "UNCERTAIN")

    def test_server_pending_without_local_record_blocks_trader_only(self):
        self.f.boundary.history = [{"transaction_id": 999, "status": "PENDING"}]
        self.assertEqual(self.cycle(mode="trader", submit=True)["trader"]["status"], "HOLD")
        self.assertEqual(self.f.boundary.posts, [])

    def test_server_escalation_without_local_record_blocks_trader(self):
        self.f.boundary.history = [{"transaction_id": 999, "status": "ESCALATED"}]
        self.assertEqual(self.cycle(mode="trader", submit=True)["trader"]["status"], "HOLD")
        self.assertEqual(self.f.boundary.posts, [])

    def test_lost_review_stops_new_trading(self):
        self.f.cycle()
        from client import UncertainWrite
        with patch.object(self.f.guardian, "submit_review", side_effect=UncertainWrite("lost")):
            result = self.cycle(submit=True)
        self.assertEqual(result["guardian_before"][0]["status"], "UNCERTAIN")
        self.assertEqual(result["trader"]["status"], "HOLD")
        self.assertEqual(len(self.f.boundary.posts), 1)

    def test_explicit_binding_matches_all_order_fields(self):
        intent, order = self.seed()
        self.f.boundary.history = [self.row(order)]
        result = bind_confirmed_transaction(self.f.trader, self.f.state, intent, 42)
        self.assertEqual(result["status"], "EXECUTED")
        self.assertEqual(self.f.state.get_unresolved_intents(), [])
        self.assertEqual(self.f.boundary.posts, [])

    def test_binding_mismatched_quantity_is_rejected(self):
        intent, order = self.seed()
        row = self.row(order)
        row["requested_quantity"] = 99
        self.f.boundary.history = [row]
        with self.assertRaises(ValueError):
            bind_confirmed_transaction(self.f.trader, self.f.state, intent, 42)
        self.assertIsNone(self.f.state.get_intent(intent)["transaction_id"])

    def test_binding_mismatched_theory_is_rejected(self):
        intent, order = self.seed()
        row = self.row(order)
        row["theory_version"] = 99
        self.f.boundary.history = [row]
        with self.assertRaises(ValueError):
            bind_confirmed_transaction(self.f.trader, self.f.state, intent, 42)

    def test_binding_absent_history_does_not_guess(self):
        intent, _ = self.seed()
        with self.assertRaises(ValueError):
            bind_confirmed_transaction(self.f.trader, self.f.state, intent, 42)
        self.assertIsNone(self.f.state.get_intent(intent)["transaction_id"])

    def test_safe_summary_omits_model_server_free_text(self):
        safe = summarize({"status": "EXECUTED", "transaction_id": 42,
                          "reason": "private text", "reasoning": "private text",
                          "api_key": "private-key"})
        self.assertEqual(safe, {"status": "EXECUTED", "transaction_id": 42})

    def test_deadline_blocks_http_before_external_request(self):
        deadline = Deadline(90, clock=lambda: 100)
        deadline.end = 120
        protect_client(self.f.trader, deadline)
        with self.assertRaises(SessionEnding):
            self.f.trader.get_policy()
        self.assertEqual(self.f.boundary.calls, [])

    def test_recovery_summary_excludes_order_text(self):
        self.seed()
        result = unresolved_summary(self.f.state)
        self.assertNotIn("Saved proposal", json.dumps(result))
        self.assertIsNone(result[0]["transaction_id"])


class ConfigurationAndCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.json"
        self.config = {"LEDGER_BASE_URL": "https://ledger.chanyeun.com/api/v1",
                       "TRADER_API_KEY": "offline-trader-key", "GUARDIAN_API_KEY": "offline-guardian-key"}

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, mode="both"):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        return load_ledger_config(self.path, mode)

    def test_both_keys_validated(self):
        self.assertEqual(self.load(), self.config)

    def test_trader_only_needs_trader_key(self):
        del self.config["GUARDIAN_API_KEY"]
        self.assertEqual(self.load("trader")["TRADER_API_KEY"], "offline-trader-key")
        with self.assertRaises(ValueError):
            self.load("both")

    def test_guardian_only_needs_guardian_key(self):
        del self.config["TRADER_API_KEY"]
        self.assertEqual(self.load("guardian")["GUARDIAN_API_KEY"], "offline-guardian-key")

    def test_model_key_rejected_in_ledger_config(self):
        self.config["GEMINI_API_KEY"] = "do-not-store-here"
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_base_rejected(self):
        for base in ("http://example.com", "https://user:password@example.com", "https://example.com?key=x"):
            self.config["LEDGER_BASE_URL"] = base
            with self.assertRaises(ValueError):
                self.load()

    def test_duplicate_config_fields_rejected(self):
        self.path.write_text('{"LEDGER_BASE_URL":"a","LEDGER_BASE_URL":"b"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_ledger_config(self.path, "both")

    def test_cli_defaults_to_preview(self):
        args = arguments(["--account", "hind-btc", "--once"])
        self.assertFalse(args.submit)
        self.assertEqual(args.interval, 60)
        self.assertEqual(args.duration, 540)

    def test_guardian_default_interval(self):
        self.assertEqual(arguments(["--account", "hind-btc", "--mode", "guardian"]).interval, 15)

    def test_invalid_intervals_and_duration(self):
        for option, value in (("--interval", "1"), ("--duration", "600"), ("--duration", "nan")):
            with self.subTest(option=option, value=value), self.assertRaises(SystemExit), patch("sys.stderr"):
                arguments(["--account", "hind-btc", option, value])

    def test_review_mode_is_single_sweep(self):
        self.assertTrue(arguments(["--account", "hind-btc", "--mode", "review"]).once)

    def test_role_lock_prevents_duplicate_role(self):
        path = Path(self.tmp.name) / "runner.lock"
        with RoleLock(path):
            with self.assertRaises(RuntimeError):
                with RoleLock(path):
                    self.fail("Second lock unexpectedly acquired")
        with RoleLock(path):
            pass

    def test_cooldown_duration_is_detected_inside_report(self):
        self.assertEqual(cooldown_seconds({"trader": {"status": "COOLDOWN", "retry_after": 25}}), 25)

    def test_log_is_json_lines(self):
        log = Path(self.tmp.name) / "session.jsonl"
        with patch("builtins.print"):
            log_event(log, "cycle", result={"status": "HOLD"})
        saved = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(saved["event"], "cycle")
        self.assertEqual(saved["result"]["status"], "HOLD")


if __name__ == "__main__":
    unittest.main()
