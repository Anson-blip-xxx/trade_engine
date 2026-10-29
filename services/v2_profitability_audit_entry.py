"""Explicit read-only end-to-end audit. JSON report to stdout; never orders.

Credentials/DSN come from environment, account scope is resolved in the database
or provided through stdin by an authorized route resolver, never hardcoded.
"""

import argparse
import json
import os
import sys

from services.v2_profitability_audit import AuditPolicy, enrich_audit, load_audit_inputs
from services.v2_reentry_archive import ReentryArchive
from services.v2_reentry_research_entry import select_scope
from v2_core.account_risk import AccountScope


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument("--active-sandbox", action="store_true")
    select.add_argument("--scope-stdin", action="store_true")
    parser.add_argument("--days", type=int, choices=(1, 7, 14, 30, 90), default=14)
    parser.add_argument("--holdout-start-ms", type=int, required=True)
    parser.add_argument("--max-trades", type=int, choices=range(1, 13), default=6)
    parser.add_argument("--trend-cases", type=int, choices=range(7), default=3)
    parser.add_argument("--archive-seconds", type=int, choices=range(1, 91), default=60)
    args = parser.parse_args(argv)
    dsn = os.environ.get("V2_PROFIT_REVIEW_READ_DSN")
    if not dsn:
        raise ValueError("PROFIT_AUDIT_READ_DSN_REQUIRED")
    policy = AuditPolicy(
        max_trades=args.max_trades,
        trend_cases=args.trend_cases,
        max_archive_seconds=args.archive_seconds,
    )
    import psycopg

    with psycopg.connect(
        dsn, connect_timeout=3, options="-c default_transaction_read_only=on"
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='100ms'")
        conn.execute("SET LOCAL jit=off")
        if args.scope_stdin:
            raw = sys.stdin.read(4097)
            if len(raw) > 4096:
                raise ValueError("PROFIT_AUDIT_SCOPE_BOUND")
            scope = AccountScope(**json.loads(raw))
            if (scope.exchange, scope.environment, scope.product) != (
                "BINANCE",
                "SANDBOX",
                "FUTURES",
            ):
                raise ValueError("PROFIT_AUDIT_SCOPE")
        else:
            scope = select_scope(conn, active_sandbox=True)
        now = conn.execute(
            "SELECT (extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        inputs = load_audit_inputs(
            conn,
            scope,
            now_ms=now,
            holdout_start_ms=args.holdout_start_ms,
            days=args.days,
            policy=policy,
        )
    # Close all business DB transactions BEFORE any archive request.
    archive = ReentryArchive.from_mapping(os.environ)
    try:
        report = enrich_audit(inputs, read_bars=archive.read)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        archive.close()


if __name__ == "__main__":
    main()
