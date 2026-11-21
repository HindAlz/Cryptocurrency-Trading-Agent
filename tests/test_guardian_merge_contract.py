"""Offline merge checks using the real client, state store and model adapter.

Run: python -m unittest discover -s tests -p test_guardian_merge_contract.py -v
The OpenAI SDK boundary and requests Session are mocked. No internet or keys.
"""
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from client import LedgerClient, Cooldown, UncertainWrite
from guardian import prepare_review, submit_review_safely, review_pending
from guardian_live import (review_pending_live, recheck_and_submit,
                           submit_non_authorizing, validated_review)
from guardian_signals import IndependentSignals, price_at
from guardian_rules import check_theory_compliance
from llm_adapter import ModelAdapter, ModelConfig, validate_context
from state_store import StateStore

NOW = 1800000000
THEORY = {
    "id": 7, "version": 2, "status": "SUBMITTED", "title": "Momentum",
    "hypothesis": "Synthetic test", "entry_rule": "Positive momentum",
    "exit_rule": "Negative momentum or stop/profit", "risk_rule": "Limits",
    "selected_symbols": ["BTC"], "strategy": "momentum", "lookback_minutes": 15,
    "threshold_pct": .5, "position_fraction": .01, "stop_loss_pct": 1,
    "take_profit_pct": 1.5
}
ORDER = {"transaction_id": 42, "symbol": "BTC", "action": "BUY", "quantity": .005,
         "requested_price": 79000., "reasoning": "Momentum",
         "theory_id": 7, "theory_version": 2}
POLICY = {"allowed_symbols": ["BTC"], "restricted_actions": ["WITHDRAW"],
          "max_transaction_amount": 1000., "max_trades_per_hour": 10,
          "max_trades_per_day": 50, "trades_last_hour": 0, "trades_last_day": 0,
          "escalation_threshold": 750.}
QUOTE = {"symbol": "BTC", "price": 80000., "timestamp_unix": NOW, "model_version": "test"}
PORTFOLIO = {"cash_balance": 100000., "holdings": []}
MODEL_APPROVE = {"decision": "APPROVE", "reason": "Checks passed", "risk_score": 10,
                 "modified_quantity": None, "rules_triggered": []}


def signals(order, theory, quote):
    return {"lookback_return_pct": .6, "mean_deviation_pct": -.6,
            "return_since_avg_cost_pct": None, "entry_triggered": True,
            "exit_triggers": [], "replay_matches_quote": True}


def pending(order):
    return {"transaction_id": order["transaction_id"], "symbol": order["symbol"],
            "action": order["action"], "requested_quantity": order["quantity"],
            "requested_price": order["requested_price"], "reasoning": order["reasoning"],
            "theory_id": order["theory_id"], "theory_version": order["theory_version"]}


