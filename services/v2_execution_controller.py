"""Trusted single-environment controller. Ports must never report speculative readiness.

The caller holds a process-lifetime database advisory lock. Web has no permission
to write these actual route states. LIVE is intentionally not a supported worker.
"""

import time
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.execution_routes import ExecutionRoutes


class ExecutionController:
    def __init__(
        self,
        connect,
        *,
        tenant_id,
        preflight,
        source_clear,
        spawn,
        resume_preflight=None,
        monotonic=time.monotonic,
        recovery_retry_seconds=30,
    ):
        self.connect, self.tenant = connect, uid(tenant_id)
        self.preflight, self.source_clear, self.spawn = preflight, source_clear, spawn
        if resume_preflight is not None and not callable(resume_preflight):
            raise ValueError("RESUME_PREFLIGHT_REQUIRED")
        self.resume_preflight = resume_preflight
        self.worker = None
        if (
            type(recovery_retry_seconds) is not int
            or not 10 <= recovery_retry_seconds <= 600
        ):
            raise ValueError("BOUNDED_RECOVERY_RETRY_REQUIRED")
        self.monotonic = monotonic
        self.recovery_retry_seconds = recovery_retry_seconds
        self.retry_at = 0
        self.retry_attempts = 0

    def actual(self):
        with self.connect() as c:
            row = c.execute(
                "SELECT epoch,target_registry::text,binding_version,phase,worker_token::text,blockers "
                "FROM v2_execution_routes WHERE tenant_id=%s AND environment='SANDBOX'",
                (self.tenant,),
            ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "epoch",
                        "target_registry",
                        "binding_version",
                        "phase",
                        "worker_token",
                        "blockers",
                    ),
                    row,
                    strict=True,
                )
            )
        )

    def desired(self):
        with self.connect() as c:
            row = c.execute(
                "SELECT result FROM v2_execution_route_events WHERE tenant_id=%s AND environment='SANDBOX' ORDER BY epoch DESC LIMIT 1",
                (self.tenant,),
            ).fetchone()
            if (
                row
                and c.execute(
                    "SELECT 1 FROM v2_execution_controller_events WHERE tenant_id=%s AND environment='SANDBOX' AND epoch=%s AND phase='REJECTED'",
                    (self.tenant, row[0]["epoch"]),
                ).fetchone()
            ):
                return None
        return row[0] if row else None

    def reject(self, target, blockers):
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_execution_controller_events(event_id,tenant_id,environment,epoch,target_registry,phase,blockers) VALUES (%s,%s,'SANDBOX',%s,%s,'REJECTED',%s)",
                (
                    str(uuid4()),
                    self.tenant,
                    target["epoch"],
                    target["target_registry"],
                    Jsonb(blockers),
                ),
            )

    def record(self, target, phase, *, token=None, blockers=()):
        if phase in {"ACTIVE", "STARTING", "DRAINING"} and not token:
            raise ValueError("WORKER_TOKEN_REQUIRED")
        with self.connect() as c:
            ExecutionRoutes._lock(c, self.tenant, "SANDBOX")
            # Rotation and controller activation share the same registry row lock.
            if phase in {"ACTIVE", "STARTING", "DRAINING"}:
                ExecutionRoutes._binding(
                    c,
                    self.tenant,
                    target["target_registry"],
                    "SANDBOX",
                    target["binding_version"],
                )
            old = c.execute(
                "SELECT epoch,target_registry::text,binding_version,phase,worker_token::text,blockers FROM v2_execution_routes WHERE tenant_id=%s AND environment='SANDBOX' FOR UPDATE",
                (self.tenant,),
            ).fetchone()
            values = (
                target["epoch"],
                target["target_registry"],
                target["binding_version"],
                phase,
                token,
                list(blockers),
            )
            if old == values:
                return
            c.execute(
                "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token,blockers) "
                "VALUES (%s,'SANDBOX',%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,environment) DO UPDATE SET "
                "epoch=EXCLUDED.epoch,target_registry=EXCLUDED.target_registry,binding_version=EXCLUDED.binding_version,"
                "phase=EXCLUDED.phase,worker_token=EXCLUDED.worker_token,blockers=EXCLUDED.blockers",
                (self.tenant, *values[:-1], Jsonb(values[-1])),
            )
            c.execute(
                "INSERT INTO v2_execution_controller_events(event_id,tenant_id,environment,epoch,target_registry,phase,blockers) "
                "VALUES (%s,%s,'SANDBOX',%s,%s,%s,%s)",
                (
                    str(uuid4()),
                    self.tenant,
                    target["epoch"],
                    target["target_registry"],
                    phase,
                    Jsonb(list(blockers)),
                ),
            )

    def start(self, target, *, recovering=False):
        # A revoked token is not proof that the old OS process has exited.
        # It may still hold protocol locks or be finishing recovery writes.
        # Never overwrite the only handle that can confirm its termination.
        if self.worker is not None:
            return False
        token = str(uuid4())
        try:
            self.record(
                target,
                "STARTING",
                token=token,
                blockers=["RECOVERING_SOURCE"] if recovering else [],
            )
            self.worker = self.spawn({**target, "worker_token": token})
            if not self.worker.ready():
                raise ValueError("NOT_READY")
            phase = "DRAINING" if recovering else "ACTIVE"
            self.record(
                target,
                phase,
                token=token,
                blockers=["RECOVERING_SOURCE"]
                if recovering
                else (
                    []
                    if getattr(self.worker, "entries_enabled", True)
                    else ["ENTRY_DISABLED_ACCEPTANCE"]
                ),
            )
            self.worker.release()
        except Exception:  # noqa: BLE001 - fixed diagnostics only
            blockers = ["WORKER_START_FAILED"]
            if (
                getattr(self.worker, "startup_failure", None)
                == "EXECUTION_GUARD_PERMISSION_DENIED"
            ):
                blockers.append("EXECUTION_GUARD_PERMISSION_DENIED")
            self.record(target, "BLOCKED", blockers=blockers)
            if self.worker is not None and not self.worker.stop():
                self.record(
                    target,
                    "BLOCKED",
                    blockers=[*blockers, "SOURCE_PROCESS_NOT_STOPPED"],
                )
                return False
            self.worker = None
            self.retry_attempts = min(self.retry_attempts + 1, 6)
            self.retry_at = self.monotonic() + min(
                600, self.recovery_retry_seconds * 2 ** (self.retry_attempts - 1)
            )
            return False
        self.retry_attempts = 0
        self.retry_at = 0
        return True

    def _resume_check(self, target):
        try:
            blockers = self.resume_preflight(target)
            if isinstance(blockers, list) and all(
                isinstance(item, str) and item for item in blockers
            ):
                return blockers
        except Exception:  # noqa: BLE001 - failed readiness must preserve management
            return ["RESUME_READINESS_UNAVAILABLE"]
        return ["RESUME_READINESS_UNAVAILABLE"]

    def step(self):
        actual, desired = self.actual(), self.desired()
        if (
            actual
            and actual["phase"] in {"BLOCKED", "STOPPED"}
            and self.worker is not None
        ):
            # A failed stop must remain authoritative even when a newer route
            # request arrives. First retire that process, then recover the
            # SAME source in manage-only mode before any account handover.
            self.record(actual, "BLOCKED", blockers=["SOURCE_PROCESS_NOT_STOPPED"])
            if not self.worker.stop():
                return
            self.worker = None
            self.start(actual, recovering=True)
            return
        if (
            actual
            and actual["phase"] == "BLOCKED"
            and actual["blockers"] == ["WORKER_START_FAILED"]
            and self.worker is None
            and desired is not None
            and all(
                actual[key] == desired[key]
                for key in ("target_registry", "binding_version")
            )
        ):
            # Transient dependency startup failures must not permanently strand
            # owned positions. Retry management only; reopening still requires
            # a newer explicit request and both coverage checks below.
            if self.monotonic() >= self.retry_at:
                self.start(actual, recovering=True)
            return
        if actual and actual["phase"] in {"ACTIVE", "DRAINING", "STARTING"}:
            if self.worker is None or not self.worker.alive():
                # Revoke the old token before recovery. Never restart into OPEN mode.
                self.record(actual, "STOPPED", blockers=["WORKER_RECOVERY_REQUIRED"])
                if self.worker is not None and not self.worker.stop():
                    self.record(
                        actual, "BLOCKED", blockers=["SOURCE_PROCESS_NOT_STOPPED"]
                    )
                    return
                self.worker = None
                self.start(actual, recovering=True)
                return
            if hasattr(self.worker, "healthy") and not self.worker.healthy():
                self.record(
                    actual,
                    "DRAINING",
                    token=actual["worker_token"],
                    blockers=["WORKER_PROGRESS_STALE"],
                )
                return
            if desired is None or desired["epoch"] <= actual["epoch"]:
                return
            if (
                actual["phase"] == "DRAINING"
                and self.resume_preflight is not None
                and all(
                    actual[key] == desired[key]
                    for key in ("target_registry", "binding_version")
                )
            ):
                # Explicit new request, identical account AND credential binding.
                # This is recovery of owned exposure, never a cross-account handover.
                blockers = self._resume_check(desired)
                if blockers:
                    self.record(
                        actual,
                        "DRAINING",
                        token=actual["worker_token"],
                        blockers=blockers,
                    )
                    return
                if not self.worker.stop():
                    self.record(
                        actual,
                        "DRAINING",
                        token=actual["worker_token"],
                        blockers=["SOURCE_PROCESS_NOT_STOPPED"],
                    )
                    return
                self.worker = None
                self.record(actual, "STOPPED")
                # Reconcile again after the old writer is confirmed gone.
                if self._resume_check(desired):
                    self.start(actual, recovering=True)
                    return
                if not self.start(desired) and self.worker is None:
                    # Failed activation must still attempt manage-only recovery;
                    # it never silently grants another OPEN attempt.
                    self.start(desired, recovering=True)
                return
            if actual["phase"] == "ACTIVE":
                blockers = self.preflight(desired)
                if blockers:
                    self.reject(desired, blockers)
                    return  # Preserve the healthy source worker and its exact authority.
            self.record(actual, "DRAINING", token=actual["worker_token"])
            blockers = self.source_clear(actual)
            if blockers:
                self.record(
                    actual, "DRAINING", token=actual["worker_token"], blockers=blockers
                )
                return
            if not self.worker.stop():
                self.record(
                    actual,
                    "DRAINING",
                    token=actual["worker_token"],
                    blockers=["SOURCE_PROCESS_NOT_STOPPED"],
                )
                return
            self.worker = None
            self.record(actual, "STOPPED")
        if desired is None or (
            actual
            and actual["phase"] == "BLOCKED"
            and desired["epoch"] <= actual["epoch"]
        ):
            return
        if actual and desired["epoch"] <= actual["epoch"]:
            return
        blockers = self.preflight(desired)
        if blockers:
            self.record(desired, "BLOCKED", blockers=blockers)
            return
        self.start(desired)
