"""Live Guardian workflow. Public review_pending defaults to read-only elsewhere.

Only recheck_and_submit can authorize trades. Its final check and POST use the
same locally constructed payload; a caller-supplied PASS is never accepted.
"""
import math
import time
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from decimal import Decimal, ROUND_FLOOR

from client import Cooldown, UncertainWrite
from guardian_rules import check_trade, check_theory_compliance, number, normalize_list
from llm_adapter import validate_guardian_response, validate_context
from guardian_signals import IndependentSignals
from custom_sma import crossover_flags
from theory_validation import validate_submitted_theory, TheoryValidationError

FINAL = {"EXECUTED", "BLOCKED", "REJECTED"}


def normalize_order(item):
    tid = item["transaction_id"]
    if type(tid) is not int or tid <= 0:
        raise ValueError("Invalid transaction ID")
    return {
        "transaction_id": tid, "symbol": item["symbol"],
        "action": item["action"], "quantity": item["requested_quantity"],
        "requested_price": item["requested_price"],
        "theory_id": item["theory_id"], "theory_version": item["theory_version"],
        "reasoning": item["reasoning"]
    }


def collect_evidence(client, order, clock=time.time):
    policy = client.get_policy()
    portfolio_response = client.get_portfolio()
    prices_response = client.get_prices(symbols=order["symbol"])
    quotes = [q for q in prices_response["prices"] if q["symbol"] == order["symbol"]]
    if len(quotes) != 1:
        raise ValueError("Missing/duplicate quote")
    quote = quotes[0]
    stamp = quote["timestamp_unix"]
    now = clock()
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp > now + 5 or now - stamp > 120):
        raise ValueError("Stale/future/invalid quote")
    number(quote["price"], positive=True)
    if quote.get("model_version") is None:
        raise ValueError("Missing model version")
    holdings = [h for h in portfolio_response["holdings"] if h["symbol"] == order["symbol"]]
    if len(holdings) > 1:
        raise ValueError("Duplicate holding")
    portfolio = {
        "cash_usd": portfolio_response["cash_balance"],
        "symbol_quantity": holdings[0]["quantity"] if holdings else 0,
        "symbol_avg_cost_usd": holdings[0]["avg_cost"] if holdings else None
    }
    return policy, portfolio, quote


def rule_review(result):
    decision = result.get("decision")
    if decision not in {"BLOCK", "ESCALATE"}:
        raise ValueError("Expected BLOCK or ESCALATE")
    rules = result.get("rules_triggered", [])
    return {"decision": decision,
            "reason": "Guardian checks: " + ", ".join(rules),
            "risk_score": 100 if decision == "BLOCK" else 50,
            "modified_quantity": None, "rules_triggered": rules}


def escalation(rule):
    return rule_review({"decision": "ESCALATE", "rules_triggered": [rule]})


def validated_review(order, model_result):
    raw = asdict(model_result) if is_dataclass(model_result) else deepcopy(model_result)
    if not isinstance(raw, dict):
        raise ValueError("Invalid model review")
    # Decimal strings are permitted only when converting a prepared API payload.
    # Never coerce bool to float for model validation.
    decision = validate_guardian_response(raw)
    payload = asdict(decision)
    payload["transaction_id"] = order["transaction_id"]
    if decision.decision == "MODIFY":
        q = number(decision.modified_quantity, positive=True)
        original = number(order["quantity"], positive=True)
        if q >= original or q != q.quantize(Decimal("1e-10")):
            raise ValueError("MODIFY must reduce quantity with <=10 decimals")
        payload["modified_quantity"] = format(q.normalize(), "f")
    return payload


def _validate_api_review(review):
    """Validate an exact Ledger payload, including prepared decimal strings."""
    fields = {"transaction_id", "decision", "reason", "risk_score", "modified_quantity", "rules_triggered"}
    if not isinstance(review, dict) or set(review) != fields:
        raise ValueError("Review must contain exactly the required fields")
    if type(review["transaction_id"]) is not int or review["transaction_id"] <= 0:
        raise ValueError("Invalid transaction ID")
    data = {k: deepcopy(v) for k, v in review.items() if k != "transaction_id"}
    if data["decision"] == "MODIFY":
        q = number(data["modified_quantity"], positive=True)
        if q != q.quantize(Decimal("1e-10")):
            raise ValueError("Invalid modification precision")
        data["modified_quantity"] = float(q)
    valid = asdict(validate_guardian_response(data))
    valid["transaction_id"] = review["transaction_id"]
    if valid["decision"] == "MODIFY":
        valid["modified_quantity"] = format(q.normalize(), "f")
    return valid


