
"""Local integration tests only. No network calls or real credentials."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from llm_adapter import TraderDecision, validate_context
from state_store import StateStore
from trader import run_trader_cycle

NOW = datetime(2026,11,20,12,0,tzinfo=timezone.utc)
POINTS = [
  {'timestamp':(NOW-timedelta(minutes=20)).isoformat(),'price':100.0},
  {'timestamp':(NOW-timedelta(minutes=10)).isoformat(),'price':95.0},
  {'timestamp':NOW.isoformat(),'price':80.0},
]
THEORY = {'id':11,'version':1,'status':'ACTIVE','title':'XRP mean reversion','hypothesis':'Test',
          'entry_rule':'Buy when below mean','exit_rule':'Sell on exit signal',
          'risk_rule':'Stop loss','selected_symbols':['XRP'],'strategy':'mean_reversion',
          'lookback_minutes':10,'threshold_pct':1,'position_fraction':0.1,
          'stop_loss_pct':3,'take_profit_pct':5}
POLICY = {'allowed_symbols':['XRP'],'restricted_actions':[],
          'max_transaction_amount_usd':200,'max_trades_per_hour':5,
          'max_trades_per_day':20,'escalation_threshold_usd':300}

class FakeClient:
    def __init__(self): self.posted=[]
    def get_policy(self): return dict(POLICY)
    def get_theory(self): return dict(THEORY)
    def get_prices(self,symbol):
        return {symbol:{'price_usd':80,'timestamp_unix':int(NOW.timestamp()),'model_version':'demo'}}
    def get_portfolio(self):
        return {'cash_usd':1000,'positions':{'XRP':{'quantity':0,'avg_cost_usd':None}},
                'trade_counts':{'executed_last_hour':0,'executed_last_day':0}}
    def get_news(self): return {'news':[]}
    def get_market_model(self): return {'historical_prices':POINTS}
    def propose_trade(self,order):
        self.posted.append(order)
        return {'transaction_id':123}

class FakeLLM:
    def __init__(self,action='BUY'): self.action=action
    def trader_decision(self, context):
        validate_context(context,'trader')
        return TraderDecision('XRP',self.action,1 if self.action=='BUY' else 0,'Test reason')

class Member2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.state=StateStore(str(Path(self.tmp.name)/'state.sqlite'),'xrp-test')
        self.client=FakeClient()
    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()
    def run_cycle(self,llm=None,client=None,**kwargs):
        return run_trader_cycle(client or self.client,llm or FakeLLM(),self.state,
                                decision_time=NOW,quantity_decimal_places=4,
                                minimum_settled_amount_usd='1',**kwargs)
    def test_proposal_attached_to_state(self):
        result=self.run_cycle()
        self.assertEqual(result['status'],'PROPOSED')
        self.assertEqual(result['transaction_id'],123)
        self.assertEqual(result['proposal']['theory_id'],11)
        self.assertEqual(len(self.client.posted),1)
        self.assertEqual(len(self.state.get_unresolved_intents()),1)
    def test_hold_no_post(self):
        result=self.run_cycle(llm=FakeLLM('HOLD'))
        self.assertEqual(result['status'],'HOLD')
        self.assertEqual(self.client.posted,[])
    def test_second_cycle_blocked(self):
        self.run_cycle()
        result=self.run_cycle()
        self.assertEqual(result['status'],'HOLD')
        self.assertEqual(len(self.client.posted),1)
    def test_missing_course_minimum_blocks(self):
        result=run_trader_cycle(self.client,FakeLLM(),self.state,decision_time=NOW)
        self.assertEqual(result['status'],'HOLD')
        self.assertEqual(self.client.posted,[])
    def test_wrong_currency_blocks(self):
        result=run_trader_cycle(self.client,FakeLLM(),self.state,symbol='ADA',decision_time=NOW)
        self.assertEqual(result['status'],'HOLD')
    def test_missing_replay_blocks(self):
        self.client.get_market_model=lambda: {'model_version':'demo'}
        result=self.run_cycle()
        self.assertEqual(result['status'],'HOLD')
        self.assertEqual(self.client.posted,[])
    def test_uncertain_post_is_not_retried(self):
        def fail(order): raise TimeoutError()
        self.client.propose_trade=fail
        result=self.run_cycle()
        self.assertEqual(result['status'],'UNRESOLVED')
        self.assertEqual(len(self.state.get_unresolved_intents()),1)
        self.assertEqual(self.run_cycle()['status'],'HOLD')

class MomentumTests(unittest.TestCase):
    def setUp(self):
        from strategy import determine_triggers
        self.determine_triggers = determine_triggers
        self.theory = dict(THEORY, strategy='momentum', threshold_pct=0.5,
                           position_fraction=0.05, stop_loss_pct=2, take_profit_pct=4,
                           lookback_minutes=30)

    def test_momentum_buy(self):
        entry, exits = self.determine_triggers(
            self.theory, {'lookback_return_pct': 0.8,
                          'return_since_avg_cost_pct': None}, 0)
        self.assertTrue(entry)
        self.assertEqual(exits, [])

    def test_momentum_below_threshold_holds(self):
        entry, exits = self.determine_triggers(
            self.theory, {'lookback_return_pct': 0.2,
                          'return_since_avg_cost_pct': None}, 0)
        self.assertFalse(entry)
        self.assertEqual(exits, [])

    def test_momentum_sell(self):
        entry, exits = self.determine_triggers(
            self.theory, {'lookback_return_pct': -0.8,
                          'return_since_avg_cost_pct': -1}, 10)
        self.assertFalse(entry)
        self.assertIn('momentum_exit', exits)

    def test_momentum_stop_loss(self):
        entry, exits = self.determine_triggers(
            self.theory, {'lookback_return_pct': 0.1,
                          'return_since_avg_cost_pct': -2.5}, 10)
        self.assertFalse(entry)
        self.assertIn('stop_loss', exits)

    def test_momentum_take_profit(self):
        entry, exits = self.determine_triggers(
            self.theory, {'lookback_return_pct': 0.1,
                          'return_since_avg_cost_pct': 4.5}, 10)
        self.assertFalse(entry)
        self.assertIn('take_profit', exits)

if __name__=='__main__': unittest.main()
