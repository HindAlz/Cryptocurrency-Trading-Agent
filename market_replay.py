"""Trader replay: Appendix A formula, exactly 61 samples ending at quote time.

Reuses the pure formula already shipped with the Guardian. Each agent obtains
its own quote/descriptor and validates its own replay. No future observations.
"""
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from guardian_signals import price_at


class MarketDataError(ValueError):
    pass


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: float


def reconstruct_prices(model, symbol, timestamp_unix, lookback_minutes):
    if type(lookback_minutes) is not int or not 1 <= lookback_minutes <= 1440:
        raise MarketDataError("Invalid lookback")
    if type(timestamp_unix) not in (int, float) or not math.isfinite(timestamp_unix):
        raise MarketDataError("Invalid quote timestamp")
    start = timestamp_unix - lookback_minutes * 60
    if start < model["epoch_unix"]:
        raise MarketDataError("Insufficient simulated history")
    samples = []
    for i in range(61):
        t = start + (timestamp_unix - start) * i / 60
        p = price_at(model, symbol, t)
        if not math.isfinite(p) or p <= 0:
            raise MarketDataError("Invalid replay price")
        samples.append(PricePoint(datetime.fromtimestamp(t, timezone.utc), p))
    return samples


def check_current_quote(points, current_quote, quote_timestamp):
    if len(points) != 61:
        raise MarketDataError("Expected 61 replay samples")
    if points[-1].timestamp.timestamp() != quote_timestamp:
        raise MarketDataError("Replay and quote timestamps differ")
    if abs(Decimal(str(points[-1].price)) - Decimal(str(current_quote))) > Decimal("0.00000001"):
        raise MarketDataError("Replay disagrees with quote")


class MarketReplay:
    def __init__(self, client, cache_seconds=180):
        self.client = client
        self.cache_seconds = cache_seconds
        self.model = None
        self.fetched_at = 0

    def _refresh(self):
        response = self.client.get_market_model()
        model = response["model"]
        if not isinstance(model, dict):
            raise MarketDataError("Invalid model descriptor")
        self.model = model
        self.fetched_at = time.monotonic()

    def samples(self, symbol, quote, lookback_minutes):
        for attempt in range(2):
            if (attempt or self.model is None
                    or self.model["version"] != quote["model_version"]
                    or time.monotonic() - self.fetched_at > self.cache_seconds):
                self._refresh()
            if self.model["version"] != quote["model_version"]:
                continue
            points = reconstruct_prices(self.model, symbol, quote["timestamp_unix"], lookback_minutes)
            try:
                check_current_quote(points, quote["price"], quote["timestamp_unix"])
            except MarketDataError:
                if attempt:
                    raise
                continue
            return points
        raise MarketDataError("Model version disagrees after one refresh")
