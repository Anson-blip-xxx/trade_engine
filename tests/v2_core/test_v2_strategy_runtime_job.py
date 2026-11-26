"""Pure boundary tests: never create a socket, live DB or exchange request.

SQL-shape assertions below are not PostgreSQL query/lock acceptance. The real
typed gate and request wrappers are constructed without calling external I/O.
"""

import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_testnet as managed
from services import v2_strategy_market_reader as market
from services import v2_strategy_runtime_job as module
from services import v2_strategy_signal_maintenance as maintenance
from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_entry_job import EntryJobSpec
from v2_core.account_risk import AccountScope
from v2_core.errors import SubmissionNotSent
from v2_core.strategy_plan import PLAN_SCHEMA


def configuration(enabled=False):
    binding = ManagementBinding(
        AccountScope("BINANCE", "runtime-job-qa", "SANDBOX", "FUTURES"),
        str(uuid4()),
        str(uuid4()),
        2,
        7,
        str(uuid4()),
    )
    entry = EntryJobSpec(
        binding,
        "causal-trend-v1",
        (module.SPEC,),
        (module.SPEC.entry_handler,),
        (module.SPEC.exit_handler,),
        (PLAN_SCHEMA,),
    )
    return module.StrategyJobConfig(entry, entries_enabled=enabled)


class Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Database:
    def __init__(self):
        self.events, self.statements, self.orders = [], [], []
        self.cursor = None
        self.cursor_failure = None
        self.active = False

    @contextmanager
    def connect(self):
        assert self.active is False, "no nested business transactions"
        self.active = True
        self.events.append("BEGIN")
        previous = self.cursor
        try:
            yield self
        except BaseException:
            self.cursor = previous
            self.events.append("ROLLBACK")
            raise
        else:
            self.events.append("COMMIT")
        finally:
            self.active = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "SELECT order_id FROM v2_strategy_dispatch_cursors" in sql:
            return Rows(() if self.cursor is None else ((self.cursor,),))
        if sql.startswith("SELECT o.order_id::text FROM v2_orders"):
            after, limit = params[-2:]
            values = sorted(self.orders, key=lambda value: (value <= after, value))
            return Rows((value,) for value in values[:limit])
        if sql.startswith("INSERT INTO v2_strategy_dispatch_cursors"):
            if self.cursor_failure:
                raise self.cursor_failure
            self.cursor = params[1]
            self.events.append(("CHECKPOINT", self.cursor))
        return Rows()


class Guard:
    def __init__(self, binding, db):
        self._args, self.db = binding.guard_args, db
        self.failure = self.preflight_failure = None

    def preflight(self, *, scope):
        self.db.events.append("PREFLIGHT")
        if self.preflight_failure:
            raise self.preflight_failure

    def require_transaction(self, conn, *, scope, operation):
        assert conn is self.db and self.db.active
        assert scope.environment == "SANDBOX"
        self.db.events.append(("FENCE", operation))
        if self.failure:
            raise self.failure

    @contextmanager
    def __call__(self, *, operation="OPEN"):
        self.db.events.append(("SIGNED_FENCE", operation))
        if self.failure:
            raise self.failure
        yield


def forbidden(*_, **__):
    pytest.fail("pure test attempted an unauthorized external dependency")


class Public:
    environment = "SANDBOX"
    __call__ = forbidden


