"""Independent research observer. No API keys, order clients, or TG access."""

import json
import os
import signal
import threading

from psycopg.types.json import Jsonb

from services.v2_reentry_observer import ObserverLimits, ReentryObserver
from services.v2_shared_market import IncrementalArchive
from v2_core.database import connection_factory
from v2_core.reentry import ReentryPolicy


def main():
    import clickhouse_connect

    limits = ObserverLimits(**json.loads(os.environ.get("V2_SHADOW_LIMITS_JSON", "{}")))
    policy = ReentryPolicy(**json.loads(os.environ.get("V2_SHADOW_POLICY_JSON", "{}")))
    connect = connection_factory(os.environ["V2_SHADOW_DSN"], schema="trade_v2")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database=os.environ["V2_SHADOW_ARCHIVE_DATABASE"],
        username="tradev2shadow",
        connect_timeout=3,
        send_receive_timeout=5,
        settings={
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": 3,
            "max_result_rows": 20000,
        },
    )
    try:
        observer = ReentryObserver(
            connect,
            IncrementalArchive(client, "SANDBOX").read,
            policy=policy,
            limits=limits,
        )
        # Session lock only for this observer; released on exit/crash, never used by trading.
        with connect() as lease:
            if not lease.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('v2-reentry-observer-singleton',0))"
            ).fetchone()[0]:
                raise ValueError("OBSERVER_ALREADY_RUNNING")
            lease.commit()
            while not stop.is_set():
                try:
                    health = observer.tick()
                except Exception:  # noqa: BLE001 - never journal credentials in exceptions
                    health = {
                        "status": "OBSERVER_READ_OR_WRITE_FAILED",
                        "execution_authorized": False,
                    }
                    with connect() as conn:
                        conn.execute(
                            "UPDATE v2_reentry_observer_state SET heartbeat_ms=%s,health=health || %s WHERE name='sandbox'",
                            (observer.clock(), Jsonb(health)),
                        )
                # Fixed codes only. Never dump evidence, account IDs, DSNs or exception text.
                print(
                    json.dumps(
                        {
                            "status": health["status"],
                            "processed": health.get("processed", 0),
                        }
                    ),
                    flush=True,
                )
                stop.wait(limits.poll_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - fixed startup diagnostic only
        raise SystemExit("REENTRY_OBSERVER_START_FAILED") from None
