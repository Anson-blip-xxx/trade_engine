"""Bounded Sandbox archive ablation; never loads exchange keys or publishes signals.

Run as an offline job, never from a request handler or the trading consumer.
--persist needs V2_RESEARCH_PG_DSN and stores research only, in PostgreSQL.
Without --persist, no database writes occur. No local business-data files.
"""

import argparse
import json
import os
import time

from services.v2_range_volume_study import archive_study, persist
from services.v2_shared_market import IncrementalArchive
from v2_core.database import connection_factory
from v2_core.quant_replay import ReplayPolicy
from v2_core.range_volume import RangeVolumePolicy


def validate_sink(connect):
    with connect() as conn:
        database, schema, table = conn.execute(
            "SELECT current_database(),current_schema(),to_regclass('trade_v2.v2_business_state')::text"
        ).fetchone()
        if (database, schema) != ("trade_v2_testnet", "trade_v2") or table not in {
            "v2_business_state",
            "trade_v2.v2_business_state",
        }:
            raise ValueError("RESEARCH_DATABASE_SCOPE_MISMATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", required=True, help="comma separated; max 3 per offline job"
    )
    parser.add_argument(
        "--end-ms", type=int, required=True, help="exclusive closed-minute boundary"
    )
    parser.add_argument("--minutes", type=int, default=2880)
    parser.add_argument("--timeframe", type=int, default=5)
    parser.add_argument("--plugin", choices=("donchian", "squeeze"), default="donchian")
    parser.add_argument("--split-percent", type=int, default=70)
    parser.add_argument(
        "--range-policy", default="{}", help="JSON overrides of research thresholds"
    )
    parser.add_argument(
        "--replay-policy",
        default="{}",
        help="JSON cost/venue assumptions; not live risk settings",
    )
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    targets = args.symbols.split(",")
    if not 1 <= len(targets) <= 3 or len(set(targets)) != len(targets):
        parser.error("one to three distinct symbols required")
    range_policy = RangeVolumePolicy(**json.loads(args.range_policy))
    replay_policy = ReplayPolicy(**json.loads(args.replay_policy))
    connect = None
    if args.persist:
        if not os.environ.get("V2_RESEARCH_PG_DSN"):
            parser.error("--persist requires V2_RESEARCH_PG_DSN")
        connect = connection_factory(
            os.environ["V2_RESEARCH_PG_DSN"], schema="trade_v2"
        )
        validate_sink(connect)
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=10,
        settings={
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": 8,
            "max_memory_usage": 134217728,
        },
    )
    started = time.monotonic()
    try:
        for target in targets:
            if time.monotonic() - started >= 240:
                raise TimeoutError("STUDY_JOB_BUDGET")
            report = archive_study(
                IncrementalArchive(client, "SANDBOX"),
                target=target,
                end_ms=args.end_ms,
                minutes=args.minutes,
                timeframe_minutes=args.timeframe,
                plugin=args.plugin,
                split_percent=args.split_percent,
                range_policy=range_policy,
                policy=replay_policy,
            )
            stored = (
                persist(connect, report)
                if connect and report["status"] == "SIMULATED"
                else None
            )
            # Full per-candidate evidence stays in PG, not stdout or local files.
            summary = {k: v for k, v in report.items() if k not in {"folds"}}
            if "folds" in report:
                summary["folds"] = {
                    fold: {
                        name: {
                            k: v
                            for k, v in result.items()
                            if k not in {"trades", "entry_attempts"}
                        }
                        for name, result in value["variants"].items()
                    }
                    for fold, value in report["folds"].items()
                }
            print(
                json.dumps({"report": summary, "stored": stored}, sort_keys=True),
                flush=True,
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()
