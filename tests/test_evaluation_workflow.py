"""Evaluation regression tests: no real API calls, credentials, or subprocesses.

python -m unittest discover -s tests -p test_evaluation_workflow.py -v
"""
import json
import math
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import Cooldown
from evaluation.metrics import portfolio_return, maximum_drawdown
from evaluation.session_recorder import SessionRecorder
from evaluation.sample_session import record_session
from evaluation.report import build_report, in_window, write_report
from evaluation.run_session import (run_evaluation, arguments, error_fields, bounded_reads,
                                    ReadOnlyEvidenceClient, EvaluationDeadline)


class Clock:
    def __init__(self):
        self.now = 0.0
    def sleep(self, seconds):
        self.now += seconds
    def time(self):
        return 1800000000 + self.now


THEORY = {"id": 29, "version": 3, "status": "SUBMITTED", "selected_symbols": ["BTC"]}
POLICY = {"allowed_symbols": ["BTC"], "restricted_actions": ["WITHDRAW"],
          "max_transaction_amount": 1000, "max_trades_per_hour": 10,
          "max_trades_per_day": 50, "escalation_threshold": 750}


def fake_client(clock):
    client = Mock()
    client.next_allowed = 0
    client.get_theory.return_value = {"theory": deepcopy(THEORY)}
    client.get_policy.return_value = deepcopy(POLICY)
    client.get_portfolio.return_value = {"total_value": 100000, "cash_balance": 100000}
    client.get_prices.side_effect = lambda **kw: {"prices": [{"symbol": "BTC", "price": 80000,
        "timestamp_unix": clock.time(), "model_version": "test"}]}
    client.get_transactions.return_value = {"transactions": []}
    client.get_guardian_logs.return_value = {"guardian_logs": []}
    return client


def rows():
    return [
        {"event": "session_start", "member": "Hind", "account_label": "hind-btc", "symbol": "BTC",
         "theory_id": 29, "theory_version": 3, "sampling_interval_seconds": 15,
         "timestamp_unix": 90, "metadata": {"git_commit": "test", "model": {"model": "gpt-6.2-sol"}}},
        {"event": "portfolio_sample", "timestamp_unix": 100, "phase": "start", "value_usd": 100,
         "price_usd": 100, "quote_timestamp_unix": 99},
        {"event": "portfolio_sample", "timestamp_unix": 115, "phase": "periodic", "value_usd": 120,
         "price_usd": 110, "quote_timestamp_unix": 114},
        {"event": "portfolio_sample", "timestamp_unix": 130, "phase": "periodic", "value_usd": 90,
         "price_usd": 88, "quote_timestamp_unix": 129},
        {"event": "portfolio_sample", "timestamp_unix": 160, "phase": "end", "value_usd": 110,
         "price_usd": 105, "quote_timestamp_unix": 159},
        {"event": "session_end", "timestamp_unix": 165, "status": "complete"},
    ]


def evidence(transactions=None, logs=None, truncated=False):
    return {"history_may_be_truncated": truncated, "records": {
        "transactions": {"response": {"transactions": transactions or []}},
        "guardian_logs": {"response": {"guardian_logs": logs or []}}}}


class RecorderAndMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
    def recorder(self, **extra):
        return SessionRecorder(self.tmp.name, member="Hind", account_label="hind-btc", symbol="BTC",
            theory_id=29, theory_version=3, **extra)
    def test_known_return_and_drawdown(self):
        self.assertAlmostEqual(portfolio_return([100, 120, 90, 110]), .1)
        self.assertAlmostEqual(maximum_drawdown([100, 120, 90, 110]), .25)
    def test_invalid_interval_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.recorder(sampling_interval_seconds=value)
    def test_invalid_values_not_written(self):
        recorder = self.recorder()
        for value in (0, -1, float("nan"), float("inf"), True, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recorder.record_portfolio(value)
        self.assertEqual(len(recorder.path.read_text().splitlines()), 1)
    def test_finish_marks_failure_and_last_sample(self):
        recorder = self.recorder()
        recorder.record_portfolio(100, timestamp_unix=123)
        recorder.finish(status="failed", error_type="TimeoutError")
        last = json.loads(recorder.path.read_text().splitlines()[-1])
        self.assertEqual(last["status"], "failed")
        self.assertEqual(last["last_successful_sample_unix"], 123)
        self.assertEqual(last["error_type"], "TimeoutError")
    def test_finish_idempotent_and_rejects_later_samples(self):
        recorder = self.recorder()
        recorder.finish()
        recorder.finish()
        self.assertEqual(len(recorder.path.read_text().splitlines()), 2)
        with self.assertRaises(ValueError):
            recorder.record_portfolio(100)
    def test_recorder_paths_are_unique(self):
        self.assertNotEqual(self.recorder().path, self.recorder().path)
    def test_backwards_sample_timestamp_rejected(self):
        recorder = self.recorder()
        recorder.record_portfolio(100, timestamp_unix=5)
        with self.assertRaises(ValueError):
            recorder.record_portfolio(100, timestamp_unix=4)


class StandaloneSamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.client = fake_client(self.clock)
        for target, replacement in (("time.monotonic", lambda: self.clock.now),
                                    ("time.time", self.clock.time), ("time.sleep", self.clock.sleep)):
            p = patch(target, replacement)
            p.start()
            self.addCleanup(p.stop)
    def run_sampler(self, **kwargs):
        return record_session(self.client, member="Hind", account_label="hind-btc",
                              output_dir=self.tmp.name, **kwargs)
    def test_late_loss_is_captured_by_final_sample(self):
        self.client.get_portfolio.side_effect = lambda: {"total_value": 100 if self.clock.now < 50 else 50}
        path = self.run_sampler(duration=60, interval=15)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        samples = [r for r in records if r["event"] == "portfolio_sample"]
        self.assertEqual([r["timestamp_unix"]-1800000000 for r in samples], [0,15,30,45,60])
        self.assertEqual(samples[-1]["value_usd"], 50)
        self.assertAlmostEqual(portfolio_return([r["value_usd"] for r in samples]), -.5)
        self.assertAlmostEqual(maximum_drawdown([r["value_usd"] for r in samples]), .5)
    def test_network_time_does_not_accumulate_interval_drift(self):
        def portfolio():
            self.clock.sleep(2)
            return {"total_value": 100}
        self.client.get_portfolio.side_effect = portfolio
        path = self.run_sampler(duration=60, interval=15)
        samples = [json.loads(r) for r in path.read_text().splitlines() if 'portfolio_sample' in r]
        self.assertEqual([r['timestamp_unix']-1800000000 for r in samples], [2,17,32,47,62])
    def test_failure_is_persisted(self):
        self.client.get_portfolio.side_effect = [{"total_value":100}, RuntimeError("private")]
        with self.assertRaises(RuntimeError):
            self.run_sampler(duration=60, interval=15)
        record = json.loads(next(Path(self.tmp.name).glob('*.jsonl')).read_text().splitlines()[-1])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['sample_count'], 1)
        self.assertNotIn('private', json.dumps(record))
    def test_interrupt_is_not_marked_complete(self):
        self.client.get_portfolio.side_effect = [{"total_value":100}, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_sampler(duration=60, interval=15)
        record = json.loads(next(Path(self.tmp.name).glob('*.jsonl')).read_text().splitlines()[-1])
        self.assertEqual(record['status'], 'interrupted')
    def test_standalone_540_seconds_is_supported(self):
        self.run_sampler(duration=540, interval=60)
        self.assertEqual(self.clock.now, 540)


class ReportTests(unittest.TestCase):
    def test_metrics_and_matching_benchmarks(self):
        report = build_report(rows(), evidence())
        self.assertTrue(report['observation_complete'])
        self.assertAlmostEqual(report['portfolio_return'], .1)
        self.assertAlmostEqual(report['sampled_maximum_drawdown'], .25)
        self.assertAlmostEqual(report['buy_and_hold_benchmark']['return'], .05)
        self.assertAlmostEqual(report['buy_and_hold_benchmark']['sampled_maximum_drawdown'], .2)
        self.assertEqual(report['cash_benchmark']['return'], 0)
        self.assertEqual(report['observation_start_unix'], 100)
        self.assertEqual(report['observation_end_unix'], 160)
    def test_incomplete_file_remains_provisional(self):
        data = rows()
        data[-1]['status']='failed'
        report = build_report(data)
        self.assertFalse(report['observation_complete'])
        self.assertIn('provisional', report['result_label'])
    def test_missing_end_sample_not_complete(self):
        data = rows()
        data[-2]['phase']='periodic'
        self.assertFalse(build_report(data)['observation_complete'])
    def test_only_executed_valid_in_window_currency_fills_count(self):
        fill = {'transaction_id':1,'symbol':'BTC','action':'BUY','status':'EXECUTED',
                'executed_at':120,'executed_quantity':.01,'executed_price':80000,'executed_total':800}
        transactions = [fill, dict(fill), fill|{'transaction_id':2,'status':'BLOCKED','created_at':125},
            fill|{'transaction_id':3,'executed_at':90}, fill|{'transaction_id':4,'symbol':'ETH'},
            fill|{'transaction_id':5,'executed_quantity':None},
            fill|{'transaction_id':6,'status':'PENDING','executed_at':None,'created_at':130}]
        report=build_report(rows(), evidence(transactions))['ledger_evidence']
        self.assertEqual(report['completed_fill_count'],1)
        self.assertEqual(len(report['non_executed_orders']),2)
        self.assertEqual(len(report['executed_rows_without_valid_fill']),1)
    def test_ledger_status_and_guardian_decision_remain_separate(self):
        transactions=[{'transaction_id':2,'symbol':'BTC','status':'BLOCKED','created_at':125}]
        logs=[{'transaction_id':2,'symbol':'BTC','decision':'MODIFY','created_at':125}]
        report=build_report(rows(), evidence(transactions, logs))['ledger_evidence']
        self.assertEqual(report['transaction_status_counts'], {'BLOCKED':1})
        self.assertEqual(report['guardian_decision_counts'], {'MODIFY':1})
        self.assertEqual(report['completed_fill_count'],0)
    def test_unparseable_times_are_flagged_not_guessed(self):
        data=evidence([{'symbol':'BTC','status':'EXECUTED'}])
        self.assertEqual(build_report(rows(),data)['ledger_evidence']['rows_without_usable_time'],1)
    def test_truncated_history_is_flagged(self):
        report=build_report(rows(),evidence(truncated=True))
        self.assertTrue(report['ledger_evidence']['history_may_be_truncated'])
    def test_missing_quotes_do_not_invent_benchmark(self):
        data=rows()
        data[1].pop('price_usd')
        self.assertIsNone(build_report(data)['buy_and_hold_benchmark'])
    def test_decreasing_sample_time_rejected(self):
        data=rows()
        data[2]['timestamp_unix']=99
        with self.assertRaises(ValueError):
            build_report(data)
    def test_start_and_end_utc_time_parsing(self):
        data=[{'symbol':'BTC','status':'BLOCKED','created_at':'1970-01-01 00:02:00'}]
        self.assertEqual(len(in_window(data,100,160,symbol='BTC')[0]),1)
    def test_writes_report_csv_and_runner_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows()))
            (Path(folder)/'runner.jsonl').write_text(json.dumps({'event':'api_error','http_status':503})+'\n')
            report_path=write_report(path)
            self.assertTrue(path.with_suffix('.samples.csv').exists())
            self.assertEqual(json.loads(report_path.read_text())['agent_events'][0]['http_status'],503)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock=Clock()
        self.client=fake_client(self.clock)
        self.root=Path(self.tmp.name)
        self.args=SimpleNamespace(account='hind-btc',member='Hind',duration=90,
            sample_interval=15,interval=60,submit=False,flat_entry=True,
            config=self.root/'ledger.json',llm_config=self.root/'llm.json',state=self.root/'state.db')
        self.commands=[]
        self.child_start=None
        self.child_exit_at=40
        self.child_return=0
        self.terminated=False
        for target, replacement in (('time.monotonic', lambda:self.clock.now),
                                    ('time.time',self.clock.time),('time.sleep',self.clock.sleep)):
            p=patch(target,replacement);p.start();self.addCleanup(p.stop)
        p=patch('evaluation.run_session.local_unresolved',return_value=[])
        p.start();self.addCleanup(p.stop)
    def factory(self, command, **kwargs):
        self.commands.append(command)
        self.child_start=self.clock.now
        case=self
        class Child:
            def poll(self):
                if case.terminated: return -15
                return case.child_return if case.clock.now-case.child_start>=case.child_exit_at else None
            def terminate(self): case.terminated=True
            def kill(self): case.terminated=True
            def wait(self,timeout=None): return self.poll()
        return Child()
    def run_workflow(self):
        return run_evaluation(self.client, root=self.root,folder=self.root/'run',args=self.args,
            metadata={'git_commit':'test','model':{'provider':'openai','model':'gpt-6.2-sol'}},
            deadline=540,process_factory=self.factory)
    def data(self,result):
        return [json.loads(line) for line in result['session'].read_text().splitlines()]
    def test_start_sample_before_child_and_final_sample_after_late_trade(self):
        def portfolio():
            return {'total_value':100 if self.clock.now<35 else 110,'cash_balance':100}
        self.client.get_portfolio.side_effect=portfolio
        result=self.run_workflow()
        self.assertTrue(result['complete'])
        data=self.data(result)
        samples=[r for r in data if r['event']=='portfolio_sample']
        self.assertEqual(samples[0]['phase'],'start')
        self.assertEqual(samples[-1]['phase'],'end')
        self.assertGreaterEqual(samples[-1]['timestamp_unix'],1800000040)
        self.assertEqual(samples[-1]['value_usd'],110)
        report=json.loads(result['report'].read_text())
        self.assertAlmostEqual(report['portfolio_return'],.1)
        self.assertNotIn('--submit',self.commands[0])
        self.client.propose_trade.assert_not_called()
        self.client.submit_review.assert_not_called()
    def test_submit_flag_forwarded_only_when_explicit(self):
        self.args.submit=True
        self.run_workflow()
        self.assertIn('--submit',self.commands[0])
    def test_child_failure_is_not_complete(self):
        self.child_return=1
        result=self.run_workflow()
        self.assertFalse(result['complete'])
        self.assertEqual(self.data(result)[-1]['status'],'incomplete')
    def test_periodic_cooldown_is_logged_and_not_hidden(self):
        original=self.client.get_prices.side_effect
        def prices(**kw):
            if 14<=self.clock.now<20:
                self.client.next_allowed=35
                raise Cooldown(20)
            return original(**kw)
        self.client.get_prices.side_effect=prices
        result=self.run_workflow()
        self.assertFalse(result['complete'])
        self.assertTrue(any(r['event']=='sample_error' and r['error_type']=='Cooldown' for r in self.data(result)))
    def test_final_sample_failure_is_not_success(self):
        def portfolio():
            if self.clock.now>=40: raise RuntimeError('private')
            return {'total_value':100,'cash_balance':100}
        self.client.get_portfolio.side_effect=portfolio
        result=self.run_workflow()
        self.assertFalse(result['complete'])
        self.assertEqual(self.data(result)[-1]['status'],'failed')
        self.assertNotIn('private',result['session'].read_text())
    def test_configuration_change_marks_incomplete(self):
        self.client.get_theory.side_effect=[{'theory':deepcopy(THEORY)}, {'theory':THEORY|{'version':4}}]
        result=self.run_workflow()
        self.assertFalse(result['complete'])
        self.assertTrue(any(r['event']=='configuration_changed' for r in self.data(result)))
    def test_missing_final_export_is_flagged(self):
        self.client.get_transactions.side_effect=[{'transactions':[]},RuntimeError('private')]
        result=self.run_workflow()
        self.assertFalse(result['complete'])
        report=json.loads(result['report'].read_text())
        self.assertIsNone(report['ledger_evidence'])
    def test_child_time_overrun_stops_process_and_marks_incomplete(self):
        self.child_exit_at=1000
        result=self.run_workflow()
        self.assertTrue(self.terminated)
        self.assertFalse(result['complete'])
        self.assertLess(self.clock.now,540)
    def test_first_sample_failure_never_launches_agents(self):
        self.client.get_prices.side_effect=RuntimeError('private')
        result=self.run_workflow()
        self.assertEqual(self.commands,[])
        self.assertFalse(result['complete'])
    def test_exact_account_and_state_forwarded(self):
        self.run_workflow()
        command=self.commands[0]
        self.assertEqual(command[command.index('--account')+1],'hind-btc')
        self.assertEqual(command[command.index('--state')+1],str(self.args.state))
        self.assertIn('--flat-entry',command)
    def test_policy_trade_counter_change_is_normal(self):
        self.client.get_policy.side_effect=[POLICY|{'trades_last_hour':0},POLICY|{'trades_last_hour':2}]
        self.assertTrue(self.run_workflow()['complete'])
    def test_stale_quotes_prevent_launch(self):
        self.client.get_prices.side_effect=lambda **kw:{'prices':[{'symbol':'BTC','price':80000,
            'timestamp_unix':self.clock.time()-100,'model_version':'test'}]}
        result=self.run_workflow()
        self.assertEqual(self.commands,[])
        self.assertFalse(result['complete'])


