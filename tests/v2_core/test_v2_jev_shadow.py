"""Offline shadow receipts, immutable bindings and shared-budget research."""

import json
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_jev_coordinator import MemoryDatabase, MemoryState, raw_response
from test_v2_jev_entry_context import SCOPE, admission_case

from services import v2_jev_coordinator as coordination
from services import v2_jev_shadow as subject
from services.v2_jev_reference import PUBLIC_SCOPE
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.jev_advisory import AdvisoryPolicy, JevAdvisory
from v2_core.producer import market_envelope
from v2_core.runtime_policy import resolve
from v2_core.state import StateKey, StateSnapshot
from v2_core.strategy import StrategyDecision

database = database_fixture


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(subject, "BusinessState", MemoryState)
    monkeypatch.setattr(coordination, "BusinessState", MemoryState)
    db = MemoryDatabase()
    db.rows[subject.CONTROL_KEY.identity] = StateSnapshot(
        1, canonical(asdict(AdvisoryPolicy(enabled=True))), False
    )
    calls, policies = [], []
    state = SimpleNamespace(db=db, calls=calls, policies=policies)

    def query(request, *, timeout_ms):
        assert db.active_statements is None
        calls.append(deepcopy(request))
        state.timeout_ms = timeout_ms
        return state.response(request)

    def advisor(policy):
        policies.append(policy)
        return JevAdvisory(
            coordination.JevCoordinator(
                db.connect,
                scope=PUBLIC_SCOPE,
                enabled=policy.enabled,
                max_age_ms=policy.max_age_ms,
                cache_ttl_ms=policy.cache_ttl_ms,
            ),
            query,
            policy=policy,
            clock_ms=lambda: db.now,
        )

    state.response = lambda _: raw_response()
    state.stop = threading.Event()
    state.service = subject.JevShadow(
        db.connect,
        SimpleNamespace(advisor=advisor),
        scope=SCOPE,
        clock_ms=lambda: db.now,
        stop=state.stop,
        enabled=True,
    )
    monkeypatch.setattr(state.service, "_start_worker", lambda: None)
    state.args = admission_case(db.now)
    state.args["signal"]["signal"] = "PUMP_UP"
    return state


def submit(case):
    return case.service.submit(**case.args, now_ms=case.db.now)


def process(case):
    identity, encoded = case.service.queue.popleft()
    case.service._process(identity, encoded)
    case.service.pending.discard(identity)
    key = case.service._state_key(subject.RESULT_NAMESPACE, identity)
    row = case.db.rows.get(key.identity)
    return None if row is None else json.loads(row.payload_json)


def assert_neutral(value):
    assert value["execution_authorized"] is False
    assert value["admission_authorized"] is False
    assert value["risk_override"] is False
    assert value["decision_effect"] == "NONE"
    assert value["trade_link_status"] == "NOT_PROVEN_EXECUTED"
    assert value["status"] not in {"PASS", "VETO"}


def test_default_off_no_database_model_or_candidate_mutation(case):
    case.service.enabled = False
    before = deepcopy(case.args)
    receipt = submit(case)
    assert receipt["status"] == "DISABLED"
    assert receipt["coverage"] == "MISSING"
    assert case.db.connections == 0 and not case.calls and not case.service.queue
    assert case.args == before
    assert_neutral(receipt)


def test_submit_only_freezes_public_candidate_and_returns_nondurable_receipt(case):
    case.args["context"]["private_account_data"] = {"wallet": "DO_NOT_SEND"}
    before = deepcopy(case.args)
    receipt = submit(case)
    assert receipt["schema"] == "jev-shadow-receipt-v1"
    assert receipt["status"] == "SUBMITTED_NOT_DURABLE"
    assert receipt["coverage"] == "MISSING_UNTIL_DURABLE_RESULT"
    assert case.db.connections == 0 and not case.calls
    assert case.args == before
    assert receipt["candidate_digest"] == digest(canonical(asdict(before["decision"])))
    frozen = json.loads(case.service.queue[0][1])
    assert "DO_NOT_SEND" not in str(frozen)
    case.args["context"]["sources"]["s3"]["features"]["15m"]["close"] = "999"
    assert frozen["request"]["state"]["price"] == "100"
    assert_neutral(receipt)


