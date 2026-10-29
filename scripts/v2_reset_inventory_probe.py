"""Credential-bearing, GET-only inventory proof for the approved Testnet reset."""

import json
import time
from uuid import uuid4

from services.v2_managed_testnet import database, readonly_request, scope_for
from scripts.v2_sandbox_maintenance_close import checked_route, route_rows, runners_stopped
from v2_core.account_inventory import AccountInventory


def main():
    if not runners_stopped():
        raise ValueError("TRADING_RUNNER_NOT_STOPPED")
    with database()() as conn:
        tenant, target = checked_route(route_rows(conn), 46)
    scope = scope_for(tenant, target)
    request = readonly_request(tenant, target, scope)
    observation = AccountInventory(
        request, scope=scope, clock_ms=lambda: time.time_ns() // 1000000
    ).collect(str(uuid4()))
    permission = request("GET", "/fapi/v1/accountConfig", {}).get("canTrade") is True
    account_positions = [
        row for row in observation.get("responses", {}).get("account", {}).get("positions", [])
        if str(row.get("positionAmt", "0")) not in {"0", "0.0", "0.000"}
    ]
    print(json.dumps({
        key: value for key, value in observation.items() if key != "responses"
    }, sort_keys=True), flush=True)
    print(json.dumps({"trade_permission": permission,
                      "account_nonzero_positions": len(account_positions)}), flush=True)
    if observation["blockers"] or not permission or account_positions:
        raise ValueError("RESET_VENUE_NOT_CLEAR")


if __name__ == "__main__":
    main()
