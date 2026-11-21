"""Custom strategy tests: formula, completed samples, real shared integration.

Network and model boundaries use the existing offline trader fixtures.
"""
import json
import math
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import test_trader as fixtures
from custom_sma import (TITLE, STRATEGY_ID, validate_custom, _metrics,
                        calculate_custom, crossover_flags)
from guardian_live import review_pending_live
from guardian_signals import IndependentSignals, price_at
from guardian_rules import check_theory_compliance
from strategy import determine_triggers, validate_theory, policy_context
from trader import run_trader_cycle
from main import run_cycle


def theory():
    return {**deepcopy(fixtures.THEORY), "title": TITLE, "strategy": "custom",
            "lookback_minutes": 16, "threshold_pct": .05,
            "entry_rule": "Buy only after the buffered SMA crossover with no BTC held",
            "exit_rule": "Sell on downward buffered crossover or stop/profit"}


def evidence(prices):
    end = fixtures.NOW - 5
    return {"strategy_id": STRATEGY_ID, "quote_timestamp_unix": fixtures.NOW,
            "sample_times_unix": [end - i * 60 for i in range(15, -1, -1)],
            "sample_prices_usd": prices, **_metrics(prices, __import__('decimal').Decimal('.05'))}


class CustomCalculations(unittest.TestCase):
    def test_buy_crossover(self):
        self.assertEqual(crossover_flags(theory(), evidence([100]*15+[110])), (True, False))

    def test_sell_crossover(self):
        self.assertEqual(crossover_flags(theory(), evidence([100]*15+[90])), (False, True))

    def test_flat_has_no_crossover(self):
        self.assertEqual(crossover_flags(theory(), evidence([100]*16)), (False, False))

    def test_above_average_without_new_cross_is_not_buy(self):
        prices = list(range(100,116))
        self.assertEqual(crossover_flags(theory(), evidence(prices)), (False, False))

    def test_does_not_trust_model_or_caller_cross_flag(self):
        data = evidence([100]*16)
        data['buy_cross'] = True
        self.assertEqual(crossover_flags(theory(), data), (False, False))

    def test_requires_registered_title(self):
        with self.assertRaises(ValueError):
            validate_custom({**theory(), 'title': 'Different strategy'})

    def test_requires_btc_only(self):
        with self.assertRaises(ValueError):
            validate_custom({**theory(), 'selected_symbols': ['ETH']})

    def test_requires_16_minute_configuration(self):
        with self.assertRaises(ValueError):
            validate_custom({**theory(), 'lookback_minutes': 15})

    def test_wrong_sample_count_rejected(self):
        data = evidence([100]*16)
        data['sample_prices_usd'] = [100]*15
        with self.assertRaises(ValueError):
            crossover_flags(theory(), data)

    def test_forming_minute_data_rejected(self):
        data = evidence([100]*16)
        data['sample_times_unix'] = [fixtures.NOW - i*60 for i in range(15,-1,-1)]
        with self.assertRaises(ValueError):
            crossover_flags(theory(), data)

    def test_bad_price_rejected(self):
        for value in (True, 0, -1, float('nan'), float('inf')):
            prices = [100]*15 + [value]
            with self.subTest(value=value), self.assertRaises((ValueError, ArithmeticError)):
                crossover_flags(theory(), evidence(prices))

    def test_flat_entry_enforced_without_flag(self):
        signals = {'lookback_return_pct':0, 'mean_deviation_pct':0,
                   'return_since_avg_cost_pct':0, 'custom':evidence([100]*15+[110])}
        entry, exits = determine_triggers(theory(), signals, 1, require_flat_entry=False)
        self.assertFalse(entry)
        self.assertEqual(exits, [])

    def test_custom_exit_name_matches_agreement(self):
        signals = {'return_since_avg_cost_pct':0, 'custom':evidence([100]*15+[90])}
        _, exits = determine_triggers(theory(), signals, 1)
        self.assertIn('custom_exit', exits)

    def test_stop_loss_still_works_without_crossover(self):
        signals = {'return_since_avg_cost_pct':-2, 'custom':evidence([100]*16)}
        _, exits = determine_triggers(theory(), signals, 1)
        self.assertIn('stop_loss', exits)

    def test_take_profit_still_works_without_crossover(self):
        signals = {'return_since_avg_cost_pct':2, 'custom':evidence([100]*16)}
        _, exits = determine_triggers(theory(), signals, 1)
        self.assertIn('take_profit', exits)

    def test_guardian_rejects_buy_without_custom_cross(self):
        order = {'symbol':'BTC','action':'BUY','quantity':.001,'theory_id':7,'theory_version':2}
        signals = {'lookback_return_pct':5, 'replay_matches_quote':True,
                   'mean_deviation_pct':-5, 'custom':evidence([100]*16)}
        result = check_theory_compliance(order, theory(), signals,
                 {'cash_usd':100000,'symbol_quantity':0,'symbol_avg_cost_usd':None}, 80000)
        self.assertEqual(result['decision'], 'BLOCK')
        self.assertIn('BUY_SIGNAL_NOT_TRIGGERED', result['rules_triggered'])

    def test_missing_custom_evidence_escalates(self):
        order = {'symbol':'BTC','action':'BUY','quantity':.001,'theory_id':7,'theory_version':2}
        result = check_theory_compliance(order, theory(),
                {'lookback_return_pct':5, 'replay_matches_quote':True},
                {'cash_usd':100000,'symbol_quantity':0}, 80000)
        self.assertEqual(result['decision'], 'ESCALATE')