@pytest.fixture
def job_case(monkeypatch):
    config = configuration(True)
    db = Database()
    stop = threading.Event()
    guard = Guard(config.entry.binding, db)
    client = SimpleNamespace(close=lambda: db.events.append("CLIENT_CLOSE"))
    value = SimpleNamespace(
        config=config,
        db=db,
        stop=stop,
        guard=guard,
        client=client,
        entry_result={"status": "COMPLETED"},
        dispatch_results={},
        producer=object(),
        reader=object(),
        public=Public(),
        runtime=object(),
    )

    def private_for(tenant, target, scope):
        assert ("FENCE", "OPEN") in db.events
        assert db.active is False
        assert tenant == config.entry.binding.tenant_id
        assert target == {
            "target_registry": config.entry.binding.registry_id,
            "binding_version": config.entry.binding.binding_version,
            "epoch": config.entry.binding.epoch,
            "worker_token": config.entry.binding.worker_token,
        }
        assert scope == config.entry.binding.scope
        db.events.append("PRIVATE")
        return {"api_key": "not-a-real-key", "api_secret": "not-a-real-secret"}

    def producer(connect, **kwargs):
        assert not db.active
        assert kwargs["reader"] is value.reader
        assert kwargs["guard"] is guard
        assert kwargs["stop"] is stop
        db.events.append("PRODUCER")
        return value.producer, value.public

    def work(connect, **kwargs):
        assert kwargs["deployment_window"] is True
        assert kwargs["candidate_producer"] is value.producer
        assert kwargs["store_factory"] is maintenance.MaintainedEntryStore
        assert kwargs["binding"] == config.entry.binding
        db.events.append("WORK")
        return value.entry_result

    def dispatch_runtime(connect, **kwargs):
        assert not db.active
        assert kwargs["public"] is value.public
        assert kwargs["reader"] is value.reader
        assert kwargs["stop"] is stop
        db.events.append("DISPATCH_FACTORY")
        return value.runtime

    def dispatch(connect, **kwargs):
        assert kwargs["runtime"] is value.runtime
        assert kwargs["config"] == config
        assert kwargs["limit"] == 1
        db.events.append("DISPATCH")
        return value.dispatch_results

    monkeypatch.setattr(managed, "database", lambda: db.connect)
    monkeypatch.setattr(managed, "private_for", private_for)
    monkeypatch.setattr(
        module, "create_execution_guard", lambda *_args, **_kwargs: guard
    )
    monkeypatch.setattr(
        maintenance,
        "run_signal_maintenance",
        lambda *_args, **_kwargs: (
            db.events.append("MAINTENANCE") or {"status": "COMPLETED"}
        ),
    )
    monkeypatch.setattr(
        maintenance, "SignalMaintenance", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        module, "scan_catalog", lambda *_args, **_kwargs: db.events.append("SCAN")
    )
    monkeypatch.setattr(
        module.MarketPolicy, "read", lambda *_a, **_kw: module.MarketPolicy()
    )
    monkeypatch.setattr(module, "_market_client", lambda: client)
    monkeypatch.setattr(
        market, "ClosedMarketReader", lambda *_args, **_kwargs: value.reader
    )
    monkeypatch.setattr(module, "_real_producer", producer)
    monkeypatch.setattr(module, "run_entry_work", work)
    monkeypatch.setattr(module, "_dispatch_runtime", dispatch_runtime)
    monkeypatch.setattr(module, "dispatch_prepared", dispatch)
    return value


@pytest.mark.parametrize("source", ["quant", "tv_bridge", "s3"])
def test_disabled_job_never_reads_keys_or_market_client(job_case, monkeypatch, source):
    value = job_case
    monkeypatch.setattr(managed, "private_for", forbidden)
    monkeypatch.setattr(module, "_market_client", forbidden)
    monkeypatch.setattr(module, "_real_producer", forbidden)
    monkeypatch.setattr(module, "run_entry_work", forbidden)
    monkeypatch.setattr(module, "_dispatch_runtime", forbidden)
    monkeypatch.setattr(module, "dispatch_prepared", forbidden)
    assert (
        module.typed_runtime_job(
            value.stop, replace(value.config, entries_enabled=False), source
        )
        == "COMPLETED"
    )
    assert value.db.events == ["PREFLIGHT", "MAINTENANCE"] + (
        ["SCAN"] if source == "quant" else []
    )


def test_pre_stopped_job_does_not_open_database(job_case, monkeypatch):
    job_case.stop.set()
    monkeypatch.setattr(managed, "database", forbidden)
    assert (
        module.typed_runtime_job(job_case.stop, job_case.config, "quant") == "STOPPED"
    )
    assert job_case.db.events == []


@pytest.mark.parametrize("source", ["s2", "unknown", "", None, True])
def test_unknown_source_rejected_before_resources(job_case, monkeypatch, source):
    monkeypatch.setattr(managed, "database", forbidden)
    with pytest.raises(ValueError, match="TYPED_STRATEGY_JOB_REQUIRED"):
        module.typed_runtime_job(job_case.stop, job_case.config, source)


def test_enabled_job_order_and_cleanup_are_explicit(job_case):
    value = job_case
    assert module.typed_runtime_job(value.stop, value.config, "quant") == "COMPLETED"
    assert value.db.events == [
        "PREFLIGHT",
        "MAINTENANCE",
        "SCAN",
        "BEGIN",
        ("FENCE", "OPEN"),
        "COMMIT",
        "PRIVATE",
        "PRODUCER",
        "WORK",
        "DISPATCH_FACTORY",
        "DISPATCH",
        "CLIENT_CLOSE",
    ]
    assert any("statement_timeout='3s'" in sql for sql, _ in value.db.statements)
    assert any(
        "idle_in_transaction_session_timeout='5s'" in sql
        for sql, _ in value.db.statements
    )


