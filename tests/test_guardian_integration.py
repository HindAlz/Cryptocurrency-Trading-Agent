
import unittest
from guardian import review_pending


class FakeLedger:
    def get_pending_orders(self, limit=2):
        return {
            "pending": [{
                "transaction_id": 42,
                "symbol": "SOL",
                "action": "BUY",
                "requested_quantity": 1,
                "requested_price": 220,
                "reasoning": "Momentum opportunity",
                "theory_id": 1,
                "theory_version": 1
            }]
        }

    def get_policy(self):
        return {
            "allowed_symbols": ["SOL"],
            "restricted_actions": ["WITHDRAW"],
            "max_transaction_amount": 1000,
            "escalation_threshold": 500,
            "max_trades_per_hour": 5,
            "max_trades_per_day": 20,
            "trades_last_hour": 0,
            "trades_last_day": 0
        }

    def get_portfolio(self):
        return {
            "cash_balance": 100000,
            "holdings": []
        }

    def get_prices(self, symbols):
        return {
            "prices": [{
                "symbol": symbols,
                "price": 220,
                "timestamp_unix": 1795165200,
                "model_version": "test"
            }]
        }


class FakeState:
    def get_theory_snapshot(self, theory_id, theory_version):
        return {
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


def fake_signals(order, theory, quote):
    # Only for testing: not verified real market evidence.
    return {
        "lookback_return_pct": 0.1,
        "replay_matches_quote": True
    }


class TestGuardianIntegration(unittest.TestCase):

    def test_buy_without_momentum(self):
        result = review_pending(
            client=FakeLedger(),
            state=FakeState(),
            signal_provider=fake_signals
        )

        self.assertEqual(result[0]["decision"], "BLOCK")
        self.assertIn(
            "BUY_SIGNAL_NOT_TRIGGERED",
            result[0]["rules_triggered"]
        )

    def test_missing_signals_escalates(self):
        result = review_pending(
            client=FakeLedger(),
            state=FakeState()
        )

        self.assertEqual(
            result[0]["decision"], "ESCALATE"
        )


if __name__ == "__main__":
    unittest.main()

