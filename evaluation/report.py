"""Observed performance and session-scoped Ledger evidence. No API calls."""
import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluation.metrics import portfolio_return, maximum_drawdown


def unix_time(value):
    if isinstance(value, bool):
        raise ValueError("Invalid timestamp")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        # Ledger's unqualified SQL timestamps are UTC.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    raise ValueError("Missing or invalid timestamp")


def in_window(rows, start, end, *, symbol, guardian=False):
    selected, unknown = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            continue
        fields = ("created_at",) if guardian else (("executed_at",)
            if row.get("status") == "EXECUTED" else ("reviewed_at", "created_at"))
        stamp = None
        for key in fields:
            if row.get(key) is not None:
                try:
                    stamp = unix_time(row[key])
                    break
                except (ValueError, TypeError, OverflowError):
                    pass
        if stamp is None:
            unknown.append(row)
        elif start <= stamp <= end:
            selected.append(row)
    return selected, unknown


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def build_report(records, evidence=None):
    starts = [r for r in records if r.get("event") == "session_start"]
    ends = [r for r in records if r.get("event") == "session_end"]
    if len(starts) != 1:
        raise ValueError("Expected exactly one session start")
    samples = [r for r in records if r.get("event") == "portfolio_sample"]
    if len(samples) < 2:
        raise ValueError("At least two successful samples are required")
    stamps = [unix_time(r["timestamp_unix"]) for r in samples]
    if any(b < a for a, b in zip(stamps, stamps[1:])):
        raise ValueError("Portfolio samples are not chronological")
    values = [s["value_usd"] for s in samples]
    status = ends[-1].get("status", "incomplete") if ends else "incomplete"
    complete = (status == "complete" and samples[0].get("phase") == "start"
                and samples[-1].get("phase") == "end")
    start = starts[0]
    result = {"format_version": 1, "member": start["member"],
        "account_label": start["account_label"], "symbol": start["symbol"],
        "theory_id": start["theory_id"], "theory_version": start["theory_version"],
        "metadata": start.get("metadata", {}), "recording_status": status,
        "observation_complete": complete,
        "result_label": "final_observed" if complete else "provisional_partial_observation",
        "observation_start_unix": stamps[0], "observation_end_unix": stamps[-1],
        "observed_duration_seconds": stamps[-1] - stamps[0], "sample_count": len(samples),
        "requested_sampling_interval_seconds": start["sampling_interval_seconds"],
        "largest_observed_gap_seconds": max(b-a for a,b in zip(stamps, stamps[1:])),
        "starting_value_usd": values[0], "ending_value_usd": values[-1],
        "portfolio_value_change_usd": values[-1] - values[0],
        "portfolio_return": portfolio_return(values), "sampled_maximum_drawdown": maximum_drawdown(values),
        "cash_benchmark": {"assumption": "Hold starting portfolio value entirely as USD cash; no interest or fees",
                           "ending_value_usd": values[0], "return": 0.0, "sampled_maximum_drawdown": 0.0},
        "sample_errors": [r for r in records if r.get("event") == "sample_error"],
        "configuration_changes": [r for r in records if r.get("event") == "configuration_changed"],
        "evidence_errors": [r for r in records if r.get("event") == "evidence_error"],
        "local_state": [r for r in records if r.get("event") == "local_state"],
        "market_model_versions": sorted({str(r['market_model_version']) for r in samples if r.get('market_model_version') is not None}),
        "runner_events": [r for r in records if r.get("event") in {"runner_start", "runner_end", "runner_stopped"}],
        "limitations": ["Drawdown is based on successful samples; movements between observations may be missed.",
            "Portfolio and quote endpoints are separate reads. Quote and portfolio observation times are retained.",
            "Benchmark assumes fractional purchases, no fees or slippage, full initial allocation, and no policy limits."]}
    prices = [s.get("price_usd") for s in samples]
    if all(_positive(p) for p in prices):
        benchmark = [values[0] * p / prices[0] for p in prices]
        result["buy_and_hold_benchmark"] = {"symbol": start["symbol"],
            "assumption": "Invest all starting portfolio value at the starting paired quote; hold unchanged",
            "starting_quote_usd": prices[0], "ending_quote_usd": prices[-1],
            "ending_value_usd": benchmark[-1], "return": portfolio_return(benchmark),
            "sampled_maximum_drawdown": maximum_drawdown(benchmark),
            "quote_start_unix": samples[0].get("quote_timestamp_unix"),
            "quote_end_unix": samples[-1].get("quote_timestamp_unix")}
    else:
        result["buy_and_hold_benchmark"] = None
        result["limitations"].append("Missing paired quotes: buy-and-hold benchmark not calculated.")

    if evidence is not None:
        # Prevent accidentally combining a session with another account export.
        if evidence.get("account_label") != start["account_label"]:
            raise ValueError("Evidence account_label must match the recorded session")
        records_by_name = evidence["records"]
        rows = records_by_name["transactions"]["response"]["transactions"]
        logs = records_by_name["guardian_logs"]["response"]["guardian_logs"]
        transactions, unknown = in_window(rows, stamps[0], stamps[-1], symbol=start["symbol"])
        reviews, unknown_reviews = in_window(logs, stamps[0], stamps[-1], symbol=start["symbol"], guardian=True)
        fills, bad_fills = [], []
        seen = set()
        for row in transactions:
            if row.get("status") != "EXECUTED":
                continue
            valid = (row.get("action") in {"BUY", "SELL"} and row.get("transaction_id") is not None
                     and all(_positive(row.get(k)) for k in ("executed_quantity", "executed_price", "executed_total")))
            if valid and row["transaction_id"] not in seen:
                fills.append(row)
                seen.add(row["transaction_id"])
            elif not valid:
                bad_fills.append(row)
        result["ledger_evidence"] = {"transaction_status_counts": dict(Counter(r.get("status", "UNKNOWN") for r in transactions)),
            "completed_fill_count": len(fills), "completed_fills": fills,
            "fills_by_theory": dict(Counter(f"{r.get('theory_id', 'unknown')}/v{r.get('theory_version', 'unknown')}" for r in fills)),
            "outstanding_orders_after": [r for r in rows if r.get('symbol') == start['symbol'] and r.get('status') in {'PENDING', 'ESCALATED'}],
            "non_executed_orders": [r for r in transactions if r.get("status") != "EXECUTED"],
            "executed_rows_without_valid_fill": bad_fills,
            "guardian_decision_counts": dict(Counter(r.get("decision", "UNKNOWN") for r in reviews)),
            "guardian_reviews": reviews,
            "history_may_be_truncated": evidence.get("history_may_be_truncated", True),
            "rows_without_usable_time": len(unknown), "guardian_rows_without_usable_time": len(unknown_reviews)}
        if evidence.get("history_may_be_truncated") or unknown or unknown_reviews or bad_fills:
            result["limitations"].append("Ledger history completeness or fill validity needs review; counts are not a completeness guarantee.")
    else:
        result["ledger_evidence"] = None
        result["limitations"].append("Final Ledger export unavailable: completed fills and reviews need reconciliation.")
    return result


def write_report(session_path, evidence_path=None):
    session_path = Path(session_path)
    records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8")) if evidence_path else None
    report = build_report(records, evidence)
    runner_log = session_path.parent / "runner.jsonl"
    if runner_log.exists():
        try:
            result_events = [json.loads(line) for line in runner_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            report["agent_events"] = result_events
        except (ValueError, OSError):
            report["agent_events"] = None
            report["limitations"].append("Runner log could not be parsed; inspect it separately.")
    else:
        report["agent_events"] = None
    report["limitations"].append("Existing runner summaries do not expose every HOLD reason; detailed model-failure classification may require separately recorded diagnostics.")
    target = session_path.with_suffix(".report.json")
    target.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    with session_path.with_suffix(".samples.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["timestamp_unix", "phase", "value_usd", "cash_usd", "price_usd", "quote_timestamp_unix"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(r for r in records if r.get("event") == "portfolio_sample")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    print(write_report(args.session, args.evidence))


if __name__ == "__main__":
    main()
