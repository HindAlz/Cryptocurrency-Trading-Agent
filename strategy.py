"""Member 2: conservative checks against the shared team theory/policy interface.

No heuristic is substituted for the user's active theory. Until the course's
strategy specification is confirmed, unknown strategies HOLD safely.
"""
from decimal import Decimal, ROUND_DOWN, InvalidOperation

class StrategyError(ValueError):
    pass

def num(value, name):
    if isinstance(value, bool) or value is None:
        raise StrategyError(f"Invalid {name}")
    try:
        x = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise StrategyError(f"Invalid {name}") from e
    if not x.is_finite():
        raise StrategyError(f"Non-finite {name}")
    return x

def validate_theory(theory, policy, symbol):
    """Require the fields specified by Mariyam's context and an active XRP theory."""
    if not isinstance(theory, dict) or not isinstance(policy, dict):
        raise StrategyError("Missing theory/policy")
    required = ('id','version','status','selected_symbols','strategy','lookback_minutes',
                'threshold_pct','position_fraction','stop_loss_pct','take_profit_pct')
    if any(k not in theory for k in required):
        raise StrategyError('Missing required theory fields')
    if type(theory['id']) is not int or theory['id'] <= 0 or type(theory['version']) is not int or theory['version'] <= 0:
        raise StrategyError('Invalid theory ID/version')
    if str(theory['status']).upper() not in ('ACTIVE','SUBMITTED'):
        raise StrategyError('Theory is not active/submitted')
    selected = theory['selected_symbols']
    allowed = policy.get('allowed_symbols')
    if not isinstance(selected, list) or not isinstance(allowed, list):
        raise StrategyError('Missing currency permissions')
    if symbol not in [str(x).upper() for x in selected] or symbol not in [str(x).upper() for x in allowed]:
        raise StrategyError('Currency not selected/allowed')
    for field in ('threshold_pct','position_fraction','stop_loss_pct','take_profit_pct'):
        if num(theory[field],field) <= 0:
            raise StrategyError('Invalid nonpositive theory setting')
    if num(theory['position_fraction'],'position_fraction') > 1:
        raise StrategyError('Position fraction exceeds 1')
    if type(theory['lookback_minutes']) is not int or theory['lookback_minutes'] <= 0:
        raise StrategyError('Invalid lookback_minutes')
    return theory

def calculate_signals(points, lookback_minutes, avg_cost, holdings):
    """Time-window prices, not an observation count. No future-data leakage."""
    if not points:
        raise StrategyError('No replay points')
    end = points[-1].timestamp
    start = end.timestamp() - lookback_minutes * 60
    baseline = [p for p in points if p.timestamp.timestamp() <= start]
    window = [p for p in points if p.timestamp.timestamp() >= start]
    if not baseline or not window:
        raise StrategyError('Insufficient replay window')
    price = num(points[-1].price, 'price')
    prior = num(baseline[-1].price,'prior')
    mean = sum(num(p.price,'historical price') for p in window) / len(window)
    out = {'lookback_return_pct': float((price / prior - 1) * 100),
           'mean_deviation_pct': float((price / mean - 1) * 100),
           'return_since_avg_cost_pct': None}
    if num(holdings,'holdings') > 0:
        acquisition = num(avg_cost, 'average acquisition cost')
        if acquisition <= 0:
            raise StrategyError('Missing positive acquisition cost')
        out['return_since_avg_cost_pct'] = float((price / acquisition - 1) * 100)
    return out

def determine_triggers(theory, signals, holdings):
    """Implemented strategy: mean-reversion only. Unsupported strategy -> HOLD."""
    strategy = str(theory['strategy']).lower().replace('-','_').replace(' ','_')
    if strategy not in ('mean_reversion', 'meanreversion'):
        raise StrategyError('Strategy needs instructor-specific implementation')
    threshold = num(theory['threshold_pct'],'threshold_pct')
    owned = num(holdings,'holdings')
    deviation = num(signals['mean_deviation_pct'],'mean deviation')
    exit_triggers = []
    if owned > 0:
        ret = num(signals['return_since_avg_cost_pct'],'cost return')
        if ret <= -num(theory['stop_loss_pct'],'stop-loss'):
            exit_triggers.append('stop_loss')
        if ret >= num(theory['take_profit_pct'],'take-profit'):
            exit_triggers.append('take_profit')
        if deviation >= threshold:
            exit_triggers.append('mean_reversion_exit')
    return owned == 0 and deviation <= -threshold, exit_triggers

def quantity_bounds(theory, policy, cash, holdings, price, decimal_places=8, minimum_usd='0.01'):
    p, balance, owned = num(price,'price'),num(cash,'cash'),num(holdings,'holdings')
    cap = num(policy['max_transaction_amount_usd'],'transaction cap')
    frac = num(theory['position_fraction'],'position fraction')
    minimum = num(minimum_usd,'minimum')
    if p <= 0 or balance < 0 or owned < 0 or cap <= 0 or minimum <= 0:
        raise StrategyError('Invalid account/price limits')
    if type(decimal_places) is not int or not 0 <= decimal_places <= 12:
        raise StrategyError('Invalid decimal precision')
    quantum = Decimal(1).scaleb(-decimal_places)
    buy_budget = min(balance * frac, balance, cap)
    buy_qty = (buy_budget / p).quantize(quantum, rounding=ROUND_DOWN)
    sell_qty = min(owned, cap / p).quantize(quantum, rounding=ROUND_DOWN)
    if buy_qty * p < minimum: buy_qty = Decimal('0')
    if sell_qty * p < minimum: sell_qty = Decimal('0')
    return {'max_buy_budget_usd':float(buy_budget), 'max_buy_quantity':float(buy_qty),
            'max_sell_quantity':float(sell_qty),'minimum_settled_amount_usd':float(minimum),
            'quantity_decimal_places':decimal_places}

def validate_decision(decision, symbol, expected, bounds, price, policy):
    """Accept Mariyam's TraderDecision dataclass or an equivalent exact dict."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(decision): decision = asdict(decision)
    if not isinstance(decision,dict) or set(decision) != {'symbol','action','quantity','reasoning'}:
        raise StrategyError('Invalid AI response structure')
    action = decision['action']
    if decision['symbol'] != symbol or action not in ('BUY','SELL','HOLD'):
        raise StrategyError('Wrong symbol/action')
    q = num(decision['quantity'],'quantity')
    if not isinstance(decision['reasoning'],str) or not decision['reasoning'].strip():
        raise StrategyError('Missing reasoning')
    if action == 'HOLD':
        if q != 0: raise StrategyError('Nonzero HOLD')
        return None
    if action != expected or action in [str(x).upper() for x in policy.get('restricted_actions',[])]:
        raise StrategyError('Unauthorized AI action')
    max_qty = num(bounds['max_buy_quantity' if action == 'BUY' else 'max_sell_quantity'],'max quantity')
    decimals = bounds['quantity_decimal_places']
    if q <= 0 or q > max_qty or q != q.quantize(Decimal(1).scaleb(-decimals)):
        raise StrategyError('Quantity exceeds/violates bounds')
    amount = q * num(price,'price')
    if amount < num(bounds['minimum_settled_amount_usd'],'minimum') or amount > num(policy['max_transaction_amount_usd'],'max amount'):
        raise StrategyError('Invalid trade value')
    return {'symbol':symbol,'action':action,'quantity':float(q),
            'reasoning':decision['reasoning'].strip()}
