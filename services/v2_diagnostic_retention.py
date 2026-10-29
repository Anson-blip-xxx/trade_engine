"""Bounded PostgreSQL maintenance, no exchange client or trading credentials."""

import argparse
import json
import os
import time

import psycopg


def run(connect, *, apply=False, batches=1, report=None):
    if type(apply) is not bool or type(batches) is not int or not 1 <= batches <= 24:
        raise ValueError("BOUNDED_RETENTION_RUN_REQUIRED")
    results = []
    for _ in range(batches if apply else 1):
        with connect() as conn:
            conn.execute("SET LOCAL lock_timeout='500ms'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            conn.execute("SET LOCAL work_mem='4MB'")
            result = conn.execute(
                "SELECT trade_v2.v2_run_retention(%s)", (apply,)
            ).fetchone()[0]
        results.append(result)
        if report is not None:
            report(result)
        if not result.get("batch_full"):
            break
        time.sleep(0.25)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        batches = int(os.environ.get("V2_RETENTION_MAX_BATCHES", "12"))
        dsn = "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=tradev2maintenance"
        run(
            lambda: psycopg.connect(dsn),
            apply=args.apply,
            batches=batches,
            report=lambda result: print(json.dumps(result, sort_keys=True), flush=True),
        )
    except Exception as exc:  # noqa: BLE001 - no raw database or environment data
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "committed_batches_preserved": True,
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
