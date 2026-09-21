"""Explicit testnet-only startup probe. No trading, cancellation or file state.

Run against the separately provisioned V2 database; emits summary only. Credentials
are selected from the provided legacy config without importing legacy modules.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from v2_core.account_inventory import (
    AccountInventory,
    InventoryProjector,
    persist_inventory,
)
from v2_core.account_risk import AccountScope
from v2_core.database import connection_factory
from v2_core.telegram import TelegramOperationalSink
from v2_core.transport import BinanceSignedTransport


def credentials(path, *, notify):
    wanted = {"BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"}
    if notify:
        wanted |= {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}
    return config_fields(path, wanted)


def config_fields(path, wanted):
    result = {}
    for line in Path(path).read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        if key in result:
            raise ValueError("DUPLICATE_CREDENTIAL_FIELD")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            raise ValueError("MISSING_TESTNET_CREDENTIALS")
        result[key] = value
    if set(result) != wanted:
        raise ValueError("MISSING_TESTNET_CREDENTIALS")
    return result


def deployment_database():
    # A deliberately fixed deployment boundary, not a general database installer.
    connect = connection_factory(
        "dbname=trade_v2_testnet host=/var/run/postgresql port=55432",
        schema="trade_v2",
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT current_setting('cluster_name'),current_database(),"
            "current_setting('listen_addresses'),to_regclass('v2_business_state')::text"
        ).fetchone()
        if row != ("16/tradev2", "trade_v2_testnet", "", "v2_business_state"):
            raise ValueError("WRONG_DEPLOYMENT_DATABASE")
    return connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    connect = deployment_database()
    cfg = credentials(args.config, notify=args.notify)
    clock = lambda: time.time_ns() // 1000000
    scope = AccountScope("BINANCE", args.account_id, "SANDBOX", "FUTURES")
    request = BinanceSignedTransport(
        account_id=scope.account_id,
        environment=scope.environment,
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock,
        permit=lambda method, path: method == "GET",
        enable_trading=False,
    )
    observation = AccountInventory(request, scope=scope, clock_ms=clock).collect(
        str(uuid4())
    )
    result = persist_inventory(connect, scope=scope, observation=observation)
    if args.notify:
        sink = TelegramOperationalSink(
            token=cfg["TG_NOTIFY_TOKEN"],
            chat_id=cfg["TG_NOTIFY_CHAT_ID"],
            environment=scope.environment,
        )
        # Scope filters apply before claims; no mutations of another account's queue.
        projector = InventoryProjector(connect, scope=scope, sink=sink)
        result["notification_delivery"] = projector.run_scheduled_batch(10)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never print credential-bearing exception chains
        print('{"status":"INVENTORY_COMMAND_FAILED"}')
        raise SystemExit(1) from None
