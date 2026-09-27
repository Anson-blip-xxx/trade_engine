"""Standalone Testnet-only market daemon: public GET endpoints, no credentials."""

import json
import shutil
import signal
import threading
import time

from services.v2_shared_market import IncrementalArchive, SharedMarketHub
from services.v2_testnet_inventory import deployment_database
from v2_core.public_market import BinancePublicMarket
from v2_core.scheduling import diagnostic_code
from v2_core.shared_market import HostBudget, MarketPolicy


def main():
    import clickhouse_connect
    import redis

    connect = deployment_database()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    cache = redis.Redis(
        unix_socket_path="/var/lib/trade-engine-v2/redis.sock",
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    archive = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=5,
    )
    try:
        with connect() as lock:
            lock.commit()
            lock.autocommit = True
            if not lock.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('v2-market-hub:SANDBOX',0))"
            ).fetchone()[0]:
                raise ValueError("MARKET_HUB_ALREADY_RUNNING")
            hub = SharedMarketHub(
                connect,
                transport=BinancePublicMarket(
                    environment="SANDBOX",
                    budget=HostBudget(connect, environment="SANDBOX", lane="market"),
                    enabled=True,
                    incremental=True,
                    timeout=3,
                ),
                cache=cache,
                archive=IncrementalArchive(archive, "SANDBOX"),
                clock_ms=lambda: time.time_ns() // 1000000,
                monotonic_ms=lambda: time.monotonic_ns() // 1000000,
            )
            while not stop.is_set():
                # Loss of this session fences the writer; do not reconnect it.
                lock.execute("SELECT 1")
                try:
                    policy = MarketPolicy.read(connect, "SANDBOX")
                    if (
                        shutil.disk_usage("/var/lib/trade-engine-v2").free
                        < policy.minimum_free_disk_gib * 1024**3
                    ):
                        raise ValueError("MARKET_DISK_RESERVE_REQUIRED")
                    result = hub.run_once()
                    interval = MarketPolicy.read(connect, "SANDBOX").interval_ms
                except Exception as exc:  # noqa: BLE001 - safe diagnostic code only
                    result = {"status": "RETRY", "reason": diagnostic_code(exc)}
                    interval = 5000
                print(json.dumps(result, sort_keys=True), flush=True)
                stop.wait(interval / 1000)
    finally:
        cache.close()
        archive.close()


if __name__ == "__main__":
    main()
