"""One fenced, database-only typed-entry batch, never order dispatch.

Only trusted composition may construct this job. It consumes already frozen
candidates; no models, market fetches, credential decryption or signed request
port exists. Queue metadata is not execution authority. Every business write
independently locks the current route/binding in its own transaction, before
signal/intent/account locks, and retains that fence until commit.
"""

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from v2_core.account_risk import AccountScope
from v2_core.scheduling import StrategyScheduler, diagnostic_code
from v2_core.session_lock import session_advisory_lock
from v2_core.strategy import StrategyScope
from v2_core.strategy_entry_store import StrategyEntryStore
from v2_core.strategy_entry_worker import TypedEntryWorker
from v2_core.strategy_prepare import StrategyPrepareRecovery
from v2_core.strategy_registry import (
    StrategyRegistry,
    StrategySpec,
    strategy_identifier,
)
from v2_core.strategy_signal_admission import StrategySignalAdmission
from v2_core.strategy_signal_window import deployment_history_page

_SOURCES = frozenset({"s2", "s3", "tv_bridge", "quant"})
_COMPLETE = frozenset({"PREPARED", "EXISTING_ORDER", "IGNORED", "EXPIRED"})


def _cursor(connect, scope, source):
    with connect() as conn:
        conn.execute("SET LOCAL statement_timeout='500ms'")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        row = conn.execute(
            "SELECT request_cursor FROM v2_entry_discovery_cursors "
            "WHERE (consumer,source,environment)=(%s,%s,%s)",
            (scope.consumer, source, scope.environment),
        ).fetchone()
    return None if row is None else row[0]


def _checkpoint(write, scope, source, before, after):
    # No high-frequency audit history, and no idle-page UPDATE/WAL churn.
    if before == after:
        return
    with write() as conn:
        conn.execute(
            "INSERT INTO v2_entry_discovery_cursors "
            "(consumer,source,environment,request_cursor) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (consumer,source,environment) DO UPDATE "
            "SET request_cursor=EXCLUDED.request_cursor,updated_at=clock_timestamp() "
            "WHERE v2_entry_discovery_cursors.request_cursor IS DISTINCT FROM EXCLUDED.request_cursor",
            (scope.consumer, source, scope.environment, after),
        )


def _bounds(limit, budget_ms, history_page_size, history_budget_ms):
    if any(
        type(value) is not int
        for value in (limit, budget_ms, history_page_size, history_budget_ms)
    ) or not (
        1 <= limit <= 20
        and 100 <= budget_ms <= 60000
        and 1 <= history_page_size <= 200
        and 100 <= history_budget_ms <= 5000
    ):
        raise ValueError("STRATEGY_ENTRY_JOB_BOUNDS_REQUIRED")


@dataclass(frozen=True)
class EntryJobSpec:
    """Pickle-safe explicit registrations; no runtime import names or secrets."""

    binding: ManagementBinding
    producer: str
    specs: tuple[StrategySpec, ...]
    entry_handlers: tuple[str, ...]
    exit_handlers: tuple[str, ...]
    plan_schemas: tuple[str, ...]
    limit: int = 10
    budget_ms: int = 5000
    history_page_size: int = 100
    history_budget_ms: int = 500

    def __post_init__(self):
        if type(self.binding) is not ManagementBinding:
            raise ValueError("STRATEGY_ENTRY_JOB_BINDING_REQUIRED")
        # Own the scope too; a caller cannot mutate its original nested object.
        copied = asdict(self.binding)
        copied["scope"] = AccountScope(**copied["scope"])
        object.__setattr__(self, "binding", ManagementBinding(**copied))
        strategy_identifier(self.producer)
        if any(
            type(value) is not tuple or not 1 <= len(value) <= 32
            for value in (
                self.specs,
                self.entry_handlers,
                self.exit_handlers,
                self.plan_schemas,
            )
        ):
            raise ValueError("STRATEGY_ENTRY_JOB_REGISTRY_REQUIRED")
        registry = self.registry()
        object.__setattr__(self, "specs", tuple(registry.specs.values()))
        _bounds(
            self.limit, self.budget_ms, self.history_page_size, self.history_budget_ms
        )

    def registry(self):
        return StrategyRegistry(
            list(self.specs),
            entry_handlers=self.entry_handlers,
            exit_handlers=self.exit_handlers,
            plan_schemas=self.plan_schemas,
        )


