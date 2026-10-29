"""Delayed model claims cannot be bypassed by the unclaimed queue timeout."""

import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

from test_v2_intent_admission import database as database_fixture
from test_v2_jev_entry_context import (
    POLICY,
    SCOPE,
    admission_case,
    binding,
    claim,
    model_response,
    refresh,
)
from test_v2_jev_entry_context import case as context_case_fixture
from test_v2_jev_entry_gate import case as gate_case_fixture

from services import v2_jev_entry_context as subject
from services.v2_jev_coordinator import JevCoordinator
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from v2_core.jev_advisory import AdvisoryPolicy, _result, parse_response
from v2_core.jev_entry_gate import evaluate_entry_gate
from v2_core.state import BusinessState

database = database_fixture
context_case = context_case_fixture
gate_case = gate_case_fixture


def test_explicit_inflight_never_uses_unclaimed_queue_fallback(gate_case):
    gate_case.update(
        policy=replace(gate_case["policy"], unavailable_action="BASELINE"),
        advisory={"status": "FALLBACK", "reason": "MODEL_IN_FLIGHT"},
        now_ms=gate_case["started_at_ms"] + 1600,
        baseline_valid_until_ms=gate_case["started_at_ms"] + 10000,
    )
    result = evaluate_entry_gate(**gate_case)
    assert result.status == "PENDING" and result.decision is None


def test_delayed_memory_claim_remains_pending_then_real_veto(context_case, monkeypatch):
    case = context_case
    case.service = subject.JevEntryContext(
        case.db.connect,
        case.reference,
        scope=SCOPE,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )
    started = case.db.now
    assert case.service.assess(**case.args).status == "PENDING"
    _, original, _ = binding(case)
    case.db.now = started + 1000
    coordinator, prepared, grant = claim(case)
    assert grant["status"] == "GRANTED"
    case.db.now = started + 1600
    refresh(case.args, case.db.now)
    assert case.service.assess(**case.args).status == "PENDING"
    elapsed = SimpleNamespace(seconds=0.0)

    def pause(seconds):
        assert case.db.active_statements is None
        elapsed.seconds += seconds
        case.db.now += round(seconds * 1000)

    monkeypatch.setattr(
        subject, "time", SimpleNamespace(monotonic=lambda: elapsed.seconds, sleep=pause)
    )
    assert not case.service.wait_pending(
        signal_id=case.args["signal_id"],
        consumer=case.args["consumer"],
        max_wait_ms=100,
    )
    assert elapsed.seconds >= 0.1
    result = _result(
        "ADVISORY_READY",
        "MODEL_OBSERVED",
        prepared,
        now_ms=case.db.now,
        expires_at_ms=case.db.now + 30000,
        response=parse_response(model_response(trend="0.1")),
    )
    assert coordinator.complete(
        request_key=prepared["request_key"],
        claim_id=grant["claim_id"],
        result=result,
        now_ms=case.db.now,
        expires_at_ms=result["expires_at_ms"],
        charged_input_tokens=900,
    )
    refresh(case.args, case.db.now)
    assessed = case.service.assess(**case.args)
    assert assessed.status == "VETO" and assessed.reason == "JEV_TREND_UNSUPPORTED"
    assert binding(case)[1] == original and len(case.queued) == 1


def test_real_pg_delayed_claim_has_own_deadline_then_veto_without_intents(database):
    def now():
        with database() as conn:
            return conn.execute(
                "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
            ).fetchone()[0]

    def wait_until(stamp):
        remaining = (stamp - now()) / 1000
        if remaining > 0:
            time.sleep(remaining)

    store = BusinessState(database)
    assert (
        store.change(
            CONTROL_KEY,
            expected_version=0,
            request_key=str(uuid4()),
            payload=asdict(AdvisoryPolicy(enabled=True)),
            reason="isolated inflight QA",
        ).code
        == "APPLIED"
    )
    queued = []
    service = subject.JevEntryContext(
        database,
        SimpleNamespace(submit=lambda value: queued.append(value)),
        scope=SCOPE,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )
    case = admission_case(now())
    assert service.assess(**case).status == "PENDING"
    key = service.binding_key(case["consumer"], case["signal_id"])
    original = store.read(key)
    bound = json.loads(original.payload_json)
    wait_until(bound["started_at_ms"] + 1000)
    coordinator = JevCoordinator(database, scope=PUBLIC_SCOPE, enabled=True)
    from v2_core.jev_advisory import prepare_request

    claimed_at = now()
    prepared = prepare_request(
        bound["public_features"], now_ms=claimed_at, max_age_ms=60000
    )
    grant = coordinator.claim(
        request_key=prepared["request_key"],
        request=prepared["request"],
        now_ms=claimed_at,
        deadline_ms=claimed_at + 1500,
    )
    assert grant["status"] == "GRANTED"
    wait_until(bound["started_at_ms"] + 1600)
    refresh(case, now())
    assert service.assess(**case).status == "PENDING"
    assert not service.wait_pending(
        signal_id=case["signal_id"], consumer=case["consumer"], max_wait_ms=50
    )
    finished = now()
    result = _result(
        "ADVISORY_READY",
        "MODEL_OBSERVED",
        prepared,
        now_ms=finished,
        expires_at_ms=finished + 30000,
        response=parse_response(model_response(trend="0.1")),
    )
    assert coordinator.complete(
        request_key=prepared["request_key"],
        claim_id=grant["claim_id"],
        result=result,
        now_ms=finished,
        expires_at_ms=result["expires_at_ms"],
        charged_input_tokens=900,
    )
    refresh(case, now())
    assessed = service.assess(**case)
    assert assessed.status == "VETO" and assessed.reason == "JEV_TREND_UNSUPPORTED"
    assert store.read(key) == original and len(queued) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