def test_duplicate_full_busy_and_stopped_queues_never_imply_model_success(case):
    case.service.capacity = 1
    first = submit(case)
    assert submit(case) == first
    assert len(case.service.queue) == 1
    case.args["signal_id"] = str(uuid4())
    assert submit(case)["status"] == "QUEUE_FULL"
    with case.service.lock:
        assert submit(case)["status"] == "QUEUE_BUSY"
    case.stop.set()
    assert submit(case)["status"] == "STOPPED"
    assert case.db.connections == 0 and not case.calls


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "consumer",
        "symbol",
        "direction",
        "source",
        "future",
        "expired",
        "unauthorized",
        "not_open",
        "market_digest",
        "policy_digest",
    ],
)
def test_invalid_candidate_never_queues_or_changes_admission(case, defect):
    if defect == "scope":
        case.args["context"]["account_scope"]["account_id"] = "another-account"
    elif defect == "consumer":
        case.args["consumer"] = case.args["consumer"].replace("SANDBOX", "LIVE")
    elif defect == "symbol":
        case.args["context"]["symbol"] = "ETHUSDT"
    elif defect == "direction":
        case.args["decision"] = replace(case.args["decision"], side="SELL")
    elif defect == "source":
        case.args["signal"]["source"] = "tv_bridge"
    elif defect == "future":
        old = case.args["context"]["sources"]["s3"]
        case.args["context"]["sources"]["s3"] = market_envelope(
            "s3", "SANDBOX", "BTCUSDT", case.db.now + 1, old["features"]
        )
    elif defect == "expired":
        case.args["signal"]["expires_at_ms"] = case.db.now
    elif defect == "unauthorized":
        case.args["decision"] = replace(case.args["decision"], features_json="{}")
    elif defect == "not_open":
        case.args["decision"] = StrategyDecision("IGNORED", "RISK_DENIED")
    elif defect == "market_digest":
        case.args["context"]["sources"]["s3"]["features"]["15m"]["close"] = "999"
    elif defect == "policy_digest":
        case.args["config"].update(policy_version=1, policy={}, policy_digest="bad")
    before = deepcopy(case.args)
    receipt = submit(case)
    assert receipt["status"] == "INVALID_CANDIDATE"
    assert receipt["coverage"] == "MISSING"
    assert not case.service.queue and case.db.connections == 0
    assert case.args == before
    assert_neutral(receipt)


def test_live_scope_is_rejected(case):
    with pytest.raises(ValueError):
        subject.JevShadow(
            case.db.connect,
            case.service.reference,
            scope=AccountScope("BINANCE", "qa-live", "LIVE", "FUTURES"),
            clock_ms=lambda: case.db.now,
            stop=case.stop,
        )


def test_durable_research_preserves_model_clocks_and_never_claims_execution(case):
    def respond(_):
        case.db.now += 50
        return raw_response()

    case.response = respond
    receipt = submit(case)
    outcome = process(case)
    assert outcome["status"] == "OBSERVED" and outcome["coverage"] == "OBSERVED"
    assert outcome["model_result"]["hypotheses"]["trend_supported"] == "0.5"
    assert outcome["model_clocks"]["produced_at_ms"] == receipt["submitted_at_ms"] + 50
    assert outcome["model_clocks"]["completed_at_ms"] == receipt["submitted_at_ms"] + 50
    assert outcome["available_at_original_decision"] is False
    assert outcome["binding_state_id"] == receipt["binding_state_id"]
    assert outcome["candidate_digest"] == receipt["candidate_digest"]
    assert outcome["replay_semantics"] == "RESEARCH_ONLY_NOT_HISTORICAL_AUTHORIZATION"
    assert len(case.calls) == 1 and SCOPE.account_id not in canonical(case.calls[0])
    assert_neutral(outcome)
    # The original admission receipt is never rewritten as a successful model result.
    assert receipt["status"] == "SUBMITTED_NOT_DURABLE"
    assert case.db.rows[receipt["binding_state_id"]].version == 1
    assert case.db.rows[receipt["result_state_id"]].version == 1


def test_duplicate_binding_result_reuses_existing_versions_and_provider_budget(case):
    receipt = submit(case)
    item = case.service.queue[0]
    result = process(case)
    first = deepcopy(case.db.rows)
    case.db.now += 100
    case.service._process(*item)
    assert case.db.rows == first and len(case.calls) == 1
    assert result["shared_result_version"] == 2
    assert case.db.rows[receipt["result_state_id"]].version == 1


