"""Direct settlement jobs owned by a local thread, independent of controller I/O.

The owner does DB selection only with no active job. While a job exists it only
polls local process state. A blocked selector or unconfirmed child stop retains
this worker's ownership and prevents account handover. No entry capability.
"""

import threading
import time
from dataclasses import dataclass

from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from v2_core.account_registry import uid
from v2_core.bounded_job import BoundedJobPolicy, BoundedProcessJob
from v2_core.session_lock import session_advisory_lock
from v2_core.strategy_lifecycle import LIFECYCLE_EPISODE_SQL

KINDS = ("FINANCIAL", "OUTCOME")


def _integer(values, key, default, low, high):
    value = values.get(key, str(default))
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not low <= int(value) <= high
    ):
        raise ValueError("EXPLICIT_SETTLEMENT_WORKER_CONFIG_REQUIRED")
    return int(value)


@dataclass(frozen=True)
class SettlementPorts:
    binding: ManagementBinding
    select: object
    job_args: tuple

    def __post_init__(self):
        if (
            type(self.binding) is not ManagementBinding
            or not callable(self.select)
            or type(self.job_args) is not tuple
        ):
            raise ValueError("EXPLICIT_SETTLEMENT_PORTS_REQUIRED")


class _NoVenue:
    def __init__(self, scope):
        self.account_id, self.environment = scope.account_id, scope.environment

    def __call__(self, *_args, **_kwargs):
        raise RuntimeError("SETTLEMENT_SELECTOR_MUST_NOT_CALL_VENUE")


def _bootstrap(tenant, target, _values):
    from services.v2_managed_testnet import database, scope_for

    scope = scope_for(tenant, target)
    binding = ManagementBinding(
        scope,
        tenant,
        target["target_registry"],
        target["binding_version"],
        target["epoch"],
        target["worker_token"],
    )
    connect = database()
    guard = create_execution_guard(connect, **binding.guard_args)
    guard.preflight(scope=scope)
    stage = DirectionalSettlementStage(
        connect,
        _NoVenue(scope),
        scope=scope,
        clock_ms=lambda: time.time_ns() // 1000000,
    )

    def select(kind, after):
        with guard(operation="MANAGE"):
            pass  # Never pin the route lock across the selection query.
        rows = (
            stage._pending_episodes(after=after, limit=1)
            if kind == "FINANCIAL"
            else stage._pending_outcomes(after=after, limit=1)
        )
        return rows[0][0] if rows else None

    return SettlementPorts(binding, select, (binding,))


def run_settlement_work(
    connect, request, *, binding, guard, kind, episode, stop, clock_ms
):
    """One scoped obligation, retaining stage exclusion and final commit fence."""
    if (
        type(binding) is not ManagementBinding
        or getattr(guard, "_args", None) != binding.guard_args
        or not callable(getattr(guard, "require_transaction", None))
        or kind not in KINDS
        or uid(episode) != episode
        or getattr(request, "account_id", None) != binding.scope.account_id
        or getattr(request, "environment", None) != "SANDBOX"
    ):
        raise ValueError("EXPLICIT_SETTLEMENT_WORK_REQUIRED")
    if stop.is_set():
        return "BLOCKED"
    with session_advisory_lock(
        connect, "directional-settlement-stage:" + binding.scope.key
    ) as acquired:
        if not acquired:
            return "BUSY"
        with guard(operation="MANAGE"):
            pass
        with connect() as conn:
            row = conn.execute(
                f"SELECT e.status,EXISTS (SELECT 1 FROM v2_settlements s "
                f"WHERE s.episode_id=e.episode_id AND s.revision=e.accounting_revision "
                f"AND s.currency='USDT') FROM v2_trade_intents i JOIN v2_episodes e "
                f"ON e.episode_id=i.intent_id WHERE i.intent_id=%s AND "
                f"(i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) "
                f"AND {LIFECYCLE_EPISODE_SQL}",
                (
                    episode,
                    binding.scope.exchange,
                    binding.scope.account_id,
                    binding.scope.environment,
                    binding.scope.product,
                ),
            ).fetchone()
        if row is None or stop.is_set():
            return "BLOCKED"
        if kind == "OUTCOME" and row[0] != "SETTLED":
            return "BLOCKED"
        if row[0] == "SETTLED":
            if row[1] is not True:
                raise ValueError("SETTLEMENT_JOURNAL_REQUIRED")
            if kind == "FINANCIAL":
                # The previous job may have committed before losing its IPC.
                # A scoped committed journal, not that IPC, is the replay fact.
                return "COMPLETED"

        class ReadOnlyRequest:
            account_id, environment = request.account_id, request.environment

            def __call__(self, method, path, params):
                if method != "GET" or stop.is_set():
                    raise ValueError("SETTLEMENT_READ_ONLY_OR_STOPPED")
                with guard(operation="MANAGE"):
                    pass
                return request(method, path, params)

        def commit_guard(connection, **kwargs):
            if stop.is_set():
                raise ValueError("SETTLEMENT_STOPPED_BEFORE_COMMIT")
            guard.require_transaction(connection, **kwargs)

        stage = DirectionalSettlementStage(
            connect,
            ReadOnlyRequest(),
            scope=binding.scope,
            clock_ms=clock_ms,
            limit=1,
            commit_guard=commit_guard,
        )
        if kind == "OUTCOME":
            # This is a derived research journal, not financial authority.
            stage._record_outcome(episode)
            return "COMPLETED"
        result = stage.settle(episode, record_outcome=False)
        return "COMPLETED" if result.get("status") == "SETTLED" else "BLOCKED"


