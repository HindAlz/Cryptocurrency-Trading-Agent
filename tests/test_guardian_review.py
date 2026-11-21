
import unittest
from guardian import prepare_review


class TestGuardianReview(unittest.TestCase):

    def test_valid_modify(self):
        result = prepare_review(
            transaction_id=42,
            requested_quantity=10,
            model_result={
                "decision": "MODIFY",
                "reason": "Reduce transaction exposure.",
                "risk_score": 35,
                "modified_quantity": 2,
                "rules_triggered": ["POSITION_RISK"]
            }
        )

        self.assertEqual(result["decision"], "MODIFY")
        self.assertEqual(result["modified_quantity"], "2")

    def test_invalid_modify(self):
        with self.assertRaises(ValueError):
            prepare_review(
                transaction_id=42,
                requested_quantity=10,
                model_result={
                    "decision": "MODIFY",
                    "reason": "Increase transaction quantity.",
                    "risk_score": 80,
                    "modified_quantity": 12,
                    "rules_triggered": ["POSITION_RISK"]
                }
            )


if __name__ == "__main__":
    unittest.main()
