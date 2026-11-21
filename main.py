"""Bounded-session Ledger runner. Preview by default; --submit enables writes.

Examples:
 python main.py --account hind-btc --mode both --once --flat-entry
 python main.py --account hind-btc --mode both --once --flat-entry --submit
"""
import argparse
import json
import os
import random
import re
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from client import LedgerClient, ApiError, Cooldown
from config import load_ledger_config
from guardian import inspect_pending
from guardian_live import review_pending_live
from guardian_signals import IndependentSignals
from llm_adapter import create_adapter
from market_replay import MarketReplay
from recovery import (reconcile_known, server_unresolved, unresolved_summary,
                      bind_confirmed_transaction)
from state_store import StateStore
from trader import run_trader_cycle


class SessionEnding(RuntimeError):
    pass


class Deadline:
    """540-second budget; no new external operation in its last 35 seconds.

    Standard request/provider timeouts are capped at <=30 seconds. This reserves
    time for a normal in-flight operation before the course's 10-minute limit.
    """
    def __init__(self, seconds=540, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + seconds

    def remaining(self):
        return max(0., self.end - self.clock())

    def check(self):
        if self.remaining() <= 35:
            raise SessionEnding("Session budget reached; no further external calls")


def protect_client(client, deadline):
    original = client.session.request
    def bounded_request(*args, **kwargs):
        deadline.check()
        # The original shared client already uses (5,15); preserve bounded values.
        kwargs["timeout"] = (5, 15)
        return original(*args, **kwargs)
    client.session.request = bounded_request
    return client


class BoundedModel:
    def __init__(self, adapter, deadline):
        self.adapter = adapter
        self.deadline = deadline
        if adapter.config.timeout_seconds > 30:
            raise ValueError("Set model timeout_seconds to at most 30 for bounded sessions")

    def trader_decision(self, context):
        self.deadline.check()
        return self.adapter.trader_decision(context)

    def guardian_decision(self, context):
        self.deadline.check()
        return self.adapter.guardian_decision(context)


class RoleLock:
    """OS-backed lock released on process exit, including crashes.

    Lock files remain on disk but are not themselves evidence of a held lock.
    Trader and Guardian have separate role locks; combined mode acquires both.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("Another runner holds this account/role lock") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()


def summarize(result):
    """Exclude free-text model/server bodies and credentials from runner logs."""
    if isinstance(result, list):
        return [summarize(x) for x in result]
    if not isinstance(result, dict):
        return None
    safe = {}
    for key in ("status", "ledger_status", "decision", "transaction_id", "intent_id",
                "needs_reconciliation", "retry_after", "theory_version"):
        if key in result:
            safe[key] = result[key]
    proposal = result.get("proposal")
    if isinstance(proposal, dict):
        safe["proposal"] = {key: proposal[key] for key in ("symbol", "action", "quantity", "theory_id")
                            if key in proposal}
    fill = result.get("fill")
    if isinstance(fill, dict):
        safe["fill"] = {key: fill[key] for key in ("quantity", "price", "total") if key in fill}
    return safe


def log_event(path, event, **details):
    record = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    encoded = json.dumps(record, allow_nan=False)
    print(encoded)
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(encoded + "\n")
            file.flush()


def run_cycle(mode, trader_client, guardian_client, llm, state, *, submit=False,
              flat_entry=False, replay=None, signal_provider=None, clock=time.time):
    reader = guardian_client if guardian_client is not None else trader_client
    reconcile_known(reader, state)
    if mode == "recover":
        return {"recovery": unresolved_summary(state)}
    report = {}
    if mode in {"both", "guardian", "review"}:
        if submit:
            report["guardian_before"] = review_pending_live(
                guardian_client, llm, state, signal_provider,
                require_flat_entry=flat_entry, clock=clock)
        else:
            report["guardian_inspection"] = inspect_pending(
                guardian_client, llm, state, signal_provider)
        if mode != "both":
            return report
        if any(r.get("needs_reconciliation") for r in report.get("guardian_before", [])):
            report["trader"] = {"status": "HOLD", "needs_reconciliation": True}
            return report
    # Read server history as well as local state. This includes simulator orders
    # and escalations which may not have a local proposal intent.
    if state.get_unresolved_intents() or server_unresolved(reader):
        report["trader"] = {"status": "HOLD", "needs_reconciliation": True}
        return report
    result = run_trader_cycle(trader_client, llm, state, replay=replay,
                              require_flat_entry=flat_entry, submit=submit,
                              decision_time=datetime.fromtimestamp(clock(), timezone.utc))
    report["trader"] = result
    if mode == "both" and submit and result.get("status") == "PROPOSED":
        report["guardian_after"] = review_pending_live(
            guardian_client, llm, state, signal_provider,
            require_flat_entry=flat_entry, clock=clock)
    return report


def cooldown_seconds(report):
    if isinstance(report, list):
        return max((cooldown_seconds(x) for x in report), default=0)
    if not isinstance(report, dict):
        return 0
    own = report.get("retry_after", 0)
    return max([float(own)] + [cooldown_seconds(v) for v in report.values()
                              if isinstance(v, (dict, list))])


def wait_interval(seconds, deadline):
    end = min(time.monotonic() + seconds, deadline.end - 35)
    while time.monotonic() < end:
        time.sleep(min(.5, max(0, end - time.monotonic())))


def arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["trader", "guardian", "both", "review", "recover"], default="both")
    parser.add_argument("--account", required=True, help="Stable account label shared with both agents")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--llm-config", default="config.llm.local.json")
    parser.add_argument("--state", default="runtime/ledger_state.sqlite3")
    parser.add_argument("--log", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--submit", action="store_true", help="Enable real simulated Ledger proposals/reviews")
    parser.add_argument("--flat-entry", action="store_true", help="Apply only if the theory prohibits additional buys")
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--duration", type=float, default=540, help="Session budget 90..540 seconds")
    parser.add_argument("--bind-intent", default=None, help="Explicit local recovery after dashboard investigation")
    parser.add_argument("--transaction-id", type=int, default=None)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.account):
        parser.error("Use a stable account label with letters, digits, underscores or hyphens")
    if not 90 <= args.duration <= 540:
        parser.error("duration must be between 90 and 540 seconds")
    minimum = 15 if args.mode in {"guardian", "review"} else 60
    args.interval = minimum if args.interval is None else args.interval
    if not args.interval >= minimum or not args.interval < float('inf'):
        parser.error(f"interval must be finite and at least {minimum} seconds")
    if bool(args.bind_intent) != (args.transaction_id is not None):
        parser.error("Supply both --bind-intent and --transaction-id")
    if args.bind_intent and args.mode != "recover":
        parser.error("Explicit binding is only available in recover mode")
    if args.mode in {"review", "recover"}:
        args.once = True
    return args


def main(argv=None):
    args = arguments(argv)
    config = load_ledger_config(args.config, args.mode)
    deadline = Deadline(args.duration)
    roles = (["trader", "guardian"] if args.mode in {"both", "recover"}
             else ["guardian" if args.mode == "review" else args.mode])
    state_path = Path(args.state).resolve()
    log_path = args.log or str(state_path.parent / f"{args.account}_runner.jsonl")
    with ExitStack() as stack:
        for role in roles:
            stack.enter_context(RoleLock(str(state_path) + f".{args.account}.{role}.lock"))
        state = stack.enter_context(StateStore(state_path, args.account))
        trader = guardian = None
        if args.mode in {"both", "trader"} or (args.mode == "recover" and "TRADER_API_KEY" in config):
            trader = protect_client(LedgerClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"]), deadline)
            trader.get_portfolio()
        if args.mode in {"both", "guardian", "review"} or (args.mode == "recover" and trader is None):
            guardian = protect_client(LedgerClient(config["LEDGER_BASE_URL"], config["GUARDIAN_API_KEY"]), deadline)
            guardian.get_policy()
        if args.bind_intent:
            result = bind_confirmed_transaction(guardian or trader, state, args.bind_intent, args.transaction_id)
            log_event(log_path, "explicit_local_binding", result=result)
            return 0
        # Preview trader still calls Gemini. Guardian read-only inspection does not.
        needs_model = args.mode in {"both", "trader"} or (args.submit and args.mode in {"guardian", "review"})
        llm = None
        if needs_model:
            adapter = create_adapter(args.llm_config)
            if adapter.config.provider != "gemini":
                raise ValueError("Runner requires real Gemini; mock mode is only for offline tests")
            llm = BoundedModel(adapter, deadline)
        replay = MarketReplay(trader) if trader is not None else None
        signals = IndependentSignals(guardian) if guardian is not None else None
        log_event(log_path, "session_start", account=args.account, mode=args.mode,
                  submit=args.submit, duration_budget_seconds=args.duration)
        try:
            while True:
                deadline.check()
                try:
                    report = run_cycle(args.mode, trader, guardian, llm, state,
                                       submit=args.submit, flat_entry=args.flat_entry,
                                       replay=replay, signal_provider=signals)
                    summary = {key: (value if key == "recovery" else summarize(value))
                               for key, value in report.items()}
                    log_event(log_path, "cycle", result=summary)
                    cooldown = cooldown_seconds(report)
                    if args.mode == "recover":
                        print(json.dumps(unresolved_summary(state), indent=2))
                except Cooldown as exc:
                    cooldown = exc.seconds
                    log_event(log_path, "cooldown", retry_after=cooldown)
                except ApiError as exc:
                    cooldown = 0
                    log_event(log_path, "api_error", http_status=exc.status)
                    if exc.status in {401, 403}:
                        return 1
                except SessionEnding:
                    break
                except Exception as exc:
                    # Stop on unexpected persistence/programming errors; don't send
                    # another write after an unknown point of failure.
                    log_event(log_path, "cycle_stopped", error_type=type(exc).__name__)
                    return 1
                if args.once:
                    break
                wait_interval(max(args.interval, cooldown) + random.uniform(0, 2), deadline)
        except (SessionEnding, KeyboardInterrupt):
            pass
        finally:
            log_event(log_path, "session_end", unresolved=unresolved_summary(state))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as exc:
        print(f"Startup failed: Ledger HTTP {exc.status}. Check the original key and required role scope.")
        raise SystemExit(1)
    except Cooldown as exc:
        print(f"Startup cooldown: wait at least {exc.seconds:.1f} seconds.")
        raise SystemExit(1)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Runner stopped ({type(exc).__name__}). Check configuration, file access, and role locks.")
        raise SystemExit(1)