def _settlement_job(stop, binding, kind, episode):
    from services.v2_managed_testnet import database, readonly_request

    connect = database()
    guard = create_execution_guard(connect, **binding.guard_args)
    with guard(operation="MANAGE"):
        pass
    target = {
        "target_registry": binding.registry_id,
        "binding_version": binding.binding_version,
    }
    request = (
        _NoVenue(binding.scope)
        if kind == "OUTCOME"
        else readonly_request(binding.tenant_id, target, binding.scope)
    )
    return run_settlement_work(
        connect,
        request,
        binding=binding,
        guard=guard,
        kind=kind,
        episode=episode,
        stop=stop,
        clock_ms=lambda: time.time_ns() // 1000000,
    )


class SettlementWorker:
    """Controller port; only its owner thread may touch a process-job handle.

    Non-daemon owner is retained until the selected query and owned child end.
    Startup/selection can block without delaying an already active job, because
    they never run concurrently with one. stop=False is not permission to drop
    this handle. OS scheduling and synchronous spawn are not hard-real-time.
    """

    entries_enabled = False

    def __init__(
        self,
        tenant,
        target,
        values,
        *,
        bootstrap=_bootstrap,
        job_factory=BoundedProcessJob,
        clock=time.monotonic,
    ):
        self.tenant = uid(tenant)
        self.target = {
            key: target[key]
            for key in ("target_registry", "binding_version", "epoch", "worker_token")
        }
        if (
            values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") != "false"
            or values.get("V2_ENVIRONMENT", "SANDBOX") != "SANDBOX"
            or not all(callable(x) for x in (bootstrap, job_factory, clock))
        ):
            raise ValueError("SETTLEMENT_MANAGEMENT_ONLY_REQUIRED")
        self.values = dict(values)
        self.policy = BoundedJobPolicy(
            *(
                _integer(values, "V2_SETTLEMENT_" + key + "_MS", default, low, high)
                / 1000
                for key, default, low, high in (
                    ("WORK", 30000, 100, 300000),
                    ("STOP", 250, 10, 10000),
                    ("TERMINATE", 250, 10, 10000),
                    ("KILL", 1000, 10, 10000),
                )
            )
        )
        self.poll_seconds = (
            _integer(values, "V2_SETTLEMENT_POLL_MS", 50, 10, 250) / 1000
        )
        self.idle_seconds = (
            _integer(values, "V2_SETTLEMENT_IDLE_MS", 1000, 10, 10000) / 1000
        )
        self.ready_seconds = (
            _integer(values, "V2_SETTLEMENT_READY_MS", 5000, 10, 30000) / 1000
        )
        self.join_seconds = (
            _integer(values, "V2_SETTLEMENT_STOP_JOIN_MS", 200, 10, 1000) / 1000
        )
        self.progress_timeout = _integer(
            values, "V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS", 300, 30, 3600
        )
        self.bootstrap, self.job_factory, self.clock = bootstrap, job_factory, clock
        self.startup_failure = None
        self._stop, self._release, self._ready = (threading.Event() for _ in range(3))
        self._lock = threading.Lock()
        self._status, self._kind, self._job_id = "STARTING", None, None
        self._last_success = dict.fromkeys(KINDS, clock())
        self._job = None
        self._bootstrapped = False
        self._thread = threading.Thread(
            target=self._run, name="v2-settlement-owner", daemon=False
        )
        self._thread.start()

    def _update(self, status, *, kind=None, result=None, success=False):
        with self._lock:
            self._status, self._kind = status, kind
            if result is not None:
                self._job_id = result.get("job_id")
            if success:
                self._last_success[kind] = self.clock()

    def snapshot(self):
        with self._lock:
            return {
                "status": self._status,
                "kind": self._kind,
                "job_id": self._job_id,
                "stop_confirmed": not self._thread.is_alive() and self._job is None,
                "execution_authorized": False,
                "progress_fresh": {
                    kind: self.clock() - last <= self.progress_timeout
                    for kind, last in self._last_success.items()
                },
            }

    def ready(self):
        self._ready.wait(self.ready_seconds)
        return self._bootstrapped and self.alive() and not self._stop.is_set()

    def release(self):
        if not self._bootstrapped or not self.alive() or self._stop.is_set():
            raise ValueError("SETTLEMENT_WORKER_NOT_READY")
        self._release.set()

    def alive(self):
        return self._thread.is_alive()

    def healthy(self):
        with self._lock:
            # Controller health can drain/block account handover. Derived
            # research failures belong in diagnostics, never that authority gate.
            # A fresh empty OUTCOME lane cannot hide failed financial work.
            return (
                self.alive()
                and self.clock() - self._last_success["FINANCIAL"]
                <= self.progress_timeout
            )

    def request_stop(self):
        self._stop.set()

    def stop(self):
        self.request_stop()
        self._thread.join(self.join_seconds)
        return not self._thread.is_alive() and self._job is None

    def _finish_job(self, *, stopping=False):
        while True:
            if stopping or self._stop.is_set():
                result = self._job.stop()
            else:
                result = self._job.poll()
            self._update(result["status"], kind=self._kind, result=result)
            if result["stop_confirmed"] is True:
                self._job = None
                return result
            # A set stop Event must not turn shutdown polling into a busy loop.
            time.sleep(self.poll_seconds)

    def _run(self):
        try:
            ports = self.bootstrap(self.tenant, dict(self.target), dict(self.values))
            if type(ports) is not SettlementPorts or (
                ports.binding.tenant_id != self.tenant
                or ports.binding.registry_id != self.target["target_registry"]
                or ports.binding.binding_version != self.target["binding_version"]
                or ports.binding.epoch != self.target["epoch"]
                or ports.binding.worker_token != self.target["worker_token"]
            ):
                raise ValueError("SETTLEMENT_BINDING_CHANGED")
            self._bootstrapped = True
            self._update("READY")
            self._ready.set()
            while not self._release.wait(self.poll_seconds):
                if self._stop.is_set():
                    return
            cursors, next_kind = {kind: "" for kind in KINDS}, 0
            while not self._stop.is_set():
                kind = KINDS[next_kind]
                next_kind = (next_kind + 1) % len(KINDS)
                try:
                    episode = ports.select(kind, cursors[kind])
                    if self._stop.is_set():
                        break
                    if episode is None:
                        self._update("IDLE", kind=kind, success=True)
                        self._stop.wait(self.idle_seconds)
                        continue
                    if uid(episode) != episode:
                        raise ValueError("SETTLEMENT_EPISODE_REQUIRED")
                    self._kind = kind
                    self._job = self.job_factory(_settlement_job, policy=self.policy)
                    self._job.start(*ports.job_args, kind, episode)
                    result = self._finish_job()
                    # Attempt cursor only; never proof of financial completion.
                    cursors[kind] = episode
                    if result["status"] == "COMPLETED":
                        self._update("COMPLETED", kind=kind, success=True)
                except Exception:  # Never expose DB/credential text.
                    self._update("FAILED", kind=kind)
                    if self._job is not None:
                        # No next task until the owned child is confirmed gone.
                        raise
                self._stop.wait(self.idle_seconds)
        except Exception as exc:  # noqa: BLE001 - fixed startup diagnostics only
            if not self._bootstrapped:
                self.startup_failure = (
                    "EXECUTION_GUARD_PERMISSION_DENIED"
                    if getattr(exc, "sqlstate", None) == "42501"
                    else "WORKER_START_FAILED"
                )
            self._update("FAILED")
        finally:
            self._ready.set()
            while self._job is not None:
                try:
                    self._finish_job(stopping=True)
                except Exception:  # noqa: BLE001 - retain unknown handle, never replace
                    self._update("STOP_UNCONFIRMED")
                    time.sleep(self.poll_seconds)
            self._update("STOPPED")
