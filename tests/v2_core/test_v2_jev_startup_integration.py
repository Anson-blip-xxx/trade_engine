"""Known pre-provider failures: durable quota, savepoints and fresh risk gates.

No SDK/network or production database. PG tests use the shared isolated fixture.
"""

import json
import sys
from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_jev_coordinator import (
    NOW,
    SCOPE,
    budget_value,
    claim,
    completion,
    coordinator,
    prepared,
)
from test_v2_jev_coordinator import memory as memory_fixture
from test_v2_jev_entry_context import POLICY, binding, refresh
from test_v2_jev_entry_context import SCOPE as ENTRY_SCOPE
from test_v2_jev_entry_context import case as context_case_fixture

from services import v2_jev_coordinator as subject
from services import v2_jev_transport as transport
from services.v2_jev_entry_context import JevEntryContext
from services.v2_jev_reference import PUBLIC_SCOPE
from tests.support.v2_admission import database as database_fixture
from v2_core.jev_advisory import RESERVE_TOKENS, AdvisoryPolicy, JevAdvisory
from v2_core.jev_health import MODEL_HEALTH_KEY
from v2_core.state import BusinessState
from v2_core.strategy import StrategyDecision

memory = memory_fixture
context_case = context_case_fixture
database = database_fixture
LOCAL_STARTUP = "MODEL_LOCAL_STARTUP_UNAVAILABLE"


def startup_completion(request, grant, now):
    return completion(request, grant, now=now, tokens=0, reason=LOCAL_STARTUP)


def state_payload(memory, key):
    return json.loads(memory.rows[key.identity].payload_json)


def test_startup_refund_exact_once_leaves_minute_attempt_and_tombstone(memory):
    store, request = coordinator(memory, requests_per_minute=1), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = startup_completion(request, granted, memory.now)
    assert store.complete(**args) is True
    saved = deepcopy(memory.rows)
    assert store.complete(**args) is False
    assert memory.rows == saved
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 0
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 1
    value = state_payload(memory, store.request_state_key(request["request_key"]))
    assert value["status"] == "COMPLETED"
    assert value["charged_input_tokens"] == 0
    assert value["result"]["reason"] == LOCAL_STARTUP
    assert store.lookup(request_key=request["request_key"], now_ms=memory.now) is None
    assert claim(store, request, now=memory.now) == {"status": "BUSY"}
    assert claim(store, prepared(memory.now, "ETHUSDT")) == {
        "status": "BUDGET_EXHAUSTED"
    }
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 0


@pytest.mark.parametrize("tokens", [None, 1, RESERVE_TOKENS, True, False, 0.0])
def test_startup_requires_literal_known_zero_and_rejection_keeps_claim(memory, tokens):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    before = deepcopy(memory.rows)
    args = startup_completion(request, granted, memory.now)
    with pytest.raises(ValueError):
        store.complete(**{**args, "charged_input_tokens": tokens})
    assert memory.rows == before
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


@pytest.mark.parametrize(
    "reason",
    ["MODEL_UNKNOWN_FAILURE", "MODEL_CONFIGURATION_UNAVAILABLE", "MODEL_TIMEOUT"],
)
def test_unknown_configuration_and_timeout_never_claim_zero_usage(memory, reason):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = completion(request, granted, now=memory.now, tokens=0, reason=reason)
    before = deepcopy(memory.rows)
    with pytest.raises(ValueError, match="JEV_COMPLETION_INVALID"):
        store.complete(**args)
    assert memory.rows == before
    assert store.complete(**{**args, "charged_input_tokens": None}) is True
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 1


def test_startup_cross_utc8_midnight_refunds_original_day_only(memory):
    midnight = subject._window_starts(NOW)[1] + 86400000
    memory.now = midnight - 1000
    store, request = coordinator(memory), prepared(memory.now)
    granted = claim(store, request)
    memory.now = midnight + 100
    assert claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "GRANTED"
    assert store.complete(**startup_completion(request, granted, memory.now))
    assert budget_value(memory, subject.DAY_NAMESPACE, midnight - 1)["used"] == 0
    assert (
        budget_value(memory, subject.DAY_NAMESPACE, midnight)["used"] == RESERVE_TOKENS
    )
    assert budget_value(memory, subject.MINUTE_NAMESPACE, midnight - 1)["used"] == 1
    assert budget_value(memory, subject.MINUTE_NAMESPACE, midnight)["used"] == 1


