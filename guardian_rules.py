
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

SUPPORTED = {"BTC", "ETH", "DOGE", "XMR", "XRP", "SOL"}

INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all instructions",
    "bypass guardian",
    "skip guardian",
    "disable security",
    "override security",
    "reveal api key",
)


def number(value, positive=False):
    """Convert API numbers safely to Decimal."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid numeric value")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid numeric value") from None

    if not result.is_finite():
        raise ValueError("Non-finite number")

    if positive and result <= 0:
        raise ValueError("Expected a positive number")

    if not positive and result < 0:
        raise ValueError("Expected a non-negative number")

    return result


def normalize_list(value):
    """Accept Ledger arrays or comma-separated policy values."""
    if isinstance(value, list):
        if not all(isinstance(x, str) for x in value):
            raise ValueError("Invalid policy list")
        return [x.strip().upper() for x in value]

    if isinstance(value, str):
        return [x.strip().upper() for x in value.split(",") if x.strip()]

    raise ValueError("Missing policy list")


def check_trade(order, policy, current_price,
                portfolio=None, theory_snapshot=None):
    """
    Evaluate a proposed order using independent evidence.

    portfolio is normalized to:
        {"cash_usd": ..., "symbol_quantity": ...}

    Returns PASS, BLOCK, or ESCALATE.
    PASS is not final authorization; the LLM and final
    recheck must still run.
    """
    violations = []
    escalation_reasons = []

    def block(rule):
        if rule not in violations:
            violations.append(rule)

    def escalate(rule):
        if rule not in escalation_reasons:
            escalation_reasons.append(rule)

    if not isinstance(order, dict) or not isinstance(policy, dict):
        return {
            "decision": "ESCALATE",
            "passed": False,
            "rules_triggered": ["MISSING_SECURITY_EVIDENCE"]
        }

    symbol = order.get("symbol")
    action = order.get("action")

    # Rule 1: Allowed currencies
    try:
        allowed = normalize_list(policy["allowed_symbols"])
        if symbol not in SUPPORTED or symbol not in allowed:
            block("DISALLOWED_CURRENCY")
    except (KeyError, ValueError):
        escalate("MISSING_CURRENCY_POLICY")

    # Rule 2: Restricted actions
    try:
        restricted = normalize_list(policy["restricted_actions"])
        if action not in {"BUY", "SELL"} or action in restricted:
            block("RESTRICTED_ACTION")
    except (KeyError, ValueError):
        escalate("MISSING_ACTION_POLICY")

    # Rule 3: Validate quantity, quote and monetary limits
    amount = None
    settled = None
    quantity = None

    try:
        quantity = number(order["quantity"], positive=True)
        price = number(current_price, positive=True)

        # Ledger quantities support up to 10 decimal places.
        if quantity.as_tuple().exponent < -10:
            raise ValueError("Too many quantity decimals")

        if len(quantity.as_tuple().digits) > 28:
            raise ValueError("Quantity too large")

        amount = quantity * price

        rounding = (
            ROUND_CEILING if action == "BUY" else ROUND_FLOOR
        )
        settled = amount.quantize(
            Decimal("0.01"), rounding=rounding
        )

        if settled < Decimal("0.01"):
            block("INVALID_SETTLED_AMOUNT")

        limit = number(policy["max_transaction_amount"], positive=True)
        if max(amount, settled) > limit:
            block("TRANSACTION_LIMIT_EXCEEDED")

    except (KeyError, ValueError, InvalidOperation):
        block("INVALID_TRANSACTION_DATA")

    # Rule 4: Hourly and daily frequency
    try:
        hourly = number(policy["trades_last_hour"])
        daily = number(policy["trades_last_day"])
        max_hourly = number(policy["max_trades_per_hour"], positive=True)
        max_daily = number(policy["max_trades_per_day"], positive=True)

        if hourly >= max_hourly:
            block("HOURLY_QUOTA_EXCEEDED")

        if daily >= max_daily:
            block("DAILY_QUOTA_EXCEEDED")

    except (KeyError, ValueError):
        escalate("MISSING_TRADE_COUNTS")

    # Rule 5: Affordability and holdings
    if not isinstance(portfolio, dict):
        escalate("MISSING_PORTFOLIO")
    else:
        try:
            if action == "BUY" and settled is not None:
                cash = number(portfolio["cash_usd"])
                if settled > cash:
                    block("INSUFFICIENT_CASH")

            elif action == "SELL" and quantity is not None:
                holdings = number(portfolio["symbol_quantity"])
                if quantity > holdings:
                    block("INSUFFICIENT_HOLDINGS")

        except (KeyError, ValueError):
            escalate("INVALID_PORTFOLIO_EVIDENCE")

    # Rule 6: Escalation threshold
    if amount is not None and settled is not None:
        try:
            threshold = number(
                policy["escalation_threshold"], positive=True
            )
            if max(amount, settled) > threshold:
                escalate("ESCALATION_THRESHOLD_EXCEEDED")
        except (KeyError, ValueError):
            escalate("MISSING_ESCALATION_THRESHOLD")

    # Rule 7: Suspicious trader reasoning
    reasoning = order.get("reasoning", "")
    if not isinstance(reasoning, str):
        escalate("INVALID_REASONING")
    elif any(
        phrase in reasoning.lower()
        for phrase in INJECTION_PATTERNS
    ):
        escalate("SUSPICIOUS_INSTRUCTIONS")

    # Rule 8: Saved theory verification
    if not isinstance(theory_snapshot, dict):
        escalate("MISSING_THEORY_SNAPSHOT")
    else:
        try:
            if (
                order["theory_id"] != theory_snapshot["id"]
                or order["theory_version"] != theory_snapshot["version"]
                or theory_snapshot["status"] != "SUBMITTED"
            ):
                escalate("THEORY_MISMATCH")

            selected = normalize_list(
                theory_snapshot["selected_symbols"]
            )
            if symbol not in selected:
                block("SYMBOL_NOT_IN_THEORY")

        except (KeyError, ValueError):
            escalate("INVALID_THEORY_EVIDENCE")

    # Hard failures have priority over escalation.
    if violations:
        decision = "BLOCK"
    elif escalation_reasons:
        decision = "ESCALATE"
    else:
        decision = "PASS"

    return {
        "decision": decision,
        "passed": decision == "PASS",
        "rules_triggered": violations + escalation_reasons
    }


def check_theory_compliance(
    order, theory, signals, portfolio, current_price
):
    """
    Check whether an order follows its submitted trading theory.

    IMPORTANT:
    signals must come from independently verified market
    calculations, not the trader's reasoning or claims.

    Returns PASS, BLOCK, or ESCALATE.
    """

    violations = []

    if not isinstance(theory, dict):
        return {
            "decision": "ESCALATE",
            "rules_triggered": ["MISSING_THEORY_SNAPSHOT"]
        }

    if not isinstance(signals, dict):
        return {
            "decision": "ESCALATE",
            "rules_triggered": ["MISSING_VERIFIED_SIGNALS"]
        }

    try:
        # Verify the historical theory belongs to this order.
        if (
            order["theory_id"] != theory["id"]
            or order["theory_version"] != theory["version"]
            or theory["status"] != "SUBMITTED"
        ):
            return {
                "decision": "ESCALATE",
                "rules_triggered": ["THEORY_IDENTITY_MISMATCH"]
            }

        if order["symbol"] not in normalize_list(
            theory["selected_symbols"]
        ):
            violations.append("SYMBOL_NOT_IN_THEORY")

        # Only implement the documented momentum strategy here.
        if theory["strategy"] != "momentum":
            return {
                "decision": "ESCALATE",
                "rules_triggered": ["STRATEGY_NOT_IMPLEMENTED"]
            }

        if signals.get("replay_matches_quote") is not True:
            return {
                "decision": "ESCALATE",
                "rules_triggered": ["MARKET_EVIDENCE_UNVERIFIED"]
            }

        action = order["action"]
        quantity = number(order["quantity"], positive=True)
        price = number(current_price, positive=True)

        threshold = number(
            theory["threshold_pct"], positive=True
        )
        fraction = number(
            theory["position_fraction"], positive=True
        )

        if fraction > 1:
            raise ValueError("Invalid position fraction")

        if not 1 <= int(theory["lookback_minutes"]) <= 1440:
            raise ValueError("Invalid lookback")

        lookback_return = Decimal(
            str(signals["lookback_return_pct"])
        )
        if not lookback_return.is_finite():
            raise ValueError("Invalid market return")

        if action == "BUY":
            # Momentum entry condition
            if lookback_return < threshold:
                violations.append("BUY_SIGNAL_NOT_TRIGGERED")

            cash = number(portfolio["cash_usd"])
            budget = cash * fraction

            exact_cost = quantity * price
            settled_cost = exact_cost.quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )

            if max(exact_cost, settled_cost) > budget:
                violations.append("POSITION_FRACTION_EXCEEDED")

        elif action == "SELL":
            # Momentum sell, stop loss, or take profit.
            avg_cost = number(
                portfolio["symbol_avg_cost_usd"]
            )

            if avg_cost <= 0:
                return {
                    "decision": "ESCALATE",
                    "rules_triggered": ["MISSING_AVERAGE_COST"]
                }

            change_from_cost = (
                (price - avg_cost) / avg_cost
            ) * Decimal("100")

            stop_loss = number(
                theory["stop_loss_pct"], positive=True
            )
            take_profit = number(
                theory["take_profit_pct"], positive=True
            )

            momentum_sell = lookback_return <= -threshold
            stop_trigger = change_from_cost <= -stop_loss
            profit_trigger = change_from_cost >= take_profit

            if not (
                momentum_sell or stop_trigger or profit_trigger
            ):
                violations.append("SELL_SIGNAL_NOT_TRIGGERED")

        else:
            violations.append("INVALID_THEORY_ACTION")

    except (
        KeyError, TypeError, ValueError,
        InvalidOperation, OverflowError, ArithmeticError
    ):
        return {
            "decision": "ESCALATE",
            "rules_triggered": ["INVALID_THEORY_EVIDENCE"]
        }

    return {
        "decision": "BLOCK" if violations else "PASS",
        "rules_triggered": violations
    }
