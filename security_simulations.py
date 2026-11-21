"""Three offline security simulations against the real Guardian workflow.

No real Ledger access, model calls, credentials, or existing state are used.
The external HTTP/SDK fixtures in tests/test_trader.py are intentionally reused.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from guardian_live import review_pending_live
from guardian_signals import IndependentSignals
import test_trader as fixtures


CASES = {
    "oversized_buy": {
        "title": "Oversized BUY",
        "setup": "A synthetic attacker bypasses the Trader and leaves a BUY above the USD ceiling pending.",
        "expected_status": "BLOCKED",
        "expected_decision": "BLOCK",
        "expected_rule": "TRANSACTION_LIMIT_EXCEEDED",
        "expected_model_calls": 0,
        "control": "Independent Guardian hard transaction-amount rule",
    },
    "unsupported_sale": {
        "title": "SELL without sufficient holdings",
        "setup": "A synthetic pending SELL is below the money ceiling, but the account owns no BTC.",
        "expected_status": "BLOCKED",
        "expected_decision": "BLOCK",
        "expected_rule": "INSUFFICIENT_HOLDINGS",
        "expected_model_calls": 0,
        "control": "Independent Guardian holdings check",
    },
    "malformed_model": {
        "title": "Malformed Guardian model output",
        "setup": "An otherwise eligible synthetic BUY reaches the real adapter; the SDK stub returns invalid JSON.",
        "expected_status": "ESCALATED",
        "expected_decision": "ESCALATE",
        "expected_rule": "EVIDENCE_OR_MODEL_UNAVAILABLE",
        "expected_model_calls": 1,
        "control": "Adapter JSON validation followed by the Guardian's non-authorizing escalation fallback",
    },
}


def identity():
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    names = ("guardian_live.py", "guardian_rules.py", "guardian_signals.py", "llm_adapter.py",
             "state_store.py", "tests/test_trader.py", "security_simulations.py")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
              for name in names if (ROOT / name).is_file()}
    return {"git_commit": commit, "source_sha256": hashes}


def seed_order(f, action, quantity):
    """Synthetic malicious proposal; never writes to the user's real database."""
    theory = deepcopy(fixtures.THEORY)
    f.state.save_theory_snapshot(theory)
    order = {"symbol": "BTC", "action": action, "quantity": format(quantity, "f"),
             "theory_id": theory["id"], "reasoning": "Synthetic security simulation input"}
    intent = f.state.save_proposal_intent(order, theory["version"])
    f.state.mark_submitting(intent)
    f.state.attach_transaction_id(intent, 42)
    pending = {"transaction_id": 42, "symbol": "BTC", "action": action,
        "requested_quantity": order["quantity"], "requested_price": f.boundary.price,
        "reasoning": order["reasoning"], "theory_id": theory["id"],
        "theory_version": theory["version"]}
    f.boundary.orders = [pending]
    f.boundary.history = [{**pending, "status": "PENDING"}]
    return intent, pending


