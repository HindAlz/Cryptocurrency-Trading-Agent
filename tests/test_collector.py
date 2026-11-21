from unittest.mock import Mock
import pytest
from evaluation.collector import collect_snapshot

@pytest.fixture
def client():
    fake = Mock()
    fake.get_theory.return_value = {
        "theory": {
            "id": 27,
            "version": 2,
            "status": "SUBMITTED",
            "selected_symbols": ["DOGE"],
        }
    }
    fake.get_portfolio.return_value = {
        "cash_balance": 100000,
        "holdings": [],
        "holdings_value": 0,
        "total_value": 100000,
    }
    fake.get_transactions.return_value = {"transactions": []}
    fake.get_guardian_logs.return_value = {"guardian_logs": []}
    return fake


def test_collects_account_evidence(client):
    result = collect_snapshot(client)

    assert result["theory_id"] == 27
    assert result["theory_version"] == 2
    assert result["symbol"] == "DOGE"
    assert result["history_may_be_truncated"] is False

    portfolio = result["records"]["portfolio"]["response"]
    assert portfolio["total_value"] == 100000

    client.get_transactions.assert_called_once_with(limit=200)
    client.get_guardian_logs.assert_called_once_with(limit=200)
    client.propose_trade.assert_not_called()
    client.submit_review.assert_not_called()

@pytest.mark.parametrize("name", ["transactions", "guardian_logs"])
def test_flags_potentially_truncated_history(client, name):
    method = getattr(client, f"get_{name}")
    method.return_value = {name: [{} for _ in range(200)]}

    assert collect_snapshot(client)["history_may_be_truncated"] is True

def test_rejects_draft_theory(client):
    client.get_theory.return_value["theory"]["status"] = "DRAFT"

    with pytest.raises(ValueError):
        collect_snapshot(client)

def test_rejects_multiple_currencies(client):
    client.get_theory.return_value["theory"]["selected_symbols"] = ["DOGE", "BTC"]

    with pytest.raises(ValueError):
        collect_snapshot(client)

def test_api_failure_does_not_return_success(client):
    client.get_portfolio.side_effect = RuntimeError("Mock read failure")

    with pytest.raises(RuntimeError):
        collect_snapshot(client)