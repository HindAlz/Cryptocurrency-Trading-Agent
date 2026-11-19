"""Portfolio metrics for Member 5's evaluation tools."""
import math

def _validate_values(values):
    values = list(values)

    if not values:
        raise ValueError("At least one portfolio value is required.")

    for value in values:
        if (isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0):
            raise ValueError("Portfolio values must be finite positive numbers.")

    return values

def portfolio_return(values):
    """Return a decimal: 0.02 means a 2% gain."""
    values = _validate_values(values)
    return values[-1] / values[0] - 1

def maximum_drawdown(values):
    """Return the largest sampled peak-to-trough decline as a decimal."""
    values = _validate_values(values)
    peak = values[0]
    largest_drop = 0.0

    for value in values:
        peak = max(peak, value)
        drop = (peak - value) / peak
        largest_drop = max(largest_drop, drop)

    return largest_drop