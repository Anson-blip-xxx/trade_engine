"""One fixed flat-account Binance Testnet conditional-order protocol acceptance.

No opening capability. A fixed close-all stop is created, queried and canceled;
PG records transitions. This is NOT strategy or trigger/fill acceptance.
"""

import argparse
import json
import subprocess
import time
from uuid import uuid4

from services.v2_testnet_cleanup import OLD_UNITS, SCOPE
from services.v2_testnet_inventory import credentials, deployment_database
from v2_core.account_inventory import AccountInventory, persist_inventory
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.public_market import PublicRateBudget
from v2_core.state import BusinessState
from v2_core.transport import BinanceSignedTransport

SPEC = ProtectionSpec(
    "flat-protocol-20260921", "BTCUSDT", "SELL", "STOP_MARKET", "10000"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute-testnet-probe", action="store_true", required=True)
    parser.add_argument("--take-profit", action="store_true")
    parser.add_argument("--corrected-take-profit", action="store_true")
    args = parser.parse_args()
    spec = (
        ProtectionSpec(
            "flat-protocol-20260921", "BTCUSDT", "SELL", "TAKE_PROFIT_MARKET", "1000000"
        )
        if args.take_profit
        else SPEC
    )
    if args.corrected_take_profit:
        # New protocol case, not a retry of an ambiguous order. The earlier TP
        # has an explicit -4007 rejection (price above exchange max 809484).
        spec = ProtectionSpec(
            "flat-protocol-price-corrected-20260921",
            "BTCUSDT",
            "SELL",
            "TAKE_PROFIT_MARKET",
            "90000",
        )
    for unit in OLD_UNITS:
        state = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        if state.stdout.strip() != "inactive":
            raise ValueError("OLD_TRADING_SERVICE_ACTIVE")
    connect = deployment_database()
    clock = lambda: time.time_ns() // 1000000
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        weights = {
            "/fapi/v1/openOrders": 40,
            "/fapi/v1/openAlgoOrders": 40,
            "/fapi/v1/positionSide/dual": 30,
            "/fapi/v1/multiAssetsMargin": 30,
            "/fapi/v3/positionRisk": 5,
            "/fapi/v3/account": 5,
        }
        remaining = weights.get(path, 1)
        while remaining:
            chunk = min(remaining, 10)
            if not budget.permit(chunk):
                return False
            remaining -= chunk
        return True

    cfg = credentials(args.config, notify=False)
    request = BinanceSignedTransport(
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock,
        permit=permit,
        enable_trading=False,
        enable_testnet_protection=True,
        enable_testnet_cancellation=True,
    )
    worker = TestnetProtection(BusinessState(connect), request, scope=SCOPE)
    if args.corrected_take_profit:
        previous = ProtectionSpec(
            "flat-protocol-20260921", "BTCUSDT", "SELL", "TAKE_PROFIT_MARKET", "1000000"
        )
        _, rejected = worker.read(previous)
        if (
            rejected is None
            or rejected.get("submission_error", {}).get("code") != -4007
        ):
            raise ValueError("CORRECTED_CASE_REQUIRES_EXPLICIT_PRICE_REJECTION")
    inventory = AccountInventory(request, scope=SCOPE, clock_ms=clock)
    before = inventory.collect(str(uuid4()))
    persist_inventory(connect, scope=SCOPE, observation=before)
    own = worker.params(spec)["clientAlgoId"]
    if set(before["blockers"]) - {"EXISTING_CONDITIONAL_ORDERS"} or any(
        row.get("clientAlgoId") != own
        for row in before["responses"]["conditional_orders"]
    ):
        raise ValueError("PROBE_REQUIRES_EXCLUSIVE_FLAT_ACCOUNT")
    submitted = worker.submit_once(spec)
    print(json.dumps({"phase": "SUBMIT_QUERY", **submitted}), flush=True)
    if submitted["status"] not in {"NEW", "CANCELED"}:
        raise ValueError("PROTECTION_NOT_CONFIRMED")
    result = worker.cancel_flat_once(spec)
    for _ in range(5):
        if result["status"] == "CANCELED":
            break
        time.sleep(0.5)
        result = worker.query(spec)
    print(json.dumps({"phase": "CANCEL_QUERY", **result}), flush=True)
    after = inventory.collect(str(uuid4()))
    print(
        json.dumps(persist_inventory(connect, scope=SCOPE, observation=after)),
        flush=True,
    )
    if result["status"] != "CANCELED" or after["blockers"]:
        raise ValueError("PROBE_NOT_CLEAR")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never expose signed requests or credentials
        print(
            json.dumps(
                {
                    "status": "PROBE_STOPPED",
                    "error_class": type(exc).__name__,
                    "exchange_code": getattr(exc, "code", None),
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
