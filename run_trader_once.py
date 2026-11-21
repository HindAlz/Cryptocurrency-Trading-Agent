"""One trader cycle. Preview by default; --submit explicitly proposes an order."""
import argparse
import json
from pathlib import Path

from client import LedgerClient
from llm_adapter import create_adapter
from market_replay import MarketReplay
from state_store import StateStore
from trader import run_trader_cycle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--llm-config", default="config.llm.local.json")
    parser.add_argument("--account", required=True,
                        help="Same stable account label used by the Guardian")
    parser.add_argument("--state", default="runtime/ledger_state.sqlite3")
    parser.add_argument("--symbol", default=None,
                        help="Optional check; normally derived from active theory")
    parser.add_argument("--flat-entry", action="store_true",
                        help="Use only if the submitted theory prohibits buying while already holding")
    parser.add_argument("--submit", action="store_true",
                        help="Create one real simulated pending proposal if a valid signal is found")
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_size > 32768:
        raise ValueError("Ledger configuration exceeds 32 KiB")
    config = json.loads(path.read_text(encoding="utf-8"))
    client = LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"])
    llm = create_adapter(args.llm_config)
    if llm.config.provider != "gemini":
        raise ValueError("This manual verifier requires real Gemini, not mock mode")
    with StateStore(args.state, args.account) as state:
        result = run_trader_cycle(client, llm, state, symbol=args.symbol,
                                 replay=MarketReplay(client), require_flat_entry=args.flat_entry,
                                 submit=args.submit)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["status"] in {"UNRESOLVED", "COOLDOWN"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
