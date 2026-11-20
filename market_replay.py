"""Historical market data checks and signals for Member 2.

IMPORTANT: The course's original market-descriptor reconstruction formula was not
included in the supplied team plan. A reconstruction callback must be supplied
for descriptor-only data; the module never invents that formula.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Callable, Mapping, Sequence


class MarketDataError(ValueError):
    """Market observations are missing, inconsistent, or not yet supported."""


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise MarketDataError("Price timestamps must include a timezone")
        if not isfinite(float(self.price)) or self.price <= 0:
            raise MarketDataError("Price must be positive and finite")


def parse_price_points(raw: Sequence[Mapping[str, Any]], decision_time: datetime) -> list[PricePoint]:
    """Parse and filter prices, rejecting duplicate/out-of-order timestamps.

    Timestamps may be aware ISO-8601 strings or aware datetime objects.
    Only observations at or before decision_time can influence a decision.
    """
    if decision_time.tzinfo is None:
        raise MarketDataError("decision_time must include a timezone")
    result: list[PricePoint] = []
    for item in raw:
        stamp = item["timestamp"]
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        point = PricePoint(stamp, float(item["price"]))
        if result and point.timestamp <= result[-1].timestamp:
            raise MarketDataError("Prices must be strictly chronological")
        if point.timestamp <= decision_time:
            result.append(point)
    if not result:
        raise MarketDataError("No market observations at or before decision time")
    return result


def reconstruct_prices(
    descriptor: Mapping[str, Any],
    decision_time: datetime,
    reference_reconstructor: Callable[[Mapping[str, Any], datetime], Sequence[Mapping[str, Any]]] | None = None,
) -> list[PricePoint]:
    """Replay descriptor; the course-specific formula is injected by caller.

    An explicit `historical_prices` list can be used by tests and demos.
    For real course descriptors, provide the instructor's formula as a callback.
    """
    if "historical_prices" in descriptor:
        raw = descriptor["historical_prices"]
    elif reference_reconstructor is not None:
        raw = reference_reconstructor(descriptor, decision_time)
    else:
        raise MarketDataError(
            "Course market replay formula not available. Supply "
            "reference_reconstructor from the original assignment guide."
        )
    return parse_price_points(raw, decision_time)


def verify_fixtures(
    points: Sequence[PricePoint], fixtures: Sequence[Mapping[str, Any]], tolerance: float = 1e-6
) -> None:
    """Verify independently provided expected timestamp/price fixtures."""
    if not isfinite(tolerance) or tolerance < 0:
        raise ValueError("Invalid fixture tolerance")
    lookup = {p.timestamp.isoformat(): p.price for p in points}
    for fixture in fixtures:
        stamp = fixture["timestamp"]
        if isinstance(stamp, datetime):
            stamp = stamp.isoformat()
        else:
            stamp = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).isoformat()
        expected = float(fixture["price"])
        if stamp not in lookup or not isfinite(expected) or abs(lookup[stamp] - expected) > tolerance:
            raise MarketDataError(f"Replay fixture mismatch at {stamp}")


def check_current_quote(points: Sequence[PricePoint], current_quote: float, tolerance_fraction: float = 0.001) -> None:
    """Check latest replayed sample against current quote (configurable tolerance).

    Caller must ensure the comparison timestamp is relevant to the quote.
    A mismatch requires one descriptor refresh, then HOLD if still mismatched.
    """
    quote = float(current_quote)
    if not points or not isfinite(quote) or quote <= 0:
        raise MarketDataError("Invalid quote or missing price samples")
    if not isfinite(tolerance_fraction) or tolerance_fraction < 0:
        raise MarketDataError("Invalid quote tolerance")
    if abs(points[-1].price - quote) / quote > tolerance_fraction:
        raise MarketDataError("Replay price disagrees with current quote")


def compute_signals(points: Sequence[PricePoint], lookback: int = 3) -> dict[str, float]:
    """Compute percent return and simple mean-reversion deviation (percent).

    A negative deviation indicates a price below its lookback average. Signals
    are information, not BUY/SELL instructions.
    """
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        raise MarketDataError("lookback must be a positive integer")
    if len(points) < lookback + 1:
        raise MarketDataError("Not enough price observations for lookback")
    last = points[-1].price
    prior = points[-lookback - 1].price
    window = [p.price for p in points[-lookback - 1:-1]]
    mean = sum(window) / len(window)
    return {
        "lookback_return_pct": (last / prior - 1) * 100,
        "mean_reversion_deviation_pct": (last / mean - 1) * 100,
        "latest_price": last,
    }
