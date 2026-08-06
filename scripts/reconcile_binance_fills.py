#!/usr/bin/env python3
"""Reconcile Binance user fills into PostgreSQL trade_events.

Binance limits userTrades queries to seven days, so a ten-day run is split into
two windows. The operation is idempotent on Binance trade id.
"""
import argparse
import time

from shared.binance_api import fapi_get
from shared.postgres_client import record_trade_event


def _income_symbols(start_ms: int, end_ms: int) -> set[str]:
    rows = fapi_get('/fapi/v1/income', {
        'incomeType': 'REALIZED_PNL',
        'startTime': start_ms,
        'endTime': end_ms,
        'limit': 1000,
    })
    return {row['symbol'] for row in rows or [] if row.get('symbol')}


def reconcile(days: int = 10) -> int:
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    symbols = _income_symbols(start, now)
    written = 0
    window = 7 * 86400 * 1000 - 1
    for symbol in sorted(symbols):
        windows = ((start, min(now, start + window)), (start + window + 1, now))
        for begin, end in windows:
            if begin > end:
                continue
            rows = fapi_get('/fapi/v1/userTrades', {
                'symbol': symbol,
                'startTime': begin,
                'endTime': end,
                'limit': 1000,
            })
            for fill in rows or []:
                trade_id = str(fill.get('id', ''))
                if not trade_id:
                    continue
                if record_trade_event({
                    'event_id': f'fill:{trade_id}',
                    'position_id': f"binance:{symbol}:{fill.get('orderId', '')}",
                    'event_type': 'BINANCE_FILL_RECONCILED',
                    'order_id': str(fill.get('orderId', '')),
                    'fill_id': trade_id,
                    'price': float(fill.get('price', 0)),
                    'qty': float(fill.get('qty', 0)),
                    'realized_pnl': float(fill.get('realizedPnl', 0)),
                    'payload': fill,
                }):
                    written += 1
    return written


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=10)
    args = parser.parse_args()
    print(f'reconciled_events={reconcile(max(1, min(args.days, 30)))}')
