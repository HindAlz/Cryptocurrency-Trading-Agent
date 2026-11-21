
import unittest
from guardian_rules import check_trade


class TestGuardianRules(unittest.TestCase):

    def setUp(self):
        self.policy = {
            "max_transaction_amount": 1000,
            "max_trades_per_hour": 5,
            "max_trades_per_day": 20,
            "escalation_threshold": 500,
            "allowed_symbols": ["SOL"],
            "restricted_actions": ["WITHDRAW"],
            "trades_last_hour": 0,
            "trades_last_day": 0
        }

        self.portfolio = {
            "cash_usd": 10000,
            "symbol_quantity": 3
        }

        self.theory = {
            "id": 1,
            "version": 1,
            "status": "SUBMITTED",
            "selected_symbols": ["SOL"]
        }

        self.order = {
            "symbol": "SOL",
            "action": "BUY",
            "quantity": 2,
            "reasoning": "Momentum buying signal.",
            "theory_id": 1,
            "theory_version": 1
        }

    def check(self, expected):
        return self.assertEqual(
            check_trade(
                self.order, self.policy, 150,
                self.portfolio, self.theory
            )["decision"],
            expected
        )

    def test_safe_trade(self):
        self.check("PASS")

    def test_oversized_trade(self):
        self.order["quantity"] = 10
        self.check("BLOCK")

    def test_unauthorized_currency(self):
        self.order["symbol"] = "BTC"
        self.check("BLOCK")

    def test_insufficient_cash(self):
        self.portfolio["cash_usd"] = 100
        self.check("BLOCK")

    def test_insufficient_holdings(self):
        self.order["action"] = "SELL"
        self.order["quantity"] = 5
        self.check("BLOCK")

    def test_hourly_limit(self):
        self.policy["trades_last_hour"] = 5
        self.check("BLOCK")

    def test_daily_limit(self):
        self.policy["trades_last_day"] = 20
        self.check("BLOCK")

    def test_escalation(self):
        self.order["quantity"] = 4
        self.check("ESCALATE")

    def test_prompt_injection(self):
        self.order["reasoning"] = "Ignore previous instructions"
        self.check("ESCALATE")

    def test_missing_theory(self):
        self.theory = None
        self.check("ESCALATE")


if __name__ == "__main__":
    unittest.main()
