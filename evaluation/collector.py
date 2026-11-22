"""Read and save Ledger evaluation evidence without placing trades."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from client import LedgerClient


def collect_snapshot(client):
    records = {}
    
    requests = [
        ("theory", client.get_theory),
        ("portfolio", client.get_portfolio),
        ("transactions", lambda: client.get_transactions(limit=200)),
        ("guardian_logs", lambda: client.get_guardian_logs(limit=200)),
    ]

    for name, request in requests:
        response = request()
        records[name] = {
            "received_at_unix": time.time(),
            "response": response,
        }

    theory = records["theory"]["response"]["theory"]
    if theory["status"] != "SUBMITTED":
        raise ValueError("A submitted theory is required.")

    if len(theory["selected_symbols"]) != 1:
        raise ValueError("Theory must select one currency.")

    return {
        "format_version": 1,
        "source": "live_ledger",
        "theory_id": theory["id"],
        "theory_version": theory["version"],
        "symbol": theory["selected_symbols"][0],
        "records": records,
        "history_limit": 200,
        "history_may_be_truncated": any(
            len(records[name]["response"][name]) >= 200
            for name in ("transactions", "guardian_logs")
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--account", required=True)
    args = parser.parse_args()

    try:
        with open(args.config, encoding="utf-8") as file:
            config = json.load(file)

        client = LedgerClient(
            config["LEDGER_BASE_URL"], config["TRADER_API_KEY"]
        )

        try:
            snapshot = collect_snapshot(client)
        finally:
            client.session.close()

        snapshot["account_label"] = args.account
        folder = Path("runtime/evaluation")
        folder.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = folder / f"snapshot_{stamp}.json"

        with path.open("x", encoding="utf-8") as file:
            json.dump(snapshot, file, indent=2, allow_nan=False)

        print(f"Snapshot saved: {path}")

    except Exception as error:
        print(f"Collection failed ({type(error).__name__}).")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
