"""Print a bounded read-only integration report; no local business files/writes.

Run as a module with V2_EFFECTIVENESS_READ_DSN and an explicit account/window.
Optional ClickHouse credentials come only from environment variables. No venue
or model call occurs. Stdout contains selected evidence, never a DSN or key.
"""

import argparse
import json
import os
from datetime import datetime

from services.v2_integration_effectiveness import (
    EffectivenessPolicy,
    build_report,
    load_records,
    load_tv_funnel,
)
from services.v2_shared_market import IncrementalArchive
from v2_core.account_risk import AccountScope


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("EXPLICIT_TIMEZONE_REQUIRED")
    return int(parsed.timestamp() * 1000)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--as-of", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--environment", choices=("SANDBOX", "LIVE"), default="SANDBOX")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--max-seconds", type=int, default=30)
    parser.add_argument("--horizons", default="15,60,240")
    parser.add_argument("--fee-per-side-bps", default="5")
    parser.add_argument("--slippage-per-side-bps", default="2")
    parser.add_argument("--clickhouse-host", default="127.0.0.1")
    parser.add_argument("--clickhouse-port", type=int, default=18123)
    parser.add_argument("--clickhouse-database", required=True)
    args = parser.parse_args(argv)
    scope = AccountScope("BINANCE", args.account_id, args.environment, "FUTURES")
    policy = EffectivenessPolicy(
        tuple(int(v) for v in args.horizons.split(",")),
        args.fee_per_side_bps,
        args.slippage_per_side_bps,
    )
    start, as_of = timestamp(args.start), timestamp(args.as_of)
    import clickhouse_connect
    import psycopg

    with psycopg.connect(
        os.environ["V2_EFFECTIVENESS_READ_DSN"],
        connect_timeout=5,
        application_name="v2-integration-effectiveness-read-only",
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path=trade_v2,pg_catalog")
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='100ms'")
        conn.execute("SET LOCAL jit=off")
        records, truncated = load_records(
            conn, scope, start_ms=start, as_of_ms=as_of, limit=args.limit
        )
        funnel = load_tv_funnel(conn, scope, start_ms=start, as_of_ms=as_of)
    client = clickhouse_connect.get_client(
        host=args.clickhouse_host,
        port=args.clickhouse_port,
        database=args.clickhouse_database,
        username=os.environ.get("V2_EFFECTIVENESS_CH_USER", "default"),
        password=os.environ.get("V2_EFFECTIVENESS_CH_PASSWORD", ""),
        connect_timeout=3,
        send_receive_timeout=5,
        settings={
            "readonly": 1,
            "max_execution_time": 3,
            "max_threads": 1,
            "max_result_rows": 10000,
            "result_overflow_mode": "throw",
        },
    )
    try:
        archive = IncrementalArchive(client, args.environment)
        report = build_report(
            records,
            read_bars=archive.read,
            start_ms=start,
            as_of_ms=as_of,
            policy=policy,
            max_seconds=args.max_seconds,
            truncated=truncated,
        )
        report["account_scope"] = {
            "exchange": scope.exchange,
            "environment": scope.environment,
            "product": scope.product,
        }
        report["tv_delivery_and_receipts"] = funnel
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
