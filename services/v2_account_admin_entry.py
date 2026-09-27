"""Owner console with Testnet signed GET verification. No trading workers."""

import os
import time

from services.v2_account_admin_http import handler_for
from services.v2_account_console import AccountConsole
from services.v2_account_verification import AccountVerification
from services.v2_dashboard import _Server
from services.v2_testnet_daemon_entry import PrivateRatePermit
from v2_core.database import connection_factory
from v2_core.public_market import PublicRateBudget
from v2_core.systemd_master_keys import SystemdMasterKeys


def main():
    keys = SystemdMasterKeys(os.environ["CREDENTIALS_DIRECTORY"])
    keys("v1")  # Fail closed before opening the listener.
    connect = connection_factory(
        "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=tradev2accounts",
        schema="trade_v2",
    )
    console = AccountConsole(
        connect,
        tenant_id=os.environ["V2_ADMIN_TENANT"],
        key_provider=keys,
        active_key_id="v1",
    )
    with connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM v2_tenants WHERE tenant_id=%s", (console.tenant,)
        ).fetchone():
            raise ValueError("OWNER_TENANT_REQUIRED")
    handler = handler_for(
        console,
        authenticated_user=os.environ["V2_ADMIN_OWNER"],
        origin=os.environ["V2_ADMIN_ORIGIN"],
        verification=AccountVerification(
            console,
            permit_factory=lambda account: PrivateRatePermit(
                PublicRateBudget(
                    connect, scope="v2-testnet-private:" + account, limit=2400
                ),
                entries=False,
                protection=False,
                exits=False,
            ),
            clock_ms=lambda: time.time_ns() // 1000000,
            cooldown_seconds=int(os.environ.get("V2_VERIFY_COOLDOWN_SECONDS", "60")),
            freshness_seconds=int(os.environ.get("V2_VERIFY_FRESHNESS_SECONDS", "300")),
        ),
    )

    class Server(_Server):
        def handle_error(self, request, client_address):
            pass  # Never journal request-related exception context.

    with Server(("127.0.0.1", 19528), handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - fixed diagnostic excludes secret-bearing context
        raise SystemExit("ACCOUNT_ADMIN_START_FAILED") from None
