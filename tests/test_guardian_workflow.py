
import unittest

from guardian import review_pending


class FakeLedgerClient:
    """Simulated Ledger API for local Guardian testing."""

    def get_pending_orders(self, limit=2):
        return {
            "pending": [{
                "transaction_id": 42,
                "symbol": "ETH",
                "action": "BUY",
                "requested_quantity": 1,
                "requested_price": 2000,
                "reasoning": "Positive momentum.",
                "theory_id": 7,
                "theory_version": 1
            }],
            "has_more": False
        }

    def get_policy(self):
        return {
            "allowed_symbols": ["ETH"],
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
            "cash_balance": 10000,
            "holdings": []
        }

    def get_prices(self, symbols):
        return {
            "prices": [{
                "symbol": symbols,
                "price": 2000,
                "timestamp_unix": 1795165200,
                "model_version": "test"
            }]
        }


class TestGuardianWorkflow(unittest.TestCase):

    def test_oversized_eth_order(self):
        client = FakeLedgerClient()

        results = review_pending(
            client,
            llm=None,
            state=None
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["decision"], "BLOCK"
        )
        self.assertIn(
            "TRANSACTION_LIMIT_EXCEEDED",
            results[0]["rules_triggered"]
        )


if __name__ == "__main__":
    unittest.main()
