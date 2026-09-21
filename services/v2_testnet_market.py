"""Deployed BTC/ETH data-only pilot, fixed testnet and isolated local stores.

There is no order port. Scope is deliberately NOT the full market universe or
the complete trading daemon. A future universe change needs versioned source IDs.
"""

import argparse
import json
import signal
import threading
import time

from services.v2_market_pipeline import create_market_pipeline
from services.v2_testnet_inventory import config_fields, deployment_database
from v2_core.telegram import TelegramOperationalSink


def pilot_config():
    return {
        "environment": "SANDBOX",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "egress_scope": "v2-testnet-host-public",
        "weight_limit": 100,
        "max_pending": 3,
        "max_age_ms": 120000,
        "lifetime_ms": 90000,
        "enabled": True,
    }


def main():
    import clickhouse_connect
    import redis

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--notify-config")
    args = parser.parse_args()
    connect = deployment_database()
    notify = lambda event: False
    if args.notify_config:
        cfg = config_fields(
            args.notify_config, {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}
        )
        sink = TelegramOperationalSink(
            token=cfg["TG_NOTIFY_TOKEN"],
            chat_id=cfg["TG_NOTIFY_CHAT_ID"],
            environment="SANDBOX",
        )
        notify = lambda event: sink(
            event["event_id"], event["environment"], event["kind"], event
        )
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
        send_receive_timeout=10,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        worker = create_market_pipeline(
            connect,
            clickhouse=archive,
            redis_client=cache,
            config=pilot_config(),
            notify=notify,
            clock_ms=lambda: time.time_ns() // 1000000,
            monotonic_ms=lambda: time.monotonic_ns() // 1000000,
        )
        while not stop.is_set():
            print(json.dumps(worker.run_once(), sort_keys=True), flush=True)
            if not args.serve:
                break
            stop.wait(10)
    finally:
        archive.close()
        cache.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - don't leak external-client error details
        print('{"status":"MARKET_PILOT_FAILED"}', flush=True)
        raise SystemExit(1) from None
