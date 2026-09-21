"""GET-only Testnet registered-protection recovery; no opening/cancel capability."""

import argparse
import json
import signal
import threading
import time

from services.v2_testnet_inventory import credentials, deployment_database
from services.v2_testnet_roundtrip import SCOPE
from v2_core.protection_supervisor import ProtectionAlertProjector, ProtectionSupervisor
from v2_core.public_market import PublicRateBudget
from v2_core.telegram import TelegramOperationalSink
from v2_core.transport import BinanceSignedTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    connect = deployment_database()
    cfg = credentials(args.config, notify=args.notify)
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        weights = {
            "/fapi/v1/algoOrder": 1,
            "/fapi/v1/order": 1,
            "/fapi/v1/userTrades": 5,
        }
        return method == "GET" and path in weights and budget.permit(weights[path])

    request = BinanceSignedTransport(
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=lambda: time.time_ns() // 1000000,
        permit=permit,
        enable_trading=False,
    )
    worker = ProtectionSupervisor(connect, request, scope=SCOPE)
    alerts = None
    if args.notify:
        sink = TelegramOperationalSink(
            token=cfg["TG_NOTIFY_TOKEN"],
            chat_id=cfg["TG_NOTIFY_CHAT_ID"],
            environment="SANDBOX",
        )
        alerts = ProtectionAlertProjector(connect, scope=SCOPE, sink=sink)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        try:
            result = worker.run_once(stop_requested=stop.is_set)
            if alerts:
                result["notifications"] = alerts.run_scheduled_batch(10)
            print(json.dumps(result), flush=True)
        except Exception as exc:  # noqa: BLE001 - no credentials or raw signed errors in logs
            print(
                json.dumps({"status": "UNAVAILABLE", "error_code": type(exc).__name__}),
                flush=True,
            )
            if args.once:
                raise SystemExit(1) from None
        if args.once or stop.wait(15):
            break


if __name__ == "__main__":
    main()