@pytest.mark.parametrize("phase", ["preflight", "open"])
def test_fence_denial_precedes_private_dependencies(job_case, monkeypatch, phase):
    value = job_case
    if phase == "preflight":
        value.guard.preflight_failure = ValueError("FENCED")
    else:
        value.guard.failure = ValueError("FENCED")
    monkeypatch.setattr(managed, "private_for", forbidden)
    monkeypatch.setattr(module, "_market_client", forbidden)
    with pytest.raises(ValueError, match="FENCED"):
        module.typed_runtime_job(value.stop, value.config, "quant")
    assert "PRIVATE" not in value.db.events


def test_stop_after_maintenance_skips_scan_and_private_ports(job_case, monkeypatch):
    monkeypatch.setattr(
        maintenance,
        "run_signal_maintenance",
        lambda *_args, **_kwargs: job_case.stop.set() or {"status": "COMPLETED"},
    )
    monkeypatch.setattr(module, "scan_catalog", forbidden)
    monkeypatch.setattr(managed, "private_for", forbidden)
    assert (
        module.typed_runtime_job(job_case.stop, job_case.config, "quant") == "STOPPED"
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_maintenance_failure_remains_visible_but_does_not_skip_other_entry_work(
    job_case, monkeypatch, enabled
):
    job_case.config = replace(job_case.config, entries_enabled=enabled)
    monkeypatch.setattr(
        maintenance,
        "run_signal_maintenance",
        lambda *_a, **_kw: {
            "status": "COMPLETED",
            "signals": {"poison": {"status": "BLOCKED"}},
        },
    )
    assert (
        module.typed_runtime_job(job_case.stop, job_case.config, "quant") == "BLOCKED"
    )
    assert ("DISPATCH" in job_case.db.events) is enabled


@pytest.mark.parametrize(
    "stage", ["reader", "producer", "work", "dispatch_factory", "dispatch"]
)
def test_enabled_failure_closes_market_client(job_case, monkeypatch, stage):
    def fail(*_args, **_kwargs):
        raise ValueError("FIXED_FAILURE")

    if stage == "reader":
        monkeypatch.setattr(market, "ClosedMarketReader", fail)
    else:
        monkeypatch.setattr(
            module,
            {
                "producer": "_real_producer",
                "work": "run_entry_work",
                "dispatch_factory": "_dispatch_runtime",
                "dispatch": "dispatch_prepared",
            }[stage],
            fail,
        )
    with pytest.raises(ValueError, match="FIXED_FAILURE"):
        module.typed_runtime_job(job_case.stop, job_case.config, "quant")
    assert job_case.db.events[-1] == "CLIENT_CLOSE"
    assert job_case.db.events.count("CLIENT_CLOSE") == 1


def test_stop_after_consume_never_constructs_dispatcher(job_case, monkeypatch):
    def work(*_args, **_kwargs):
        job_case.stop.set()
        return {"status": "COMPLETED"}

    monkeypatch.setattr(module, "run_entry_work", work)
    monkeypatch.setattr(module, "_dispatch_runtime", forbidden)
    assert module.typed_runtime_job(job_case.stop, job_case.config, "s3") == "STOPPED"
    assert job_case.db.events[-1] == "CLIENT_CLOSE"


@pytest.mark.parametrize(
    "outcome", ["RECONCILIATION_REQUIRED", "UNKNOWN", "DENIED", "REJECTED", "RACE_LOST"]
)
def test_dispatch_failure_is_not_reported_as_completed(job_case, outcome):
    job_case.dispatch_results = {"order": outcome}
    assert module.typed_runtime_job(job_case.stop, job_case.config, "s3") == "BLOCKED"
    assert job_case.db.events[-1] == "CLIENT_CLOSE"


@pytest.mark.parametrize("outcome", ["ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED"])
def test_confirmed_dispatch_does_not_mask_consumption_blocker(job_case, outcome):
    job_case.dispatch_results = {"order": outcome}
    job_case.entry_result = {"status": "BLOCKED"}
    assert module.typed_runtime_job(job_case.stop, job_case.config, "s3") == "BLOCKED"


def test_fixed_entry_ports_use_bound_scope_and_no_keys(monkeypatch):
    config = configuration()
    binding = config.entry.binding
    target = {
        "target_registry": binding.registry_id,
        "binding_version": binding.binding_version,
        "epoch": binding.epoch,
        "worker_token": binding.worker_token,
    }
    monkeypatch.setattr(managed, "scope_for", lambda *_: binding.scope)
    monkeypatch.setattr(managed, "private_for", forbidden)
    monkeypatch.setattr(module, "_market_client", forbidden)
    result = module.typed_entry_ports_for(binding.tenant_id, target, {})
    assert result.binding == binding
    assert result.job_target is module.typed_runtime_job
    assert result.sources == ("quant", "tv_bridge", "s3")
    assert result.job_args[0].entries_enabled is False
    enabled = module.typed_entry_ports_for(
        binding.tenant_id,
        target,
        {"V2_CONTROLLER_ENABLE_ENTRIES": "true", "V2_TYPED_LEVERAGE": "5"},
    )
    assert enabled.job_args[0].entries_enabled is True
    assert enabled.job_args[0].selected_leverage == 5


@pytest.mark.parametrize(
    "values",
    [
        {"V2_ENVIRONMENT": "LIVE"},
        {"V2_CONTROLLER_ENABLE_ENTRIES": True},
        {"V2_TYPED_LEVERAGE": "8"},
        {"V2_TYPED_INSTANCE": "../module"},
    ],
)
def test_port_configuration_fails_before_scope_lookup(monkeypatch, values):
    monkeypatch.setattr(managed, "scope_for", forbidden)
    with pytest.raises(ValueError):
        module.typed_entry_ports_for(str(uuid4()), {}, values)


@pytest.mark.parametrize(
    "change",
    [
        {"entries_enabled": 1},
        {"selected_leverage": True},
        {"selected_leverage": 8},
        {"scan_limit": 0},
        {"scan_limit": 101},
        {"scan_limit": True},
        {"entry": {}},
    ],
)
def test_job_configuration_requires_exact_fixed_types(change):
    with pytest.raises(ValueError, match="TYPED_STRATEGY_JOB_CONFIG_REQUIRED"):
        replace(configuration(), **change)


def dispatch_case():
    config, db, stop = configuration(True), Database(), threading.Event()
    guard = Guard(config.entry.binding, db)
    seen = []

    def dispatch(order):
        assert not db.active, (
            "venue work must not occur in selector/checkpoint transaction"
        )
        seen.append(order)
        db.events.append(("DISPATCH", order))
        return "ACKNOWLEDGED"

    runtime = SimpleNamespace(
        scope=config.entry.binding.scope, execution=SimpleNamespace(dispatch=dispatch)
    )
    return SimpleNamespace(
        config=config, db=db, stop=stop, guard=guard, seen=seen, runtime=runtime
    )


def invoke_dispatch(value, **kwargs):
    return module.dispatch_prepared(
        value.db.connect,
        runtime=value.runtime,
        config=value.config,
        guard=value.guard,
        stop=value.stop,
        **kwargs,
    )


def test_dispatch_selects_only_scoped_typed_prepared_open_and_fences_checkpoint():
    value = dispatch_case()
    value.db.orders = ["c", "a", "b"]
    assert invoke_dispatch(value, limit=2) == {"a": "ACKNOWLEDGED", "b": "ACKNOWLEDGED"}
    query, params = next(
        item
        for item in value.db.statements
        if item[0].startswith("SELECT o.order_id::text FROM v2_orders")
    )
    assert "e.snapshot ? 'strategy_plan'" in query
    assert "o.leg='OPEN'" in query and "o.status='PREPARED'" in query
    assert "i.producer=%s" in query
    assert params == (
        *asdict(value.runtime.scope).values(),
        value.config.entry.producer,
        "",
        2,
    )
    assert value.db.events.count(("FENCE", "OPEN")) == 3
    for order in ("a", "b"):
        assert value.db.events.index(("CHECKPOINT", order)) < value.db.events.index(
            ("DISPATCH", order)
        )


def test_dispatch_cursor_survives_new_calls_and_wraps_failed_order():
    value = dispatch_case()
    value.db.orders = ["a", "b", "c"]

    def fail(order):
        value.seen.append(order)
        raise RuntimeError("never expose exception details or credentials")

    value.runtime.execution.dispatch = fail
    outcomes = [invoke_dispatch(value, limit=1) for _ in range(4)]
    assert value.seen == ["a", "b", "c", "a"]
    assert all(list(item.values()) == ["RECONCILIATION_REQUIRED"] for item in outcomes)
    assert "credentials" not in str(outcomes)


def test_dispatch_does_not_send_after_failed_checkpoint():
    value = dispatch_case()
    value.db.orders = ["a"]
    value.db.cursor_failure = ValueError("COMMIT_UNKNOWN")
    with pytest.raises(ValueError, match="COMMIT_UNKNOWN"):
        invoke_dispatch(value)
    assert value.seen == [] and value.db.cursor is None


def test_dispatch_fence_failure_and_stop_prevent_order_send():
    value = dispatch_case()
    value.db.orders = ["a"]
    value.guard.failure = ValueError("OLD_ROUTE")
    with pytest.raises(ValueError, match="OLD_ROUTE"):
        invoke_dispatch(value)
    assert value.seen == []
    value.guard.failure = None
    value.stop.set()
    with pytest.raises(ValueError, match="STRATEGY_ENTRY_STOPPED"):
        invoke_dispatch(value)
    assert value.seen == []


def test_dispatch_stop_after_first_send_leaves_remaining_unattempted():
    value = dispatch_case()
    value.db.orders = ["a", "b"]

    def dispatch(order):
        value.seen.append(order)
        value.stop.set()
        return "ACKNOWLEDGED"

    value.runtime.execution.dispatch = dispatch
    assert invoke_dispatch(value) == {"a": "ACKNOWLEDGED"}
    assert value.db.cursor == "a" and value.seen == ["a"]


@pytest.mark.parametrize("limit", [0, 11, True, "1"])
def test_dispatch_limit_invalid_before_db(limit):
    value = dispatch_case()
    with pytest.raises(ValueError, match="TYPED_DISPATCH_ENABLED_REQUIRED"):
        invoke_dispatch(value, limit=limit)
    assert value.db.events == []


def test_dispatch_scope_or_disabled_rejected_without_db():
    value = dispatch_case()
    value.config = replace(value.config, entries_enabled=False)
    with pytest.raises(ValueError, match="TYPED_DISPATCH_ENABLED_REQUIRED"):
        invoke_dispatch(value)
    value.config = replace(value.config, entries_enabled=True)
    value.runtime.scope = replace(value.runtime.scope, account_id="another-account")
    with pytest.raises(ValueError, match="TYPED_DISPATCH_SCOPE_MISMATCH"):
        invoke_dispatch(value)
    assert value.db.events == []


def test_actual_dispatcher_constructs_only_typed_guarded_opening_ports(monkeypatch):
    from v2_core.guarded_transport import GuardedSignedRequest
    from v2_core.strategy_opening import StrategyOpeningGate
    from v2_core.venue_readiness import GuardedOpeningSubmit

    value = dispatch_case()
    runtime = module._dispatch_runtime(
        value.db.connect,
        binding=value.config.entry.binding,
        guard=value.guard,
        private={"api_key": "qa-only", "api_secret": "qa-only-secret"},
        public=Public(),
        reader=object(),
        stop=value.stop,
        signed_connection_factory=forbidden,
        risk_reference=lambda _: {},
    )
    assert value.db.events == []
    submit = runtime.execution.submit
    assert type(submit) is GuardedOpeningSubmit
    assert submit.enabled is True and submit.reduce_only_enabled is False
    assert type(submit.validate_reference.__self__) is StrategyOpeningGate
    assert submit.validate_reduction.__self__ is submit.validate_reference.__self__
    venue = submit.submit.__self__
    assert type(venue.request) is GuardedSignedRequest
    assert venue.request.guard is value.guard
    assert venue.request.request.environment == "SANDBOX"
    assert runtime.scope == value.runtime.scope

    monkeypatch.setattr(StrategyOpeningGate, "terms", lambda *_: None)
    with pytest.raises(SubmissionNotSent, match="VENUE_READINESS_BLOCKED"):
        submit.plan({"order_id": "untyped"})
    value.stop.set()
    assert runtime.risk_check({"leg": "OPEN"}) is False
    assert runtime.risk_check({"leg": "CLOSE"}) is False
    assert value.db.events == []


def test_actual_signed_mutation_requires_guard_before_network():
    value = dispatch_case()
    runtime = module._dispatch_runtime(
        value.db.connect,
        binding=value.config.entry.binding,
        guard=value.guard,
        private={"api_key": "qa-only", "api_secret": "qa-only-secret"},
        public=Public(),
        reader=object(),
        stop=value.stop,
        signed_connection_factory=forbidden,
        risk_reference=lambda _: {},
    )
    request = runtime.execution.submit.submit.__self__.request
    value.guard.failure = ValueError("revoked route")
    with pytest.raises(SubmissionNotSent, match="EXECUTION_FENCED"):
        request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert value.db.events == [("SIGNED_FENCE", "OPEN")]