class CustomIntegration(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TraderContractTests()
        self.f.setUp()
        self.f.boundary.theory = theory()
        model = self.f.boundary.model
        model['assets']['BTC']['power_exponent'] = 0
        model['cycles'][0]['period_seconds'] = 1200
        model['assets']['BTC']['cycle_amplitudes'] = [.02]
        # Search only SYNTHETIC test descriptors for a deterministic crossing.
        # This is not tuning a live strategy or using a future quote.
        for i in range(629):
            model['assets']['BTC']['phase'] = i / 100
            quote = {'price':self.f.boundary.price, 'timestamp_unix':fixtures.NOW,
                     'model_version':model['version']}
            data = calculate_custom(model, 'BTC', quote, theory())
            if crossover_flags(theory(), data)[0]:
                break
        else:
            self.fail('Synthetic buy-cross fixture not found')

    def tearDown(self):
        self.f.tearDown()

    def cycle(self, **kwargs):
        return run_trader_cycle(self.f.trader, self.f.llm, self.f.state,
                   decision_time=datetime.fromtimestamp(fixtures.NOW, timezone.utc), **kwargs)

    def test_preview_contains_custom_evidence_in_agreed_context(self):
        self.assertEqual(self.cycle(submit=False)['status'], 'PREVIEW')
        self.assertEqual(self.f.last_context['context_version'], 1)
        self.assertIn('custom', self.f.last_context['signals'])
        self.assertTrue(self.f.last_context['signals']['entry_triggered'])
        self.assertEqual(self.f.last_context['signals']['custom']['strategy_id'], STRATEGY_ID)
        self.assertEqual(self.f.boundary.posts, [])

    def test_trader_and_guardian_compute_same_custom_evidence_independently(self):
        self.cycle(submit=False)
        trader_data = self.f.last_context['signals']['custom']
        quote = {'price':self.f.boundary.price,'timestamp_unix':fixtures.NOW,
                 'model_version':self.f.boundary.model['version']}
        guardian = IndependentSignals(self.f.guardian)
        signals = guardian({'symbol':'BTC'}, theory(), quote)
        self.assertEqual(signals['custom'], trader_data)

    def test_completed_samples_only(self):
        self.cycle(submit=False)
        data = self.f.last_context['signals']['custom']
        self.assertEqual(len(data['sample_prices_usd']), 16)
        self.assertLess(max(data['sample_times_unix']), fixtures.NOW)
        self.assertTrue(all(b-a==60 for a,b in zip(data['sample_times_unix'],data['sample_times_unix'][1:])))

    def test_custom_trade_runs_through_guardian_and_persistent_state(self):
        result = run_cycle('both', self.f.trader, self.f.guardian, self.f.llm, self.f.state,
                           submit=True, clock=lambda: fixtures.NOW)
        self.assertEqual(result['trader']['status'], 'PROPOSED')
        self.assertEqual(result['guardian_after'][0]['status'], 'EXECUTED')
        self.assertEqual(self.f.state.get_unresolved_intents(), [])
        self.assertEqual(self.f.state.get_theory_snapshot(7,2)['strategy'], 'custom')
        self.assertEqual([p[0] for p in self.f.boundary.posts], ['trade_propose.php','trade_review.php'])

    def test_existing_holding_prevents_buy_even_without_cli_option(self):
        self.f.boundary.holdings = [{'symbol':'BTC','quantity':.001,'avg_cost':self.f.boundary.price}]
        self.assertEqual(self.cycle()['status'], 'HOLD')
        self.assertEqual(self.f.boundary.posts, [])

    def test_replay_mismatch_still_holds(self):
        self.f.boundary.override_price = 1
        self.assertEqual(self.cycle()['status'], 'HOLD')
        self.assertEqual(self.f.boundary.posts, [])

    def test_unregistered_custom_theory_holds(self):
        self.f.boundary.theory['title'] = 'Another custom theory'
        self.assertEqual(self.cycle()['status'], 'HOLD')
        self.assertEqual(self.f.boundary.posts, [])

    def test_model_failure_still_holds(self):
        self.f.create.side_effect = TimeoutError()
        self.assertEqual(self.cycle()['status'], 'HOLD')
        self.assertEqual(self.f.boundary.posts, [])

    def test_policy_limit_still_applies(self):
        self.f.reply_quantity = 10
        self.assertEqual(self.cycle()['status'], 'HOLD')
        self.assertEqual(self.f.boundary.posts, [])


if __name__ == '__main__':
    unittest.main()