class LedgerBoundary:
    """Respond to the actual client's HTTP requests with Appendix B envelopes."""
    def __init__(self):
        self.policy = deepcopy(POLICY)
        self.quote = deepcopy(QUOTE)
        self.portfolio = deepcopy(PORTFOLIO)
        self.orders = [pending(ORDER)]
        self.history = []
        self.posts = []
        self.calls = []
        self.fail_post = False
        self.fill_status = "EXECUTED"
        self.on_policy = None
        self.policy_count = 0
        self.cooldown = False

    def request(self, method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((method, endpoint))
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"]["X-Api-Key"] == "offline-guardian-key"
        status = 200
        if self.cooldown and endpoint == "policies.php":
            return types.SimpleNamespace(status_code=429, headers={"Retry-After": "15"},
                                         ok=False, json=lambda: {"error": "rate_limit"})
        if method == "POST":
            assert endpoint == "trade_review.php"
            body = json.loads(kwargs["data"])
            self.posts.append(body)
            if self.fail_post:
                import requests
                raise requests.ConnectionError("Synthetic lost response")
            result = {"transaction_id": body["transaction_id"], "status": self.fill_status}
            if self.fill_status == "EXECUTED":
                result["fill"] = {"quantity": .005, "price": 80000., "total": 400.}
            if body["decision"] == "BLOCK":
                result = {"transaction_id": body["transaction_id"], "status": "BLOCKED", "reason": "blocked"}
            if body["decision"] == "ESCALATE":
                result = {"transaction_id": body["transaction_id"], "status": "ESCALATED"}
        elif endpoint == "policies.php":
            self.policy_count += 1
            if self.on_policy:
                self.on_policy(self)
            result = self.policy
        elif endpoint == "market_prices.php":
            result = {"prices": [self.quote]}
        elif endpoint == "portfolio.php":
            result = self.portfolio
        elif endpoint == "trade_pending.php":
            result = {"pending": self.orders, "has_more": False}
        elif endpoint == "transactions.php":
            result = {"transactions": self.history}
        else:
            raise AssertionError("Unexpected HTTP call: " + endpoint)
        saved = deepcopy(result)
        return types.SimpleNamespace(status_code=status, headers={}, ok=True, json=lambda: saved)


def model_boundary(result=MODEL_APPROVE, finish="stop", refusal=None):
    sdk = types.ModuleType("openai")
    sdk.OpenAI = MagicMock()
    create = sdk.OpenAI.return_value.__enter__.return_value.chat.completions.create
    create.return_value = types.SimpleNamespace(choices=[types.SimpleNamespace(
        finish_reason=finish, message=types.SimpleNamespace(
            content=json.dumps(result), refusal=refusal))])
    return sdk, create


class MergeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.state = StateStore(self.path, "test-btc")
        self.state.save_theory_snapshot(THEORY)
        api_order = {k: ORDER[k] for k in ("symbol", "action", "quantity", "theory_id", "reasoning")}
        self.intent = self.state.save_proposal_intent(api_order, 2)
        self.state.mark_submitting(self.intent)
        self.state.attach_transaction_id(self.intent, 42)
        self.boundary = LedgerBoundary()
        self.client = LedgerClient("https://ledger.chanyeun.com/api/v1", "offline-guardian-key")
        self.client.session.request = self.boundary.request
        self.adapter = ModelAdapter(ModelConfig(provider="gemini", model="offline-test-model"))
        self.patches = [patch("client.time.sleep"), patch("os.environ", {"GEMINI_API_KEY": "offline-model-key"})]
        for p in self.patches:
            p.start()
        self.sdk, self.create = model_boundary()
        self.sdkpatch = patch.dict(sys.modules, {"openai": self.sdk})
        self.sdkpatch.start()

    def tearDown(self):
        self.sdkpatch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.state.close()
        self.tmp.cleanup()

    def run_live(self, **kwargs):
        return review_pending_live(self.client, self.adapter, self.state, signals,
                                   clock=lambda: NOW, **kwargs)

    def payload(self, decision="APPROVE", quantity=None):
        return {"transaction_id": 42, "decision": decision, "reason": "test",
                "risk_score": 10, "modified_quantity": quantity, "rules_triggered": []}

    def test_full_real_component_contract(self):
        result = self.run_live()[0]
        self.assertEqual(result["status"], "EXECUTED")
        self.assertEqual(result["fill"]["total"], 400)
        self.assertEqual(len(self.boundary.posts), 1)
        self.assertEqual(self.boundary.policy_count, 2)
        self.assertEqual(self.state.get_unresolved_intents(), [])
        saved = self.state.get_intent(self.intent)["outcome"]
        self.assertEqual(saved["fill"], result["fill"])
        arguments = self.create.call_args.kwargs
        context = json.loads(arguments["messages"][1]["content"])
        validate_context(context, "guardian")
        self.assertEqual(context["order"]["requested_price_usd"], 79000.)
        self.assertEqual(context["quote"]["price_usd"], 80000.)
        self.assertEqual(context["quote"]["timestamp_unix"], NOW)
        self.assertEqual(arguments["response_format"]["type"], "json_schema")
        self.assertNotIn("offline-guardian-key", json.dumps(context))
        self.assertNotIn("offline-model-key", json.dumps(context))

    def test_modify_reaches_ledger_as_decimal_string(self):
        self.create.return_value.choices[0].message.content = json.dumps({
            **MODEL_APPROVE, "decision": "MODIFY", "modified_quantity": .002})
        self.run_live()
        self.assertEqual(self.boundary.posts[0]["modified_quantity"], "0.002")

    def test_boolean_modification_rejected_before_post(self):
        for q in (True, False):
            with self.assertRaises(ValueError):
                recheck_and_submit(self.client, ORDER, self.payload("MODIFY", q), THEORY,
                                   signals, clock=lambda: NOW)
        self.assertEqual(self.boundary.posts, [])

    def test_equal_increased_nonfinite_or_precision_rejected(self):
        for q in (.005, .006, 0, -1, float("nan"), float("inf"), "0.00000000001"):
            with self.subTest(q=q):
                with self.assertRaises((ValueError, ArithmeticError)):
                    recheck_and_submit(self.client, ORDER, self.payload("MODIFY", q), THEORY,
                                       signals, clock=lambda: NOW)
        self.assertEqual(self.boundary.posts, [])

    def test_arbitrary_pass_cannot_authorize(self):
        with self.assertRaises(ValueError):
            submit_review_safely(self.client, self.payload(), {"decision": "PASS"})
        self.assertEqual(self.boundary.posts, [])

    def test_other_transaction_rejected(self):
        payload = self.payload()
        payload["transaction_id"] = 999
        with self.assertRaises(ValueError):
            recheck_and_submit(self.client, ORDER, payload, THEORY, signals, clock=lambda: NOW)
        self.assertEqual(self.boundary.posts, [])

    def test_changed_policy_blocks(self):
        def tighten(boundary):
            if boundary.policy_count == 2:
                boundary.policy["max_transaction_amount"] = 100
        self.boundary.on_policy = tighten
        self.assertEqual(self.run_live()[0]["status"], "BLOCKED")
        self.assertEqual(self.boundary.posts[0]["decision"], "BLOCK")

    def test_changed_cash_blocks(self):
        def reduce_cash(boundary):
            if boundary.policy_count == 2:
                boundary.portfolio["cash_balance"] = 100
        self.boundary.on_policy = reduce_cash
        self.assertEqual(self.run_live()[0]["status"], "BLOCKED")

    def test_changed_quota_blocks(self):
        def exhaust(boundary):
            if boundary.policy_count == 2:
                boundary.policy["trades_last_hour"] = 10
        self.boundary.on_policy = exhaust
        self.assertEqual(self.run_live()[0]["status"], "BLOCKED")

    def test_price_change_blocks(self):
        def increase(boundary):
            if boundary.policy_count == 2:
                boundary.quote["price"] = 300000
        self.boundary.on_policy = increase
        self.assertEqual(self.run_live()[0]["status"], "BLOCKED")

    def test_sell_holdings_change_blocks(self):
        order = {**ORDER, "action": "SELL"}
        self.boundary.portfolio["holdings"] = [{"symbol": "BTC", "quantity": .001, "avg_cost": 79000}]
        result = recheck_and_submit(self.client, order, self.payload(), THEORY,
                                    signals, clock=lambda: NOW)
        self.assertEqual(result["status"], "BLOCKED")

    def test_model_failure_escalates(self):
        self.create.side_effect = TimeoutError("private details")
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")
        self.assertEqual(self.state.get_unresolved_intents()[0]["status"], "ESCALATED")

    def test_incomplete_model_response_escalates(self):
        self.create.return_value.choices[0].finish_reason = "length"
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")

    def test_model_refusal_escalates(self):
        self.create.return_value.choices[0].message.refusal = "No"
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")

    def test_malformed_model_output_escalates(self):
        self.create.return_value.choices[0].message.content = "{broken"
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")

    def test_hard_block_skips_model(self):
        self.boundary.policy["max_transaction_amount"] = 100
        self.assertEqual(self.run_live()[0]["status"], "BLOCKED")
        self.create.assert_not_called()

    def test_threshold_escalates_without_model(self):
        self.boundary.policy["escalation_threshold"] = 100
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")
        self.create.assert_not_called()

    def test_missing_snapshot_escalates(self):
        self.boundary.orders[0]["theory_version"] = 99
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")
        self.create.assert_not_called()

    def test_uses_historical_snapshot(self):
        new = {**THEORY, "id": 8, "version": 3, "title": "New theory"}
        self.state.save_theory_snapshot(new)
        self.run_live()
        context = json.loads(self.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(context["theory"]["id"], 7)
        self.assertEqual(context["theory"]["version"], 2)

    def test_cooldown_stops_before_model_or_post(self):
        self.boundary.cooldown = True
        with self.assertRaises(Cooldown):
            self.run_live()
        self.assertEqual(self.boundary.posts, [])
        self.create.assert_not_called()

    def test_lost_reply_marks_uncertain_and_no_retry(self):
        self.boundary.fail_post = True
        self.assertEqual(self.run_live()[0]["status"], "UNCERTAIN")
        self.assertEqual(len(self.boundary.posts), 1)
        self.assertEqual(self.state.get_intent(self.intent)["status"], "UNCERTAIN")
        self.state.close()
        self.state = StateStore(self.path, "test-btc")
        self.assertEqual(self.state.get_intent(self.intent)["status"], "UNCERTAIN")

    def test_restart_reconciles_finalized_without_post(self):
        self.state.mark_uncertain(self.intent)
        self.boundary.history = [{"transaction_id": 42, "status": "EXECUTED",
                                  "executed_quantity": .005, "executed_total": 400.}]
        self.boundary.orders = []
        self.assertEqual(self.run_live(), [])
        self.assertEqual(self.boundary.posts, [])
        self.assertEqual(self.state.get_unresolved_intents(), [])

    def test_pending_retry_fetches_fresh_evidence(self):
        self.state.mark_uncertain(self.intent)
        self.boundary.history = [{"transaction_id": 42, "status": "PENDING"}]
        self.assertEqual(self.run_live()[0]["status"], "EXECUTED")
        self.assertEqual(self.boundary.policy_count, 2)
        self.assertEqual(len(self.boundary.posts), 1)

    def test_attack_order_without_intent_processed(self):
        self.boundary.orders[0]["transaction_id"] = 43
        self.assertEqual(self.run_live()[0]["status"], "EXECUTED")
        self.assertIsNone(self.state.get_intent_by_transaction(43))

    def test_unresolved_unknown_proposal_not_forgotten(self):
        self.state.record_outcome(self.intent, {"transaction_id": 42, "status": "BLOCKED"})
        order = {k: ORDER[k] for k in ("symbol", "action", "quantity", "theory_id", "reasoning")}
        other = self.state.save_proposal_intent(order, 2)
        self.state.mark_submitting(other)
        self.boundary.orders = []
        self.run_live()
        self.assertEqual(self.state.get_intent(other)["status"], "SUBMITTING")

    def test_rejection_is_not_reported_as_execution(self):
        self.boundary.fill_status = "REJECTED"
        self.assertEqual(self.run_live()[0]["status"], "REJECTED")
        self.assertEqual(self.state.get_intent(self.intent)["status"], "REJECTED")

    def test_invalid_quote_age_escalates(self):
        self.boundary.quote["timestamp_unix"] = NOW - 121
        self.assertEqual(self.run_live()[0]["status"], "ESCALATED")
        self.create.assert_not_called()

    def test_case_normalization_preserves_snapshot(self):
        # Different account avoids changing an immutable snapshot.
        with StateStore(self.path, "lower-case-account") as other:
            lower = {**THEORY, "status": "submitted"}
            other.save_theory_snapshot(lower)
            result = review_pending_live(self.client, self.adapter, other, signals, clock=lambda: NOW)
            self.assertEqual(result[0]["status"], "EXECUTED")
            self.assertEqual(other.get_theory_snapshot(7, 2)["status"], "submitted")

    def test_optional_no_additional_buy_rule(self):
        self.boundary.portfolio["holdings"] = [{"symbol": "BTC", "quantity": .001, "avg_cost": 79000}]
        self.assertEqual(self.run_live(require_flat_entry=True)[0]["status"], "BLOCKED")
        self.create.assert_not_called()

    def test_two_orders_isolate_model_failure(self):
        self.boundary.orders.append({**pending(ORDER), "transaction_id": 43})
        original = self.create.return_value
        self.create.side_effect = [TimeoutError(), original]
        results = self.run_live()
        self.assertEqual([r["status"] for r in results], ["ESCALATED", "EXECUTED"])
        self.assertEqual(len(self.boundary.posts), 2)

    def test_read_only_default_does_not_post(self):
        result = review_pending(self.client, self.adapter, self.state, signals)
        self.assertEqual(result[0]["decision"], "PASS")
        self.assertEqual(self.boundary.posts, [])
        self.create.assert_not_called()

    def test_public_live_opt_in(self):
        result = review_pending(self.client, self.adapter, self.state, signals,
                                submit=True, clock=lambda: NOW)
        self.assertEqual(result[0]["status"], "EXECUTED")

    def test_manual_script_import_has_no_file_reads_or_http(self):
        path = Path(__file__).resolve().parents[1] / "test_connection.py"
        spec = importlib.util.spec_from_file_location("offline_manual_connection", path)
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.open", side_effect=AssertionError("private file read")):
            spec.loader.exec_module(module)
        self.assertEqual(self.boundary.calls, [])

    def test_account_isolation(self):
        with StateStore(self.path, "another-account") as other:
            self.assertIsNone(other.get_theory_snapshot(7, 2))
            self.assertIsNone(other.get_intent_by_transaction(42))


class IndependentReplayTests(unittest.TestCase):
    def model(self):
        return {"version": "test", "tick_seconds": 5, "epoch_unix": NOW - 86400,
                "power_timescale_days": 365, "min_price": .00000001, "max_price": 1e12,
                "noise_modulus": 2147483647, "noise_multiplier": 48271,
                "cycles": [{"period_seconds": 7200, "phase": 0}],
                "assets": {"BTC": {"base_price": 80000, "power_exponent": .45,
                                     "phase": 0, "cycle_amplitudes": [.01],
                                     "noise_amplitude": .001, "noise_seed": 17}}}

    def test_61_samples_and_no_future(self):
        model = self.model()
        client = MagicMock()
        client.get_market_model.return_value = {"model": model}
        quote = {**QUOTE, "price": price_at(model, "BTC", NOW)}
        provider = IndependentSignals(client)
        with patch("guardian_signals.price_at", wraps=price_at) as replay:
            result = provider(ORDER, THEORY, quote)
            self.assertEqual(replay.call_count, 61)
            times = [call.args[2] for call in replay.call_args_list]
            self.assertEqual(times[0], NOW - 900)
            self.assertEqual(times[-1], NOW)
            self.assertTrue(all(t <= NOW for t in times))
        self.assertTrue(result["replay_matches_quote"])
        provider(ORDER, THEORY, quote)
        self.assertEqual(client.get_market_model.call_count, 1)

    def test_mismatch_refreshes_once_then_fails(self):
        client = MagicMock()
        client.get_market_model.return_value = {"model": self.model()}
        with self.assertRaises(ValueError):
            IndependentSignals(client)(ORDER, THEORY, {**QUOTE, "price": 1})
        self.assertEqual(client.get_market_model.call_count, 2)

    def test_insufficient_history_fails(self):
        model = self.model()
        model["epoch_unix"] = NOW - 60
        client = MagicMock()
        client.get_market_model.return_value = {"model": model}
        with self.assertRaises(ValueError):
            IndependentSignals(client)(ORDER, THEORY, QUOTE)

    def test_mean_reversion_supported(self):
        theory = {**THEORY, "strategy": "mean_reversion"}
        result = check_theory_compliance(ORDER, theory, signals(None, None, None),
                    {"cash_usd": 100000, "symbol_quantity": 0, "symbol_avg_cost_usd": None}, 80000)
        self.assertEqual(result["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
