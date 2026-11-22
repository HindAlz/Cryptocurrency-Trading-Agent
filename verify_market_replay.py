"""Read-only replay evidence and the guide's default-descriptor formula checks.

Future day-365 values are OFFLINE TEST FIXTURES ONLY. Never feed them to trading.
"""
import argparse
import json
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from client import LedgerClient
from config import load_ledger_config
from guardian_signals import price_at

TOLERANCE = Decimal("0.00000001")
GUIDE_BASELINES = {0: "64000.00000000", 365: "87426.57643228"}
GUIDE_QUOTES = {0: "67000.36767791", 30: "69404.90274610", 365: "91475.89718627"}


def check_value(actual, expected):
    difference = abs(Decimal(str(actual)) - Decimal(expected))
    return {"actual": actual, "expected": expected, "absolute_error": str(difference),
            "passed": difference <= TOLERANCE}


def verify_descriptor(model, quote=None, observed_at=None):
    asset = model["assets"]["BTC"]
    if not all(math.isfinite(float(asset[k])) for k in ("base_price", "power_exponent")):
        raise ValueError("Invalid descriptor")
    baseline = {str(day): check_value(
        asset["base_price"] * (1 + day / model["power_timescale_days"]) ** asset["power_exponent"], expected)
        for day, expected in GUIDE_BASELINES.items()}
    full = {str(day): check_value(price_at(model, "BTC", model["epoch_unix"] + day * 86400), expected)
            for day, expected in GUIDE_QUOTES.items()}
    matches = all(r["passed"] for r in [*baseline.values(), *full.values()])
    result = {"format_version": 1, "market_descriptor": model,
        "formula_fixture_scope": "offline_default_descriptor_test_only_not_trading_inputs",
        "baseline_checks": baseline, "full_quote_checks": full,
        "all_published_fixtures_match": matches,
        "guide_fixture_status": "MATCH" if matches else "MISMATCH_REQUIRES_DESCRIPTOR_OR_FORMULA_REVIEW",
        "interpretation": ("Captured descriptor reproduces the guide's published check values." if matches else
            "These prices apply to the guide's DEFAULT descriptor. A mismatch can mean the captured "
            "descriptor differs or the implementation is wrong. Do not alter expected prices, tune parameters "
            "to force a match, or claim verification passed. Retain this descriptor and investigate."),
        "quote_check": None}
    if quote is not None:
        symbol = quote["symbol"]
        timestamp = quote["timestamp_unix"]
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            raise ValueError("Invalid quote timestamp")
        if timestamp < model["epoch_unix"]:
            raise ValueError("Quote predates the market epoch")
        if observed_at is not None and not observed_at - 120 <= timestamp <= observed_at + 5:
            raise ValueError("Stale/future quote")
        if model["version"] != quote["model_version"]:
            raise ValueError("Descriptor and quote versions differ")
        checked = check_value(price_at(model, symbol, timestamp), str(quote["price"]))
        result["quote_check"] = {"symbol": symbol, "quote_timestamp_unix": timestamp,
                                  "observed_at_unix": observed_at, **checked}
    return result


def capture(client):
    # Fetching the descriptor may race a market version change: refresh once.
    model = client.get_market_model()["model"]
    quote = None
    result = None
    for attempt in range(2):
        quotes = [q for q in client.get_prices(symbols="BTC")["prices"] if q.get("symbol") == "BTC"]
        if len(quotes) != 1:
            raise ValueError("Expected one BTC quote")
        quote = quotes[0]
        result = None  # Only the latest quote can be reported as verified.
        if model["version"] == quote["model_version"]:
            observed = datetime.now(timezone.utc).timestamp()
            result = verify_descriptor(model, quote, observed)
            if result["quote_check"]["passed"]:
                return model, result
        if not attempt:
            model = client.get_market_model()["model"]
    if result is None or result['market_descriptor']['version'] != model['version']:
        result = verify_descriptor(model)
        result['quote_check'] = {'passed': False, 'reason': 'descriptor_quote_version_mismatch',
                                 'quote': quote}
    return model, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--descriptor", type=Path, help="Verify an already captured public descriptor offline")
    parser.add_argument("--output", type=Path, default=Path("runtime/requirement_checks"))
    args = parser.parse_args(argv)
    client = None
    try:
        if args.descriptor:
            data = json.loads(args.descriptor.read_text(encoding="utf-8"))
            model = data.get("model", data.get("market_descriptor", data))
            result = verify_descriptor(model)
            result["source"] = "saved_public_descriptor_offline_verification"
        else:
            cfg = load_ledger_config(args.config, "trader")
            client = LedgerClient(cfg["LEDGER_BASE_URL"], cfg["TRADER_API_KEY"])
            model, result = capture(client)
            result["source"] = "live_read_only_descriptor_and_quote_plus_offline_formula_checks"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        folder = args.output / f"replay_{stamp}_{uuid.uuid4().hex[:8]}"
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "market_model.json").write_text(json.dumps({"model": model}, indent=2, allow_nan=False), encoding="utf-8")
        (folder / "replay_checks.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
        print(f"Guide default fixtures: {result['guide_fixture_status']}")
        print(f"Current quote replay: {result['quote_check']['passed'] if result['quote_check'] else 'not run (offline file)'}")
        print(f"Evidence: {folder}")
        quote_ok = result['quote_check'] is None or result['quote_check']['passed']
        return 0 if result["all_published_fixtures_match"] and quote_ok else 2
    except Exception as exc:
        print(f"Verification incomplete ({type(exc).__name__}). No trade or model request was made.")
        return 1
    finally:
        if client is not None:
            client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
