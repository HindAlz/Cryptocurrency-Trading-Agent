
from guardian import evaluate_order

# A fake trade proposed by the Trading Agent
order = {
    "symbol": "SOL",
    "action": "BUY",
    "quantity": 10,
    "reasoning": "Momentum buying opportunity.",
    "theory_id": 1,
    "theory_version": 1
}

# Your actual configured security policy values
policy = {
    "max_transaction_amount": 1000,
    "max_trades_per_hour": 5,
    "max_trades_per_day": 20,
    "escalation_threshold": 500,
    "allowed_symbols": ["SOL"],
    "restricted_actions": ["WITHDRAW"],
    "trades_last_hour": 0,
    "trades_last_day": 0
}

# Fake account balance and holdings
portfolio = {
    "cash_usd": 10000,
    "symbol_quantity": 3
}

# Simplified example of a saved theory
theory = {
    "id": 1,
    "version": 1,
    "status": "SUBMITTED",
    "selected_symbols": ["SOL"]
}

# Fake current SOL price
current_price = 150

result = evaluate_order(
    order,
    policy,
    current_price,
    portfolio,
    theory
)

assert result["decision"] == "BLOCK"
print("\nSecurity test passed!")
