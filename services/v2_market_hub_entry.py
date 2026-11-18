"""Public GET-only collector: Testnet signals or independent LIVE shadow data.

LIVE shadow is explicit, never routes orders/signals and never replaces the
Testnet execution quote client. Installing it is a separate rollout action.
"""

import json
import os
import shutil
import signal
import threading
import time

from services.v2_quant_research import QuantResearch
from services.v2_s3_candles import CORRECTED_VERSION, PRICE_VERSION, STRUCTURE_VERSION
from services.v2_shared_market import IncrementalArchive, SharedMarketHub
from services.v2_testnet_inventory import deployment_database
from v2_core.public_market import BinancePublicMarket
from v2_core.scheduling import diagnostic_code
from v2_core.shared_market import HostBudget, MarketPolicy


def configured_feature_version(environ=None):
    """Explicit operator selection; never auto-activate a research candidate.

    V3/V4 remain replayable but are not daemon options: their cross-window
    ATR_EXPAND consumer is incompatible. V5 has a separately defined reference.
    """
    settings = os.environ if environ is None else environ
    version = settings.get("V2_MARKET_FEATURE_VERSION", PRICE_VERSION)
    if version not in {PRICE_VERSION, CORRECTED_VERSION, STRUCTURE_VERSION}:
        raise ValueError("MARKET_DAEMON_FEATURE_VERSION_UNSUPPORTED")
    return version


def create_hub(connect, cache, archive, *, feature_version, public_shadow=False):
    if type(public_shadow) is not bool:
        raise ValueError("MARKET_PUBLIC_SHADOW_MODE_INVALID")
    environment = "LIVE" if public_shadow else "SANDBOX"
    return SharedMarketHub(
        connect,
        transport=BinancePublicMarket(
            environment=environment,
            budget=HostBudget(connect, environment=environment, lane="market"),
            enabled=True,
            incremental=True,
            timeout=3,
        ),
        cache=cache,
        archive=IncrementalArchive(archive, environment),
        clock_ms=lambda: time.time_ns() // 1000000,
        monotonic_ms=lambda: time.monotonic_ns() // 1000000,
        feature_version=feature_version,
        publish_signals=not public_shadow,
        # Priorities are read only, and still intersect the LIVE catalog. This
        # does not grant Testnet tradability to a LIVE-only listed contract.
        priority_environment="SANDBOX",
        quant_research=None
        if public_shadow
        else QuantResearch(
            connect,
            archive=IncrementalArchive(archive, environment),
            environment=environment,
            clock_ms=lambda: time.time_ns() // 1000000,
        ),
    )


def main(*, public_shadow=False):
    import clickhouse_connect
    import redis

    feature_version = configured_feature_version()
    if type(public_shadow) is not bool:
        raise ValueError("MARKET_PUBLIC_SHADOW_MODE_INVALID")
    if public_shadow and "V2_MARKET_FEATURE_VERSION" not in os.environ:
        raise ValueError("MARKET_PUBLIC_SHADOW_FEATURE_VERSION_REQUIRED")
    environment = "LIVE" if public_shadow else "SANDBOX"
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
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("v2-market-hub:" + environment,),
            ).fetchone()[0]:
                raise ValueError("MARKET_HUB_ALREADY_RUNNING")
            hub = create_hub(
                connect,
                cache,
                archive,
                feature_version=feature_version,
                public_shadow=public_shadow,
            )
            while not stop.is_set():
                # Loss of this session fences the writer; do not reconnect it.
                lock.execute("SELECT 1")
                try:
                    policy = MarketPolicy.read(connect, environment)
                    if (
                        shutil.disk_usage("/var/lib/trade-engine-v2").free
                        < policy.minimum_free_disk_gib * 1024**3
                    ):
                        raise ValueError("MARKET_DISK_RESERVE_REQUIRED")
                    result = hub.run_once()
                    interval = MarketPolicy.read(connect, environment).interval_ms
                except Exception as exc:  # noqa: BLE001 - safe diagnostic code only
                    result = {"status": "RETRY", "reason": diagnostic_code(exc)}
                    interval = 5000
                print(json.dumps(result, sort_keys=True), flush=True)
                stop.wait(interval / 1000)
    finally:
        cache.close()
        archive.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-public-shadow", action="store_true")
    main(public_shadow=parser.parse_args().live_public_shadow)