def _submit_once(client, payload):
    tid = payload["transaction_id"]
    try:
        response = client.submit_review(deepcopy(payload))
    except UncertainWrite:
        return {"transaction_id": tid, "status": "UNCERTAIN", "needs_reconciliation": True}
    if (not isinstance(response, dict)
            or type(response.get("transaction_id")) is not int
            or response["transaction_id"] != tid
            or response.get("status") not in FINAL | {"ESCALATED"}):
        return {"transaction_id": tid, "status": "UNCERTAIN", "needs_reconciliation": True}
    if response["status"] == "EXECUTED" and not response.get("idempotent"):
        fill = response.get("fill")
        try:
            number(fill["quantity"], positive=True)
            number(fill["price"], positive=True)
            number(fill["total"], positive=True)
        except (KeyError, TypeError, ValueError):
            return {"transaction_id": tid, "status": "UNCERTAIN", "needs_reconciliation": True}
    return {**response, "needs_reconciliation": False}


def submit_non_authorizing(client, review):
    payload = _validate_api_review(review)
    if payload["decision"] not in {"BLOCK", "ESCALATE"}:
        raise ValueError("Use recheck_and_submit for APPROVE/MODIFY")
    return _submit_once(client, payload)


def assess(order, theory, policy, portfolio, quote, signal_provider,
           require_flat_entry=False):
    hard = check_trade(order, policy, quote["price"], portfolio, theory)
    if hard["decision"] != "PASS":
        return hard, None
    try:
        validate_submitted_theory(theory)
    except TheoryValidationError:
        return {"decision": "ESCALATE", "rules_triggered": ["INVALID_THEORY_EVIDENCE"]}, None
    if ((require_flat_entry or (isinstance(theory, dict) and theory.get("strategy") == "custom"))
            and order["action"] == "BUY"
            and number(portfolio["symbol_quantity"]) > 0):
        return {"decision": "BLOCK", "rules_triggered": ["EXISTING_POSITION"]}, None
    if signal_provider is None:
        return {"decision": "ESCALATE", "rules_triggered": ["MISSING_VERIFIED_SIGNALS"]}, None
    signals = signal_provider(deepcopy(order), deepcopy(theory), deepcopy(quote))
    result = check_theory_compliance(order, theory, signals, portfolio, quote["price"])
    return result, signals


def build_context(order, theory, policy, portfolio, quote, signals, now,
                  require_flat_entry=False):
    price = number(quote["price"], positive=True)
    cash = number(portfolio["cash_usd"])
    holding = number(portfolio["symbol_quantity"])
    ceiling = number(policy["max_transaction_amount"], positive=True)
    fraction = number(theory["position_fraction"], positive=True)
    budget = min(cash * fraction, ceiling, cash).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    buy_qty = (budget / price).quantize(Decimal("1e-10"), rounding=ROUND_FLOOR)
    sell_qty = min(holding, ceiling / price).quantize(Decimal("1e-10"), rounding=ROUND_FLOOR)
    evidence = deepcopy(signals)
    threshold = number(theory["threshold_pct"], positive=True)
    lookback = Decimal(str(signals["lookback_return_pct"]))
    mean = Decimal(str(signals["mean_deviation_pct"]))
    cost_return = None
    exits = []
    if holding > 0:
        avg = number(portfolio["symbol_avg_cost_usd"], positive=True)
        cost_return = (price / avg - 1) * 100
        if cost_return <= -number(theory["stop_loss_pct"], positive=True):
            exits.append("stop_loss")
        if cost_return >= number(theory["take_profit_pct"], positive=True):
            exits.append("take_profit")
        if theory["strategy"] == "momentum" and lookback <= -threshold:
            exits.append("negative_momentum")
        if theory["strategy"] == "mean_reversion" and mean >= threshold:
            exits.append("mean_reversion_exit")
    if theory["strategy"] == "custom":
        entry, custom_sell = crossover_flags(theory, signals.get("custom"))
        if holding > 0 and custom_sell:
            exits.append("custom_exit")
        require_flat_entry = True
    else:
        entry = lookback >= threshold if theory["strategy"] == "momentum" else mean <= -threshold
    evidence.update(entry_triggered=bool(entry and not (require_flat_entry and holding > 0)),
                    exit_triggers=exits,
                    return_since_avg_cost_pct=float(cost_return) if cost_return is not None else None)
    context = {
        "context_version": 1, "decision_time_unix": int(now), "symbol": order["symbol"],
        "quote": {"price_usd": float(price), "timestamp_unix": quote["timestamp_unix"],
                  "model_version": quote["model_version"]},
        "theory": deepcopy(theory), "signals": evidence,
        "policy": {"allowed_symbols": normalize_list(policy["allowed_symbols"]),
                   "restricted_actions": normalize_list(policy["restricted_actions"]),
                   "max_transaction_amount_usd": float(ceiling),
                   "max_trades_per_hour": policy["max_trades_per_hour"],
                   "max_trades_per_day": policy["max_trades_per_day"],
                   "escalation_threshold_usd": policy["escalation_threshold"]},
        "trade_counts": {"executed_last_hour": policy["trades_last_hour"],
                         "executed_last_day": policy["trades_last_day"]},
        "portfolio": deepcopy(portfolio),
        "bounds": {"max_buy_budget_usd": float(budget), "max_buy_quantity": float(buy_qty),
                   "max_sell_quantity": float(sell_qty), "minimum_settled_amount_usd": .01,
                   "quantity_decimal_places": 10},
        "order": {"transaction_id": order["transaction_id"], "theory_id": order["theory_id"],
                  "theory_version": order["theory_version"], "action": order["action"],
                  "requested_quantity": float(number(order["quantity"], positive=True)),
                  "requested_price_usd": float(number(order["requested_price"], positive=True)),
                  "reasoning": order["reasoning"]},
        "assessment": {"requested_amount_at_current_quote_usd": float(number(order["quantity"]) * price),
                       "theory_snapshot_matches_order": True, "hard_rules_passed": True,
                       "rules_triggered": [], "requires_human_approval": False},
        "news": []
    }
    validate_context(context, "guardian")
    return context


