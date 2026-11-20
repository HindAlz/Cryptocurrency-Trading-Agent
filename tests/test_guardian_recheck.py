
import unittest
from guardian import final_recheck


class FakeLedger:
    def __init__(self, price=260, cash=500, limit=1000):
        self.price = price
        self.cash = cash
        self.limit = limit

    def get_policy(self):
        return {
            "allowed_symbols": ["SOL"],
            "restricted_actions": ["WITHDRAW"],
            "max_transaction_amount": self.limit,
            "escalation_threshold": 500,
            "max_trades_per_hour": 5,
            "max_trades_per_day": 20,
            "trades_last_hour": 0,
            "trades_last_day": 0
        }

    def get_prices(self, symbols):
        return {
            "prices": [{
                "symbol": symbols,
                "price": self.price,
                "timestamp_unix": 1795165200,
                "model_version": "test"
            }]
        }

    def get_portfolio(self):
        return {
            "cash_balance": self.cash,
            "holdings": []
        }


def fake_signals(order, theory, quote):
    # Synthetic evidence for local testing ONLY.
    return {
        "lookback_return_pct": 0.3,
        "replay_matches_quote": True
    }


class TestFinalRecheck(unittest.TestCase):

    def setUp(self):
        self.order = {
            "symbol": "SOL",
            "action": "BUY",
            "quantity": 2,
            "reasoning": "Momentum signal.",
            "theory_id": 1,
            "theory_version": 1
        }

        self.theory = {
            "id": 1,
            "version": 1,
            "status": "SUBMITTED",
            "selected_symbols": ["SOL"],
            "strategy": "momentum",
            "lookback_minutes": 30,
            "threshold_pct": 0.2,
            "position_fraction": 1,
            "stop_loss_pct": 2,
            "take_profit_pct": 3
        }

    def test_price_increase_blocks_buy(self):
        client = FakeLedger(price=260, cash=500)

        result = final_recheck(
            client,
            self.order,
            {"decision": "APPROVE"},
            self.theory,
            signal_provider=fake_signals
        )

        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "INSUFFICIENT_CASH",
            result["rules_triggered"]
        )

    def test_reduced_quantity_passes(self):
        client = FakeLedger(price=260, cash=500)

        result = final_recheck(
            client,
            self.order,
            {
                "decision": "MODIFY",
                "modified_quantity": 1
            },
            self.theory,
            signal_provider=fake_signals
        )

        self.assertEqual(result["decision"], "PASS")

    def test_changed_policy_blocks(self):
        client = FakeLedger(
            price=220, cash=500, limit=200
        )

        result = final_recheck(
            client,
            self.order,
            {"decision": "APPROVE"},
            self.theory,
            signal_provider=fake_signals
        )

        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "TRANSACTION_LIMIT_EXCEEDED",
            result["rules_triggered"]
        )

    def test_missing_fresh_signals_escalates(self):
        client = FakeLedger(price=220, cash=500)

        result = final_recheck(
            client,
            self.order,
            {"decision": "APPROVE"},
            self.theory
        )

        self.assertEqual(result["decision"], "ESCALATE")

    def test_modify_cannot_increase_quantity(self):
        client = FakeLedger()

        result = final_recheck(
            client,
            self.order,
            {
                "decision": "MODIFY",
                "modified_quantity": 3
            },
            self.theory,
            signal_provider=fake_signals
        )

        self.assertEqual(result["decision"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
