"""One trader cycle using real Ledger envelopes and agreed context version 1."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import math

from client import ApiError, Cooldown, UncertainWrite
from llm_adapter import validate_context
from market_replay import MarketReplay
from strategy import (StrategyError, num, validate_theory, validate_policy,
                      policy_context, calculate_signals, determine_triggers,
                      quantity_bounds, validate_decision)


def _hold(reason):
    return {"status": "HOLD", "reason": reason}


def _quote(response, symbol, now):
    prices = response["prices"]
    if not isinstance(prices, list):
        raise StrategyError("Expected prices list")
    found = [q for q in prices if q["symbol"] == symbol]
    if len(found) != 1:
        raise StrategyError("Missing/duplicate symbol quote")
    quote = found[0]
    stamp = quote["timestamp_unix"]
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp > now + 5 or now - stamp > 120):
        raise StrategyError("Stale/future quote")
    if num(quote["price"]) <= 0 or quote.get("model_version") is None:
        raise StrategyError("Invalid quote")
    return quote


def _portfolio(response, symbol):
    holdings = response["holdings"]
    if not isinstance(holdings, list):
        raise StrategyError("Expected holdings list")
    positions = [h for h in holdings if h["symbol"] == symbol]
    if len(positions) > 1:
        raise StrategyError("Duplicate holdings")
    cash = num(response["cash_balance"], "cash")
    held = num(positions[0]["quantity"], "holdings") if positions else num(0)
    avg = positions[0]["avg_cost"] if positions else None
    if cash < 0 or held < 0:
        raise StrategyError("Negative cash/holdings")
    return cash, held, avg


def _remember_response(state, intent_id, reply):
    """Keep unresolved on missing/ambiguous evidence. Never infer execution."""
    if (not isinstance(reply, dict)
            or type(reply.get("transaction_id")) is not int
            or reply["transaction_id"] <= 0):
        state.mark_uncertain(intent_id)
        return {"status": "UNRESOLVED", "intent_id": intent_id,
                "reason": "Missing transaction identity; reconcile before new proposals"}
    state.attach_transaction_id(intent_id, reply["transaction_id"])
    # A BUY/SELL proposal should be PENDING, or a documented logged rejection.
    if reply.get("status") not in {"PENDING", "REJECTED", "BLOCKED"}:
        state.mark_uncertain(intent_id)
        return {"status": "UNRESOLVED", "intent_id": intent_id,
                "transaction_id": reply["transaction_id"],
                "reason": "Unexpected proposal response; reconcile"}
    state.record_outcome(intent_id, reply)
    return {"status": "PROPOSED" if reply["status"] == "PENDING" else reply["status"],
            "ledger_status": reply["status"], "intent_id": intent_id,
            "transaction_id": reply["transaction_id"], "response": reply}


def run_trader_cycle(client, llm, state, *, symbol=None, decision_time=None,
                     replay=None, require_flat_entry=False, submit=True, buy_reserve_pct=1):
    """At most one proposal. Pass submit=False for a no-write preview.

    Reuse MarketReplay(client) between cycles for descriptor caching.
    Hind's chosen flat-entry theory needs require_flat_entry=True.
    Other written custom rules need explicit implementation, not just prompting.
    """
    now = decision_time if decision_time is not None else datetime.now(timezone.utc)
    try:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise StrategyError("Timezone-aware decision time required")
        if state.get_unresolved_intents():
            return _hold("Earlier local order remains unresolved")
        policy = validate_policy(client.get_policy())
        if (policy["trades_last_hour"] >= policy["max_trades_per_hour"]
                or policy["trades_last_day"] >= policy["max_trades_per_day"]):
            return _hold("Executed trade quota reached")
        theory = client.get_theory()["theory"]
        if not isinstance(theory, dict):
            return _hold("No active submitted theory")
        selected = theory["selected_symbols"]
        if not isinstance(selected, list) or len(selected) != 1:
            raise StrategyError("Each member's theory must select one currency")
        symbol = selected[0] if symbol is None else symbol
        try:
            validate_theory(theory, policy, symbol)
        except StrategyError as exc:
            return _hold(f"Invalid theory: {exc}")
        quote = _quote(client.get_prices(symbols=symbol), symbol, now.timestamp())
        cash, held, avg = _portfolio(client.get_portfolio(), symbol)
        news = client.get_news()["news"]
        if not isinstance(news, list):
            raise StrategyError("Expected news list")
        replay = replay if replay is not None else MarketReplay(client)
        points = replay.samples(symbol, quote, theory["lookback_minutes"])
        signals = calculate_signals(points, theory["lookback_minutes"], avg, held)
        if theory["strategy"] == "custom":
            from custom_sma import calculate_custom
            signals["custom"] = calculate_custom(replay.model, symbol, quote, theory)
        entry, exits = determine_triggers(theory, signals, held,
                                          require_flat_entry=require_flat_entry)
        signals.update(entry_triggered=entry, exit_triggers=exits, replay_matches_quote=True)
        expected = "SELL" if exits else ("BUY" if entry else "HOLD")
        if expected == "HOLD":
            return _hold("No eligible theory trigger")
        if expected in policy["restricted_actions"]:
            return _hold("Theory action is restricted")
        internal_policy = policy_context(policy)
        bounds = quantity_bounds(theory, internal_policy, cash, held, quote["price"])
        # Planning reserve only: theory/policy and Guardian limits stay unchanged.
        # A 1% reserve on a $500 hard BUY budget targets at most $495.
        reserve = num(buy_reserve_pct, "BUY reserve percent")
        if not 0 <= reserve < 100:
            raise StrategyError("BUY reserve percent must be in [0, 100)")
        if expected == "BUY":
            target = (num(bounds["max_buy_budget_usd"]) *
                      (1 - reserve / 100)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
            quantity = (target / num(quote["price"])).quantize(
                Decimal("1e-10"), rounding=ROUND_FLOOR)
            bounds["max_buy_budget_usd"] = float(target)
            bounds["max_buy_quantity"] = float(quantity)
        if bounds["max_buy_quantity" if expected == "BUY" else "max_sell_quantity"] <= 0:
            return _hold("No affordable/permitted quantity")
        context = {
            "context_version": 1, "decision_time_unix": int(now.timestamp()), "symbol": symbol,
            "quote": {"price_usd": float(num(quote["price"])),
                      "timestamp_unix": quote["timestamp_unix"], "model_version": quote["model_version"]},
            "theory": theory, "signals": signals, "policy": internal_policy,
            "trade_counts": {"executed_last_hour": policy["trades_last_hour"],
                             "executed_last_day": policy["trades_last_day"]},
            "portfolio": {"cash_usd": float(cash), "symbol_quantity": float(held),
                          "symbol_avg_cost_usd": float(num(avg)) if avg is not None else None},
            "bounds": bounds, "execution_state": {"has_unresolved_order": False}, "news": news
        }
        validate_context(context, "trader")
        # The adapter performs the real model call and schema validation.
        decision = llm.trader_decision(context)
        proposal = validate_decision(decision, symbol, expected, bounds, quote["price"], internal_policy)
        if proposal is None:
            return _hold("Model chose HOLD")
    except Cooldown as exc:
        return {"status": "COOLDOWN", "retry_after": exc.seconds}
    except Exception as exc:
        # Never expose raw provider/server errors, keys, or untrusted text.
        return _hold(f"Trade could not be verified ({type(exc).__name__})")

    order = {**proposal, "theory_id": theory["id"]}
    if not submit:
        return {"status": "PREVIEW", "proposal": order, "theory_version": theory["version"]}
    try:
        state.save_theory_snapshot(theory)
        intent_id = state.save_proposal_intent(order, theory_version=theory["version"])
        state.mark_submitting(intent_id)
    except Exception:
        return _hold("Persistence failed; no proposal was sent")
    try:
        reply = client.propose_trade(order)
    except Cooldown as exc:
        # Conservatively retain this started intent for reconciliation.
        state.mark_uncertain(intent_id)
        return {"status": "COOLDOWN", "retry_after": exc.seconds, "intent_id": intent_id,
                "needs_reconciliation": True}
    except ApiError as exc:
        # Ledger may return a logged REJECTED transaction with an HTTP error.
        body = exc.body
        if (isinstance(body, dict) and body.get("status") == "REJECTED"
                and type(body.get("transaction_id")) is int and body["transaction_id"] > 0):
            return _remember_response(state, intent_id, body)
        state.mark_uncertain(intent_id)
        return {"status": "UNRESOLVED", "intent_id": intent_id,
                "reason": "API write failed; reconcile before another proposal"}
    except Exception:
        state.mark_uncertain(intent_id)
        return {"status": "UNRESOLVED", "intent_id": intent_id,
                "reason": "Proposal response lost; reconcile before another proposal"}
    # If local persistence now fails, let the runner stop the cycle. The durable
    # SUBMITTING/PENDING record still prevents a new proposal after restart.
    result = _remember_response(state, intent_id, reply)
    result["proposal"] = order
    return result

