"""Bounded read-only source comparison. Never fetch prices, submit or store orders.

Reads existing source-labelled Redis observations. Missing LIVE shadow data is
UNKNOWN; the reader never starts the collector or falls back to Demo prices.
"""

from v2_core.ingress import milliseconds, symbol
from v2_core.paired_market_observation import compare_market_observations
from v2_core.producer import RedisMarketContext


def collect_comparisons(cache, symbols, *, clock_ms, max_age_ms=90000):
    if not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= 20:
        raise ValueError("BOUNDED_COMPARISON_SYMBOLS_REQUIRED")
    targets = [symbol(target) for target in symbols]
    if len(set(targets)) != len(targets):
        raise ValueError("DUPLICATE_COMPARISON_SYMBOL")
    if type(max_age_ms) is not int or not 1 <= max_age_ms <= 86400000:
        raise ValueError("BOUNDED_MARKET_COMPARISON_FRESHNESS_REQUIRED")
    readers = {
        environment: RedisMarketContext(cache, environment=environment)
        for environment in ("LIVE", "SANDBOX")
    }
    rows = []
    for target in targets:
        observations, failures = {}, []
        for environment, reader in readers.items():
            try:
                observations[environment] = reader.read("s3", environment, target)
            except Exception:  # noqa: BLE001 - no raw cache payloads/errors in reports
                observations[environment] = None
                failures.append(environment)
        report = compare_market_observations(
            observations["LIVE"],
            observations["SANDBOX"],
            now_ms=milliseconds(clock_ms()),
            max_age_ms=max_age_ms,
        )
        rows.append(
            {**report, "requested_symbol": target, "source_read_failures": failures}
        )
    return {
        "version": "market-source-compare-reader-v1",
        "execution_authorized": False,
        "pnl_evaluated": False,
        "rows": rows,
        "counts": {
            status: sum(row["status"] == status for row in rows)
            for status in ("COMPARABLE", "PARTIAL", "UNKNOWN")
        },
    }


def main():
    import argparse
    import json
    import time

    import redis

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--max-age-ms", type=int, default=90000)
    args = parser.parse_args()
    cache = redis.Redis(
        unix_socket_path="/var/lib/trade-engine-v2/redis.sock",
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        result = collect_comparisons(
            cache,
            args.symbols,
            clock_ms=lambda: time.time_ns() // 1000000,
            max_age_ms=args.max_age_ms,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        cache.close()


if __name__ == "__main__":
    main()
