"""GET-only Testnet registered-protection recovery; no opening/cancel capability."""

import argparse
import json
import signal
import threading
import time

from services.v2_testnet_inventory import credentials, deployment_database
from services.v2_testnet_roundtrip import SCOPE
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.protection_supervisor import ProtectionAlertProjector, ProtectionSupervisor
from v2_core.public_market import PublicRateBudget
from v2_core.telegram import TelegramOperationalSink
from v2_core.transport import BinanceSignedTransport


def query_permit(budget, method, path, *, audit_account):
    weights = {"/fapi/v1/algoOrder": 1, "/fapi/v1/order": 1, "/fapi/v1/userTrades": 5}
    if audit_account:
        weights.update(
            {
                "/fapi/v1/openOrders": 40,
                "/fapi/v1/openAlgoOrders": 40,
                "/fapi/v1/positionSide/dual": 30,
                "/fapi/v1/multiAssetsMargin": 30,
                "/fapi/v3/account": 5,
                "/fapi/v3/positionRisk": 5,
            }
        )
    if method != "GET" or path not in weights:
        return False
    weight = weights[path]
    while weight:
        chunk = min(weight, 10)
        if not budget.permit(chunk):
            return False
        weight -= chunk
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--audit-account", action="store_true")
    args = parser.parse_args()
    connect = deployment_database()
    cfg = credentials(args.config, notify=args.notify)
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        return query_permit(budget, method, path, audit_account=args.audit_account)

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
    audit = (
        AccountCoverageAudit(
            connect, request, scope=SCOPE, clock_ms=lambda: time.time_ns() // 1000000
        )
        if args.audit_account
        else None
    )
    next_audit = 0
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
            if (
                audit is not None
                and not stop.is_set()
                and time.monotonic() >= next_audit
            ):
                result["account_coverage"] = audit.run_once()
                next_audit = time.monotonic() + 60
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
