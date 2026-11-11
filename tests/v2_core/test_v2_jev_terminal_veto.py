"""Negative-only JEV completion; pure fakes and explicit disposable PG only."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_jev_entry_context import POLICY, binding, publish
from test_v2_jev_entry_context import case as memory_fixture
from test_v2_jev_fullflow import prepare_candidate
from test_v2_trading_pipeline import case as pipeline_fixture

from services import v2_jev_entry_context as subject
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical
from v2_core.scheduling import StrategyScheduler
from v2_core.state import BusinessState, StateKey, StateSnapshot

memory_case = memory_fixture
pipeline_case = pipeline_fixture
database = database_fixture


class LockedMemory:
    def __init__(self, db):
        self.db = db
        self.locks = []

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, params=()):
        if "FOR SHARE" in sql:
            self.locks.append(params)
            ids = params[0] if isinstance(params[0], list) else [params[0]]
            rows = [(key,) for key in ids if key in self.db.rows]
            return SimpleNamespace(
                fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None
            )
        return self.db.execute(sql, params)


def check(case):
    conn = LockedMemory(case.db)
    args = {
        k: deepcopy(case.args[k]) for k in ("signal_id", "signal", "config", "consumer")
    }
    with case.db.transaction():
        result = case.service.terminal_veto(conn=conn, **args)
    if result is not None:
        assert conn.locks
    return result


def set_payload(case, key, change):
    row = case.db.rows[key.identity]
    value = json.loads(row.payload_json)
    change(value)
    case.db.rows[key.identity] = replace(row, payload_json=canonical(value))


def request_key(case):
    return StateKey(
        **asdict(PUBLIC_SCOPE),
        namespace=subject.REQUEST_NAMESPACE,
        key=binding(case)[2]["request_key"],
    )


@pytest.mark.parametrize(
    "trend,chase,reason",
    [
        ("0.1", "0.2", "JEV_TREND_UNSUPPORTED"),
        ("0.8", "0.8", "JEV_CHASE_RISK_HIGH"),
    ],
)
def test_pure_completed_negative_is_not_an_open_or_account_proof(
    memory_case, trend, chase, reason
):
    case = memory_case
    assert case.service.assess(**case.args).status == "PENDING"
    publish(case, trend=trend, chase=chase)
    before = deepcopy(case.db.rows)
    result = check(case)
    assert result is not None and result.status == "VETO" and result.reason == reason
    assert result.decision.action == "IGNORED" and result.decision.side is None
    proof = json.loads(result.evidence_json)
    assert proof["fresh_account_recheck"] == "NOT_RUN_TERMINAL_VETO"
    assert (
        proof["execution_authorized"]
        is proof["admission_authorized"]
        is proof["risk_override"]
        is False
    )
    assert "original_admission" not in proof and "baseline_context" not in proof
    assert before == case.db.rows and len(case.queued) == 1


@pytest.mark.parametrize(
    "change",
    [
        "pass",
        "fallback",
        "pending",
        "missing",
        "legacy",
        "binding_version",
        "deleted",
        "signal",
        "config",
        "source",
        "account",
        "symbol",
        "direction",
        "request_key",
        "request",
        "model",
        "prompt",
        "hypotheses",
        "control_changed_back",
        "gate_changed_back",
        "deadline",
        "model_expired",
        "signal_expired",
        "future_result",
        "late_result",
        "bad_completion",
    ],
)
def test_pure_uncertainty_and_nonveto_keep_original_path(memory_case, change):
    case = memory_case
    if change == "gate_changed_back":
        case.service.policy = None
        case.db.rows[subject.policy_key(case.service.scope).identity] = StateSnapshot(
            1, canonical(asdict(POLICY)), False
        )
    assert case.service.assess(**case.args).status == "PENDING"
    if change != "pending":
        publish(
            case,
            trend="0.8" if change == "pass" else "0.1",
            failure="MODEL_TIMEOUT" if change == "fallback" else None,
        )
    key, row, bound = binding(case)
    rk = request_key(case)
    if change == "missing":
        del case.db.rows[rk.identity]
    elif change == "legacy":
        set_payload(case, key, lambda p: p.pop("model_control_version"))
    elif change == "binding_version":
        case.db.rows[key.identity] = replace(row, version=2)
    elif change == "deleted":
        case.db.rows[key.identity] = replace(row, deleted=True)
    elif change == "signal":
        case.args["signal"]["features"]["strength"] = 1
    elif change == "config":
        case.args["config"]["revision"] = 999
    elif change == "source":
        case.args["config"]["source"] = "tv_bridge"
    elif change == "account":
        case.args["config"]["account_scope"]["account_id"] = "other"
    elif change in {"symbol", "direction", "request_key"}:
        set_payload(case, key, lambda p: p.update({change: "different"}))
    elif change == "request":
        set_payload(case, rk, lambda p: p["request"]["state"].update(price="999"))
    elif change in {
        "model",
        "prompt",
        "hypotheses",
        "future_result",
        "late_result",
        "model_expired",
        "bad_completion",
    }:

        def corrupt(p):
            if change == "bad_completion":
                p["completed_at_ms"] = p["claimed_at_ms"] - 1
            else:
                field, value = {
                    "model": ("model", "unknown"),
                    "prompt": ("prompt_version", "unknown"),
                    "hypotheses": ("hypotheses", {"trend_supported": "0"}),
                    "future_result": ("produced_at_ms", case.db.now + 1),
                    "late_result": ("produced_at_ms", p["deadline_ms"] + 1),
                    "model_expired": ("expires_at_ms", case.db.now),
                }[change]
                p["result"][field] = value

        set_payload(case, rk, corrupt)
    elif change in {"control_changed_back", "gate_changed_back"}:
        changed_key = (
            CONTROL_KEY
            if change == "control_changed_back"
            else subject.policy_key(case.service.scope)
        )
        case.db.rows[changed_key.identity] = replace(
            case.db.rows[changed_key.identity], version=3
        )
    elif change == "deadline":
        case.db.now = bound["deadline_ms"]
    elif change == "signal_expired":
        case.db.now = case.args["signal"]["expires_at_ms"]
    assert check(case) is None


def test_pure_read_failure_is_optional_not_global_block(memory_case, monkeypatch):
    case = memory_case
    case.service.assess(**case.args)
    publish(case, trend="0.1")
    monkeypatch.setattr(
        LockedMemory, "execute", lambda *_: (_ for _ in ()).throw(TimeoutError())
    )
    with case.db.transaction():
        assert (
            case.service.terminal_veto(
                conn=LockedMemory(case.db),
                **{
                    k: case.args[k]
                    for k in ("signal_id", "signal", "config", "consumer")
                },
            )
            is None
        )


@pytest.mark.parametrize(
    "change", ["none", "version", "digest", "values", "missing_lock"]
)
def test_pure_current_runtime_policy_is_locked_and_bound(
    memory_case, monkeypatch, change
):
    from v2_core.evidence import digest
    from v2_core.runtime_policy import resolve

    case = memory_case
    key = StateKey(
        **asdict(case.service.scope), namespace="runtime-policy-v1", key="active"
    )
    values = resolve()
    case.db.rows[key.identity] = StateSnapshot(1, canonical({"values": values}), False)
    case.args["config"].update(
        policy=values, policy_version=1, policy_digest=digest(canonical(values))
    )
    case.service.assess(**case.args)
    publish(case, trend="0.1")
    if change == "version":
        case.db.rows[key.identity] = replace(case.db.rows[key.identity], version=3)
    elif change == "digest":
        case.args["config"]["policy_digest"] = "wrong"
    elif change == "values":
        set_payload(
            case, key, lambda p: p["values"].update({"scheduler.signal_batch": 17})
        )
    elif change == "missing_lock":
        original = LockedMemory.execute

        def unlocked(self, sql, params=()):
            row = original(self, sql, params)
            if "ANY(%s::uuid[])" in sql:
                return SimpleNamespace(
                    fetchall=lambda: [(k,) for k in params[0] if k != key.identity]
                )
            return row

        monkeypatch.setattr(LockedMemory, "execute", unlocked)
    assert (check(case) is not None) is (change == "none")


@pytest.mark.parametrize("mode", ["slow", "stop", "pass", "veto"])
def test_pure_slow_negative_check_cannot_start_unbudgeted_account_io(mode):
    state = SimpleNamespace(now=0.0, stopped=False)

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *_: SimpleNamespace(fetchone=lambda: (10000,))
        )

    def negative(*_, **__):
        if mode == "slow":
            state.now = 5.0
        if mode == "stop":
            state.stopped = True
        return {"status": "IGNORED"} if mode == "veto" else None

    calls = []
    worker = SimpleNamespace(
        _connect=connect,
        scope=SimpleNamespace(consumer="qa"),
        entry_filter=SimpleNamespace(wait_pending=lambda **_: True),
        reject_completed_jev=negative,
        runtime=SimpleNamespace(_now=lambda: 0),
        consume=lambda *_args, **_kwargs: {"status": "PREPARED"},
    )
    scheduler = StrategyScheduler(
        worker,
        context_provider=lambda _: calls.append(True) or {},
        monotonic=lambda: state.now,
    )
    pending = {"status": "DEFERRED", "reason": "JEV_RESULT_PENDING"}
    args = {
        "signal_id": str(uuid4()),
        "snapshot": {"expires_at_ms": 999999},
        "token": str(uuid4()),
        "deadline": 1.0,
        "stop_requested": lambda: state.stopped,
        "diagnostics": {},
        "metrics": {},
    }
    if mode == "stop":
        from v2_core.scheduling import _DrainAttempt

        with pytest.raises(_DrainAttempt):
            scheduler._review_pending_once(pending, **args)
    else:
        result = scheduler._review_pending_once(pending, **args)
        assert (
            result["status"]
            == {"slow": "DEFERRED", "pass": "PREPARED", "veto": "IGNORED"}[mode]
        )
    assert calls == ([True] if mode == "pass" else [])


def lease(database, candidate, *, cause="JEV_RESULT_PENDING", expired=False):
    token = str(uuid4())
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks (consumer,signal_id,attempts,error_code,lease_token,lease_until) "
            "VALUES (%s,%s,1,%s,%s,clock_timestamp()+(%s)*interval '1 second')",
            (
                candidate.worker.scope.consumer,
                candidate.signal_id,
                cause,
                token,
                -1 if expired else 30,
            ),
        )
    return token


def candidate_with_policy(database, pipeline_case):
    from v2_core.runtime_policy import PolicyStore

    pipeline = pipeline_case[0]
    store = PolicyStore(database, pipeline.scope)
    store.patch({}, expected_version=0, reason="isolated terminal-veto QA")
    profile = store.read()
    worker = pipeline.schedulers[0].worker
    worker.config_json = canonical(
        {
            **json.loads(worker.config_json),
            "policy": profile.values,
            "policy_version": profile.version,
            "policy_digest": profile.digest,
        }
    )
    return prepare_candidate(database, pipeline_case)


def assert_no_orders(database):
    with database() as conn:
        for table in ("v2_trade_intents", "v2_orders"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_pg_continuation_veto_needs_no_account_reads_and_is_idempotent(
    database, pipeline_case
):
    candidate = candidate_with_policy(database, pipeline_case)
    candidate.model(trend="0.1")
    scheduler = pipeline_case[0].schedulers[0]
    scheduler.actionable_events = ("TREND_UP",)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks (consumer,signal_id,error_code) VALUES (%s,%s,%s)",
            (
                candidate.worker.scope.consumer,
                candidate.signal_id,
                "JEV_RECHECK_ADMISSION_BUDGET_UNAVAILABLE",
            ),
        )
    scheduler.context_provider = lambda *_: pytest.fail(
        "veto must not acquire account context"
    )
    candidate.worker.decide = lambda *_: pytest.fail("veto must not rerun admission")
    candidate.worker.entry_filter.reference.submit = lambda *_: pytest.fail(
        "veto must not call model"
    )
    assert scheduler.run_once(limit=1, budget_ms=4000, hot_only=True) == {
        candidate.signal_id: "IGNORED"
    }
    assert_no_orders(database)
    assert (
        candidate.worker.reject_completed_jev(
            candidate.signal_id, lease_token=str(uuid4())
        )
        is None
    )
    with database() as conn:
        row = conn.execute(
            "SELECT d.action,e.snapshot FROM v2_strategy_decisions d JOIN v2_decision_evidence e USING(evidence_ref) WHERE d.signal_id=%s",
            (candidate.signal_id,),
        ).fetchone()
        assert row[0] == "IGNORED" and row[1]["features"]["context"] == {}
        assert conn.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone() == (1,)
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )


@pytest.mark.parametrize(
    "fault",
    ["wrong_lease", "expired_lease", "pass", "sql_timeout", "deadline_during_check"],
)
def test_pg_negative_path_uncertainty_rolls_back_without_decision(
    database, pipeline_case, fault
):
    candidate = candidate_with_policy(database, pipeline_case)
    candidate.model(trend="0.8" if fault == "pass" else "0.1")
    token = lease(database, candidate, expired=fault == "expired_lease")
    if fault == "wrong_lease":
        token = str(uuid4())
    original = candidate.worker.entry_filter.terminal_veto
    if fault in {"sql_timeout", "deadline_during_check"}:

        def changed(**kwargs):
            result = original(**kwargs)
            assert result is not None
            if fault == "sql_timeout":
                kwargs["conn"].execute("SELECT pg_sleep(0.2)")
            proof = json.loads(result.evidence_json)
            proof["timing"]["deadline_ms"] = proof["timing"]["checked_at_ms"]
            return replace(result, evidence_json=canonical(proof))

        candidate.worker.entry_filter.terminal_veto = changed
    assert (
        candidate.worker.reject_completed_jev(candidate.signal_id, lease_token=token)
        is None
    )
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
    assert_no_orders(database)


def test_pg_same_lease_wake_veto_skips_only_second_account_read(
    database, pipeline_case
):
    candidate = candidate_with_policy(database, pipeline_case)
    scheduler = pipeline_case[0].schedulers[0]
    scheduler.actionable_events = ("TREND_UP",)
    original = scheduler.context_provider
    reads = []

    def context(signal):
        reads.append(True)
        assert len(reads) == 1, "model veto must not spend a second context quota"
        return original(signal)

    scheduler.context_provider = context

    def ready(**_):
        candidate.model(trend="0.1")
        return True

    candidate.worker.entry_filter.wait_pending = ready
    assert scheduler.run_once(limit=1, budget_ms=4000, hot_only=True) == {
        candidate.signal_id: "IGNORED"
    }
    assert len(reads) == 1
    assert_no_orders(database)


def test_pg_concurrent_veto_writes_one_immutable_rejection(database, pipeline_case):
    candidate = candidate_with_policy(database, pipeline_case)
    candidate.model(trend="0.1")
    token = lease(database, candidate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: candidate.worker.reject_completed_jev(
                    candidate.signal_id, lease_token=token
                ),
                range(2),
            )
        )
    assert sum(row is not None for row in outcomes) == 1
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone() == (1,)
    assert_no_orders(database)


def test_pg_failure_after_evidence_insert_rolls_back_entire_shortcut(
    database, pipeline_case
):
    candidate = candidate_with_policy(database, pipeline_case)
    candidate.model(trend="0.1")
    token = lease(database, candidate)
    with database() as conn:
        before = conn.execute("SELECT count(*) FROM v2_decision_evidence").fetchone()

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, params=()):
            if isinstance(sql, str) and sql.startswith(
                "INSERT INTO v2_strategy_decisions"
            ):
                self.conn.execute("SELECT pg_sleep(0.2)")
            return self.conn.execute(sql, params)

    @contextmanager
    def failing():
        with database() as conn:
            yield Connection(conn)

    candidate.worker._connect = failing
    assert (
        candidate.worker.reject_completed_jev(candidate.signal_id, lease_token=token)
        is None
    )
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_decision_evidence").fetchone()
            == before
        )
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
    assert_no_orders(database)


def test_pg_existing_open_is_never_replaced(database, pipeline_case):
    candidate = candidate_with_policy(database, pipeline_case)
    candidate.model()
    assert (
        candidate.worker.consume(candidate.signal_id, context=candidate.context())[
            "status"
        ]
        == "PREPARED"
    )
    token = lease(database, candidate)
    candidate.worker.entry_filter.terminal_veto = lambda **_: pytest.fail(
        "existing OPEN wins"
    )
    assert (
        candidate.worker.reject_completed_jev(candidate.signal_id, lease_token=token)
        is None
    )
    with database() as conn:
        assert conn.execute(
            "SELECT action FROM v2_strategy_decisions WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone() == ("OPEN",)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize("mode", ["slow", "stop"])
def test_pg_retry_negative_check_obeys_budget_and_drain(database, pipeline_case, mode):
    candidate = candidate_with_policy(database, pipeline_case)
    scheduler = pipeline_case[0].schedulers[0]
    scheduler.actionable_events = ("TREND_UP",)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks (consumer,signal_id,error_code) VALUES (%s,%s,%s)",
            (
                candidate.worker.scope.consumer,
                candidate.signal_id,
                "JEV_RECHECK_ADMISSION_BUDGET_UNAVAILABLE",
            ),
        )
    state = SimpleNamespace(now=0.0, stopped=False)
    scheduler.monotonic = lambda: state.now

    def negative(*_, **__):
        state.now = 5.0
        state.stopped = mode == "stop"

    candidate.worker.reject_completed_jev = negative
    scheduler.context_provider = lambda *_: pytest.fail(
        "exhausted shortcut must not start GETs"
    )
    result = scheduler.run_once(
        limit=1, budget_ms=1000, hot_only=True, stop_requested=lambda: state.stopped
    )
    assert result == {candidate.signal_id: "DRAINED" if mode == "stop" else "DEFERRED"}
    assert_no_orders(database)
