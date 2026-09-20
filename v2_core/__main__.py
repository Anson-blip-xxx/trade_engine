"""Read-only V2 trace entry point. Schema installation is explicitly separate."""

import argparse
import json
import os

from v2_core.database import connection_factory
from v2_core.service import TradingData


def main():
    parser = argparse.ArgumentParser(description="V2 durable trading trace")
    parser.add_argument("intent_id")
    parser.add_argument(
        "--schema", default=os.environ.get("V2_POSTGRES_SCHEMA", "trade_v2")
    )
    parser.add_argument("--pnl-currency", choices=("USDT", "USDC"))
    parser.add_argument(
        "--decision",
        action="store_true",
        help="read a durable strategy decision, including ignored signals",
    )
    args = parser.parse_args()
    if args.decision and args.pnl_currency is not None:
        parser.error("--decision cannot be combined with --pnl-currency")
    dsn = os.environ.get("V2_POSTGRES_DSN")
    if not dsn:
        parser.error("V2_POSTGRES_DSN is required")
    connect = connection_factory(dsn, schema=args.schema, read_only=True)
    service = TradingData(connect)
    if args.decision:
        result = service.decision_trace(args.intent_id)
    elif args.pnl_currency is None:
        result = service.trace(args.intent_id)
    else:
        result = service.ledger.report(
            args.intent_id, settlement_currency=args.pnl_currency
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
