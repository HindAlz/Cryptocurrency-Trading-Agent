"""Read-only custom signal inspection. --demo needs no credentials or network."""
import argparse
import json
from datetime import datetime, timezone

from client import LedgerClient
from config import load_ledger_config
from custom_sma import (TITLE, STRATEGY_ID, calculate_custom, crossover_flags, _metrics)
from market_replay import MarketReplay
from decimal import Decimal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.local.json')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    if args.demo:
        result = {'synthetic_demo_only': True, 'prices': [100]*15+[110],
                  'buffer_pct': .05, **_metrics([100]*15+[110], Decimal('.05'))}
        print(json.dumps(result, indent=2))
        return
    cfg = load_ledger_config(args.config, 'trader')
    client = LedgerClient(cfg['LEDGER_BASE_URL'], cfg['TRADER_API_KEY'])
    theory = client.get_theory()['theory']
    if not isinstance(theory, dict) or theory.get('title') != TITLE or theory.get('strategy') != 'custom':
        raise ValueError('Activate the tested registered custom theory first; current theory differs')
    quotes = [q for q in client.get_prices(symbols='BTC')['prices'] if q['symbol']=='BTC']
    if len(quotes) != 1:
        raise ValueError('Missing/duplicate quote')
    quote = quotes[0]
    now = datetime.now(timezone.utc).timestamp()
    if not now-120 <= quote['timestamp_unix'] <= now+5:
        raise ValueError('Stale/future quote')
    replay = MarketReplay(client)
    replay.samples('BTC', quote, theory['lookback_minutes'])
    data = calculate_custom(replay.model, 'BTC', quote, theory)
    buy, sell = crossover_flags(theory, data)
    print(json.dumps({'read_only':True, 'theory_id':theory['id'], 'theory_version':theory['version'],
                      'quote_price_usd':quote['price'], 'custom':data,
                      'verified_buy_cross':buy,'verified_sell_cross':sell}, indent=2, allow_nan=False))


if __name__=='__main__':
    main()
