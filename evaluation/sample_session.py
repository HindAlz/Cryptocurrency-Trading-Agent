"""Read-only standalone sampler. For trading plus sampling use run_session."""
import argparse
import math
import time

from client import LedgerClient
from config import load_ledger_config
from evaluation.session_recorder import SessionRecorder
from evaluation.metrics import portfolio_return, maximum_drawdown


def record_session(client, *, member, account_label, duration=60, interval=15,
                   output_dir="runtime/evaluation", metadata=None):
    if (not math.isfinite(duration) or not math.isfinite(interval)
            or not 5 <= duration <= 540 or not 5 <= interval <= duration):
        raise ValueError("Use duration 5..540 seconds and interval 5..duration")
    theory = client.get_theory()["theory"]
    if not theory or theory.get("status", "").upper() != "SUBMITTED":
        raise ValueError("A submitted theory is required")
    if len(theory["selected_symbols"]) != 1:
        raise ValueError("Theory must select one currency")
    recorder = SessionRecorder(output_dir, member=member, account_label=account_label,
        symbol=theory["selected_symbols"][0], theory_id=theory["id"],
        theory_version=theory["version"], sampling_interval_seconds=interval,
        metadata=metadata)
    start = time.monotonic()
    deadline = start + duration
    due = start
    status, error = "complete", None
    values = []
    try:
        while True:
            final = time.monotonic() >= deadline
            portfolio = client.get_portfolio()
            phase = "end" if final else ("start" if not values else "periodic")
            recorder.record_portfolio(portfolio["total_value"], phase=phase)
            values.append(portfolio["total_value"])
            if final:
                break
            # Anchor to scheduled time, not the end of each network request.
            due += interval
            now = time.monotonic()
            if due <= now:
                due = start + (math.floor((now - start) / interval) + 1) * interval
            time.sleep(max(0, min(due, deadline) - now))
    except KeyboardInterrupt:
        status, error = "interrupted", "KeyboardInterrupt"
        raise
    except Exception as exc:
        status, error = "failed", type(exc).__name__
        raise
    finally:
        recorder.finish(status=status, error_type=error)
    if len(values) >= 2:
        print(f"Sampled portfolio return: {portfolio_return(values):.2%}")
        print(f"Sampled maximum drawdown: {maximum_drawdown(values):.2%}")
    return recorder.path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    config = load_ledger_config(args.config, "trader")
    client = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"])
    try:
        path = record_session(client, member=args.member, account_label=args.account,
                              duration=args.duration, interval=args.interval)
        print(f"Read-only samples saved: {path}")
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Sampling incomplete ({type(exc).__name__}); inspect the session file.")
        return 1
    finally:
        client.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
