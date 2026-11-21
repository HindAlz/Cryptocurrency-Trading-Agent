import json
from unittest.mock import Mock
import pytest
from evaluation.sample_session import record_session

@pytest.fixture
def setup_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    clock = [0.0]
    monkeypatch.setattr("evaluation.sample_session.time.monotonic", lambda: clock[0],)

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr("evaluation.sample_session.time.sleep", advance)

    client = Mock()
    client.get_theory.return_value = {
        "theory": {
            "id": 27,
            "version": 2,
            "status": "SUBMITTED",
            "selected_symbols": ["DOGE"],
        }
    }
    return client, tmp_path


def test_records_timed_samples(setup_session, capsys):
    client, _ = setup_session
    client.get_portfolio.side_effect = [{"total_value": value} for value in [100, 120, 90, 110]]

    path = record_session(
        client,
        member="Test member",
        account_label="test-doge",
        duration=60,
        interval=15,
    )

    records = [json.loads(line)
               for line in path.read_text(encoding="utf-8").splitlines()]
    
    samples = [r["value_usd"]
               for r in records
               if r["event"] == "portfolio_sample"]

    assert samples == [100, 120, 90, 110]
    assert records[-1]["event"] == "session_end"
    assert client.get_portfolio.call_count == 4

    output = capsys.readouterr().out
    assert "return: 10.00%" in output
    assert "drawdown: 25.00%" in output

    client.propose_trade.assert_not_called()
    client.submit_review.assert_not_called()

def test_read_failure_preserves_collected_samples(setup_session):
    client, folder = setup_session
    client.get_portfolio.side_effect = [{"total_value": 100000}, RuntimeError("Mock API failure"),]

    with pytest.raises(RuntimeError):
        record_session(
            client,
            member="Test member",
            account_label="test-doge",
            duration=60,
            interval=15,
        )

    path = next((folder / "evaluation/results").glob("*.jsonl"))
    records = [json.loads(line)
               for line in path.read_text(encoding="utf-8").splitlines()]

    assert [r["event"] for r in records] == ["session_start", "portfolio_sample", "session_end", ]