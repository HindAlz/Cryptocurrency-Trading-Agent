"""Regression tests for BUY planning reserve; reuses offline HTTP/SDK fixtures."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import test_trader as fixtures
from guardian_live import review_pending_live
from trader import run_trader_cycle


class BuyReserveTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TraderContractTests()
        self.f.setUp()
        # Ask the fake provider to take the entire permitted BUY bound.
        original = self.f.fake_provider
        def take_max(**kwargs):
            import json
            context = json.loads(kwargs["messages"][1]["content"])
            if "execution_state" in context:
                self.f.reply_quantity = context["bounds"]["max_buy_quantity"]
            return original(**kwargs)
        self.f.create.side_effect = take_max
        # The trader fixture only implements APPROVE/ESCALATE replies; add the
        # documented BLOCKED response for these Guardian regression cases.
        original_http = self.f.boundary.request
        def with_block_response(method, url, **kwargs):
            import json
            from types import SimpleNamespace
            response = original_http(method, url, **kwargs)
            if method == "POST" and url.endswith("trade_review.php"):
                body = json.loads(kwargs["data"])
                if body["decision"] == "BLOCK":
                    return SimpleNamespace(status_code=200, headers={}, ok=True,
                        json=lambda: {"transaction_id": body["transaction_id"], "status": "BLOCKED"})
            return response
        self.f.guardian.session.request = with_block_response

    def tearDown(self):
        self.f.tearDown()

    def cycle(self, reserve=1):
        return run_trader_cycle(self.f.trader, self.f.llm, self.f.state,
            decision_time=datetime.fromtimestamp(fixtures.NOW, timezone.utc),
            buy_reserve_pct=reserve)

    def test_model_buy_budget_is_495_not_500(self):
        self.assertEqual(self.cycle()["status"], "PROPOSED")
        self.assertEqual(self.f.last_context["bounds"]["max_buy_budget_usd"], 495)
        self.assertEqual(self.f.last_context["theory"]["position_fraction"], .005)

    def test_small_price_rise_fits_reserve(self):
        self.cycle()
        # Price rises by 0.2% after proposal. Independent signal calculations
        # are supplied as test evidence only, to isolate the money check.
        self.f.boundary.override_price = self.f.boundary.price * 1.002
        def signals(order, theory, quote):
            return {"lookback_return_pct": .6, "mean_deviation_pct": 0,
                    "return_since_avg_cost_pct": None, "entry_triggered": True,
                    "exit_triggers": [], "replay_matches_quote": True}
        result = review_pending_live(self.f.guardian, self.f.llm, self.f.state,
                                     signals, clock=lambda: fixtures.NOW)
        self.assertEqual(result[0]["status"], "EXECUTED")

    def test_larger_price_rise_still_blocked_by_original_limit(self):
        self.cycle()
        self.f.boundary.override_price = self.f.boundary.price * 1.02
        def signals(order, theory, quote):
            return {"lookback_return_pct": .6, "mean_deviation_pct": 0,
                    "return_since_avg_cost_pct": None, "entry_triggered": True,
                    "exit_triggers": [], "replay_matches_quote": True}
        result = review_pending_live(self.f.guardian, self.f.llm, self.f.state,
                                     signals, clock=lambda: fixtures.NOW)
        self.assertEqual(result[0]["status"], "BLOCKED")
        self.assertIn("POSITION_FRACTION_EXCEEDED", self.f.boundary.posts[-1][1]["rules_triggered"])

    def test_without_reserve_small_price_rise_reproduces_block(self):
        self.cycle(reserve=0)
        self.f.boundary.override_price = self.f.boundary.price * 1.002
        def signals(order, theory, quote):
            return {"lookback_return_pct": .6, "mean_deviation_pct": 0,
                    "return_since_avg_cost_pct": None, "entry_triggered": True,
                    "exit_triggers": [], "replay_matches_quote": True}
        result = review_pending_live(self.f.guardian, self.f.llm, self.f.state,
                                     signals, clock=lambda: fixtures.NOW)
        self.assertEqual(result[0]["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
