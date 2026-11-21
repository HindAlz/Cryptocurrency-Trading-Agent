"""Deterministic strategy and money checks. Percent values are percentage points."""
from dataclasses import asdict, is_dataclass
from theory_validation import validate_submitted_theory, TheoryValidationError
from custom_sma import validate_custom, crossover_flags
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

SUPPORTED = {"BTC", "ETH", "DOGE", "XMR", "XRP", "SOL"}
CENT = Decimal("0.01")
UNIT = Decimal("1e-10")


class StrategyError(ValueError):
    pass


def num(value, name="number"):
    if isinstance(value, bool) or value is None:
        raise StrategyError(f"Invalid {name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise StrategyError(f"Invalid {name}") from None
    if not result.is_finite():
        raise StrategyError(f"Non-finite {name}")
    return result


def validate_theory(theory, policy, symbol):
    try:
        validate_submitted_theory(theory)
    except TheoryValidationError as exc:
        raise StrategyError(str(exc)) from None
    if not isinstance(theory, dict) or not isinstance(policy, dict):
        raise StrategyError("Missing theory/policy")
    for field in ("id", "version"):
        if type(theory.get(field)) is not int or theory[field] <= 0:
            raise StrategyError("Invalid theory identity")
    if not isinstance(theory.get("status"), str) or theory["status"].upper() != "SUBMITTED":
        raise StrategyError("Theory must be submitted")
    if symbol not in SUPPORTED:
        raise StrategyError("Unsupported symbol")
    for owner, field in ((theory, "selected_symbols"), (policy, "allowed_symbols")):
        entries = owner.get(field)
        if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
            raise StrategyError("Invalid symbol list")
        if symbol not in entries:
            raise StrategyError("Symbol not permitted")
    if theory.get("strategy") == "custom":
        validate_custom(theory)
    elif theory.get("strategy") not in {"momentum", "mean_reversion"}:
        raise StrategyError("Unimplemented strategy")
    lookback = theory.get("lookback_minutes")
    if type(lookback) is not int or not 1 <= lookback <= 1440:
        raise StrategyError("Invalid lookback")
    for field, low, high in (("threshold_pct", ".01", "50"),
                             ("position_fraction", ".001", "1"),
                             ("stop_loss_pct", ".01", "100"),
                             ("take_profit_pct", ".01", "100")):
        if not Decimal(low) <= num(theory.get(field), field) <= Decimal(high):
            raise StrategyError(f"Out-of-range {field}")
    for field in ("title", "hypothesis", "entry_rule", "exit_rule", "risk_rule"):
        if not isinstance(theory.get(field), str) or not theory[field].strip():
            raise StrategyError(f"Missing theory text: {field}")
    return theory


def validate_policy(policy):
    for field in ("allowed_symbols", "restricted_actions"):
        entries = policy[field]
        if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
            raise StrategyError("Expected policy arrays")
    for field in ("max_transaction_amount", "escalation_threshold"):
        if num(policy[field], field) <= 0:
            raise StrategyError("Invalid money policy")
    for field in ("max_trades_per_hour", "max_trades_per_day", "trades_last_hour", "trades_last_day"):
        if type(policy[field]) is not int or policy[field] < 0:
            raise StrategyError("Invalid quota/count")
    return policy


def policy_context(policy):
    return {
        "allowed_symbols": list(policy["allowed_symbols"]),
        "restricted_actions": list(policy["restricted_actions"]),
        "max_transaction_amount_usd": float(num(policy["max_transaction_amount"])),
        "max_trades_per_hour": policy["max_trades_per_hour"],
        "max_trades_per_day": policy["max_trades_per_day"],
        "escalation_threshold_usd": float(num(policy["escalation_threshold"]))
    }


def calculate_signals(points, lookback_minutes, avg_cost, holdings):
    if len(points) != 61:
        raise StrategyError("Expected 61 samples")
    start = points[0].timestamp.timestamp()
    end = points[-1].timestamp.timestamp()
    if end - start != lookback_minutes * 60:
        raise StrategyError("Wrong replay window")
    for i, point in enumerate(points):
        if abs(point.timestamp.timestamp() - (start + (end - start) * i / 60)) > .000001:
            raise StrategyError("Samples must be evenly spaced")
    prices = [num(p.price, "sample price") for p in points]
    if any(p <= 0 for p in prices):
        raise StrategyError("Nonpositive sample")
    price = prices[-1]
    mean = sum(prices) / 61
    held = num(holdings, "holdings")
    if held < 0:
        raise StrategyError("Negative holdings")
    cost_return = None
    if held > 0:
        avg = num(avg_cost, "average cost")
        if avg <= 0:
            raise StrategyError("Invalid average cost")
        cost_return = float((price / avg - 1) * 100)
    return {"lookback_return_pct": float((price / prices[0] - 1) * 100),
            "mean_deviation_pct": float((price / mean - 1) * 100),
            "return_since_avg_cost_pct": cost_return}


def determine_triggers(theory, signals, holdings, *, require_flat_entry=False):
    threshold = num(theory["threshold_pct"])
    owned = num(holdings)
    if theory["strategy"] == "momentum":
        signal = num(signals["lookback_return_pct"])
        entry, exit_signal = signal >= threshold, signal <= -threshold
        exit_name = "negative_momentum"
    elif theory["strategy"] == "mean_reversion":
        signal = num(signals["mean_deviation_pct"])
        entry, exit_signal = signal <= -threshold, signal >= threshold
        exit_name = "mean_reversion_exit"
    elif theory["strategy"] == "custom":
        try:
            entry, exit_signal = crossover_flags(theory, signals.get("custom"))
        except (ValueError, KeyError, TypeError) as exc:
            raise StrategyError("Invalid/unsupported custom evidence") from exc
        exit_name = "custom_exit"
        require_flat_entry = True
    else:
        raise StrategyError("Unsupported strategy")
    exits = []
    if owned > 0:
        cost_return = num(signals["return_since_avg_cost_pct"])
        if cost_return <= -num(theory["stop_loss_pct"]):
            exits.append("stop_loss")
        if cost_return >= num(theory["take_profit_pct"]):
            exits.append("take_profit")
        if exit_signal:
            exits.append(exit_name)
    return bool(entry and not (require_flat_entry and owned > 0)), exits


def settled_value(action, quantity, price):
    exact = num(quantity) * num(price)
    return exact.quantize(CENT, rounding=ROUND_CEILING if action == "BUY" else ROUND_FLOOR)


def quantity_bounds(theory, policy, cash, holdings, price, decimal_places=10, minimum_usd=".01"):
    if decimal_places != 10 or num(minimum_usd) != CENT:
        raise StrategyError("Course execution rules require 10 quantity decimals and $0.01 minimum")
    p, cash, held = num(price), num(cash), num(holdings)
    cap = num(policy["max_transaction_amount_usd"])
    fraction = num(theory["position_fraction"])
    if p <= 0 or cash < 0 or held < 0 or cap <= 0 or not 0 < fraction <= 1:
        raise StrategyError("Invalid sizing inputs")
    budget = min(cash * fraction, cash, cap).quantize(CENT, rounding=ROUND_FLOOR)
    buy = (budget / p).quantize(UNIT, rounding=ROUND_FLOOR)
    sell = min(held, cap / p).quantize(UNIT, rounding=ROUND_FLOOR)
    if settled_value("BUY", buy, p) < CENT:
        buy = Decimal(0)
    if settled_value("SELL", sell, p) < CENT:
        sell = Decimal(0)
    return {"max_buy_budget_usd": float(budget), "max_buy_quantity": float(buy),
            "max_sell_quantity": float(sell), "minimum_settled_amount_usd": .01,
            "quantity_decimal_places": 10}


def validate_decision(decision, symbol, expected, bounds, price, policy):
    if is_dataclass(decision):
        decision = asdict(decision)
    if not isinstance(decision, dict) or set(decision) != {"symbol", "action", "quantity", "reasoning"}:
        raise StrategyError("Invalid model fields")
    action = decision["action"]
    if not isinstance(action, str) or action not in {"BUY", "SELL", "HOLD"}:
        raise StrategyError("Invalid action")
    q = num(decision["quantity"], "quantity")
    reason = decision["reasoning"]
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 16000:
        raise StrategyError("Invalid reasoning")
    # HOLD is local and does not need a permitted currency or minimum value.
    if action == "HOLD":
        if q != 0:
            raise StrategyError("HOLD requires zero quantity")
        return None
    if decision["symbol"] != symbol or symbol not in policy["allowed_symbols"]:
        raise StrategyError("Wrong/disallowed symbol")
    if action != expected or action in policy["restricted_actions"]:
        raise StrategyError("Action not justified/permitted")
    maximum = num(bounds["max_buy_quantity" if action == "BUY" else "max_sell_quantity"])
    if q <= 0 or q > maximum or q != q.quantize(UNIT):
        raise StrategyError("Invalid quantity/precision")
    p = num(price)
    exact = q * p
    settled = settled_value(action, q, p)
    if settled < CENT or max(exact, settled) > num(policy["max_transaction_amount_usd"]):
        raise StrategyError("Invalid settled value")
    if action == "BUY" and max(exact, settled) > num(bounds["max_buy_budget_usd"]):
        raise StrategyError("BUY exceeds budget after settlement")
    return {"symbol": symbol, "action": action, "quantity": format(q.normalize(), "f"),
            "reasoning": reason.strip()}

