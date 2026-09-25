"""Operator-only, GET-only venue reconciliation of an approved maintenance case.

The trading daemon must be stopped. Schema migration/deployment are separate
operator actions. This command never places or cancels exchange orders.
"""

import argparse
import json
import subprocess
import time

from services.v2_testnet_inventory import credentials, deployment_database
from v2_core.account_risk import AccountScope
from v2_core.maintenance_reconciliation import MaintenanceCloseReconciler
from v2_core.maintenance_resolution import MaintenanceResolution
from v2_core.transport import BinanceSignedTransport


def runner_stopped():
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "trade-v2-testnet-daemon.service",
            "-p",
            "ActiveState",
            "--value",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return result.stdout.strip() == "inactive"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--apply-approved-resolution", action="store_true", required=True
    )
    args = parser.parse_args()
    if not runner_stopped():
        raise ValueError("TRADING_RUNNER_NOT_STOPPED")
    connect = deployment_database()
    scope = AccountScope("BINANCE", args.account_id, "SANDBOX", "FUTURES")
    with connect() as conn:
        row = conn.execute(
            """SELECT o.episode_id::text FROM v2_orders o JOIN v2_trade_intents i
            ON i.intent_id=o.episode_id WHERE o.order_id=%s AND i.account_id=%s
            AND i.exchange='BINANCE' AND i.environment='SANDBOX' AND i.product='FUTURES'""",
            (args.case_id, args.account_id),
        ).fetchone()
    if row is None:
        raise ValueError("CASE_NOT_IN_ACCOUNT")
    cfg = credentials(args.credentials, notify=False)
    clock = lambda: time.time_ns() // 1000000
    request = BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock,
        permit=lambda method, path: method == "GET",
    )
    adopted = MaintenanceCloseReconciler(connect, request, scope=scope).reconcile(
        row[0], args.case_id
    )
    resolved = MaintenanceResolution(
        connect, request, scope=scope, runner_stopped=runner_stopped, clock_ms=clock
    ).resolve(args.case_id)
    print(
        json.dumps(
            {
                "adoption": adopted["status"],
                "disposition": resolved["disposition"],
                "venue_outcome": resolved["venue_outcome"],
                "settlement_checked": False,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never leak credentials or response text
        print(
            json.dumps(
                {"status": "RECONCILIATION_PAUSED", "error_class": type(exc).__name__}
            )
        )
        raise SystemExit(1) from None
