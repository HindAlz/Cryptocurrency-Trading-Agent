"""Registered BTC custom strategy. Experimental parameters, not optimized.

Fast SMA: 5 completed one-minute closes. Slow SMA: 15. Compare the
percentage SMA spread with +/- theory.threshold_pct to detect buffered crosses.
Both roles replay their own evidence; never evaluate future timestamps.
"""
import math
from decimal import Decimal

STRATEGY_ID = "btc_sma5_15_v1"
TITLE = "BTC Buffered SMA 5/15 v1"
FAST = 5
SLOW = 15


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid custom number")
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Non-finite custom number")
    return value


def validate_custom(theory):
    if (theory.get("strategy") != "custom" or theory.get("title") != TITLE
            or theory.get("selected_symbols") != ["BTC"]):
        raise ValueError("Unregistered custom theory; use the documented exact title")
    if type(theory.get("lookback_minutes")) is not int or theory["lookback_minutes"] != 16:
        raise ValueError("This custom implementation requires lookback_minutes=16")
    if not Decimal("0.01") <= _number(theory["threshold_pct"]) <= Decimal("50"):
        raise ValueError("Invalid SMA spread threshold")


def _metrics(prices, threshold):
    if not isinstance(prices, list) or len(prices) != 16:
        raise ValueError("Custom strategy needs 16 completed minute prices")
    p = [_number(v) for v in prices]
    if any(v <= 0 for v in p):
        raise ValueError("Custom prices must be positive")
    fast = sum(p[-FAST:]) / FAST
    slow = sum(p[-SLOW:]) / SLOW
    previous_fast = sum(p[-FAST-1:-1]) / FAST
    previous_slow = sum(p[:-1]) / SLOW
    spread = (fast / slow - 1) * 100
    previous = (previous_fast / previous_slow - 1) * 100
    return {
        "fast_sma": float(fast), "slow_sma": float(slow),
        "previous_fast_sma": float(previous_fast), "previous_slow_sma": float(previous_slow),
        "spread_pct": float(spread), "previous_spread_pct": float(previous),
        "buy_cross": bool(previous <= threshold and spread > threshold),
        "sell_cross": bool(previous >= -threshold and spread < -threshold)
    }


def crossover_flags(theory, custom):
    """Recompute the decision from samples instead of trusting supplied flags."""
    validate_custom(theory)
    if not isinstance(custom, dict) or custom.get("strategy_id") != STRATEGY_ID:
        raise ValueError("Missing registered custom evidence")
    stamps = custom.get("sample_times_unix")
    if (not isinstance(stamps, list) or len(stamps) != 16
            or any(type(t) is not int for t in stamps)
            or any(b - a != 60 for a, b in zip(stamps, stamps[1:]))):
        raise ValueError("Custom samples must be 60 seconds apart")
    quote_time = custom.get("quote_timestamp_unix")
    if type(quote_time) not in (int, float) or not math.isfinite(quote_time):
        raise ValueError("Missing custom quote timestamp")
    boundary = math.floor(quote_time / 60) * 60
    if not boundary - 60 <= stamps[-1] < boundary:
        raise ValueError("Custom samples must end in the latest completed minute")
    calculated = _metrics(custom.get("sample_prices_usd"), _number(theory["threshold_pct"]))
    return calculated["buy_cross"], calculated["sell_cross"]


def calculate_custom(model, symbol, quote, theory):
    from guardian_signals import price_at
    validate_custom(theory)
    if symbol != "BTC":
        raise ValueError("This registered custom theory is BTC-only")
    now = quote["timestamp_unix"]
    step = model["tick_seconds"]
    epoch = model["epoch_unix"]
    if type(step) is not int or step <= 0 or 60 % step:
        raise ValueError("Custom strategy needs an integer tick dividing 60")
    if model["version"] != quote["model_version"]:
        raise ValueError("Custom descriptor version mismatch")
    if abs(_number(price_at(model, symbol, now)) - _number(quote["price"])) > Decimal("0.00000001"):
        raise ValueError("Custom replay disagrees with current quote")
    # Last market tick strictly before the start of the current minute.
    boundary = math.floor(now / 60) * 60
    last = epoch + math.floor((boundary - epoch - 1) / step) * step
    times = [last - 60 * i for i in range(15, -1, -1)]
    if times[0] < epoch or times[-1] > now:
        raise ValueError("Insufficient completed history")
    prices = [price_at(model, symbol, t) for t in times]
    return {
        "strategy_id": STRATEGY_ID, "quote_timestamp_unix": now,
        "sample_times_unix": times, "sample_prices_usd": prices,
        "fast_periods": FAST, "slow_periods": SLOW, "sample_interval_seconds": 60,
        **_metrics(prices, _number(theory["threshold_pct"]))
    }
