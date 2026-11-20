"""Member 2 Trading Agent integration for Hind's client/state and Mariyam's AI.

Fail-closed: needs course replay callback and confirmed Ledger response envelope.
Never retry uncertain POST outcomes automatically.
"""
from __future__ import annotations
from datetime import datetime, timezone
from market_replay import reconstruct_prices, verify_fixtures, check_current_quote, MarketDataError
from strategy import (StrategyError, num, validate_theory, calculate_signals,
                      determine_triggers, quantity_bounds, validate_decision)


def _payload(result, key):
    """Do not invent Ledger payload schemas: accept explicit direct dictionary only."""
    if not isinstance(result, dict): raise StrategyError(f'Invalid {key} response')
    return result


def run_trader_cycle(client, llm, state, *, symbol='XRP', decision_time=None,
                     reference_reconstructor=None, quote_tolerance_fraction=0.001,
                     quantity_decimal_places=None, minimum_settled_amount_usd=None):
    """One decision, at most one submitted proposal.

    Confirmation of market schema, replay formula, counts and minimum rules
    must precede production use. Missing evidence is always HOLD.
    """
    symbol = str(symbol).upper()
    now = decision_time or datetime.now(timezone.utc)
    if now.tzinfo is None: return {'status':'HOLD','reason':'Timezone missing'}
    try:
        if state.get_unresolved_intents():
            return {'status':'HOLD','reason':'Earlier order remains unresolved'}
        policy = _payload(client.get_policy(),'policy')
        theory = _payload(client.get_theory(),'theory')
        validate_theory(theory,policy,symbol)
        # Explicit direct schemas; inspect live Ledger envelopes before enabling.
        quotes = _payload(client.get_prices(symbol),'prices')
        quote = quotes[symbol]
        if not isinstance(quote,dict): raise StrategyError('Unexpected quote schema')
        price = num(quote['price_usd'],'price_usd')
        stamp = quote['timestamp_unix']
        if type(stamp) not in (int,float) or stamp > now.timestamp():
            raise StrategyError('Bad/future quote timestamp')
        portfolio = _payload(client.get_portfolio(),'portfolio')
        cash = portfolio['cash_usd']
        position = portfolio['positions'][symbol]
        holding = position['quantity']
        avg_cost = position.get('avg_cost_usd')
        counts = _payload(portfolio['trade_counts'],'trade_counts')
        if any(x not in counts for x in ('executed_last_hour','executed_last_day')):
            raise StrategyError('Missing execution counts')
        if (num(counts['executed_last_hour'],'hour count') >= num(policy['max_trades_per_hour'],'hour max') or
                num(counts['executed_last_day'],'day count') >= num(policy['max_trades_per_day'],'day max')):
            return {'status':'HOLD','reason':'Trading quota reached'}
        news_response = client.get_news()
        if not isinstance(news_response, dict) or not isinstance(news_response.get('news'),list):
            raise StrategyError('Unexpected news schema')
        news = news_response['news']
        descriptor = _payload(client.get_market_model(),'market model')
        points = reconstruct_prices(descriptor,now,reference_reconstructor)
        verify_fixtures(points,descriptor.get('fixtures',[]))
        try:
            check_current_quote(points,float(price),quote_tolerance_fraction)
        except MarketDataError:
            descriptor = _payload(client.get_market_model(),'refreshed market model')
            points = reconstruct_prices(descriptor,now,reference_reconstructor)
            verify_fixtures(points,descriptor.get('fixtures',[]))
            check_current_quote(points,float(price),quote_tolerance_fraction)
        signals = calculate_signals(points,theory['lookback_minutes'],avg_cost,holding)
        entry, exits = determine_triggers(theory,signals,holding)
        signals.update(entry_triggered=entry,exit_triggers=exits,replay_matches_quote=True)
        expected = 'SELL' if exits else ('BUY' if entry else 'HOLD')
        if expected=='HOLD': return {'status':'HOLD','reason':'No active strategy trigger'}
        if expected in [str(a).upper() for a in policy['restricted_actions']]:
            return {'status':'HOLD','reason':'Action restricted'}
        # These Ledger-specific execution rules must be provided explicitly by
        # caller once confirmed from course spec; no guessed defaults for live trades.
        if quantity_decimal_places is None or minimum_settled_amount_usd is None:
            raise StrategyError('Instructor quantity precision/minimum required')
        bounds = quantity_bounds(theory,policy,cash,holding,price,
                                 quantity_decimal_places,minimum_settled_amount_usd)
        maximum = bounds['max_buy_quantity' if expected=='BUY' else 'max_sell_quantity']
        if maximum <= 0: return {'status':'HOLD','reason':'No valid affordable quantity'}
        context = {'context_version':1,'decision_time_unix':int(now.timestamp()),'symbol':symbol,
                   'quote':{'price_usd':float(price),'timestamp_unix':stamp,
                            'model_version':quote.get('model_version')},
                   'theory':theory,'signals':signals,'policy':policy,'trade_counts':counts,
                   'portfolio':{'cash_usd':float(num(cash,'cash')),
                                'symbol_quantity':float(num(holding,'holding')),
                                'symbol_avg_cost_usd':float(num(avg_cost,'avg cost')) if avg_cost is not None else None},
                   'bounds':bounds,'execution_state':{'has_unresolved_order':False},'news':news}
        # Mariyam's adapter validates this full context before calling Muse.
        from llm_adapter import validate_context
        validate_context(context,'trader')
        decision = llm.trader_decision(context)
        proposal = validate_decision(decision,symbol,expected,bounds,price,policy)
        if proposal is None: return {'status':'HOLD','reason':'AI chose HOLD'}
    except Exception as exc:
        # No credential, response body, or untrusted text is copied into result.
        return {'status':'HOLD','reason':f'Unable to verify safe trade ({type(exc).__name__})'}
    # Persist BEFORE posting. This follows Hind's exact state-store interface.
    order = {**proposal, 'theory_id':theory['id']}
    try:
        state.save_theory_snapshot(theory)
        intent_id = state.save_proposal_intent(order, theory['version'])
        state.mark_submitting(intent_id)
    except Exception:
        return {'status':'HOLD','reason':'Local intent persistence failed'}
    try:
        reply = client.propose_trade(order)
    except Exception:
        state.mark_uncertain(intent_id)
        return {'status':'UNRESOLVED','intent_id':intent_id,
                'reason':'POST outcome uncertain; reconciliation required'}
    if not isinstance(reply,dict) or type(reply.get('transaction_id')) is not int or reply['transaction_id'] <= 0:
        state.mark_uncertain(intent_id)
        return {'status':'UNRESOLVED','intent_id':intent_id,
                'reason':'Missing confirmed transaction ID; reconcile'}
    state.attach_transaction_id(intent_id,reply['transaction_id'])
    return {'status':'PROPOSED','intent_id':intent_id,
            'transaction_id':reply['transaction_id'],'proposal':order}