@pytest.mark.parametrize(
    "reason",
    [
        "control_disabled",
        "deadline",
        "timeout",
        "late_model",
        "missing",
        "policy_changed",
    ],
)
def test_nonresults_remain_distinct_and_never_pass(case, reason):
    receipt = submit(case)
    expected = "MISSING"
    if reason == "control_disabled":
        case.db.rows[subject.CONTROL_KEY.identity] = StateSnapshot(
            2, canonical(asdict(AdvisoryPolicy())), False
        )
        expected = "DISABLED"
    elif reason == "deadline":
        case.db.now = receipt["deadline_ms"]
        expected = "LATE"
    elif reason == "timeout":

        def timeout(_):
            raise TimeoutError()

        case.response = timeout
    elif reason == "late_model":

        def late(_):
            case.db.now += 1600
            return raw_response()

        case.response = late
        expected = "LATE"
    elif reason == "missing":
        case.db.allow_lock = False
    else:

        def changed(_):
            original = case.db.rows[subject.CONTROL_KEY.identity]
            case.db.rows[subject.CONTROL_KEY.identity] = replace(original, version=2)
            return raw_response()

        case.response = changed
        expected = "POLICY_CHANGED"
    result = process(case)
    assert result["status"] == expected
    assert result["coverage"] == "MISSING"
    assert_neutral(result)
    if reason in {"control_disabled", "deadline", "missing"}:
        assert not case.calls


def test_frozen_runtime_policy_version_cannot_change_away_and_back(case):
    values = resolve()
    case.args["config"].update(
        policy=values, policy_version=1, policy_digest=digest(canonical(values))
    )
    key = StateKey(**asdict(SCOPE), namespace="runtime-policy-v1", key="active")
    case.db.rows[key.identity] = StateSnapshot(2, canonical({"values": values}), False)
    submit(case)
    result = process(case)
    assert result["status"] == "POLICY_CHANGED"
    assert not case.calls


def test_default_runtime_policy_version_zero_is_supported(case):
    values = resolve()
    case.args["config"].update(
        policy=values, policy_version=0, policy_digest=digest(canonical(values))
    )
    submit(case)
    assert process(case)["status"] == "OBSERVED"


def test_binding_write_failure_prevents_provider_and_receipt_stays_missing(case):
    receipt = submit(case)
    case.db.fail_namespace = subject.BINDING_NAMESPACE
    with pytest.raises(ValueError):
        process(case)
    assert not case.calls and receipt["binding_state_id"] not in case.db.rows
    assert receipt["coverage"] == "MISSING_UNTIL_DURABLE_RESULT"


def test_result_write_failure_never_relabels_main_receipt(case):
    receipt = submit(case)
    case.db.fail_namespace = subject.RESULT_NAMESPACE
    with pytest.raises(ValueError):
        process(case)
    assert len(case.calls) == 1
    assert receipt["result_state_id"] not in case.db.rows
    assert receipt["status"] == "SUBMITTED_NOT_DURABLE"


def test_shared_public_snapshot_reuses_one_budget_across_distinct_candidates(case):
    submit(case)
    first = process(case)
    case.args["signal_id"] = str(uuid4())
    case.args["decision"] = replace(case.args["decision"], quantity="2")
    submit(case)
    second = process(case)
    assert len(case.calls) == 1
    assert first["shared_result_state_id"] == second["shared_result_state_id"]
    assert first["binding_state_id"] != second["binding_state_id"]
    assert first["candidate_digest"] != second["candidate_digest"]
    assert first["model_clocks"] == second["model_clocks"]


