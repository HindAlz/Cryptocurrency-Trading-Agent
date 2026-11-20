
import unittest

from guardian import guardian_decision_dry_run
from llm_adapter import create_adapter
from test_guardian_llm import TestGuardianLLM


class TestGuardianOrchestration(unittest.TestCase):

    def setUp(self):
        # Reuse the test fixtures from your earlier LLM tests.
        fixtures = TestGuardianLLM()
        fixtures.setUp()

        self.order = fixtures.order
        self.policy = fixtures.policy
        self.portfolio = fixtures.portfolio
        self.theory = fixtures.theory
        self.signals = fixtures.signals
        self.llm = create_adapter(provider="mock")

    def evaluate(self):
        return guardian_decision_dry_run(
            self.order,
            self.policy,
            220,
            self.portfolio,
            self.theory,
            self.signals,
            self.llm
        )

    def test_oversized_order(self):
        self.order["quantity"] = 10

        result = self.evaluate()

        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["stage"], "HARD_RULES")

    def test_invalid_momentum(self):
        self.signals["lookback_return_pct"] = 0.1

        result = self.evaluate()

        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["stage"], "THEORY_CHECK")

    def test_safe_order_reaches_llm(self):
        result = self.evaluate()

        # Mock mode always escalates by default.
        self.assertEqual(result["decision"], "ESCALATE")
        self.assertEqual(result["stage"], "LLM")


if __name__ == "__main__":
    unittest.main()

