"""Flat-only controller release orchestration, with injected deployment ports.

No standalone CLI or credentials. The caller selects a reviewed immutable
release protocol and prepares a read-only inventory gate before stopping it.
The same in-memory gate must succeed again after every executor has exited.
"""

import time
from uuid import uuid4


def execute(protocol, *, prepare_gate, runners_stopped, apply=False):
    stage, baseline, stop_requested = "PRECHECK", None, False
    try:
        protocol.verify_layout()
        baseline = protocol.database("capture")
        gate = prepare_gate(baseline)
        before = gate()
        if not isinstance(before, dict) or before.get("status") != "FLAT_VERIFIED":
            raise protocol.ReleaseError("FLAT_PROOF_REQUIRED")
        protocol.emit("FLAT_RELEASE_PREFLIGHT", proof=before, mutation_authorized=apply)
        if not apply:
            return 0
        stage = "GRACEFUL_STOP"
        cursor = protocol.journal_cursor()
        stop_started_us = time.time_ns() // 1000
        stop_requested = True
        protocol.run(["sudo", "-n", "systemctl", "stop", protocol.UNIT], timeout=95)
        props = protocol.unit_properties()
        if (
            props.get("ActiveState") != "inactive"
            or props.get("MainPID") != "0"
            or not runners_stopped()
        ):
            raise protocol.ReleaseError("EXECUTORS_NOT_STOPPED")
        journal = protocol.run(
            [
                "sudo",
                "-n",
                "journalctl",
                "-u",
                protocol.UNIT,
                "--after-cursor",
                cursor,
                "--no-pager",
                "-o",
                "json",
            ]
        )
        if not protocol.graceful_stop_proved(journal, since_us=stop_started_us):
            raise protocol.ReleaseError("GRACEFUL_STOP_NOT_PROVEN")
        protocol.emit("GRACEFUL_STOP_VERIFIED", forced=False, stopped=True)
        stage = "QUIESCENT_FLAT_GATE"
        after = gate()
        if not isinstance(after, dict) or after.get("status") != "FLAT_VERIFIED":
            raise protocol.ReleaseError("FLAT_PROOF_REQUIRED")
        # Catch a concurrent service start during the venue/ledger observation.
        if not runners_stopped():
            raise protocol.ReleaseError("EXECUTOR_RESTARTED_DURING_FLAT_GATE")
        protocol.emit("QUIESCENT_FLAT_VERIFIED", proof=after)
        stage = "SWITCH_RELEASE"
        protocol.replace_link()
        stage = "MANAGE_ONLY_RECOVERY"
        started_ms = time.time_ns() // 1000000
        protocol.run(["sudo", "-n", "systemctl", "start", protocol.UNIT], timeout=30)
        recovery = protocol.wait_route(
            baseline, phase="DRAINING", since_ms=started_ms, timeout=60
        )
        protocol.emit(
            "MANAGE_ONLY_VERIFIED",
            phase=recovery["phase"],
            coverage=recovery["coverage"],
        )
        stage = "SAME_BINDING_REQUEST"
        requested = protocol.database("request", baseline, request_id=str(uuid4()))
        if requested.get("epoch") != baseline["epoch"] + 1:
            raise protocol.ReleaseError("UNEXPECTED_REQUEST_EPOCH")
        stage = "ACTIVE_RECOVERY"
        active = protocol.wait_route(
            baseline, phase="ACTIVE", since_ms=started_ms, timeout=90
        )
        props = protocol.unit_properties()
        if props.get("ActiveState") != "active" or props.get("SubState") != "running":
            raise protocol.ReleaseError("CONTROLLER_NOT_RUNNING")
        protocol.emit(
            "FLAT_RELEASE_ACTIVE",
            epoch=active["epoch"],
            phase=active["phase"],
            same_binding=True,
            coverage=active["coverage"],
            pid=int(props["MainPID"]),
            trading_profitability_verified=False,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - no provider/credential exception text
        # If the conditional release is declined after stopping but before
        # changing the link, restore the original protection manager. This is
        # not a rollback, never changes accounts and never forces entries on.
        old_management = "NOT_ATTEMPTED"
        if stop_requested and baseline is not None:
            try:
                if protocol.LINK.resolve() == protocol.PRIOR and runners_stopped():
                    current = protocol.database("status", baseline)
                    if (
                        current.get("same_binding") is True
                        and current.get("epoch") == baseline["epoch"]
                        and current.get("request_epoch") == baseline["request_epoch"]
                        and current.get("pending") is False
                    ):
                        old_management = "START_REQUESTED"
                        started_ms = time.time_ns() // 1000000
                        protocol.run(
                            ["sudo", "-n", "systemctl", "start", protocol.UNIT],
                            timeout=30,
                        )
                        protocol.wait_route(
                            baseline, phase="DRAINING", since_ms=started_ms, timeout=60
                        )
                        old_management = "MANAGE_ONLY_VERIFIED"
            except Exception:  # noqa: BLE001 - preserve a running manager; no forced resume
                old_management = "RECOVERY_UNVERIFIED"
        protocol.emit(
            "FLAT_RELEASE_BLOCKED",
            stage=stage,
            code=str(exc)
            if isinstance(exc, protocol.ReleaseError)
            else "FLAT_OR_RELEASE_CHECK_FAILED",
            old_management=old_management,
            automatic_rollback=False,
        )
        return 1
