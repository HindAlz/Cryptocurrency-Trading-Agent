
import unittest

from guardian import review_mock_order
from llm_adapter import create_adapter


class TestGuardianLLM(unittest.TestCase):

    def setUp(self):
        self.llm = create_adapter(provider="mock")

        self.policy = {
            "allowed_symbols": ["SOL"],
            "restricted_actions": ["WITHDRAW"],
            "max_transaction_amount": 1000,
            "max_trades_per_hour": 5,
            "max_trades_per_day": 20,
            "escalation_threshold": 500,
            "trades_last_hour": 0,
            "trades_last_day": 0
        }

        self.portfolio = {
            "cash_usd": 100000,
            "symbol_quantity": 0,
            "symbol_avg_cost_usd": 0
        }

        self.theory = {
            "id": 1,
            "version": 1,
            "status": "SUBMITTED",
            "title": "SOL Momentum",
            "hypothesis": "Test short-term momentum.",
            "entry_rule": "Buy on positive momentum.",
            "exit_rule": "Sell on negative momentum.",
            "risk_rule": "Limit position size.",
            "selected_symbols": ["SOL"],
            "strategy": "momentum",
            "lookback_minutes": 30,
            "threshold_pct": 0.2,
            "position_fraction": 0.004,
            "stop_loss_pct": 2,
            "take_profit_pct": 3
        }

        self.signals = {
            "lookback_return_pct": 0.3,
            "mean_deviation_pct": 0,
            "return_since_avg_cost_pct": 0,
            "entry_triggered": True,
            "exit_triggers": [],
            "replay_matches_quote": True
        }

        self.order = {
            "symbol": "SOL",
            "action": "BUY",
            "quantity": 1,
            "reasoning": "Positive momentum signal.",
            "theory_id": 1,
            "theory_version": 1
        }

    def evaluate(self):
        return review_mock_order(
            self.order,
            self.policy,
            220,
            self.portfolio,
            self.theory,
            self.signals,
            self.llm
        )

    def test_safe_order_reaches_mock_llm(self):
        result = self.evaluate()

        # Default mock mode escalates rather than pretending
        # that a real LLM has approved the transaction.
        self.assertEqual(result["decision"], "ESCALATE")
        self.assertIn("mock_mode", result["rules_triggered"])

    def test_oversized_order_blocked_before_llm(self):
        self.order["quantity"] = 10
        result = self.evaluate()

        self.assertEqual(result["decision"], "BLOCK")
        self.assertIn(
            "TRANSACTION_LIMIT_EXCEEDED",
            result["rules_triggered"]
        )


if __name__ == "__main__":
    unittest.main()
