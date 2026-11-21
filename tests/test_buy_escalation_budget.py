"""Offline BUY planning checks: no real API or model calls."""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_trader as fixture


class BuyEscalationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TraderContractTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.boundary.cash = 200000
        self.f.boundary.policy['escalation_threshold'] = 500
        def choose_maximum(**kwargs):
            context = json.loads(kwargs['messages'][1]['content'])
            self.f.reply_quantity = context['bounds']['max_buy_quantity']
            return self.f.fake_provider(**kwargs)
        self.f.create.side_effect = choose_maximum

    def test_threshold_caps_buy_planning_before_default_reserve(self):
        result = self.f.cycle(submit=False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(self.f.last_context['bounds']['max_buy_budget_usd'], 495.)
        amount = Decimal(result['proposal']['quantity']) * Decimal(str(self.f.boundary.price))
        self.assertLessEqual(amount, Decimal('495'))
        self.assertEqual(self.f.boundary.policy['escalation_threshold'], 500)
        self.assertEqual(self.f.boundary.posts, [])

    def test_smaller_theory_budget_stays_authoritative(self):
        self.f.boundary.cash = 20000
        result = self.f.cycle(submit=False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(self.f.last_context['bounds']['max_buy_budget_usd'], 99.)

    def test_smaller_transaction_ceiling_stays_authoritative(self):
        self.f.boundary.policy['max_transaction_amount'] = 200
        result = self.f.cycle(submit=False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(self.f.last_context['bounds']['max_buy_budget_usd'], 198.)


if __name__ == '__main__':
    unittest.main()
