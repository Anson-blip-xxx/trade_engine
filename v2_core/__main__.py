"""Read-only V2 trace entry point. Schema installation is explicitly separate."""

import argparse
import json
import os
from contextlib import contextmanager

from v2_core.service import TradingData


def main():
    parser = argparse.ArgumentParser(description="V2 durable trading trace")
    parser.add_argument("intent_id")
    args = parser.parse_args()
    dsn = os.environ.get("V2_POSTGRES_DSN")
    if not dsn:
        parser.error("V2_POSTGRES_DSN is required")
    import psycopg

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn

    result = TradingData(connect).trace(args.intent_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
