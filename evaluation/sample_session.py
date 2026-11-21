"""Record portfolio values over time without placing trades."""

import argparse
import json
import math
import time
from client import LedgerClient
from evaluation.metrics import portfolio_return, maximum_drawdown
from evaluation.session_recorder import SessionRecorder

def record_session(client, *, member, account_label, duration=60, interval=15):
    if (not math.isfinite(duration)
        or not math.isfinite(interval)
        or not 0 < duration <= 480
        or not 5 <= interval <= duration
        ):
        raise ValueError("Use duration 5–480 seconds and a valid interval.")

    theory = client.get_theory()["theory"]
    if theory["status"] != "SUBMITTED":
        raise ValueError("A submitted theory is required.")

    symbols = theory["selected_symbols"]
    if len(symbols) != 1:
        raise ValueError("Theory must select one currency.")

    recorder = SessionRecorder(
        "evaluation/results",
        member=member,
        account_label=account_label,
        symbol=symbols[0],
        theory_id=theory["id"],
        theory_version=theory["version"],
        sampling_interval_seconds=interval,
    )

    values = []
    deadline = time.monotonic() + duration

    print(f"Recording {symbols[0]} portfolio samples. No trades are placed.")

    try:
        while time.monotonic() < deadline:
            portfolio = client.get_portfolio()
            value = portfolio["total_value"]
            recorder.record_portfolio(value)
            values.append(value)
            print(f"Sample {len(values)}: ${value:,.2f}")

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))
    finally:
        recorder.finish()
        print(f"Session file: {recorder.path}")

    if len(values) >= 2:
        print(f"Sampled portfolio return: {portfolio_return(values):.2%}")
        print(f"Sampled maximum drawdown: {maximum_drawdown(values):.2%}")
    else:
        print("Insufficient samples for an evaluation.")

    return recorder.path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--member", default="Reham Alsereidi")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()

    client = None
    try:
        with open("config.local.json", encoding="utf-8") as file:
            config = json.load(file)

        client = LedgerClient(
            config["LEDGER_BASE_URL"], config["TRADER_API_KEY"]
        )

        record_session(
            client,
            member=args.member,
            account_label=args.account,
            duration=args.duration,
            interval=args.interval,
        )
    except KeyboardInterrupt:
        print("Collection interrupted.")
    except Exception as error:
        print(f"Collection stopped ({type(error).__name__}); results may be incomplete.")
        raise SystemExit(1)
    finally:
        if client is not None:
            client.session.close()

if __name__ == "__main__":
    main()