import json
import pytest
from evaluation.session_recorder import SessionRecorder

@pytest.fixture
def recorder(tmp_path):
    return SessionRecorder(
        tmp_path,
        member="Test member",
        account_label="test-account",
        symbol="BTC",
        theory_id=1,
        theory_version=1,
        sampling_interval_seconds=30,
    )

def read_records(recorder):
    return [json.loads(line)
            for line in recorder.path.read_text(encoding="utf-8").splitlines()]

def test_session_records_metadata_samples_and_end(recorder):
    recorder.record_portfolio(100000)
    recorder.record_portfolio(101000)
    recorder.finish()

    records = read_records(recorder)

    assert [r["event"] for r in records] == [ "session_start", "portfolio_sample", "portfolio_sample", "session_end",]

    start = records[0]
    assert start["member"] == "Test member"
    assert start["account_label"] == "test-account"
    assert start["symbol"] == "BTC"
    assert start["theory_id"] == 1
    assert start["theory_version"] == 1
    assert start["sampling_interval_seconds"] == 30

    assert records[1]["value_usd"] == 100000
    assert records[2]["value_usd"] == 101000
    assert all("timestamp_unix" in record for record in records)
    assert records[-1]["duration_seconds"] >= 0

@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "100000"],)
def test_invalid_sample_is_not_saved(recorder, value):
    with pytest.raises(ValueError):
        recorder.record_portfolio(value)

    assert len(read_records(recorder)) == 1

def test_cannot_record_after_finish(recorder):
    recorder.finish()

    with pytest.raises(ValueError):
        recorder.record_portfolio(100000)

    assert len(read_records(recorder)) == 2

def test_finishing_twice_does_not_duplicate_end(recorder):
    recorder.finish()
    recorder.finish()

    assert len(read_records(recorder)) == 2

@pytest.mark.parametrize("interval", [0, -30])
def test_invalid_sampling_interval(tmp_path, interval):
    with pytest.raises(ValueError):
        SessionRecorder(
            tmp_path,
            member="Test member",
            account_label="test-account",
            symbol="BTC",
            theory_id=1,
            theory_version=1,
            sampling_interval_seconds=interval,
        )