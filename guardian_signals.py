"""Independent replay using Appendix A of the course guide.

No requests for future quotes. Cache the static descriptor per provider instance.
"""
import math
import time
from decimal import Decimal


def price_at(model, symbol, t):
    """Appendix A reference formula; caller enforces the live time range."""
    step = model["tick_seconds"]
    tick = max(0, math.floor((t - model["epoch_unix"]) / step))
    seconds = tick * step
    a = model["assets"][symbol]
    ratio = seconds / 86400 / model["power_timescale_days"]
    trend = a["base_price"] * (1 + ratio) ** a["power_exponent"]
    if len(a["cycle_amplitudes"]) != len(model["cycles"]):
        raise ValueError("Cycle count mismatch")
    wave = sum(
        amp * math.sin(2 * math.pi * (seconds / c["period_seconds"])
                       + a["phase"] + c["phase"])
        for amp, c in zip(a["cycle_amplitudes"], model["cycles"]))
    m, k = model["noise_modulus"], model["noise_multiplier"]
    if type(m) is not int or type(k) is not int or m <= 1:
        raise ValueError("Invalid integer noise parameters")
    x = (tick + a["noise_seed"]) % m
    x = x * k % m
    x = x * k % m
    noise = 2 * x / m - 1
    raw = trend * math.exp(wave + a["noise_amplitude"] * noise)
    clamped = min(model["max_price"], max(model["min_price"], raw))
    result = math.floor(clamped * 1e8 + 0.5) / 1e8
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid replay price")
    return result


class IndependentSignals:
    def __init__(self, client, cache_seconds=180):
        self.client = client
        self.cache_seconds = cache_seconds
        self.model = None
        self.fetched_at = 0

    def _refresh(self):
        self.model = self.client.get_market_model()["model"]
        self.fetched_at = time.monotonic()

    def __call__(self, order, theory, quote):
        now = quote["timestamp_unix"]
        lookback = theory["lookback_minutes"]
        if type(lookback) is not int or not 1 <= lookback <= 1440:
            raise ValueError("Invalid lookback")
        for attempt in range(2):
            if (attempt or self.model is None
                    or time.monotonic() - self.fetched_at > self.cache_seconds
                    or self.model["version"] != quote["model_version"]):
                self._refresh()
            model = self.model
            if model["version"] != quote["model_version"]:
                continue
            start = now - lookback * 60
            if start < model["epoch_unix"]:
                raise ValueError("Insufficient history")
            samples = [price_at(model, order["symbol"],
                                start + (now - start) * i / 60)
                       for i in range(61)]
            if abs(Decimal(str(samples[-1])) - Decimal(str(quote["price"]))) > Decimal("0.00000001"):
                continue
            return {
                "lookback_return_pct": (quote["price"] / samples[0] - 1) * 100,
                "mean_deviation_pct": (quote["price"] / (sum(samples) / 61) - 1) * 100,
                "return_since_avg_cost_pct": None,
                "entry_triggered": False,
                "exit_triggers": [],
                "replay_matches_quote": True
            }
        raise ValueError("Descriptor/replay disagrees with quote after refresh")
