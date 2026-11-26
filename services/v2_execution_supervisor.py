"""Single Testnet controller process; no LIVE activation or shell child commands."""

import json
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager

from services.v2_execution_controller import ExecutionController
from services.v2_managed_health import build_managed_health
from services.v2_managed_testnet import TestnetPorts, database
from v2_core.account_registry import uid


@contextmanager
def control_connection():
    with database()() as c:
        c.execute("SET LOCAL ROLE trade_v2_execution_operator")
        yield c


def shutdown_worker(controller):
    """Stop local ownership first; DB availability never precedes stopping.

    A failed/unknown stop retains the exact handle and performs no DB work.
    Individual member stop calls still own their existing boundedness contract.
    Only confirmed local termination permits a best-effort recovery marker;
    this is not a route handover, account switch or permission to restart.
    """
    result = {
        "status": "IDLE",
        "stop_confirmed": True,
        "route_recorded": False,
        "request_stop_failed": False,
        "error_code": None,
        "execution_authorized": False,
    }
    worker = controller.worker
    if worker is None:
        return result
    try:
        request_stop = getattr(worker, "request_stop", None)
        if callable(request_stop):
            request_stop()
    except Exception:  # noqa: BLE001 - a failed hint must not skip the real stop
        result["request_stop_failed"] = True
    try:
        stopped = worker.stop() is True
        error = None if stopped else "SOURCE_PROCESS_NOT_STOPPED"
    except Exception:  # noqa: BLE001 - preserve local ownership, no exception payload
        stopped, error = False, "WORKER_STOP_FAILED"
    result.update(
        status="STOPPED" if stopped else "STOP_UNCONFIRMED",
        stop_confirmed=stopped,
        error_code=error,
    )
    if not stopped:
        return result
    try:
        actual = controller.actual()
        if actual and actual["phase"] in {"ACTIVE", "STARTING", "DRAINING"}:
            controller.record(
                actual,
                "DRAINING",
                token=actual["worker_token"],
                blockers=["CONTROLLER_RESTART_RECOVERY"],
            )
            result["route_recorded"] = True
    except Exception:  # noqa: BLE001 - stop is confirmed even if its route marker cannot be saved
        result["error_code"] = "CONTROLLER_STOP_ROUTE_UNRECORDED"
    return result


def shutdown_controller(controller, *, wait=time.sleep):
    """Keep the caller's controller lease while any local stop is unconfirmed.

    The main routine invokes this inside its existing lease context. There is
    no ordinary return/lease handover until all owned members confirm stop.
    Forced OS exit and systemd cgroup cleanup remain the final external bound;
    this loop cannot prove termination of a process the OS has not reaped.
    """
    previous = None
    while True:
        result = shutdown_worker(controller)
        if result != previous:
            try:
                print(
                    json.dumps({"component": "execution-controller-stop", **result}),
                    flush=True,
                )
            except Exception:  # noqa: BLE001,S110 - logging must not control handle ownership
                pass
            previous = result
        if result["stop_confirmed"]:
            return result
        # The controller's stop Event is already set; waiting on it here would
        # spin immediately and repeatedly call a failed member stop.
        wait(0.2)


def recover_paused_typed(controller, values):
    """Explicit startup recovery of the same STOPPED binding, never activation.

    The caller must own the controller's existing session lease. No request,
    policy, epoch or account is changed here. The normal controller start path
    creates a fresh worker token and leaves the route DRAINING. Any existing
    local handle, including a previously unconfirmed stop, is left untouched.
    """
    enabled = values.get("V2_TYPED_RECOVER_PAUSED", "false")
    if type(enabled) is not str or enabled not in {"false", "true"}:
        raise ValueError("TYPED_PAUSED_RECOVERY_CONFIG_INVALID")
    if enabled == "false":
        return "DISABLED"
    if (
        values.get("V2_CONTROLLER_WORKER_MODE") != "TYPED_STRATEGY"
        or values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") != "false"
        or values.get("V2_ENVIRONMENT", "SANDBOX") != "SANDBOX"
    ):
        raise ValueError("TYPED_PAUSED_RECOVERY_CONFIG_INVALID")
    if controller.worker is not None:
        return "EXISTING_OWNER_RETAINED"
    try:
        actual, desired = controller.actual(), controller.desired()
        if actual is None or actual.get("phase") != "STOPPED":
            return "NOT_STOPPED"
        if desired is None:
            return "NO_MATCHING_REQUEST"
        if any(
            type(row) is not dict
            or row.get("environment", "SANDBOX") != "SANDBOX"
            or any(
                type(row.get(key)) is not int or row[key] < 1
                for key in ("epoch", "binding_version")
            )
            for row in (actual, desired)
        ):
            raise ValueError()
        for row in (actual, desired):
            uid(row["target_registry"])
    except Exception:  # noqa: BLE001 - no route/SQL payload in startup diagnostics
        raise ValueError("TYPED_PAUSED_RECOVERY_ROUTE_UNAVAILABLE") from None
    # This helper runs immediately before the normal controller loop. Returning
    # a no-op here would let step() consume a previously queued activation or
    # account switch during explicitly paused startup. Fail inside the caller's
    # lease/finally boundary instead; never change the pending request itself.
    if any(
        actual.get(key) != desired.get(key)
        for key in ("target_registry", "binding_version")
    ):
        raise ValueError("TYPED_PAUSED_RECOVERY_DIFFERENT_BINDING_PENDING")
    if desired["epoch"] > actual["epoch"]:
        raise ValueError("TYPED_PAUSED_RECOVERY_NEW_REQUEST_PENDING")
    return (
        "RECOVERING"
        if controller.start(actual, recovering=True) is True
        else "RECOVERY_NOT_STARTED"
    )


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
        resume_preflight=ports.resume_preflight,
        recovery_retry_seconds=int(
            values.get("V2_CONTROLLER_RECOVERY_RETRY_SECONDS", "30")
        ),
    )
    stop = threading.Event()

    def runtime_snapshot():
        from services.v2_strategy_runtime import TypedStrategyRuntime

        worker = controller.worker
        return worker.snapshot() if type(worker) is TypedStrategyRuntime else None

    health = build_managed_health(
        tenant, values, stop, runtime_snapshot=runtime_snapshot
    )
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
            recover_paused_typed(controller, values)
            while not stop.is_set():
                lease.execute("SELECT 1")
                lease.commit()
                controller.step()
                # Independent of child success: alive and CYCLE are not proof
                # that actual opening/protection/settlement is progressing.
                health.step(controller.actual())
                stop.wait(10)
        finally:
            shutdown_controller(controller)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - no config, credential or exception payload in logs
        raise SystemExit("EXECUTION_CONTROLLER_FAILED") from None
