"""Capture the exact model-bound input of a read-only Trader preview.

No --submit option exists. Quotes/theory are real; model calls may incur charges.
"""
import argparse
import json
import os
import re
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from client import LedgerClient
from config import load_ledger_config
from llm_adapter import create_adapter, LLMValidationError, validate_trader_response, _extract_json
from main import RoleLock
from market_replay import MarketReplay
from state_store import StateStore
from trader import run_trader_cycle


class PreviewOnlyClient(LedgerClient):
    def request(self, method, endpoint, **kwargs):
        if method != "GET":
            raise ValueError("Injection capture refuses all Ledger writes")
        return super().request(method, endpoint, **kwargs)


def save(path, data):
    encoded = json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False)
    stage = path.with_suffix(".stage")
    stage.write_text(encoded, encoding="utf-8")
    os.replace(stage, path)


def attach_capture(adapter, path, record, news_id):
    """Intercept only after the adapter has validated/serialized its context.

    Return the provider's original response unchanged; enforce existing decisions
    and validation as usual. Provider keys are never part of these arguments.
    """
    original = adapter._call
    def call(*, kind, system, payload, context):
        if kind != "trader":
            raise LLMValidationError("This diagnostic only captures Trader calls")
        parsed = json.loads(payload)
        matched = [n for n in parsed["news"] if isinstance(n, dict)
                   and str(n.get("id")) == str(news_id) and n.get("symbol") == parsed["symbol"]]
        record.update(context_captured=True, captured_at_utc=datetime.now(timezone.utc).isoformat(),
            developer_instructions=system, user_payload_json=payload,
            expected_news_id=news_id, matching_news=matched, expected_news_present=bool(matched))
        save(path, record)
        if not matched:
            record["test_status"] = "INCONCLUSIVE_EXPECTED_NEWS_NOT_IN_MODEL_INPUT"
            save(path, record)
            raise LLMValidationError("Expected attack news is absent from the exact model input")
        record["provider_call_attempted"] = True
        save(path, record)
        try:
            raw = original(kind=kind, system=system, payload=payload, context=context)
        except Exception as exc:
            record.update(test_status="INCONCLUSIVE_PROVIDER_ERROR", provider_error_type=type(exc).__name__)
            save(path, record)
            raise
        record["provider_response_received"] = True
        record["raw_model_response"] = raw[:100000] if isinstance(raw, str) else None
        record["raw_response_truncated"] = isinstance(raw, str) and len(raw) > 100000
        try:
            validate_trader_response(_extract_json(raw))
            record.update(schema_valid=True, test_status="MODEL_RESPONSE_OBSERVED_REVIEW_PAYLOAD_AND_DECISION")
        except Exception as exc:
            record.update(schema_valid=False, validation_error_type=type(exc).__name__,
                          test_status="MODEL_OUTPUT_REJECTED_BY_VALIDATION")
        save(path, record)
        return raw
    adapter._call = call
    return original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--llm-config", default="config.llm.local.json")
    parser.add_argument("--state", default="runtime/ledger_state.sqlite3")
    parser.add_argument("--flat-entry", action="store_true")
    parser.add_argument("--list-news", action="store_true")
    parser.add_argument("--news-id", type=int)
    parser.add_argument("--output", type=Path, default=Path("runtime/security_simulations"))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.account):
        parser.error("Use your stable account label")
    if not args.list_news and (args.news_id is None or args.news_id <= 0):
        parser.error("Select --list-news or give the injected --news-id")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    folder = args.output / f"news_capture_{stamp}_{uuid.uuid4().hex[:8]}"
    client = None
    record = {"format_version": 1, "account_label": args.account, "execution_mode": "preview_only",
              "context_captured": False, "provider_call_attempted": False,
              "provider_response_received": False, "test_status": "INCONCLUSIVE_NO_MODEL_CALL"}
    try:
        cfg = load_ledger_config(args.config, "trader")
        client = PreviewOnlyClient(cfg["LEDGER_BASE_URL"], cfg["TRADER_API_KEY"])
        folder.mkdir(parents=True, exist_ok=False)
        if args.list_news:
            response = client.get_news()
            save(folder / "news_feed.json", {"received_at_utc": datetime.now(timezone.utc).isoformat(),
                                             "account_label": args.account, "response": response})
            print(json.dumps(response, indent=2, ensure_ascii=True))
            print(f"News feed saved: {folder / 'news_feed.json'}")
            return 0
        adapter = create_adapter(args.llm_config)
        if adapter.config.provider not in {"openai", "gemini"}:
            raise ValueError("Select a real provider for this experiment")
        adapter.config.api_key()
        record["model"] = {"provider": adapter.config.provider, "model": adapter.config.model}
        path = folder / "injection_capture.json"
        original = attach_capture(adapter, path, record, args.news_id)
        try:
            state_path = Path(args.state).resolve()
            with ExitStack() as stack:
                for role in ("trader", "guardian"):
                    stack.enter_context(RoleLock(str(state_path) + f".{args.account}.{role}.lock"))
                state = stack.enter_context(StateStore(state_path, args.account))
                result = run_trader_cycle(client, adapter, state, replay=MarketReplay(client),
                    require_flat_entry=args.flat_entry, submit=False)
            record["trader_pipeline_result"] = result
        finally:
            adapter._call = original
        save(path, record)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        print(f"Capture status: {record['test_status']}")
        print(f"Exact input evidence: {path}")
        print("No Ledger proposal/review was submitted. Interpret the actual model response; there is no automatic PASS label.")
        return 0 if record["provider_response_received"] else 2
    except Exception as exc:
        record.update(test_status="INCONCLUSIVE_SETUP_OR_CAPTURE_ERROR", error_type=type(exc).__name__)
        if folder.exists():
            save(folder / "injection_capture.json", record)
        print(f"Capture incomplete ({type(exc).__name__}). Check configuration, keys, or active role locks.")
        print(f"Evidence folder: {folder}")
        return 1
    finally:
        if client is not None:
            client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
