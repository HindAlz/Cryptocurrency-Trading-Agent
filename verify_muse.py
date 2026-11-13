"""Verify ONE real Muse call (not tested live; everything is offline)

    $env:MODEL_API_KEY = "LLM_...paste-yours-here..."
    python verify_muse.py

This makes exactly one trader call and one guardian call with
provider "meta" (set "provider": "meta" in your PRIVATE config.json
first), prints the validated results, and exits.

It prints NO API key. If anything fails, it prints the error type
and a short hint instead of a stack trace full of details.
Switch config.json back to "provider": "mock" afterwards so you
do not spend money by accident.
"""
from llm_adapter import LLMError, create_adapter

TRADER_CONTEXT = {
    "symbol": "BTC",
    "policy": {"allowed_symbols": ["BTC"], "transaction_ceiling_usd": 100.0,
               "restricted_actions": ["WITHDRAW", "TRANSFER"]},
    "portfolio": {"cash_usd": 500.0, "holdings": {"BTC": 0.02}},
    "price_usd": 90000.0,
    "max_quantity": 0.001,
    "signals": {"lookback_return": -0.02, "mean_reversion": "oversold"},
    "news": ["Bitcoin steady after ETF inflows."],
}

GUARDIAN_CONTEXT = {
    "symbol": "BTC",
    "action": "BUY",
    "quantity": 0.001,
    "requested_usd": 90.0,
    "policy": {"transaction_ceiling_usd": 100.0},
    "counts": {"executed_last_hour": 1, "executed_today": 2},
    "portfolio": {"cash_usd": 500.0},
    "current_price_usd": 90000.0,
    "trader_reasoning": "Mean reversion signal, within ceiling.",
}


def main():
    llm = create_adapter("config.json")
    print(f"Provider: {llm.config.provider} | Model: {llm.config.model} | Base URL: {llm.config.base_url}")
    if llm.config.provider != "meta":
        print("You are in mock mode. Change config.json to \"provider\": \"meta\" for a real call.")
    try:
        trader = llm.trader_decision(TRADER_CONTEXT)
        print(f"Trader: {trader}")
    except LLMError as exc:
        print(f"Trader call failed ({type(exc).__name__}): {exc}")
        print("Trader rule on failure: HOLD and send nothing.")
        return 1
    try:
        guardian = llm.guardian_decision(GUARDIAN_CONTEXT)
        print(f"Guardian: {guardian}")
    except LLMError as exc:
        print(f"Guardian call failed ({type(exc).__name__}): {exc}")
        print("Guardian rule on failure: ESCALATE when possible.")
        return 1
    print("OK — both calls returned validated JSON. Save this output (without any key) for your report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
