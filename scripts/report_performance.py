#!/usr/bin/env python3
"""Print Binance strategy attribution from PostgreSQL position episodes."""
import argparse
import os
import psycopg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    args = parser.parse_args()
    dsn = os.environ.get('POSTGRES_DSN', '').strip()
    if not dsn:
        raise SystemExit('POSTGRES_DSN is required')
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("""
            SELECT event_type, count(*), count(*) FILTER (WHERE pnl_usdt > 0),
                   coalesce(sum(pnl_usdt), 0), avg(raw_strength),
                   avg(taker_buy_ratio_15m), avg(duration_min)
            FROM trade_episode_attribution
            WHERE closed_at >= now() - (%s * interval '1 day')
            GROUP BY event_type ORDER BY sum(pnl_usdt) DESC
        """, (max(1, args.days),)).fetchall()
        print('event_type\ttrades\twins\tpnl\tavg_raw_strength\tavg_taker_buy\tavg_hold_min')
        for row in rows:
            print('\t'.join(str(x if x is not None else '') for x in row))


if __name__ == '__main__':
    main()
