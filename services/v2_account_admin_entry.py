"""Owner console with Testnet signed GET verification. No trading workers."""

import os
import time

from services.v2_account_admin_http import handler_for
from services.v2_account_console import AccountConsole
from services.v2_account_verification import AccountVerification
from services.v2_dashboard import _Server
from services.v2_risk_recovery_console import RiskRecoveryConsole
from services.v2_systemd_master_keys import SystemdMasterKeys
from v2_core.database import connection_factory
from v2_core.execution_routes import ExecutionRoutes
from v2_core.web_verification_quota import WebVerificationPermit


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
    strategy_console = None
    strategy_flag = os.environ.get("V2_ENABLE_STRATEGY_CONSOLE", "0")
    if strategy_flag not in {"0", "1"}:
        raise ValueError("INVALID_STRATEGY_CONSOLE_FLAG")
    if strategy_flag == "1":
        from services.v2_strategy_console import StrategyConsole, preflight

        strategy_console = StrategyConsole(
            connect, tenant_id=console.tenant, actor=os.environ["V2_ADMIN_OWNER"]
        )
        preflight(connect)
    handler = handler_for(
        console,
        authenticated_user=os.environ["V2_ADMIN_OWNER"],
        origin=os.environ["V2_ADMIN_ORIGIN"],
        execution_routes=ExecutionRoutes(connect),
        strategy_console=strategy_console,
        risk_recovery=RiskRecoveryConsole(
            connect, tenant_id=console.tenant, actor=os.environ["V2_ADMIN_OWNER"]
        ),
        verification=AccountVerification(
            console,
            permit_factory=lambda _account: WebVerificationPermit(connect),
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
