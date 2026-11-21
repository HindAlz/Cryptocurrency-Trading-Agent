"""Shared validation of submitted theory records (course guide section 6.3)."""
from decimal import Decimal, InvalidOperation

SUPPORTED_SYMBOLS = {"BTC", "ETH", "DOGE", "XMR", "XRP", "SOL"}
TEXT_LIMITS = {"title": 120, "hypothesis": 4000, "entry_rule": 4000,
               "exit_rule": 4000, "risk_rule": 4000}
RANGES = {"threshold_pct": ("0.01", "50"), "position_fraction": ("0.001", "1"),
          "stop_loss_pct": ("0.01", "100"), "take_profit_pct": ("0.01", "100")}


class TheoryValidationError(ValueError):
    pass


def validate_submitted_theory(theory):
    if not isinstance(theory, dict):
        raise TheoryValidationError("Theory must be an object")
    for field in ("id", "version"):
        if type(theory.get(field)) is not int or theory[field] <= 0:
            raise TheoryValidationError(f"Invalid theory {field}")
    status = theory.get("status")
    if not isinstance(status, str) or status.upper() != "SUBMITTED":
        raise TheoryValidationError("Theory must be submitted")
    for field, limit in TEXT_LIMITS.items():
        value = theory.get(field)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise TheoryValidationError(f"Missing or invalid theory {field}")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeError:
            raise TheoryValidationError(f"Invalid UTF-8 theory {field}") from None
        if length > limit:
            raise TheoryValidationError(f"Theory {field} exceeds {limit} UTF-8 bytes")
    selected = theory.get("selected_symbols")
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 6
            or any(not isinstance(s, str) or s not in SUPPORTED_SYMBOLS for s in selected)
            or len(set(selected)) != len(selected)):
        raise TheoryValidationError("Invalid theory currencies")
    strategy = theory.get("strategy")
    if not isinstance(strategy, str) or strategy not in {"momentum", "mean_reversion", "custom"}:
        raise TheoryValidationError("Unrecognized strategy")
    lookback = theory.get("lookback_minutes")
    if type(lookback) is not int or not 1 <= lookback <= 1440:
        raise TheoryValidationError("Invalid theory lookback")
    for field, (low, high) in RANGES.items():
        value = theory.get(field)
        # Ledger theory fields are JSON numbers; booleans and numeric text are invalid.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TheoryValidationError(f"Theory {field} must be a number")
        try:
            number = Decimal(str(value))
            if not number.is_finite() or not Decimal(low) <= number <= Decimal(high):
                raise TheoryValidationError(f"Out-of-range theory {field}")
            if number != number.quantize(Decimal("0.0001")):
                raise TheoryValidationError(f"Theory {field} exceeds four decimal places")
        except InvalidOperation:
            raise TheoryValidationError(f"Invalid theory {field}") from None
    return theory
