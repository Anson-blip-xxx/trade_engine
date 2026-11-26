"""Local supervision of bounded typed-entry batches, without runtime activation.

A trusted builder supplies a detached binding, a fixed spawn-safe job target and
its arguments. The child owns one complete finite scheduler batch. This owner
has no database selector, credential decryption or venue transport: while a job
exists it only polls local process state. Targets must not spawn descendants or
receive live database connections. Killing/reaping a job proves process death,
not rollback; every later batch must recover durable original work first.
"""

import threading
import time
from dataclasses import asdict, dataclass

from services.v2_management_runtime import ManagementBinding
from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.bounded_job import BoundedJobPolicy, BoundedProcessJob

SOURCES = frozenset({"s2", "s3", "tv_bridge", "quant"})
_JOB_STATUSES = frozenset(
    {
        "IDLE",
        "RUNNING",
        "STOPPING",
        "STOP_UNCONFIRMED",
        "COMPLETED",
        "BLOCKED",
        "BUSY",
        "FAILED",
        "TIMEOUT",
        "STOPPED",
    }
)


def _integer(values, key, default, low, high):
    value = values.get(key, str(default))
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not low <= int(value) <= high
    ):
        raise ValueError("EXPLICIT_ENTRY_WORKER_CONFIG_REQUIRED")
    return int(value)


@dataclass(frozen=True)
class EntryPorts:
    """Explicit metadata and trusted code, never a dynamic module/import name.

    ``job_target(stop, *job_args, source)`` returns BoundedProcessJob's fixed
    status protocol. Args must be spawn-safe data, not connected resources or
    hidden child owners. The builder, not this thread, owns scope/source policy.
    """

    binding: ManagementBinding
    job_target: object
    job_args: tuple
    sources: tuple

    def __post_init__(self):
        if (
            type(self.binding) is not ManagementBinding
            or not callable(self.job_target)
            or type(self.job_args) is not tuple
            or len(self.job_args) > 16
            or type(self.sources) is not tuple
            or not 1 <= len(self.sources) <= len(SOURCES)
            or any(
                type(source) is not str or source not in SOURCES
                for source in self.sources
            )
            or len(set(self.sources)) != len(self.sources)
        ):
            raise ValueError("EXPLICIT_ENTRY_PORTS_REQUIRED")
        binding = self.binding
        object.__setattr__(
            self,
            "binding",
            ManagementBinding(
                AccountScope(**asdict(binding.scope)),
                binding.tenant_id,
                binding.registry_id,
                binding.binding_version,
                binding.epoch,
                binding.worker_token,
            ),
        )


