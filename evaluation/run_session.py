"""Sample before/after the existing agents and export one evaluation window.

python -m evaluation.run_session --member "Hind Alzaabi" --account hind-btc --flat-entry
Add --submit to enable simulated Ledger trades. No strategy files are modified.
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from client import LedgerClient, ApiError, Cooldown
from config import load_ledger_config
from llm_adapter import ModelConfig
from main import RoleLock
from evaluation.collector import collect_snapshot
from evaluation.report import write_report
from evaluation.session_recorder import SessionRecorder


class EvaluationDeadline(RuntimeError):
    pass


class ReadOnlyEvidenceClient(LedgerClient):
    """Use the shared HTTP protections and refuse every write from this client."""
    def request(self, method, endpoint, **kwargs):
        if method != "GET":
            raise ValueError("Evaluation collector is read-only")
        return super().request(method, endpoint, **kwargs)


def bounded_reads(client, deadline):
    original = client.session.request
    def request(*args, **kwargs):
        if time.monotonic() > deadline - 15:
            raise EvaluationDeadline("Evaluation deadline reached")
        kwargs["timeout"] = (2, 4)
        return original(*args, **kwargs)
    client.session.request = request
    return client


def error_fields(error):
    result = {"error_type": type(error).__name__}
    if isinstance(error, ApiError):
        result["http_status"] = error.status
    if isinstance(error, Cooldown):
        result["retry_after_seconds"] = max(0, error.seconds)
    return result


def save_json(path, value):
    # Refuse to overwrite an existing evidence export.
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)


def code_identity(root):
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True,
                                  text=True, timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    commit = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--untracked-files=normal")
    paths = list(root.glob("*.py")) + list((root / "evaluation").glob("*.py"))
    paths += list((root / "prompts").glob("*.txt"))
    if (root / "requirements.txt").exists():
        paths.append(root / "requirements.txt")
    hashes = {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(paths) if p.is_file()}
    return {"git_commit": commit, "working_tree_dirty": bool(dirty) if dirty is not None else None,
            "source_sha256": hashes}


def paired_sample(client, recorder, symbol, phase):
    quotes = client.get_prices(symbols=symbol)["prices"]
    quotes = [q for q in quotes if q.get("symbol") == symbol]
    if len(quotes) != 1:
        raise ValueError("Missing or duplicate currency quote")
    quote = quotes[0]
    portfolio = client.get_portfolio()
    received = time.time()
    price, quote_time = quote["price"], quote["timestamp_unix"]
    for value in (price, quote_time):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Invalid quote")
    if price <= 0 or not received - 30 <= quote_time <= received + 5:
        raise ValueError("Quote is stale, future-dated, or nonpositive")
    recorder.record_portfolio(portfolio["total_value"], timestamp_unix=received,
        phase=phase, cash_usd=portfolio["cash_balance"], price_usd=price,
        quote_timestamp_unix=quote_time, market_model_version=quote.get("model_version"))


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def local_unresolved(state_path, account):
    from state_store import StateStore
    from recovery import unresolved_summary
    with StateStore(state_path, account) as state:
        return unresolved_summary(state)


def run_evaluation(client, *, root, folder, args, metadata, deadline,
                   process_factory=subprocess.Popen):
    """External requests and the child process can be replaced by offline tests."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    before = collect_snapshot(client)
    before["account_label"] = args.account
    save_json(folder / "before.json", before)
    policy = client.get_policy()
    market_model = client.get_market_model()["model"]
    if not isinstance(market_model, dict):
        raise ValueError("Invalid public market descriptor")
    save_json(folder / "market_model.json", {"model": market_model,
                                          "received_at_unix": time.time()})
    theory = before["records"]["theory"]["response"]["theory"]
    metadata = {**metadata, "theory_snapshot": theory, "policy_start": policy,
                "submit_enabled": args.submit, "runner_duration_seconds": args.duration,
                "market_generator": market_model}
    recorder = SessionRecorder(folder, member=args.member, account_label=args.account,
        symbol=before["symbol"], theory_id=before["theory_id"],
        theory_version=before["theory_version"], sampling_interval_seconds=args.sample_interval,
        metadata=metadata)
    process = None
    evidence_path = None
    incomplete = False
    status = "complete"
    error_type = None
    try:
        paired_sample(client, recorder, before["symbol"], "start")
        if time.monotonic() + args.duration + 75 > deadline:
            raise EvaluationDeadline("Insufficient session budget after setup")
        command = [sys.executable, "-u", str(root / "main.py"), "--account", args.account,
                   "--mode", "both", "--duration", str(args.duration),
                   "--interval", str(args.interval), "--config", str(args.config),
                   "--llm-config", str(args.llm_config), "--state", str(args.state),
                   "--log", str(folder / "runner.jsonl")]
        if args.flat_entry:
            command.append("--flat-entry")
        if args.submit:
            command.append("--submit")
        launched = time.monotonic()
        process = process_factory(command, cwd=root)
        recorder.event("runner_start", submit=args.submit)
        next_due = launched + args.sample_interval
        while process.poll() is None:
            now = time.monotonic()
            if now > min(launched + args.duration + 45, deadline - 45):
                recorder.event("runner_stopped", reason="Time budget exhausted; reconcile local state")
                incomplete = True
                stop_child(process)
                break
            if now >= next_due:
                # Respect the collector client's cooldown before another read.
                if now < getattr(client, "next_allowed", 0):
                    incomplete = True
                    recorder.event("sample_error", error_type="Cooldown", phase="periodic",
                                   retry_after_seconds=client.next_allowed - now)
                else:
                    try:
                        paired_sample(client, recorder, before["symbol"], "periodic")
                    except Exception as exc:
                        incomplete = True
                        recorder.event("sample_error", phase="periodic", **error_fields(exc))
                next_due += args.sample_interval
                now = time.monotonic()
                if next_due <= now:
                    missed = math.floor((now - next_due) / args.sample_interval) + 1
                    next_due += missed * args.sample_interval
                    incomplete = True
                    recorder.event("sample_error", error_type="MissedSamplingIntervals", count=missed)
            else:
                time.sleep(min(.25, max(0, next_due - now)))
        recorder.event("runner_end", returncode=process.poll())
        if process.poll() != 0:
            incomplete = True
        # This read happens AFTER the child exits, so late fills affect the endpoint value.
        paired_sample(client, recorder, before["symbol"], "end")
        try:
            after = collect_snapshot(client)
            after["account_label"] = args.account
            policy_end = client.get_policy()
            after["policy"] = policy_end
            save_json(folder / "after.json", after)
            evidence_path = folder / "after.json"
            if ((after["theory_id"], after["theory_version"], after["symbol"]) !=
                    (before["theory_id"], before["theory_version"], before["symbol"])):
                incomplete = True
                recorder.event("configuration_changed", field="active_theory")
            # Executed counters change normally; compare policy settings, not counters.
            settings = ("allowed_symbols", "restricted_actions", "max_transaction_amount",
                        "max_trades_per_hour", "max_trades_per_day", "escalation_threshold")
            if any(policy.get(k) != policy_end.get(k) for k in settings):
                incomplete = True
                recorder.event("configuration_changed", field="security_policy")
        except Exception as exc:
            incomplete = True
            recorder.event("evidence_error", **error_fields(exc))
        try:
            recorder.event("local_state", unresolved=local_unresolved(args.state, args.account))
        except Exception as exc:
            incomplete = True
            recorder.event("evidence_error", source="local_state", **error_fields(exc))
    except KeyboardInterrupt:
        status, error_type = "interrupted", "KeyboardInterrupt"
        stop_child(process)
        recorder.event("runner_stopped", reason="User interrupted; reconcile local state")
        try:
            paired_sample(client, recorder, before["symbol"], "end")
        except Exception as exc:
            recorder.event("sample_error", phase="end", **error_fields(exc))
    except Exception as exc:
        status, error_type = "failed", type(exc).__name__
        stop_child(process)
        recorder.event("sample_error", phase="evaluation", **error_fields(exc))
    finally:
        stop_child(process)
        recorder.finish(status="incomplete" if incomplete and status == "complete" else status,
                        error_type=error_type)
    try:
        report_path = write_report(recorder.path, evidence_path)
    except (ValueError, OSError, TypeError, KeyError):
        report_path = None
    return {"folder": folder, "session": recorder.path, "report": report_path,
            "complete": status == "complete" and not incomplete}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--llm-config", type=Path, default=Path("config.llm.local.json"))
    parser.add_argument("--state", type=Path, default=Path("runtime/ledger_state.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("runtime/evaluation"))
    parser.add_argument("--duration", type=float, default=360, help="Agent budget: 90..420 seconds")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--sample-interval", type=float, default=15)
    parser.add_argument("--flat-entry", action="store_true")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.account) or not args.member.strip():
        parser.error("Use a stable account label and a nonempty member name")
    if not math.isfinite(args.duration) or not 90 <= args.duration <= 420:
        parser.error("Agent duration must be 90..420 seconds to leave time for evaluation reads")
    if not math.isfinite(args.interval) or args.interval < 60:
        parser.error("Trading interval must be at least 60 seconds")
    if not math.isfinite(args.sample_interval) or not 10 <= args.sample_interval <= 60:
        parser.error("Sampling interval must be 10..60 seconds")
    for name in ("config", "llm_config", "state", "output"):
        setattr(args, name, getattr(args, name).resolve())
    return args


def main(argv=None):
    args = arguments(argv)
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    folder = args.output / f"{args.account}_{stamp}_{uuid.uuid4().hex[:8]}"
    client = None
    try:
        config = load_ledger_config(args.config, "both")
        model = ModelConfig.from_file(args.llm_config)
        if model.provider not in {"openai", "gemini"}:
            raise ValueError("Evaluation needs a real provider")
        model.api_key()  # Check before launching trading, without printing the key.
        metadata = code_identity(root)
        metadata["model"] = {name: getattr(model, name) for name in
            ("provider", "model", "reasoning_effort", "max_output_tokens", "timeout_seconds", "max_retries")}
        client = bounded_reads(ReadOnlyEvidenceClient(config["LEDGER_BASE_URL"], config["TRADER_API_KEY"]),
                               time.monotonic() + 540)
        deadline = time.monotonic() + 539
        with RoleLock(str(args.state) + f".{args.account}.evaluation.lock"):
            result = run_evaluation(client, root=root, folder=folder, args=args,
                                    metadata=metadata, deadline=deadline)
        print(f"Evaluation files: {result['folder']}")
        print(f"Recording complete: {result['complete']}")
        if result["report"]:
            print(f"Report: {result['report']}")
        else:
            print("No report: inspect the recording; fewer than two valid observations may be available.")
        return 0 if result["complete"] and result["report"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Evaluation stopped ({type(exc).__name__}). Check setup; no credentials are printed.")
        print(f"Evidence folder, if created: {folder}")
        return 1
    finally:
        if client is not None:
            client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
