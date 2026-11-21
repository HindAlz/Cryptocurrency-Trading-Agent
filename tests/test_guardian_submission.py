
import unittest

from client import UncertainWrite
from guardian import submit_review_safely


class FakeLedger:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def submit_review(self, review):
        self.calls += 1

        if self.fail:
            raise UncertainWrite("Response lost")

        return {
            "transaction_id": review["transaction_id"],
            "status": "BLOCKED"
        }


class TestGuardianSubmission(unittest.TestCase):

    def setUp(self):
        self.review = {
            "transaction_id": 42,
            "decision": "BLOCK",
            "reason": "Transaction exceeds security limit.",
            "risk_score": 90,
            "modified_quantity": None,
            "rules_triggered": [
                "TRANSACTION_LIMIT_EXCEEDED"
            ]
        }

    def test_successful_submission(self):
        client = FakeLedger()

        result = submit_review_safely(
            client, self.review
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(client.calls, 1)

    def test_uncertain_write_no_retry(self):
        client = FakeLedger(fail=True)

        result = submit_review_safely(
            client, self.review
        )

        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertTrue(result["needs_reconciliation"])
        self.assertEqual(client.calls, 1)

    def test_approve_requires_final_recheck(self):
        client = FakeLedger()
        self.review["decision"] = "APPROVE"
        self.review["modified_quantity"] = None

        with self.assertRaises(ValueError):
            submit_review_safely(
                client,
                self.review,
                recheck_result={
                    "decision": "BLOCK"
                }
            )

        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
