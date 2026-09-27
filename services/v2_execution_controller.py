"""Trusted single-environment controller. Ports must never report speculative readiness.

The caller holds a process-lifetime database advisory lock. Web has no permission
to write these actual route states. LIVE is intentionally not a supported worker.
"""

from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.execution_routes import ExecutionRoutes


class ExecutionController:
    def __init__(self, connect, *, tenant_id, preflight, source_clear, spawn):
        self.connect, self.tenant = connect, uid(tenant_id)
        self.preflight, self.source_clear, self.spawn = preflight, source_clear, spawn
        self.worker = None

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
            self.record(target, "BLOCKED", blockers=["WORKER_START_FAILED"])
            if self.worker is not None:
                self.worker.stop()
            self.worker = None
            return False
        return True

    def step(self):
        actual, desired = self.actual(), self.desired()
        if actual and actual["phase"] in {"ACTIVE", "DRAINING", "STARTING"}:
            if self.worker is None or not self.worker.alive():
                # Revoke the old token before recovery. Never restart into OPEN mode.
                self.record(actual, "STOPPED", blockers=["WORKER_RECOVERY_REQUIRED"])
                if self.worker is not None and not self.worker.stop():
                    self.record(
                        actual, "BLOCKED", blockers=["SOURCE_PROCESS_NOT_STOPPED"]
                    )
                    return
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
