"""Operator-run read-only report. No daemon, policy write or exchange credentials."""

import argparse
import json
import os
import time

from services.v2_reentry_archive import ReentryArchive
from services.v2_reentry_research_entry import select_scope
from services.v2_trade_path_review import enrich_path_inputs, load_path_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    account = parser.add_mutually_exclusive_group(required=True)
    account.add_argument("--registry")
    account.add_argument("--active-sandbox", action="store_true")
    parser.add_argument("--days", type=int, choices=(1, 7, 14, 30, 90), default=7)
    parser.add_argument("--max-trades", type=int, choices=range(1, 31), default=20)
    parser.add_argument(
        "--max-minutes", type=int, choices=range(1, 10081), default=2880
    )
    args = parser.parse_args()
    dsn = os.environ.get("V2_TRADE_PATH_READ_DSN")
    if not dsn:
        raise ValueError("PATH_READ_DSN_REQUIRED")
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        conn.execute("SET LOCAL jit=off")
        scope = select_scope(
            conn, registry=args.registry, active_sandbox=args.active_sandbox
        )
        inputs = load_path_inputs(
            conn,
            scope,
            now_ms=time.time_ns() // 1000000,
            days=args.days,
            max_trades=args.max_trades,
        )
    # The PG snapshot is closed BEFORE the first CH call.
    archive = ReentryArchive.from_mapping(os.environ)
    try:
        print(
            json.dumps(
                enrich_path_inputs(
                    inputs, read_bars=archive.read, max_minutes=args.max_minutes
                ),
                ensure_ascii=False,
            )
        )
    finally:
        archive.close()


if __name__ == "__main__":
    main()
