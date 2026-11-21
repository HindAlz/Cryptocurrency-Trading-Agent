"""An account's samples must never be combined with another account's fills."""
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.report import build_report, write_report
from test_evaluation_workflow import rows, evidence


class EvidenceIdentityTests(unittest.TestCase):
    def test_matching_account_is_accepted(self):
        report = build_report(rows(), evidence())
        self.assertEqual(report["account_label"], "hind-btc")

    def test_wrong_account_cannot_contribute_fills(self):
        fill = {"transaction_id": 999, "symbol": "BTC", "action": "BUY", "status": "EXECUTED",
                "executed_at": 120, "executed_quantity": .01, "executed_price": 80000,
                "executed_total": 800}
        exported = evidence([fill])
        exported["account_label"] = "another-account"
        with self.assertRaisesRegex(ValueError, "account_label"):
            build_report(rows(), exported)

    def test_missing_account_is_not_guessed(self):
        exported = evidence()
        exported.pop("account_label")
        with self.assertRaises(ValueError):
            build_report(rows(), exported)

    def test_portfolio_only_report_still_allowed(self):
        report = build_report(rows())
        self.assertIsNone(report["ledger_evidence"])

    def test_wrong_export_does_not_write_report(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "session.jsonl"
            session.write_text("\n".join(json.dumps(r) for r in rows()), encoding="utf-8")
            exported = evidence()
            exported["account_label"] = "other-account"
            export = Path(folder) / "after.json"
            export.write_text(json.dumps(exported), encoding="utf-8")
            with self.assertRaises(ValueError):
                write_report(session, export)
            self.assertFalse(session.with_suffix(".report.json").exists())


if __name__ == "__main__":
    unittest.main()
