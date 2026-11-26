"""Explicit controller composition for the typed strategy runtime.

Management and financial settlement remain independent critical workers. Entry
is an auxiliary lane: its construction, readiness and bounded retries occur on
its own thread, never on the controller or a management/settlement worker. An
entry failure cannot stop those critical workers. Every returned handle remains
owned, and an unconfirmed entry stop prevents account handover/replacement.

This module neither activates a route nor dispatches an order. Entries default
to paused. Explicit enablement accepts only the fixed typed job composition;
management and settlement still receive entries-disabled configurations.
Factories must fail before creating a hidden worker, or return its owned handle
even on startup failure. Arbitrary callbacks cannot be given a hard deadline by
this thread adapter. Stuck construction/stop is retained, never called stopped.
"""

import json
import threading
import time
from dataclasses import dataclass

from services.v2_entry_worker import EntryPorts, EntryWorker
from services.v2_worker_group import WorkerGroup
from v2_core.account_registry import uid

MODE = "TYPED_STRATEGY"
_FAILED_BATCHES = frozenset(
    {"BLOCKED", "BUSY", "FAILED", "TIMEOUT", "STOP_UNCONFIRMED"}
)


def _integer(values, key, default, low, high):
    value = values.get(key, str(default))
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdecimal()
        or not low <= int(value) <= high
    ):
        raise ValueError("TYPED_RUNTIME_CONFIG_INVALID")
    return int(value)


@dataclass(frozen=True)
class _EntryPolicy:
    retries: int
    retry_seconds: float
    poll_seconds: float
    join_seconds: float

    @classmethod
    def read(cls, values):
        return cls(
            _integer(values, "V2_TYPED_ENTRY_MAX_RESTARTS", 3, 0, 5),
            _integer(values, "V2_TYPED_ENTRY_RETRY_MS", 1000, 10, 60000) / 1000,
            _integer(values, "V2_TYPED_ENTRY_POLL_MS", 50, 10, 250) / 1000,
            _integer(values, "V2_TYPED_ENTRY_STOP_JOIN_MS", 100, 10, 1000) / 1000,
        )


