"""Independent, localhost-only research runner; never order authority."""

import json
import os
import time
from contextlib import contextmanager

from v2_core.database import connection_factory

LOCK = "sandbox-trend-campaign-worker-v1"


def run(values=None, *, base=None, archive_factory=None, worker=None, now_ms=None):
    values = os.environ if values is None else values
    if base is None:
        base = connection_factory(values["V2_TREND_CAMPAIGN_DSN"], schema="trade_v2")
    if archive_factory is None:
        from services.v2_daily_mover_entry import archive_client
        from services.v2_reentry_archive import ReentryArchive

        archive_factory = lambda: ReentryArchive(factory=archive_client)
    if worker is None:
        from services.v2_trend_campaign_research import run as worker

    @contextmanager
    def connect():
        with base() as conn:
            conn.execute("SET LOCAL statement_timeout='5s'")
            conn.execute("SET LOCAL lock_timeout='200ms'")
            yield conn

    with connect() as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (LOCK,)
        ).fetchone()[0]
        lock.commit()
        if not acquired:
            return {"status": "ALREADY_RUNNING", "execution_authorized": False}
        archive = None
        try:
            archive = archive_factory()
            return worker(
                connect,
                archive,
                now_ms=time.time_ns() // 1_000_000 if now_ms is None else now_ms,
            )
        finally:
            try:
                if archive is not None:
                    archive.close()
            finally:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (LOCK,)
                )


def main():
    try:
        report = run()
    except Exception:  # noqa: BLE001 - no raw DB errors or connection strings
        report = {
            "status": "UNAVAILABLE",
            "reason": "TREND_CAMPAIGN_JOB_UNAVAILABLE",
            "execution_authorized": False,
        }
    print(json.dumps(report, separators=(",", ":"), sort_keys=True), flush=True)
    return 1 if report["status"] in {"UNAVAILABLE", "PARTIAL"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
