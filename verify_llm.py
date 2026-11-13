"""Verify real Gemini connectivity using synthetic context data.

No Ledger proposals or reviews are submitted.
The examples are not live trading evidence.
"""

from copy import deepcopy

from llm_adapter import LLMError, create_adapter


SAMPLE_TRADER_CONTEXT = {
    "context_version": 1,
    "decision_time_unix": 1800000000,
    "symbol": "BTC",
    "quote": {
        "price_usd": 80000.0,
        "timestamp_unix": 1800000000,
        "model_version": "synthetic-example"
    },
    "theory": {
        "id": 7,
        "version": 2,
        "status": "submitted",
        "title": "BTC Short-Term Momentum",
        "hypothesis": "Recent upward movement may continue briefly.",
        "entry_rule": (
            "Buy only when no BTC is held and the 15-minute "
            "return is at least 0.5%."
        ),
        "exit_rule": (
            "Sell on a 15-minute return at or below -0.5%, "
            "a 1% stop loss, or a 1.5% take profit."
        ),
        "risk_rule": (
            "Use at most 0.5% of current cash per BUY. "
            "Respect all policy limits and do not add to an existing position."
        ),
        "selected_symbols": ["BTC"],
        "strategy": "momentum",
        "lookback_minutes": 15,
        "threshold_pct": 0.5,
        "position_fraction": 0.005,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 1.5
    },
    "signals": {
        "lookback_return_pct": 0.6,
        "mean_deviation_pct": None,
        "return_since_avg_cost_pct": None,
        "entry_triggered": True,
        "exit_triggers": [],
        "replay_matches_quote": True
    },
    "policy": {
        "allowed_symbols": ["BTC"],
        "restricted_actions": ["WITHDRAW"],
        "max_transaction_amount_usd": 1000.0,
        "max_trades_per_hour": 10,
        "max_trades_per_day": 50,
        "escalation_threshold_usd": 750.0
    },
    "trade_counts": {
        "executed_last_hour": 0,
        "executed_last_day": 0
    },
    "portfolio": {
        "cash_usd": 100000.0,
        "symbol_quantity": 0.0,
        "symbol_avg_cost_usd": None
    },
    "bounds": {
        "max_buy_budget_usd": 500.0,
        "max_buy_quantity": 0.00625,
        "max_sell_quantity": 0.0,
        "minimum_settled_amount_usd": 0.01,
        "quantity_decimal_places": 10
    },
    "execution_state": {
        "has_unresolved_order": False
    },
    "news": []
}


SAMPLE_GUARDIAN_CONTEXT = deepcopy(SAMPLE_TRADER_CONTEXT)
SAMPLE_GUARDIAN_CONTEXT.pop("execution_state")
SAMPLE_GUARDIAN_CONTEXT["order"] = {
    "transaction_id": 42,
    "theory_id": 7,
    "theory_version": 2,
    "action": "BUY",
    "requested_quantity": 0.00625,
    "requested_price_usd": 80000.0,
    "reasoning": "Momentum entry condition met; no BTC currently held."
}
SAMPLE_GUARDIAN_CONTEXT["assessment"] = {
    "requested_amount_at_current_quote_usd": 500.0,
    "theory_snapshot_matches_order": True,
    "hard_rules_passed": True,
    "rules_triggered": [],
    "requires_human_approval": False
}


def main():
    try:
        llm = create_adapter("config.llm.local.json")

        if llm.config.provider != "gemini":
            print("Real verification requires provider=gemini.")
            return 1

        print(f"Provider: {llm.config.provider}")
        print(f"Model: {llm.config.model}")
        print("Using synthetic data; no Ledger writes will occur.")

        trader = llm.trader_decision(deepcopy(SAMPLE_TRADER_CONTEXT))
        print(f"Trader: {trader}")

        guardian = llm.guardian_decision(
            deepcopy(SAMPLE_GUARDIAN_CONTEXT)
        )
        print(f"Guardian: {guardian}")

    except (LLMError, ValueError, OSError) as exc:
        print(f"Verification failed ({type(exc).__name__}): {exc}")
        return 1

    print("OK: both real Gemini calls returned validated responses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())