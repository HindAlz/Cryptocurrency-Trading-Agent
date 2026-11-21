"""One Guardian sweep. Read-only unless --submit is explicitly supplied."""
import argparse
import json
from pathlib import Path

from client import LedgerClient, Cooldown
from guardian import review_pending
from guardian_signals import IndependentSignals
from llm_adapter import create_adapter
from state_store import StateStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--llm-config", default="config.llm.local.json")
    parser.add_argument("--state", default="runtime/ledger_state.sqlite3")
    parser.add_argument("--account", required=True,
                        help="Must match the trader's stable local account label")
    parser.add_argument("--submit", action="store_true",
                        help="Allow actual reviews: APPROVE/MODIFY can execute trades")
    parser.add_argument("--flat-entry", action="store_true",
                        help="Enforce no additional BUY while this account holds the currency")
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_size > 32768:
        raise ValueError("Configuration exceeds 32 KiB")
    config = json.loads(path.read_text(encoding="utf-8"))
    client = LedgerClient(config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"])
    llm = create_adapter(args.llm_config) if args.submit else None
    if args.submit and llm.config.provider == "mock":
        raise ValueError("Live submissions require a real provider, not mock mode")
    # One provider instance caches static model parameters throughout this sweep.
    provider = IndependentSignals(client)
    with StateStore(args.state, args.account) as state:
        result = review_pending(client, llm, state, provider, submit=args.submit,
                                require_flat_entry=args.flat_entry)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Cooldown as exc:
        print(f"Cooldown: wait at least {exc.seconds:.1f} seconds before retrying.")
        raise SystemExit(1)