def entry_transaction(connect, *, binding, guard, stop):
    """Connection factory for short typed business transactions only.

    Do not use this factory for queue discovery, network I/O or session locks.
    Stop is a cooperative hint, not a substitute for the transaction-held route
    fence. A lost commit acknowledgement must be recovered from durable proof.
    """
    if (
        not callable(connect)
        or type(binding) is not ManagementBinding
        or getattr(guard, "_args", None) != binding.guard_args
        or not callable(getattr(guard, "require_transaction", None))
        or not callable(getattr(stop, "is_set", None))
    ):
        raise ValueError("STRATEGY_ENTRY_TRANSACTION_PORTS_REQUIRED")

    @contextmanager
    def fenced():
        if stop.is_set():
            raise ValueError("STRATEGY_ENTRY_STOPPED")
        with connect() as conn:
            conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            # No isolation-changing or network call occurs inside this adapter.
            guard.require_transaction(conn, scope=binding.scope, operation="OPEN")
            if stop.is_set():
                raise ValueError("STRATEGY_ENTRY_STOPPED")
            yield conn
            if stop.is_set():
                raise ValueError("STRATEGY_ENTRY_STOPPED")

    return fenced


def run_entry_work(
    connect,
    *,
    binding,
    registry,
    producer,
    source,
    stop,
    clock_ms,
    limit=10,
    budget_ms=5000,
    history_page_size=100,
    history_budget_ms=500,
    guard=None,
    candidate_producer=None,
    maintenance=None,
    store_factory=StrategyEntryStore,
    deployment_window=False,
):
    """Paged cold discovery then hot consumption; no global historical sweep.

    This adapter's new PostgreSQL checkpoint survives disposable jobs. Work limits
    bound rows returned/attempted, not all rows examined or OS/database latency;
    the independent process owner is responsible for enforcing wall time.
    """
    _bounds(limit, budget_ms, history_page_size, history_budget_ms)
    strategy_identifier(producer)
    if (
        type(binding) is not ManagementBinding
        or type(registry) is not StrategyRegistry
        or source not in _SOURCES
        or not callable(connect)
        or not callable(clock_ms)
        or not callable(getattr(stop, "is_set", None))
        or not callable(store_factory)
        or type(deployment_window) is not bool
        or (
            candidate_producer is not None
            and not callable(getattr(candidate_producer, "prepare", None))
        )
        or (
            maintenance is not None
            and not callable(getattr(maintenance, "maintain", None))
        )
    ):
        raise ValueError("STRATEGY_ENTRY_JOB_PORTS_REQUIRED")
    guard = (
        create_execution_guard(connect, **binding.guard_args)
        if guard is None
        else guard
    )
    write = entry_transaction(connect, binding=binding, guard=guard, stop=stop)
    result = {"status": "BLOCKED", "signals": {}, "execution_authorized": False}
    if stop.is_set():
        return {**result, "reason": "STRATEGY_ENTRY_STOPPED"}
    scope = StrategyScope(**asdict(binding.scope), producer=producer)
    # Different sources in this instance must not produce concurrent batches;
    # independent strategies still share the ordinary account/risk locks.
    with session_advisory_lock(
        connect, "typed-entry-batch:" + scope.consumer
    ) as acquired:
        if not acquired:
            return {**result, "status": "BUSY", "reason": "STRATEGY_ENTRY_BATCH_BUSY"}
        # Fail before queue work on revoked/stopped ownership. Later business
        # transactions repeat this check, so this probe is not a cached permit.
        with write():
            pass
        worker = TypedEntryWorker(
            connect,
            registry,
            scope=scope,
            source=source,
            store=store_factory(write),
            clock_ms=clock_ms,
            admission=StrategySignalAdmission(write, registry),
            preparation=StrategyPrepareRecovery(write, scope=binding.scope),
        )
        if candidate_producer is not None or maintenance is not None:
            # Hooks run only for genuinely unfrozen work. Original durable
            # receipts/candidates always recover without acquiring new facts.
            consume = worker.consume

            def prepare_then_consume(signal_id, *, context):
                state = worker._load(signal_id)
                if state.receipt is None and state.candidate is None:
                    if maintenance is not None:
                        maintenance.maintain(signal_id)
                        state = worker._load(signal_id)
                    if (
                        candidate_producer is not None
                        and state.receipt is None
                        and state.candidate is None
                    ):
                        prepared = candidate_producer.prepare(signal_id)
                        if prepared.get("status") == "DEFERRED":
                            return {
                                **prepared,
                                "signal_id": signal_id,
                                "execution_authorized": False,
                            }
                return consume(signal_id, context=context)

            worker.consume = prepare_then_consume

        def no_context(*_args, **_kwargs):
            raise ValueError("STRATEGY_ENTRY_NETWORK_CONTEXT_FORBIDDEN")

        queue = StrategyScheduler(
            worker, context_provider=no_context, context_required=lambda _: False
        )
        if deployment_window:
            # Explicit typed-runtime subscription; generic callers retain their
            # original discovery/recovery contract. Hot discovery already uses
            # the stricter current expiry boundary, so is unchanged.
            queue._history_page = lambda conn, *, cursor, size: deployment_history_page(
                conn, scope=scope, source=source, cursor=cursor, size=size
            )
        history_error = None
        try:
            before = _cursor(connect, scope, source)
            queue._discovery_history_cursor = before
            queue.run_once(
                limit=history_page_size,
                discover_only=True,
                history_page_size=history_page_size,
                budget_ms=history_budget_ms,
                stop_requested=stop.is_set,
            )
            # Advance only after confirmed discovery commit. If this separate
            # checkpoint fails, replaying the page is safe; it never skips work.
            if not stop.is_set():
                _checkpoint(
                    write, scope, source, before, queue._discovery_history_cursor
                )
        except Exception as exc:  # noqa: BLE001 - failed cold metadata cannot stop independent hot work
            history_error = diagnostic_code(exc)
        signals = queue.run_once(
            limit=limit,
            hot_only=True,
            budget_ms=budget_ms,
            stop_requested=stop.is_set,
        )
        diagnostics = queue.last_run_diagnostics
        unchanged = [
            identity for identity, status in signals.items() if status == "UNCHANGED"
        ]
        completed = set()
        if unchanged:
            with connect() as conn:
                conn.execute("SET LOCAL statement_timeout='500ms'")
                conn.execute("SET LOCAL lock_timeout='250ms'")
                completed = {
                    row[0]
                    for row in conn.execute(
                        "SELECT signal_id::text FROM v2_strategy_tasks WHERE consumer=%s "
                        "AND signal_id=ANY(%s::uuid[]) AND completed_at IS NOT NULL",
                        (scope.consumer, unchanged),
                    ).fetchall()
                }
        wait_reasons = {
            value["signal_id"]: value["reason"]
            for key in ("typed_waits", "reconciliation_waits")
            for value in diagnostics.get(key, [])
        }
        waits = [
            {"signal_id": identity, "reason": wait_reasons.get(identity, status)}
            for identity, status in signals.items()
            if status not in _COMPLETE and identity not in completed
        ]
        clear = (
            not stop.is_set()
            and not history_error
            and not waits
            and not diagnostics.get("failures")
        )
        return {
            **result,
            "status": "COMPLETED" if clear else "BLOCKED",
            "signals": signals,
            "pending": waits,
            "history_error": history_error,
            "diagnostics": diagnostics,
            "metrics": queue.last_run_metrics,
            "trading_health": "NOT_EVALUATED",
        }


def entry_ports(spec, *, sources):
    """Explicit trusted composition; no reading env or automatically starting it."""
    from services.v2_entry_worker import EntryPorts

    if type(spec) is not EntryJobSpec:
        raise ValueError("STRATEGY_ENTRY_JOB_SPEC_REQUIRED")
    return EntryPorts(spec.binding, entry_job, (spec,), sources)


def entry_job(stop, spec, source):
    """Trusted spawn target; opens its own DB connection, never venue keys."""
    if type(spec) is not EntryJobSpec:
        raise ValueError("STRATEGY_ENTRY_JOB_SPEC_REQUIRED")
    from services.v2_managed_testnet import database

    return run_entry_work(
        database(),
        binding=spec.binding,
        registry=spec.registry(),
        producer=spec.producer,
        source=source,
        stop=stop,
        clock_ms=lambda: time.time_ns() // 1000000,
        limit=spec.limit,
        budget_ms=spec.budget_ms,
        history_page_size=spec.history_page_size,
        history_budget_ms=spec.history_budget_ms,
    )["status"]