class EntryWorker:
    """One non-daemon owner; its handle survives every unconfirmed child stop.

    No target starts until release. Status, ready/alive/healthy and stop callers
    never poll the child themselves. Entry failures do not invalidate management
    or exit progress; ``healthy`` describes ONLY owner-thread liveness, while
    ``snapshot`` exposes batch failures separately. Synchronous OS spawn and OS
    scheduling are not hard-real-time. A job factory must not hide a child then
    raise before returning its handle.
    """

    entries_enabled = False

    def __init__(
        self, ports, values, *, job_factory=BoundedProcessJob, clock=time.monotonic
    ):
        if (
            type(ports) is not EntryPorts
            or type(values) is not dict
            or values.get("V2_ENVIRONMENT", "SANDBOX") != "SANDBOX"
            or values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false")
            not in {"true", "false"}
            or not callable(job_factory)
            or not callable(clock)
        ):
            raise ValueError("EXPLICIT_SANDBOX_ENTRY_WORKER_REQUIRED")
        self.ports = EntryPorts(
            ports.binding, ports.job_target, ports.job_args, ports.sources
        )
        # Describes the explicitly configured downstream target, not permission
        # to trade. The fixed runtime factory independently verifies that target.
        self.entry_dispatch_enabled = (
            values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") == "true"
        )
        self.policy = BoundedJobPolicy(
            *(
                _integer(values, "V2_ENTRY_" + key + "_MS", default, low, high) / 1000
                for key, default, low, high in (
                    ("WORK", 10000, 100, 300000),
                    ("STOP", 250, 10, 10000),
                    ("TERMINATE", 250, 10, 10000),
                    ("KILL", 1000, 10, 10000),
                )
            )
        )
        self.poll_seconds = _integer(values, "V2_ENTRY_POLL_MS", 50, 10, 250) / 1000
        self.idle_seconds = _integer(values, "V2_ENTRY_IDLE_MS", 1000, 10, 10000) / 1000
        self.ready_seconds = (
            _integer(values, "V2_ENTRY_READY_MS", 1000, 10, 30000) / 1000
        )
        self.join_seconds = (
            _integer(values, "V2_ENTRY_STOP_JOIN_MS", 200, 10, 1000) / 1000
        )
        self.progress_seconds = (
            _integer(values, "V2_ENTRY_PROGRESS_TIMEOUT_MS", 120000, 100, 3600000)
            / 1000
        )
        self.job_factory, self.clock = job_factory, clock
        self.startup_failure = None
        self._stop, self._release, self._ready = (threading.Event() for _ in range(3))
        self._lock = threading.Lock()
        self._status, self._source, self._job_id = "STARTING", None, None
        self._job = None
        self._lanes = {
            source: {
                "attempts": 0,
                "last_status": "NOT_STARTED",
                "last_failure": None,
                "last_completed": None,
            }
            for source in ports.sources
        }
        self._thread = threading.Thread(
            target=self._run, name="v2-entry-owner", daemon=False
        )
        self._thread.start()

    def snapshot(self):
        with self._lock:
            now = self.clock()
            return {
                "status": self._status,
                "source": self._source,
                "job_id": self._job_id,
                "owner_alive": self._thread.is_alive(),
                "released": self._release.is_set(),
                "stop_requested": self._stop.is_set(),
                "stop_confirmed": not self._thread.is_alive() and self._job is None,
                "execution_authorized": False,
                "entry_dispatch_enabled": self.entry_dispatch_enabled,
                "health_scope": "PROCESS_OWNERSHIP_ONLY",
                "trading_health": "NOT_EVALUATED",
                "outcome_requires_reconciliation": self._job_id is not None,
                "lanes": {
                    source: {
                        "attempts": lane["attempts"],
                        "last_status": lane["last_status"],
                        "last_failure": lane["last_failure"],
                        # Completed batch != admitted order or successful trade.
                        "completed_batch_fresh": lane["last_completed"] is not None
                        and 0 <= now - lane["last_completed"] <= self.progress_seconds,
                    }
                    for source, lane in self._lanes.items()
                },
            }

    def ready(self):
        self._ready.wait(self.ready_seconds)
        return self._ready.is_set() and self.alive() and not self._stop.is_set()

    def release(self):
        if not self._ready.is_set() or not self.alive() or self._stop.is_set():
            raise ValueError("ENTRY_WORKER_NOT_READY")
        self._release.set()

    def alive(self):
        return self._thread.is_alive()

    def healthy(self):
        # WorkerGroup must not convert an entry/model failure into stopping
        # protection or exits. Diagnostics below are deliberately independent.
        return self.alive() and not self._stop.is_set()

    def request_stop(self):
        self._stop.set()

    def stop(self):
        self.request_stop()
        self._thread.join(self.join_seconds)
        return not self._thread.is_alive() and self._job is None

    def _update(self, status, *, result=None, finished=False):
        with self._lock:
            self._status = status
            if result is not None:
                self._job_id = result["job_id"]
            if self._source is not None:
                lane = self._lanes[self._source]
                lane["last_status"] = status
                if status in {"FAILED", "TIMEOUT", "STOP_UNCONFIRMED"}:
                    # Cleanup can end in STOPPED; retain its original entry
                    # fault instead of making successful reaping look healthy.
                    lane["last_failure"] = status
                if finished and status == "COMPLETED":
                    lane["last_completed"] = self.clock()

    @staticmethod
    def _result(value):
        if (
            type(value) is not dict
            or value.get("status") not in _JOB_STATUSES
            or type(value.get("stop_confirmed")) is not bool
            or value.get("execution_authorized") is not False
            or "job_id" not in value
            or (value["job_id"] is not None and uid(value["job_id"]) != value["job_id"])
        ):
            raise ValueError("ENTRY_JOB_STATUS_INVALID")
        return value

    def _finish_job(self, *, stopping=False):
        while True:
            result = self._result(
                self._job.stop()
                if stopping or self._stop.is_set()
                else self._job.poll()
            )
            self._update(
                result["status"], result=result, finished=result["stop_confirmed"]
            )
            if result["stop_confirmed"]:
                with self._lock:
                    self._job = None
                return
            # A set stop Event must not busy-loop shutdown escalation.
            time.sleep(self.poll_seconds)

    def _retain_until_stopped(self):
        while self._job is not None:
            try:
                self._finish_job(stopping=True)
            except Exception:  # noqa: BLE001 - unknown ownership is never discardable
                self._update("STOP_UNCONFIRMED")
                time.sleep(self.poll_seconds)

    def _run(self):
        try:
            self._update("READY")
            self._ready.set()
            while not self._release.wait(self.poll_seconds):
                if self._stop.is_set():
                    return
            next_lane = 0
            while not self._stop.is_set():
                source = self.ports.sources[next_lane]
                next_lane = (next_lane + 1) % len(self.ports.sources)
                with self._lock:
                    self._source = source
                    self._lanes[source]["attempts"] += 1
                    self._job_id = None
                try:
                    self._update("STARTING_BATCH")
                    # No selection, DB callback, key access or network here.
                    job = self.job_factory(self.ports.job_target, policy=self.policy)
                    with self._lock:
                        self._job = job
                    if self._stop.is_set():
                        self._retain_until_stopped()
                        break
                    self._result(job.start(*self.ports.job_args, source))
                    self._finish_job()
                except Exception:  # noqa: BLE001 - no credential/DB text in diagnostics
                    self._update("FAILED")
                    self._retain_until_stopped()
                self._stop.wait(self.idle_seconds)
        except Exception:  # noqa: BLE001 - actual owner failure, not strategy health
            self.startup_failure = "WORKER_START_FAILED"
            self._update("FAILED")
        finally:
            self._ready.set()
            self._retain_until_stopped()
            self._update("STOPPED")