def recheck_and_submit(client, order, review, theory_snapshot, signal_provider=None,
                       *, clock=time.time, require_flat_entry=False):
    order = deepcopy(order)
    payload = _validate_api_review(review)
    if payload["transaction_id"] != order["transaction_id"]:
        raise ValueError("Review transaction differs from original order")
    if payload["decision"] not in {"APPROVE", "MODIFY"}:
        return submit_non_authorizing(client, payload)
    final_order = deepcopy(order)
    if payload["decision"] == "MODIFY":
        q = number(payload["modified_quantity"], positive=True)
        if q >= number(order["quantity"], positive=True):
            raise ValueError("MODIFY must reduce original quantity")
        final_order["quantity"] = format(q, "f")
    try:
        policy, portfolio, quote = collect_evidence(client, order, clock)
        check, _ = assess(final_order, theory_snapshot, policy, portfolio,
                          quote, signal_provider, require_flat_entry)
        if check["decision"] != "PASS":
            payload = validated_review(order, rule_review(check))
    except Cooldown:
        raise
    except Exception:
        payload = validated_review(order, escalation("FINAL_EVIDENCE_UNAVAILABLE"))
    return _submit_once(client, payload)


def persist_result(state, tid, response):
    if state is None:
        return
    intent = state.get_intent_by_transaction(tid)
    if intent is not None:
        if response["status"] == "UNCERTAIN":
            state.mark_uncertain(intent["intent_id"])
        else:
            state.record_outcome(intent["intent_id"], response)


def reconcile_known_reviews(client, state):
    """Read-only reconciliation of known IDs. Unknown proposal IDs remain unresolved.

    Pending records may be reviewed again with fresh evidence. Never resend a
    proposal here. History is bounded; missing entries stay unresolved.
    """
    if state is None:
        return
    intents = state.get_unresolved_intents()
    if not intents:
        return
    history = client.get_transactions(limit=200)["transactions"]
    by_id = {x["transaction_id"]: x for x in history}
    for intent in intents:
        tid = intent["transaction_id"]
        record = by_id.get(tid)
        if record is not None and record.get("status") in FINAL | {"ESCALATED", "PENDING"}:
            state.record_outcome(intent["intent_id"], record)


def review_pending_live(client, llm, state, signal_provider=None, *,
                        clock=time.time, require_flat_entry=False):
    if state is None or llm is None:
        raise ValueError("Live mode requires a state store and an LLM adapter")
    if signal_provider is None:
        signal_provider = IndependentSignals(client)
    reconcile_known_reviews(client, state)
    pending = client.get_pending_orders(limit=2)["pending"]
    if not isinstance(pending, list):
        raise ValueError("Invalid pending response")
    results = []
    for item in pending:
        # Cannot safely send a review without a valid original ID.
        try:
            tid = item["transaction_id"]
            if type(tid) is not int or tid <= 0:
                raise ValueError("Invalid ID")
        except (KeyError, TypeError, ValueError):
            results.append({"status": "UNREVIEWABLE", "error": "INVALID_ORDER_ID"})
            continue
        try:
            order = normalize_order(item)
            theory = state.get_theory_snapshot(order["theory_id"], order["theory_version"])
            policy, portfolio, quote = collect_evidence(client, order, clock)
            check, signals = assess(order, theory, policy, portfolio, quote,
                                    signal_provider, require_flat_entry)
            if check["decision"] != "PASS":
                raw = rule_review(check)
            else:
                context = build_context(order, theory, policy, portfolio, quote,
                                        signals, clock(), require_flat_entry)
                raw = llm.guardian_decision(context)
            payload = validated_review(order, raw)
        except Cooldown:
            raise
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Guardian review failed for transaction %s: %s",
                tid,
                type(exc).__name__
                )

            # Preserve fail-safe escalation.
            order = {"transaction_id": tid}
            payload = {
                "transaction_id": tid,
                **escalation("EVIDENCE_OR_MODEL_UNAVAILABLE")
            }
        if payload["decision"] in {"APPROVE", "MODIFY"}:
            response = recheck_and_submit(client, order, payload, theory, signal_provider,
                                          clock=clock, require_flat_entry=require_flat_entry)
        else:
            response = submit_non_authorizing(client, payload)
        # If this write fails locally, propagate: do not continue to another POST.
        persist_result(state, tid, response)
        results.append(response)
        if response["status"] == "UNCERTAIN":
            break
    return results