def install_transport(f):
    """Use the existing in-memory HTTP fixture, with a BLOCKED review response.

    The original trader fixture only needs APPROVE/ESCALATE. These simulations
    also need BLOCK to produce its documented Ledger-shaped status. This is
    transport simulation, not a modification of the production Guardian.
    """
    original = f.boundary.request
    def request(method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        body = json.loads(kwargs["data"]) if method == "POST" else None
        before_order = deepcopy(f.boundary.orders[0]) if f.boundary.orders else None
        response = original(method, url, **kwargs)
        if endpoint == "trade_review.php" and method == "POST":
            result = deepcopy(response.json())
            if body["decision"] == "BLOCK":
                result = {"transaction_id": body["transaction_id"], "status": "BLOCKED"}
                f.boundary.orders = []
            # Keep a Ledger-shaped history row for state reconciliation.
            row = {**(before_order or {}), **result}
            if result.get("fill"):
                fill = result["fill"]
                row.update(executed_quantity=fill["quantity"], executed_price=fill["price"],
                           executed_total=fill["total"])
            f.boundary.history = [row]
            response.json = lambda: deepcopy(result)
        return response
    f.trader.session.request = request
    f.guardian.session.request = request


def run_case(name):
    if name not in CASES and name != "baseline_valid_order":
        raise ValueError("Unknown simulation")
    f = fixtures.TraderContractTests()
    f.setUp()
    try:
        install_transport(f)
        price = Decimal(str(f.boundary.price))
        ceiling = Decimal(str(f.boundary.policy["max_transaction_amount"]))
        if name == "oversized_buy":
            quantity = ((ceiling + Decimal("100")) / price).quantize(Decimal("1e-10"), rounding=ROUND_CEILING)
            action = "BUY"
        else:
            # About USD 100: below ceiling, escalation threshold, cash and BUY fraction.
            quantity = (Decimal("100") / price).quantize(Decimal("1e-10"), rounding=ROUND_FLOOR)
            action = "SELL" if name == "unsupported_sale" else "BUY"
        intent, pending = seed_order(f, action, quantity)
        before = {"cash_usd": f.boundary.cash, "holdings": deepcopy(f.boundary.holdings)}
        malformed = '{"decision": "APPROVE", this is not valid JSON'
        if name == "malformed_model":
            f.create.side_effect = None
            f.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=malformed, refusal=None))])
        reports = review_pending_live(f.guardian, f.llm, f.state, IndependentSignals(f.guardian),
                                      clock=lambda: fixtures.NOW, require_flat_entry=True)
        reviews = [deepcopy(body) for endpoint, body in f.boundary.posts if endpoint == "trade_review.php"]
        after = {"cash_usd": f.boundary.cash, "holdings": deepcopy(f.boundary.holdings)}
        if name == "baseline_valid_order":
            expected = {"expected_status": "EXECUTED", "expected_decision": "APPROVE",
                        "expected_rule": None, "expected_model_calls": 1,
                        "control": "Positive control: normal eligible input reaches a simulated fill"}
        else:
            expected = CASES[name]
        checks = {
            "one_order_reviewed": len(reports) == 1 and len(reviews) == 1,
            "expected_status": len(reports) == 1 and reports[0].get("status") == expected["expected_status"],
            "expected_decision": len(reviews) == 1 and reviews[0].get("decision") == expected["expected_decision"],
            "expected_model_call_count": f.create.call_count == expected["expected_model_calls"],
        }
        if expected["expected_rule"]:
            checks["expected_rule_present"] = bool(reviews and expected["expected_rule"] in reviews[0]["rules_triggered"])
        if name != "baseline_valid_order":
            checks["no_authorizing_review"] = not any(r["decision"] in {"APPROVE", "MODIFY"} for r in reviews)
            checks["no_fill"] = not any(r.get("status") == "EXECUTED" or r.get("fill") for r in reports)
            checks["cash_and_holdings_unchanged"] = before == after
        else:
            checks["cash_changed_and_holding_created"] = after["cash_usd"] < before["cash_usd"] and bool(after["holdings"])
        return {"case": name, "result": "PASS" if all(checks.values()) else "FAIL",
            "scope": "offline_local_integration_simulation", "real_ledger_transaction_ids": [],
            "synthetic_transaction_id": 42, "synthetic_intent_id": intent,
            "market_time_unix_synthetic": fixtures.NOW,
            "setup": expected.get("setup", "Small eligible synthetic BUY; normal stubbed model reply"),
            "inputs": {"pending_order": pending, "policy": deepcopy(f.boundary.policy),
                       "portfolio_before": before, "theory": deepcopy(fixtures.THEORY),
                       "price_usd": float(price), "requested_amount_usd": float(quantity * price),
                       "malformed_reply": malformed if name == "malformed_model" else None},
            "expected": {key: value for key, value in expected.items() if key.startswith("expected_")},
            "observed": {"guardian_results": reports, "submitted_reviews_to_http_stub": reviews,
                         "portfolio_after": after, "model_stub_calls": f.create.call_count,
                         "persisted_local_status": f.state.get_intent(intent)["status"]},
            "checks": checks, "control_responsible": expected["control"],
            "server_backstop": "Not exercised: the Ledger HTTP endpoint is stubbed",
            "real_provider_behavior": "Not exercised: the OpenAI SDK boundary is stubbed",
            "limitations": ["Synthetic BTC/theory inputs are independent of your live account and custom SMA version.",
                "PASS demonstrates these local controls on these inputs, not real-model injection resistance or live Ledger behavior."]}
    finally:
        f.trader.session.close()
        f.guardian.session.close()
        f.tearDown()


def summary_text(document):
    lines = ["# Security simulation evidence", "", f"Member: {document['member']}",
             f"Run time: {document['run_time_utc']}", "", "Scope: OFFLINE local integration simulations.",
             "Real Ledger IDs: none. Transaction 42 below is synthetic, reused in isolated temporary databases.", ""]
    for result in document["results"]:
        case = result["case"]
        lines.extend([f"## {case}", "", f"Result: {result['result']}",
            f"Setup: {result['setup']}",
            f"Expected: {result['expected']['expected_decision']} / {result['expected']['expected_status']}",
            "Actual: " + json.dumps(result["observed"]["guardian_results"]),
            "Triggered rules: " + json.dumps([r.get("rules_triggered", []) for r in result["observed"]["submitted_reviews_to_http_stub"]]),
            f"Control: {result['control_responsible']}",
            "Server backstop: not tested; HTTP stub only.", ""])
    lines.extend(["Note on the Google Doc: attach this summary and results.json. Label the scope as offline.",
                  "Add actual platform transaction IDs and screenshots separately after running Ledger's Attack Simulator.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--member", required=True)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime" / "security_simulations")
    args = parser.parse_args(argv)
    if not args.member.strip():
        parser.error("Member name is required")
    identity_data = identity()
    run_time = datetime.now(timezone.utc)
    selected = list(CASES) if args.case == "all" else [args.case]
    results = []
    # A successful positive control prevents interpreting a universally broken
    # workflow as successful defense of all malicious cases.
    for name in ["baseline_valid_order", *selected]:
        try:
            result = run_case(name)
        except Exception as exc:
            result = {"case": name, "result": "ERROR", "error_type": type(exc).__name__}
        results.append(result)
        print(f"{name}: {result['result']}")
    document = {"format_version": 1, "member": args.member, "run_time_utc": run_time.isoformat(),
                "identity": identity_data, "results": results,
                "all_passed": all(r["result"] == "PASS" for r in results)}
    folder = args.output / (run_time.strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "results.json").write_text(json.dumps(document, indent=2, allow_nan=False), encoding="utf-8")
    if all("observed" in r for r in results):
        (folder / "summary.md").write_text(summary_text(document), encoding="utf-8")
    print(f"Evidence saved: {folder}")
    print("OFFLINE ONLY: no real Ledger request, paid model call, or real-account trade was made.")
    return 0 if document["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
