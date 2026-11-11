"""Single bounded PG-only housekeeper; never evaluates or dispatches signals."""

import threading
import time
from contextlib import contextmanager
from copy import deepcopy

from psycopg.errors import LockNotAvailable, QueryCanceled

from v2_core.scheduling import StrategyScheduler


class SignalMaintenance:
    def __init__(self, schedulers, *, settings, stop, monotonic=time.monotonic):
        self.settings, self.stop, self.monotonic = settings, stop, monotonic
        self.lock = threading.Lock()
        self.snapshots = {}
        self.last_run = {}
        self.last_expiry = {}
        self.history_page_caps = {}
        self.history_page_reductions = {}
        self.history_next_lane = {}
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
            report = deepcopy(
                self.snapshots.get(self.key(scheduler), {"status": "STARTING"})
            )
            if self.failure:
                report.update(status="UNAVAILABLE", failure_type=self.failure)
            return report

    @staticmethod
    def _record_page(report, component, metrics, observed):
        page = dict(metrics["history_page"])
        previous = report.setdefault("history", {}).get(component, {})
        page["scanned_total"] = previous.get("scanned_total", 0) + page["scanned"]
        page["observed_at_ms"] = observed
        page["last_sweep_completed_at_ms"] = (
            observed
            if page["sweep_complete"]
            else previous.get("last_sweep_completed_at_ms")
        )
        for name in (
            "configured_page_size",
            "effective_page_size",
            "page_reductions",
            "minimum_page_reached",
        ):
            if name in previous:
                page[name] = previous[name]
        report["history"][component] = page

    @staticmethod
    def _page_timeout(exc):
        return isinstance(exc, (QueryCanceled, LockNotAvailable)) or (
            type(exc) is TimeoutError and exc.args == ("HISTORY_PAGE_BUDGET_EXHAUSTED",)
        )

    def _page_cap(self, scheduler, component, report, settings, *, reduce=False):
        identity = self.key(scheduler), component
        configured = settings["scheduler.history_page_size"]
        ceiling = min(
            configured,
            settings["scheduler.expiry_batch"] if component == "cold_expiry" else 1000,
        )
        cap = min(ceiling, self.history_page_caps.get(identity, ceiling))
        if reduce and cap > 1:
            cap = max(1, cap // 2)
            self.history_page_reductions[identity] = (
                self.history_page_reductions.get(identity, 0) + 1
            )
        # Shrink only for this process lifetime. Successful pages must not
        # immediately restore a known-too-large batch and oscillate forever.
        self.history_page_caps[identity] = cap
        report.setdefault("history", {}).setdefault(component, {}).update(
            configured_page_size=configured,
            effective_page_size=cap,
            page_reductions=self.history_page_reductions.get(identity, 0),
            minimum_page_reached=cap == 1,
        )
        return cap

    def _history(self, scheduler, report, failures, settings):
        """Advance finite pages, not repeated whole-history anti-joins/counts."""
        deadline = self.monotonic() + settings["scheduler.history_budget_ms"] / 1000
        operation_budget = settings["scheduler.history_budget_ms"] // 2
        identity = self.key(scheduler)
        lanes = ("cold_expiry", "discovery")
        first = self.history_next_lane.get(identity, lanes[0])
        lanes = (first, lanes[1] if first == lanes[0] else lanes[0])
        completed = set()
        progressed = False
        for _ in range(settings["scheduler.history_pages_per_tick"]):
            for component in lanes:
                if component in completed:
                    continue
                remaining = int((deadline - self.monotonic()) * 1000)
                if self.stop.is_set():
                    return progressed
                if remaining < operation_budget:
                    # A tail fragment is not a meaningful capacity test. Give
                    # this unstarted lane first refusal next tick so a slow
                    # first lane cannot starve the other indefinitely.
                    self.history_next_lane[identity] = component
                    return progressed
                page_size = self._page_cap(scheduler, component, report, settings)
                self.history_next_lane[identity] = (
                    "discovery" if component == "cold_expiry" else "cold_expiry"
                )
                try:
                    if component == "cold_expiry":
                        report["cold_expired"] = scheduler.expire_pending(
                            limit=settings["scheduler.expiry_batch"],
                            reconcile_seconds=settings[
                                "scheduler.discovery_reconcile_seconds"
                            ],
                            history_page_size=page_size,
                            budget_ms=operation_budget,
                        )
                        metrics = scheduler.last_expiry_metrics
                    else:
                        scheduler.run_once(
                            limit=page_size,
                            discover_only=True,
                            history_page_size=page_size,
                            budget_ms=operation_budget,
                            reconcile_seconds=settings[
                                "scheduler.discovery_reconcile_seconds"
                            ],
                            stop_requested=self.stop.is_set,
                        )
                        metrics = scheduler.last_run_metrics
                    self._record_page(
                        report, component, metrics, scheduler.worker.runtime._now()
                    )
                    progressed = True
                    failures.pop(component, None)
                    if metrics["history_page"]["sweep_complete"]:
                        completed.add(component)
                except Exception as exc:  # noqa: BLE001 - no SQL/values in diagnostics
                    failures[component] = type(exc).__name__
                    if self._page_timeout(exc):
                        # The failed transaction did not advance its cursor.
                        # Retry a smaller prefix next tick, with the same time
                        # budget; never skip the page or retry within this tick.
                        self._page_cap(
                            scheduler, component, report, settings, reduce=True
                        )
                    completed.add(component)  # No same-tick failure retry burst.
            if len(completed) == 2:
                break
        return progressed

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
            report = self.snapshot(scheduler)
            component_failures = dict(report.get("component_failures", {}))
            swept = all(
                report.get("history", {}).get(component, {}).get("sweep_complete")
                for component in ("cold_expiry", "discovery")
            )
            history_seconds = (
                reconcile_seconds
                if swept
                else settings["scheduler.history_interval_seconds"]
            )
            if any(name in component_failures for name in ("cold_expiry", "discovery")):
                history_seconds = max(
                    settings["scheduler.history_interval_seconds"],
                    settings["scheduler.retry_seconds"],
                )
            discover_due = (
                now - self.last_run.get(key, float("-inf")) >= history_seconds
            )
            if not (expire_due or discover_due):
                continue
            started = now
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
                progressed = self._history(
                    scheduler, report, component_failures, settings
                )
                # No full-history COUNT on the live database. A completed scan
                # is not proof that every leased/decided task is terminal.
                if progressed:
                    report.update(
                        progress={
                            "mode": "BOUNDED_HISTORY_SWEEP",
                            "caught_up": None,
                            "history": deepcopy(report.get("history", {})),
                        },
                        progress_monotonic_ms=round(now * 1000),
                        progress_observed_at_ms=scheduler.worker.runtime._now(),
                    )
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
