
import unittest
from guardian_rules import check_theory_compliance


class TestGuardianTheory(unittest.TestCase):

    def setUp(self):
        self.theory = {
            "id": 1,
            "version": 1,
            "status": "SUBMITTED",
            "selected_symbols": ["SOL"],
            "strategy": "momentum",
            "lookback_minutes": 30,
            "threshold_pct": 0.2,
            "position_fraction": 0.004,
            "stop_loss_pct": 2,
            "take_profit_pct": 3
        }

        self.portfolio = {
            "cash_usd": 100000,
            "symbol_quantity": 3,
            "symbol_avg_cost_usd": 200
        }

        self.order = {
            "symbol": "SOL",
            "action": "BUY",
            "quantity": 1,
            "theory_id": 1,
            "theory_version": 1
        }

        self.signals = {
            "lookback_return_pct": 0.3,
            "replay_matches_quote": True
        }

    def evaluate(self, price=220):
        return check_theory_compliance(
            self.order,
            self.theory,
            self.signals,
            self.portfolio,
            price
        )

    def test_valid_buy(self):
        self.assertEqual(
            self.evaluate()["decision"], "PASS"
        )

    def test_buy_without_signal(self):
        self.signals["lookback_return_pct"] = 0.1
        result = self.evaluate()
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "BUY_SIGNAL_NOT_TRIGGERED",
            result["rules_triggered"]
        )

    def test_buy_exceeds_position_fraction(self):
        self.order["quantity"] = 3
        result = self.evaluate()
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "POSITION_FRACTION_EXCEEDED",
            result["rules_triggered"]
        )

    
    def test_sell_without_exit_signal(self):
        self.order["action"] = "SELL"
        self.signals["lookback_return_pct"] = 0

        # Average purchase price is $200.
        # Current price of $202 represents only +1%.
        # No momentum SELL, stop loss, or take profit.
        result = self.evaluate(price=202)

        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "SELL_SIGNAL_NOT_TRIGGERED",
            result["rules_triggered"]
        )


    def test_stop_loss_sell(self):
        self.order["action"] = "SELL"
        self.signals["lookback_return_pct"] = 0
        self.assertEqual(
            self.evaluate(price=195)["decision"],
            "PASS"
        )

    def test_missing_signals(self):
        self.signals = None
        self.assertEqual(
            self.evaluate()["decision"], "ESCALATE"
        )

    def test_wrong_theory_version(self):
        self.order["theory_version"] = 2
        self.assertEqual(
            self.evaluate()["decision"], "ESCALATE"
        )


if __name__ == "__main__":
    unittest.main()

