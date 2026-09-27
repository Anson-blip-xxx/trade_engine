"""Single Testnet controller process; no LIVE activation or shell child commands."""

import os
import signal
import subprocess
import threading
from contextlib import contextmanager

from services.v2_execution_controller import ExecutionController
from services.v2_managed_testnet import TestnetPorts, database


@contextmanager
def control_connection():
    with database()() as c:
        c.execute("SET LOCAL ROLE trade_v2_execution_operator")
        yield c


def main():
    for unit in (
        "trade-v2-testnet-daemon.service",
        "trade-v2-testnet-watchdog.service",
    ):
        if (
            subprocess.run(
                ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            != "inactive"
        ):
            raise ValueError("LEGACY_RUNNER_MUST_BE_STOPPED")
    values = {k: v for k, v in os.environ.items() if k.startswith("V2_")}
    tenant = values["V2_CONTROLLER_TENANT"]
    ports = TestnetPorts(tenant, values)
    controller = ExecutionController(
        control_connection,
        tenant_id=tenant,
        preflight=ports.preflight,
        source_clear=ports.source_clear,
        spawn=ports.spawn,
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with database()() as lease:
        if not lease.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            ("execution-controller:" + tenant + ":SANDBOX",),
        ).fetchone()[0]:
            raise ValueError("CONTROLLER_ALREADY_RUNNING")
        lease.commit()
        try:
            while not stop.is_set():
                lease.execute("SELECT 1")
                lease.commit()
                controller.step()
                stop.wait(10)
        finally:
            actual = controller.actual()
            if controller.worker is not None:
                try:
                    if actual and actual["phase"] in {"ACTIVE", "STARTING", "DRAINING"}:
                        controller.record(
                            actual,
                            "DRAINING",
                            token=actual["worker_token"],
                            blockers=["CONTROLLER_RESTART_RECOVERY"],
                        )
                finally:
                    controller.worker.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - no config, credential or exception payload in logs
        raise SystemExit("EXECUTION_CONTROLLER_FAILED") from None
