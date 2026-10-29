"""Single bounded PG-only housekeeper; never evaluates or dispatches signals."""

import threading
import time
from contextlib import contextmanager

from v2_core.scheduling import StrategyScheduler


class SignalMaintenance:
    def __init__(self, schedulers, *, settings, stop, monotonic=time.monotonic):
        self.settings, self.stop, self.monotonic = settings, stop, monotonic
        self.lock = threading.Lock()
        self.snapshots = {}
        self.last_run = {}
        self.last_expiry = {}
        self.thread = None
        self.failure = None
        self.work = []
        for source in schedulers:
            clone = StrategyScheduler(
                source.worker,
                context_provider=self._no_context,
                priority_events=source.priority_events,
            )
            clone._connect = self._bounded(source._connect)
            self.work.append((self.key(source), clone))

    @staticmethod
    def _no_context(_):
        raise RuntimeError("MAINTENANCE_MUST_NOT_EVALUATE")

    @staticmethod
    def _bounded(connect):
        @contextmanager
        def bounded():
            with connect() as conn:
                conn.execute("SET LOCAL statement_timeout='15s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                # One housekeeping query at a time. Bound its memory, and do
                # not borrow parallel workers from the live trading workload.
                conn.execute("SET LOCAL work_mem='64MB'")
                conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
                yield conn

        return bounded

    @staticmethod
    def key(scheduler):
        return scheduler.worker.scope.consumer, scheduler.worker.source

    def snapshot(self, scheduler):
        with self.lock:
            report = dict(
                self.snapshots.get(self.key(scheduler), {"status": "STARTING"})
            )
            if self.failure:
                report.update(status="UNAVAILABLE", failure_type=self.failure)
            return report

    def run_once(self):
        if not self.work:
            return
        settings = self.settings()
        with self.lock:
            self.failure = None
        for key, scheduler in self.work:
            if self.stop.is_set():
                break
            now = self.monotonic()
            reconcile_seconds = settings["scheduler.discovery_reconcile_seconds"]
            # Expired inputs need prompt terminal receipts, not a minute-long
            # wait for cold historical discovery. Recent indexed expiry stays
            # independent of the unchanged full reconciliation cadence.
            expiry_seconds = min(
                reconcile_seconds, max(1, settings["health.queue_seconds"] // 2)
            )
            expire_due = (
                now - self.last_expiry.get(key, float("-inf")) >= expiry_seconds
            )
            discover_due = (
                now - self.last_run.get(key, float("-inf")) >= reconcile_seconds
            )
            if not (expire_due or discover_due):
                continue
            started = now
            report = self.snapshot(scheduler)
            component_failures = dict(report.get("component_failures", {}))
            if expire_due:
                try:
                    report["expired"] = scheduler.expire_pending(
                        limit=settings["scheduler.expiry_batch"],
                        reconcile_seconds=reconcile_seconds,
                        hot_only=True,
                        budget_ms=2000,
                    )
                    report["expiry_observed_at_ms"] = scheduler.worker.runtime._now()
                    component_failures.pop("expiry", None)
                except Exception as exc:  # noqa: BLE001 - fixed type, no raw SQL
                    component_failures["expiry"] = type(exc).__name__
                self.last_expiry[key] = self.monotonic()
            if discover_due and not self.stop.is_set():
                # A failed/unfinished history sweep must never turn the fast
                # path into repeated full scans. Cold reconciliation retains
                # its own cadence, budget and unresolved failure diagnostic.
                try:
                    report["cold_expired"] = scheduler.expire_pending(
                        limit=settings["scheduler.expiry_batch"],
                        reconcile_seconds=reconcile_seconds,
                        budget_ms=5000,
                    )
                    component_failures.pop("cold_expiry", None)
                except Exception as exc:  # noqa: BLE001 - no history retry burst
                    component_failures["cold_expiry"] = type(exc).__name__
                try:
                    scheduler.run_once(
                        limit=settings["scheduler.signal_batch"],
                        discover_only=True,
                        reconcile_seconds=reconcile_seconds,
                        stop_requested=self.stop.is_set,
                    )
                    if (
                        now - report.get("progress_monotonic_ms", float("-inf")) / 1000
                        >= settings["scheduler.progress_seconds"]
                    ):
                        report.update(
                            progress=scheduler.progress(),
                            progress_monotonic_ms=round(now * 1000),
                            progress_observed_at_ms=scheduler.worker.runtime._now(),
                        )
                    component_failures.pop("discovery", None)
                except Exception as exc:  # noqa: BLE001 - keep last good timestamp
                    component_failures["discovery"] = type(exc).__name__
                self.last_run[key] = self.monotonic()
            report["component_failures"] = component_failures
            report["status"] = "UNAVAILABLE" if component_failures else "CURRENT"
            if component_failures:
                report["failure_type"] = next(iter(component_failures.values()))
            else:
                report.pop("failure_type", None)
            report["elapsed_ms"] = round((self.monotonic() - started) * 1000)
            with self.lock:
                self.snapshots[key] = report

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - no raw config or SQL
                with self.lock:
                    self.failure = type(exc).__name__
            self.stop.wait(1)

    def start(self):
        if self.thread is not None:
            raise ValueError("MAINTENANCE_ALREADY_STARTED")
        self.thread = threading.Thread(
            target=self._loop, name="signal-maintenance", daemon=True
        )
        self.thread.start()

    def close(self):
        # Parent stop event is shared. Queries are bounded and daemon shutdown
        # rolls uncommitted work back; durable receipts make replay idempotent.
        if self.thread is not None:
            self.thread.join(1)