def _target(target):
    if type(target) is not dict or target.get("environment", "SANDBOX") != "SANDBOX":
        raise ValueError("TYPED_RUNTIME_TARGET_INVALID")
    try:
        selected = {
            "target_registry": uid(target["target_registry"]),
            "binding_version": target["binding_version"],
            "epoch": target["epoch"],
            "worker_token": uid(target["worker_token"]),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("TYPED_RUNTIME_TARGET_INVALID") from None
    if any(
        type(selected[key]) is not int or selected[key] < 1
        for key in ("binding_version", "epoch")
    ):
        raise ValueError("TYPED_RUNTIME_TARGET_INVALID")
    return selected


def _entry_valid(worker):
    return all(
        callable(getattr(worker, name, None))
        for name in ("ready", "release", "alive", "stop")
    )


class _EntryFailure(Exception):
    """Internal fixed-code control flow; callback text never leaves this module."""


class _IsolatedEntry:
    """Non-critical WorkerGroup member, not a claim that an entry worker is alive.

    Group ready/alive/healthy describe only eligibility to keep the critical
    lanes running. ``snapshot`` reports the actual auxiliary state explicitly.
    The controller thread never invokes entry callbacks, including stop: a
    blocked callback cannot prevent its peers receiving their own stop hints.
    """

    entries_enabled = False
    startup_failure = None

    def __init__(self, tenant, target, values, *, ports_for, entry_factory, policy):
        self.tenant, self.target, self.values = tenant, dict(target), dict(values)
        self.ports_for, self.entry_factory, self.policy = (
            ports_for,
            entry_factory,
            policy,
        )
        self._stop, self._release = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._workers, self._confirmed = [], []
        self._status, self._blocker = "WAITING_RELEASE", None
        self._attempts = 0
        self._thread = threading.Thread(
            target=self._run, name="v2-typed-entry-lane", daemon=False
        )
        self._thread.start()

    @property
    def owned_workers(self):
        with self._lock:
            return tuple(self._workers)

    def snapshot(self):
        with self._lock:
            return {
                "status": self._status,
                "blocker": self._blocker,
                "attempts": self._attempts,
                "restart_limit": self.policy.retries,
                "owner_alive": self._thread.is_alive(),
                "owned_workers": len(self._workers),
                "stop_confirmations": list(self._confirmed),
                "stop_confirmed": not self._thread.is_alive()
                and all(value is True for value in self._confirmed),
                "execution_authorized": False,
                "entry_dispatch_enabled": self.values["V2_CONTROLLER_ENABLE_ENTRIES"]
                == "true",
                "health_scope": "AUXILIARY_ENTRY_NOT_CRITICAL_MANAGEMENT",
            }

    def _update(self, status, blocker=None):
        with self._lock:
            changed = (self._status, self._blocker) != (status, blocker)
            self._status, self._blocker = status, blocker
            attempts = self._attempts
        if changed:
            try:
                print(
                    json.dumps(
                        {
                            "component": "typed-entry-lane",
                            "status": status,
                            "blocker": blocker,
                            "attempts": attempts,
                            "execution_authorized": False,
                        }
                    ),
                    flush=True,
                )
            except Exception:  # noqa: BLE001,S110 - diagnostics cannot discard worker ownership
                pass

    def ready(self):
        return not self._stop.is_set()

    def release(self):
        if self._stop.is_set():
            raise ValueError("TYPED_ENTRY_LANE_CLOSING")
        self._release.set()

    def alive(self):
        return not self._stop.is_set()

    def healthy(self):
        return not self._stop.is_set()

    def request_stop(self):
        self._stop.set()

    def stop(self):
        self.request_stop()
        self._thread.join(self.policy.join_seconds)
        with self._lock:
            return not self._thread.is_alive() and all(
                value is True for value in self._confirmed
            )

    def _dispose_all(self):
        while True:
            with self._lock:
                remaining = [
                    index
                    for index, value in enumerate(self._confirmed)
                    if value is not True
                ]
            if not remaining:
                return
            for index in remaining:
                worker = self._workers[index]
                try:
                    request = getattr(worker, "request_stop", None)
                    if callable(request):
                        request()
                except Exception:  # noqa: BLE001,S110 - still try the owned stop port
                    pass
                try:
                    stopped = worker.stop()
                    confirmed = stopped if type(stopped) is bool else None
                except Exception:  # noqa: BLE001 - no stop proof means retained ownership
                    confirmed = None
                with self._lock:
                    self._confirmed[index] = confirmed
            with self._lock:
                confirmed = all(value is True for value in self._confirmed)
            if confirmed:
                return
            self._update("STOP_UNCONFIRMED", "ENTRY_SOURCE_PROCESS_NOT_STOPPED")
            # A set stop flag must not cause a shutdown busy loop.
            time.sleep(self.policy.poll_seconds)

    def _ports(self):
        ports = self.ports_for(self.tenant, dict(self.target), dict(self.values))
        if type(ports) is not EntryPorts:
            raise _EntryFailure("ENTRY_PORTS_INVALID")
        binding = ports.binding
        if (
            binding.tenant_id != self.tenant
            or binding.registry_id != self.target["target_registry"]
            or binding.binding_version != self.target["binding_version"]
            or binding.epoch != self.target["epoch"]
            or binding.worker_token != self.target["worker_token"]
        ):
            raise _EntryFailure("ENTRY_BINDING_MISMATCH")
        if self.values["V2_CONTROLLER_ENABLE_ENTRIES"] == "true":
            from services.v2_strategy_runtime_job import (
                StrategyJobConfig,
                typed_runtime_job,
            )

            if (
                ports.job_target is not typed_runtime_job
                or len(ports.job_args) != 1
                or type(ports.job_args[0]) is not StrategyJobConfig
                or ports.job_args[0].entries_enabled is not True
                or ports.job_args[0].entry.binding != binding
                or ports.sources != ("quant", "tv_bridge", "s3")
            ):
                raise _EntryFailure("ENTRY_FIXED_TYPED_JOB_REQUIRED")
        return ports

    def _attempt(self):
        with self._lock:
            self._attempts += 1
        self._update("CONSTRUCTING")
        ports = self._ports()
        if self._stop.is_set():
            return
        worker = self.entry_factory(ports, dict(self.values))
        # Retain before checking a returned port. Invalid ports are still owned.
        with self._lock:
            for index, prior in enumerate(self._workers):
                if worker is prior:
                    # A factory returning an old object may have restarted its
                    # internals. Its prior termination proof no longer applies.
                    self._confirmed[index] = None
                    raise _EntryFailure("ENTRY_HANDLE_REUSED")
            self._workers.append(worker)
            self._confirmed.append(None)
        if (
            not _entry_valid(worker)
            or getattr(worker, "entries_enabled", None) is not False
        ):
            raise _EntryFailure("ENTRY_OWNER_INVALID")
        if self._stop.is_set():
            return
        if worker.ready() is not True:
            raise _EntryFailure("ENTRY_OWNER_NOT_READY")
        if self._stop.is_set():
            return
        worker.release()
        while not self._stop.is_set():
            if worker.alive() is not True:
                raise _EntryFailure("ENTRY_OWNER_LOST")
            healthy = getattr(worker, "healthy", None)
            if callable(healthy) and healthy() is not True:
                raise _EntryFailure("ENTRY_OWNER_UNHEALTHY")
            report = getattr(worker, "snapshot", None)
            status = report().get("status") if callable(report) else None
            if status in _FAILED_BATCHES:
                self._update("DEGRADED", "ENTRY_BATCH_UNAVAILABLE")
            else:
                self._update("RUNNING")
            self._stop.wait(self.policy.poll_seconds)

    def _run(self):
        try:
            while not self._release.wait(self.policy.poll_seconds):
                if self._stop.is_set():
                    return
            while not self._stop.is_set():
                try:
                    self._attempt()
                    blocker = None
                except _EntryFailure as exc:
                    blocker = str(exc)
                except Exception:  # noqa: BLE001 - fixed diagnostic, never callback details
                    blocker = "ENTRY_OWNER_START_FAILED"
                self._dispose_all()
                if self._stop.is_set():
                    return
                if self._attempts > self.policy.retries:
                    self._update("EXHAUSTED", blocker or "ENTRY_RETRY_EXHAUSTED")
                    return
                self._update("RETRY_WAIT", blocker or "ENTRY_OWNER_START_FAILED")
                self._stop.wait(self.policy.retry_seconds)
        finally:
            self._dispose_all()
            if self._stop.is_set():
                self._update("STOPPED")


class TypedStrategyRuntime:
    """ExecutionController port retaining its complete WorkerGroup ownership."""

    def __init__(self, group, *, binding=None):
        self.group = group
        self.entries_enabled = group.entries_enabled
        self.binding = None if binding is None else dict(binding)

    @property
    def members(self):
        return self.group.members

    @property
    def startup_failure(self):
        return self.group.startup_failure

    @property
    def stop_confirmations(self):
        return self.group.stop_confirmations

    def ready(self):
        return self.group.ready()

    def release(self):
        return self.group.release()

    def alive(self):
        return self.group.alive()

    def healthy(self):
        return self.group.healthy()

    def request_stop(self):
        return self.group.request_stop()

    def stop(self):
        return self.group.stop()

    def snapshot(self):
        auxiliary = next(
            (member for member in self.members if isinstance(member, _IsolatedEntry)),
            None,
        )
        settlement = None
        if len(self.members) > 1:
            try:
                report = getattr(self.members[1], "snapshot", None)
                settlement = report() if callable(report) else None
            except Exception:  # noqa: BLE001 - local diagnostic only
                settlement = {"status": "UNAVAILABLE"}
        return {
            "mode": MODE,
            "entries_enabled": self.entries_enabled,
            "execution_authorized": False,
            "entry": None if auxiliary is None else auxiliary.snapshot(),
            "binding": None if self.binding is None else dict(self.binding),
            "settlement": settlement,
            "startup_failure": self.startup_failure,
            "stop_confirmations": list(self.stop_confirmations),
        }


class StrategyRuntimeFactory:
    def __init__(
        self,
        tenant,
        values,
        *,
        entry_ports_for,
        management_factory=None,
        settlement_factory=None,
        entry_factory=EntryWorker,
    ):
        self.tenant = uid(tenant)
        if (
            type(values) is not dict
            or values.get("V2_CONTROLLER_WORKER_MODE") != MODE
            or values.get("V2_ENVIRONMENT", "SANDBOX") != "SANDBOX"
        ):
            raise ValueError("EXPLICIT_TYPED_RUNTIME_REQUIRED")
        enabled = values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false")
        if enabled not in {"true", "false"}:
            raise ValueError("TYPED_RUNTIME_ENTRY_FLAG_INVALID")
        if enabled == "true":
            from services.v2_strategy_runtime_job import typed_entry_ports_for

            if (
                entry_ports_for is not typed_entry_ports_for
                or entry_factory is not EntryWorker
            ):
                raise ValueError("TYPED_RUNTIME_FIXED_FACTORY_REQUIRED")
        if (
            not callable(entry_ports_for)
            or not callable(entry_factory)
            or any(
                factory is not None and not callable(factory)
                for factory in (management_factory, settlement_factory)
            )
        ):
            raise TypeError("TYPED_RUNTIME_FACTORIES_REQUIRED")
        self.values = {
            **values,
            "V2_ENVIRONMENT": "SANDBOX",
            "V2_CONTROLLER_ENABLE_ENTRIES": enabled,
            "V2_ENTRY_WORK_MS": str(
                _integer(values, "V2_ENTRY_WORK_MS", 90000, 60000, 300000)
            ),
            "V2_ENTRY_PROGRESS_TIMEOUT_MS": str(
                _integer(values, "V2_ENTRY_PROGRESS_TIMEOUT_MS", 180000, 100, 3600000)
            ),
        }
        self.policy = _EntryPolicy.read(values)
        self.entry_ports_for, self.entry_factory = entry_ports_for, entry_factory
        self.management_factory, self.settlement_factory = (
            management_factory,
            settlement_factory,
        )

    def spawn(self, target):
        from services.v2_managed_testnet import ChildWorker
        from services.v2_settlement_worker import SettlementWorker

        selected = _target(target)
        management = self.management_factory or ChildWorker
        settlement = self.settlement_factory or SettlementWorker
        group = WorkerGroup.create(
            (
                lambda: management(
                    self.tenant,
                    dict(selected),
                    {
                        **self.values,
                        "V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY",
                        "V2_CONTROLLER_ENABLE_ENTRIES": "false",
                    },
                ),
                lambda: settlement(
                    self.tenant,
                    dict(selected),
                    {**self.values, "V2_CONTROLLER_ENABLE_ENTRIES": "false"},
                ),
                lambda: _IsolatedEntry(
                    self.tenant,
                    selected,
                    self.values,
                    ports_for=self.entry_ports_for,
                    entry_factory=self.entry_factory,
                    policy=self.policy,
                ),
            ),
            entries_enabled=self.values["V2_CONTROLLER_ENABLE_ENTRIES"] == "true",
        )
        return TypedStrategyRuntime(
            group,
            binding={
                "tenant_id": self.tenant,
                "environment": "SANDBOX",
                "registry_id": selected["target_registry"],
                "binding_version": selected["binding_version"],
                "epoch": selected["epoch"],
                "worker_token": selected["worker_token"],
            },
        )
