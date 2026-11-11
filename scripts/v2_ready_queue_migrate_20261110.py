"""Additive SANDBOX task projection; dry-run unless --execute.

Does not restart services, change leases, delete history, change accounts or
contact an exchange. Re-running invalidates old projection proof before bounded
backfill. A failed stage rolls back its transaction and never enables READY.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DDL = ROOT / "db/migrations/20261110_strategy_ready_queue.sql"
DDL_SHA256 = "a885043abe9ef68120e559087e1ff756b86f7767063a0c065a576506d2d247ab"


def connect():
    return psycopg.connect(
        "dbname=trade_v2_testnet host=/var/run/postgresql port=55432",
        connect_timeout=5,
        options="-c search_path=trade_v2,pg_catalog -c lock_timeout=200ms "
        "-c statement_timeout=2000ms",
        application_name="v2-ready-queue-migration",
    )


def boundary(conn):
    row = conn.execute(
        "SELECT current_setting('cluster_name'),current_database(),"
        "current_setting('listen_addresses'),current_schema(),"
        "pg_get_userbyid(relowner)=current_user FROM pg_class "
        "WHERE oid='v2_strategy_tasks'::regclass"
    ).fetchone()
    if row != ("16/tradev2", "trade_v2_testnet", "", "trade_v2", True):
        raise ValueError("SANDBOX_TASK_OWNER_REQUIRED")


def emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, sort_keys=True), flush=True)


def main():
    apply = sys.argv[1:] == ["--execute"]
    if sys.argv[1:] and not apply:
        raise ValueError("EXPECTED_DRY_RUN_OR_EXECUTE")
    ddl = DDL.read_bytes()
    if hashlib.sha256(ddl).hexdigest() != DDL_SHA256:
        raise ValueError("TESTED_MIGRATION_HASH_REQUIRED")
    stage = "PRECHECK"
    try:
        with connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            boundary(conn)
            exists = conn.execute(
                "SELECT to_regclass('v2_strategy_ready') IS NOT NULL"
            ).fetchone()[0]
        emit(stage, projection_exists=exists, mutation_authorized=apply)
        if not apply:
            return 0
        stage = "CREATE_DISABLED_PROJECTION"
        started = time.monotonic()
        with connect() as conn:
            boundary(conn)
            conn.execute(ddl.decode())
        emit(stage, elapsed_ms=round((time.monotonic() - started) * 1000))
        stage = "BOUNDED_BACKFILL"
        copied = 0
        for batch in range(100):
            with connect() as conn:
                added = conn.execute(
                    "SELECT v2_strategy_ready_backfill(100)"
                ).fetchone()[0]
            copied += added
            if added == 0:
                break
        else:
            raise ValueError("BACKFILL_BATCH_LIMIT_REQUIRES_RECHECK")
        emit(stage, batches=batch + 1, copied=copied)
        # Zero copied may mean locked rows; exact bidirectional validation is
        # authoritative. Any concurrent missed rows cause rollback, not enable.
        stage = "EXACT_SET_ENABLE"
        started = time.monotonic()
        with connect() as conn:
            enabled = conn.execute("SELECT v2_strategy_ready_enable()").fetchone()[0]
        emit(
            stage,
            enabled=enabled,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - never print DB/provider payloads
        emit(stage, status="NOT_COMPLETED", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