def test_background_database_delay_does_not_delay_submit_or_admission(
    case, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    main = threading.current_thread()

    @contextmanager
    def slow_connect():
        assert threading.current_thread() is not main
        entered.set()
        assert release.wait(2)
        with case.db.connect() as conn:
            yield conn

    case.service.connect = slow_connect
    monkeypatch.setattr(
        case.service,
        "_start_worker",
        lambda: subject.JevShadow._start_worker(case.service),
    )
    receipt = submit(case)
    try:
        assert receipt["status"] == "SUBMITTED_NOT_DURABLE"
        assert entered.wait(1) and not release.is_set()
        assert case.args["decision"].action == "OPEN"
    finally:
        case.stop.set()
        release.set()
        case.service.thread.join(timeout=2)
    assert not case.service.thread.is_alive()


def test_worker_start_failure_has_no_orphan_pending_item(case, monkeypatch):
    def fail():
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(case.service, "_start_worker", fail)
    assert submit(case)["status"] == "SUBMISSION_UNAVAILABLE"
    assert not case.service.queue and not case.service.pending and not case.calls


@pytest.mark.parametrize("field", ["request", "model", "clock", "usage"])
def test_shared_result_tampering_is_missing_coverage(case, field):
    submit(case)
    prior = process(case)
    key = prior["shared_result_state_id"]
    original = case.db.rows[key]
    record = json.loads(original.payload_json)
    if field == "request":
        record["request"]["state"]["symbol"] = "ETHUSDT"
    elif field == "model":
        record["result"]["model"] = "different-model"
    elif field == "clock":
        record["result"]["produced_at_ms"] = case.db.now + 1
    else:
        record["charged_input_tokens"] = 1
    case.db.rows[key] = replace(original, payload_json=canonical(record))
    case.args["signal_id"] = str(uuid4())
    submit(case)
    result = process(case)
    assert result["status"] == "INVALID_RESULT" and result["coverage"] == "MISSING"
    assert result["model_result"] is None
    assert_neutral(result)


def test_cached_result_must_fit_current_bound_model_age(case):
    submit(case)
    process(case)
    case.db.now += 2000
    case.args["signal_id"] = str(uuid4())
    case.db.rows[subject.CONTROL_KEY.identity] = StateSnapshot(
        2,
        canonical(
            asdict(AdvisoryPolicy(enabled=True, max_age_ms=1000, cache_ttl_ms=1000))
        ),
        False,
    )
    submit(case)
    result = process(case)
    assert result["status"] == "INVALID_RESULT" and result["coverage"] == "MISSING"
    assert len(case.calls) == 1


def test_pg_shadow_binding_and_result_are_versioned_and_share_coordinator(
    database, monkeypatch
):
    from v2_core.state import BusinessState

    now = time.time_ns() // 1000000
    calls = []
    model_policy = AdvisoryPolicy(enabled=True)
    BusinessState(database).change(
        subject.CONTROL_KEY,
        expected_version=0,
        request_key="qa-shadow-control",
        payload=asdict(model_policy),
        reason="isolated shadow QA",
    )

    def advisor(policy):
        def query(request, *, timeout_ms):
            calls.append(request)
            return raw_response()

        return JevAdvisory(
            coordination.JevCoordinator(database, scope=PUBLIC_SCOPE, enabled=True),
            query,
            policy=policy,
            clock_ms=lambda: time.time_ns() // 1000000,
        )

    service = subject.JevShadow(
        database,
        SimpleNamespace(advisor=advisor),
        scope=SCOPE,
        enabled=True,
        clock_ms=lambda: time.time_ns() // 1000000,
        stop=threading.Event(),
        max_wait_ms=10000,
    )
    monkeypatch.setattr(service, "_start_worker", lambda: None)
    args = admission_case(now)
    args["signal"]["signal"] = "PUMP_UP"
    receipt = service.submit(**args, now_ms=now, strategy_version="qa-shadow-v1")
    item = service.queue.popleft()
    service._process(*item)
    service._process(*item)
    store = BusinessState(database)
    binding = store.read(service._state_key(subject.BINDING_NAMESPACE, item[0]))
    outcome = store.read(service._state_key(subject.RESULT_NAMESPACE, item[0]))
    value = json.loads(outcome.payload_json)
    assert binding.version == outcome.version == 1
    assert value["status"] == "OBSERVED" and len(calls) == 1
    assert value["candidate_digest"] == receipt["candidate_digest"]
    assert value["shared_result_version"] == 2
    assert (
        json.loads(binding.payload_json)["candidate"]["strategy_version"]
        == "qa-shadow-v1"
    )
    assert_neutral(value)
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_state_history WHERE state_id IN (%s,%s)",
                (receipt["binding_state_id"], receipt["result_state_id"]),
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