class CLIAndBoundaryTests(unittest.TestCase):
    def test_cli_member_required_and_preview_default(self):
        args=arguments(['--member','Hind','--account','hind-btc'])
        self.assertFalse(args.submit)
        self.assertEqual(args.duration,360)
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            arguments(['--account','hind-btc'])
    def test_invalid_duration_and_intervals(self):
        for name,value in (('--duration','540'),('--duration','nan'),('--sample-interval','1'),('--interval','15')):
            with self.subTest(name=name),patch('sys.stderr'),self.assertRaises(SystemExit):
                arguments(['--member','Hind','--account','hind-btc',name,value])
    def test_evidence_client_rejects_writes(self):
        client=ReadOnlyEvidenceClient('https://ledger.chanyeun.com/api/v1','offline-key')
        try:
            with self.assertRaises(ValueError): client.post('trade_propose.php',{})
        finally: client.session.close()
    def test_deadline_prevents_external_read(self):
        client=Mock();client.session.request=Mock()
        original=client.session.request
        with patch('time.monotonic',return_value=100):
            bounded_reads(client,110)
            with self.assertRaises(EvaluationDeadline): client.session.request('GET','https://example.com')
        original.assert_not_called()
    def test_error_details_never_include_raw_exception(self):
        self.assertEqual(error_fields(RuntimeError('fake-secret')),{'error_type':'RuntimeError'})


if __name__=='__main__':
    unittest.main()