def test_health_cas_failure_isolated_from_refund_and_completed_result(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    memory.fail_namespace = MODEL_HEALTH_KEY.namespace
    assert store.complete(**startup_completion(request, granted, memory.now)) is True
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 0
    assert (
        state_payload(memory, store.request_state_key(request["request_key"]))["status"]
        == "COMPLETED"
    )
    assert MODEL_HEALTH_KEY.identity not in memory.rows

    def forbidden_model(*args, **kwargs):
        pytest.fail("diagnostic projection failure must not repeat provider request")

    result = JevAdvisory(
        store,
        forbidden_model,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: memory.now,
    ).evaluate(request["request"]["state"])
    assert result["reason"] == "BUSY"
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 0
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 1


@pytest.mark.parametrize("late", [False, True])
def test_typed_startup_through_process_advisory_durable_entry_baseline_only(
    context_case, late
):
    case = context_case

    def gate(action):
        return JevEntryContext(
            case.db.connect,
            case.reference,
            scope=ENTRY_SCOPE,
            policy=replace(POLICY, unavailable_action=action),
        )

    case.service = gate("BASELINE")
    assert case.service.assess(**case.args).status == "PENDING"
    _, original_binding, bound = binding(case)
    calls = []

    def query(request, *, timeout_ms):
        calls.append("startup")
        if late:
            case.db.now += timeout_ms
        proof = {
            "schema": "jev-local-startup-failure-v1",
            "stage": "PURE_CONTRACT_IMPORT",
            "provider_request_started": False,
        }
        script = "import sys;print(" + repr(json.dumps(proof)) + ");sys.exit(7)"
        return transport._bounded_process(
            [sys.executable, "-I", "-c", script], b"{}", 1000
        )

    model = JevAdvisory(
        subject.JevCoordinator(case.db.connect, scope=PUBLIC_SCOPE, enabled=True),
        query,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: case.db.now,
    )
    result = model.evaluate(bound["public_features"])
    assert result["reason"] == LOCAL_STARTUP
    assert result["status"] == "FALLBACK"
    assert result["usage"] is None and result["hypotheses"] is None
    assert budget_value(case.db, subject.DAY_NAMESPACE)["used"] == 0
    assert budget_value(case.db, subject.MINUTE_NAMESPACE)["used"] == 1
    refresh(case.args, case.db.now)
    assert gate("SKIP").assess(**case.args).status == "SKIP"
    assessed = gate("BASELINE").assess(**case.args)
    assert assessed.status == "FALLBACK"
    assert assessed.decision is case.args["decision"]
    proof = json.loads(assessed.evidence_json)
    assert proof["fallback_cause"] == LOCAL_STARTUP
    assert proof["execution_authorized"] is False and proof["risk_override"] is False
    assert proof["valid_until_ms"] <= case.args["context"]["valid_until_ms"]
    assert binding(case)[1] == original_binding
    assert len(case.queued) == 1
    assert model.evaluate(bound["public_features"])["reason"] == "BUSY"
    assert calls == ["startup"]

    rejected = StrategyDecision("IGNORED", "HARD_RISK_LIMIT")
    assessment = gate("BASELINE").assess(**{**case.args, "decision": rejected})
    assert assessment.status == "VETO"
    assert assessment.reason == "PREEXISTING_REJECTION"
    assert assessment.decision is rejected
    assert len(case.queued) == 1 and calls == ["startup"]


def pg_payload(database, key):
    return json.loads(BusinessState(database).read(key).payload_json)


def test_pg_startup_refund_and_idempotence_keep_request_attempt(database):
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    granted = claim(store, request)
    with database() as conn:
        finished = subject._clock(conn)
    args = startup_completion(request, granted, finished)
    assert store.complete(**args) is True
    assert store.complete(**args) is False
    minute, day = subject._window_starts(now)
    assert (
        pg_payload(database, subject._budget_key(subject.DAY_NAMESPACE, day))["used"]
        == 0
    )
    assert (
        pg_payload(database, subject._budget_key(subject.MINUTE_NAMESPACE, minute))[
            "used"
        ]
        == 1
    )
    saved = pg_payload(database, store.request_state_key(request["request_key"]))
    assert saved["status"] == "COMPLETED" and saved["charged_input_tokens"] == 0
    assert pg_payload(database, MODEL_HEALTH_KEY)["consecutive_failures"] == 1


def test_pg_health_sql_error_savepoint_preserves_result_no_provider_retry(
    database, monkeypatch
):
    original_write = subject._write
    failed_health = []

    def faulty_health(store, key, version, payload, operation):
        if key == MODEL_HEALTH_KEY:
            failed_health.append(True)
            with store._connect() as conn:
                conn.execute("SELECT 1 / 0")
        return original_write(store, key, version, payload, operation)

    monkeypatch.setattr(subject, "_write", faulty_health)
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    granted = claim(store, request)
    with database() as conn:
        finished = subject._clock(conn)
    assert store.complete(**startup_completion(request, granted, finished)) is True
    assert failed_health == [True]
    assert BusinessState(database).read(MODEL_HEALTH_KEY) is None
    assert (
        pg_payload(database, store.request_state_key(request["request_key"]))["status"]
        == "COMPLETED"
    )
    day = subject._window_starts(now)[1]
    assert (
        pg_payload(database, subject._budget_key(subject.DAY_NAMESPACE, day))["used"]
        == 0
    )

    def forbidden_model(*args, **kwargs):
        pytest.fail("SQL diagnostic failure may not authorize provider retry")

    result = JevAdvisory(
        store,
        forbidden_model,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: finished,
    ).evaluate(request["request"]["state"])
    assert result["reason"] == "BUSY"


def test_pg_cross_midnight_startup_refund_does_not_touch_new_day(database, monkeypatch):
    midnight = subject._window_starts(NOW)[1] + 86400000
    clock = {"now": midnight - 1000}
    monkeypatch.setattr(subject, "_clock", lambda conn: clock["now"])
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    request = prepared(clock["now"])
    granted = claim(store, request)
    clock["now"] = midnight + 100
    assert claim(store, prepared(clock["now"], "ETHUSDT"))["status"] == "GRANTED"
    assert store.complete(**startup_completion(request, granted, clock["now"]))
    old_day = subject._budget_key(subject.DAY_NAMESPACE, midnight - 86400000)
    new_day = subject._budget_key(subject.DAY_NAMESPACE, midnight)
    assert pg_payload(database, old_day)["used"] == 0
    assert pg_payload(database, new_day)["used"] == RESERVE_TOKENS
