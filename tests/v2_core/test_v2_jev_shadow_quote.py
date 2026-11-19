"""Frozen public premiumIndex research: no live provider, quote or account reads."""

import json
import threading
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_jev_coordinator import raw_response
from test_v2_jev_entry_context import SCOPE, admission_case
from test_v2_jev_shadow import case as case_fixture
from test_v2_jev_shadow import process, submit
from test_v2_jev_structure_context import v6_envelope as v6_envelope_fixture

from services import v2_jev_coordinator as coordination
from services import v2_jev_shadow as subject
from services.v2_jev_reference import PUBLIC_SCOPE, public_features
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical, digest
from v2_core.jev_advisory import AdvisoryPolicy, JevAdvisory, prepare_request
from v2_core.state import BusinessState, StateKey

database = database_fixture
case = case_fixture
v6_envelope = v6_envelope_fixture


def quote_args(envelope, now):
    args = admission_case(now)
    args["signal"]["signal"] = "PUMP_UP"
    key = StateKey(
        **asdict(SCOPE), namespace="directional-account-context-v1", key=str(uuid4())
    )
    quote_at = now - 1325
    args["context"].update(
        sources={"s3": deepcopy(envelope)},
        directional={
            "market": deepcopy(envelope["features"]),
            "sizing": {"sizing_mark_price": "119.9", "balance": "PRIVATE_BALANCE"},
            "funding_rate": "0.0001",
        },
        account_evidence={
            "state_id": key.identity,
            "version": 1,
            "sizing_mark_observed_at_ms": quote_at,
        },
        freshness_policy={"s3": {"scope": "SYMBOL", "max_age_ms": 90000}},
    )
    payload = {
        "observation_id": key.key,
        "account_scope": asdict(SCOPE),
        "symbol": "BTCUSDT",
        "started_at_ms": now - 2000,
        "finished_at_ms": now - 10,
        "sizing_budget_inputs": {"sizing_mark_price": "119.90000000"},
        "market_sources": {"contract_environment": "SANDBOX"},
        "market_observed_at_ms": {"premium_index": quote_at},
        "response_digests": {"premium_index": "a" * 64},
        "funding_rate": "0.00010000",
        "balance": "PRIVATE_BALANCE",
    }
    return args, key, payload


@pytest.fixture
def quote_case(case, v6_envelope, monkeypatch):
    case.db.now = v6_envelope["observed_at"] + 63325
    case.args, case.quote_key, case.quote_payload = quote_args(v6_envelope, case.db.now)
    case.quote_row = [1, case.quote_payload, False, asdict(case.quote_key)]
    execute = case.db.execute

    def with_quote(sql, params=()):
        if (
            sql
            == "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s"
        ):
            assert case.db.active_statements is not None
            assert params == (case.quote_key.identity,)
            case.db.active_statements.append(sql)
            return SimpleNamespace(fetchone=lambda: case.quote_row)
        return execute(sql, params)

    monkeypatch.setattr(case.db, "execute", with_quote)
    return case


def test_natural_63_second_candle_keeps_quote_candle_and_indicator_clocks(quote_case):
    case = quote_case
    original = deepcopy(case.args)
    receipt = submit(case)
    assert receipt["status"] == "SUBMITTED_NOT_DURABLE"
    assert case.db.connections == 0 and not case.calls
    frozen = json.loads(case.service.queue[0][1])
    state = frozen["request"]["state"]
    assert state["observed_at_ms"] == case.db.now - 1325
    assert state["candle_observed_at_ms"] == case.db.now - 63325
    assert state["indicator_closed_at_ms"] == 1440 * 60000
    assert state["price"] == "119.9" and state["candle_price"] == "120"
    assert len(canonical(frozen["request"]).encode()) < 4096
    for question in frozen["request"]["questions"].values():
        assert "premiumIndex mark at observed_at_ms" in question["instructions"]
        assert "candle_observed_at_ms" in question["instructions"]
        assert "indicator_closed_at_ms" in question["instructions"]
        assert "price is the latest closed 1m" not in question["instructions"]
    assert case.args == original
    outcome = process(case)
    assert outcome["status"] == "OBSERVED"
    assert len(case.calls) == 1
    model_input = canonical(case.calls[0])
    assert "PRIVATE_BALANCE" not in model_input
    assert (
        SCOPE.account_id not in model_input
        and case.quote_key.identity not in model_input
    )
    binding = json.loads(case.db.rows[receipt["binding_state_id"]].payload_json)
    assert binding["public_quote_verification"]["status"] == "VERIFIED"
    assert binding["public_quote_verification"]["account_state_digest"] == digest(
        canonical(case.quote_payload)
    )
    assert outcome["execution_authorized"] is False
    assert outcome["decision_effect"] == "NONE"


def test_same_original_v6_candle_remains_expired_for_original_gate(quote_case):
    context = quote_case.args["context"]
    old = public_features(
        context["sources"]["s3"],
        target="BTCUSDT",
        direction="LONG",
        context=context,
        now_ms=quote_case.db.now,
    )
    with pytest.raises(ValueError, match="JEV_MARKET_SNAPSHOT_EXPIRED"):
        prepare_request(old, now_ms=quote_case.db.now, max_age_ms=60000)
    submit(quote_case)
    frozen = json.loads(quote_case.service.queue[0][1])
    original_candle = public_features(
        context["sources"]["s3"], target="BTCUSDT", direction="LONG"
    )
    at_candle = prepare_request(
        original_candle, now_ms=old["observed_at_ms"] + 2000, max_age_ms=60000
    )
    assert frozen["request_key"] != at_candle["request_key"]
    # Only the explicit quote projection has different units and cache identity.
    assert "public_quote_contract" not in at_candle["request"]["state"]
    assert (
        "price is the latest closed 1m"
        in at_candle["request"]["questions"]["trend_supported"]["instructions"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "future",
        "stale",
        "invalid_price",
        "bool_version",
        "missing_state",
        "context_future",
    ],
)
def test_unproved_or_stale_frozen_quote_never_queues(quote_case, change):
    case = quote_case
    evidence = case.args["context"]["account_evidence"]
    if change == "future":
        evidence["sizing_mark_observed_at_ms"] = case.db.now + 1
    elif change == "stale":
        evidence["sizing_mark_observed_at_ms"] = case.db.now - 15000
    elif change == "invalid_price":
        case.args["context"]["directional"]["sizing"]["sizing_mark_price"] = "0"
    elif change == "bool_version":
        evidence["version"] = True
    elif change == "missing_state":
        evidence.pop("state_id")
    else:
        case.args["context"]["assembled_at"] = (
            evidence["sizing_mark_observed_at_ms"] - 1
        )
    receipt = submit(case)
    assert receipt["status"] == "INVALID_CANDIDATE" and receipt["coverage"] == "MISSING"
    assert not case.service.queue and case.db.connections == 0 and not case.calls


@pytest.mark.parametrize("age", [90000, 90001])
def test_fresh_mark_never_rescues_expired_research_candle(case, v6_envelope, age):
    case.db.now = v6_envelope["observed_at"] + age
    case.args, _, _ = quote_args(v6_envelope, case.db.now)
    receipt = submit(case)
    assert receipt["status"] == "INVALID_CANDIDATE"
    assert receipt["reason"] == "JEV_SHADOW_CANDLE_SNAPSHOT_EXPIRED"
    assert receipt["coverage"] == "MISSING" and not case.service.queue


def test_existing_stricter_market_context_freshness_is_not_relaxed(quote_case):
    quote_case.args["context"]["freshness_policy"]["s3"]["max_age_ms"] = 60000
    assert submit(quote_case)["reason"] == "JEV_SHADOW_CANDLE_SNAPSHOT_EXPIRED"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "version",
        "deleted",
        "scope",
        "symbol",
        "environment",
        "time",
        "price",
        "digest",
        "future_finished",
        "funding",
    ],
)
def test_background_requires_exact_durable_public_quote_before_any_model(
    quote_case, change
):
    case = quote_case
    receipt = submit(case)
    if change == "missing":
        case.quote_row = None
    elif change == "version":
        case.quote_row[0] = 2
    elif change == "deleted":
        case.quote_row[2] = True
    elif change == "scope":
        case.quote_row[3]["account_id"] = "other-account"
    elif change == "symbol":
        case.quote_payload["symbol"] = "ETHUSDT"
    elif change == "environment":
        case.quote_payload["market_sources"]["contract_environment"] = "LIVE"
    elif change == "time":
        case.quote_payload["market_observed_at_ms"]["premium_index"] += 1
    elif change == "price":
        case.quote_payload["sizing_budget_inputs"]["sizing_mark_price"] = "120"
    elif change == "digest":
        case.quote_payload["response_digests"]["premium_index"] = "invalid"
    elif change == "future_finished":
        case.quote_payload["finished_at_ms"] = case.db.now + 1
    else:
        case.quote_payload["funding_rate"] = "0.0002"
    result = process(case)
    assert (
        result["status"] == "MISSING"
        and result["reason"] == "PUBLIC_QUOTE_EVIDENCE_INVALID"
    )
    assert result["coverage"] == "MISSING" and result["model_result"] is None
    assert not case.calls
    assert receipt["status"] == "SUBMITTED_NOT_DURABLE"
    assert case.db.rows[receipt["binding_state_id"]].version == 1


def test_model_control_age_and_indicator_age_remain_strict(quote_case):
    submit(quote_case)
    state = json.loads(quote_case.service.queue[0][1])["request"]["state"]
    with pytest.raises(ValueError, match="JEV_MARKET_SNAPSHOT_EXPIRED"):
        prepare_request(state, now_ms=quote_case.db.now, max_age_ms=1000)
    # Fresh quote/candle cannot make a prior quarter's indicators fresh.
    old = deepcopy(state)
    old.update(
        observed_at_ms=1456 * 60000 + 1,
        candle_observed_at_ms=1454 * 60000 + 60000,
        indicator_closed_at_ms=1440 * 60000,
        funding_rate=None,
        funding_observed_at_ms=None,
    )
    with pytest.raises(ValueError, match="JEV_INDICATOR_TIME_INVALID"):
        prepare_request(old, now_ms=old["observed_at_ms"], max_age_ms=60000)


@pytest.mark.parametrize(
    "field", ["public_quote_contract", "candle_observed_at_ms", "candle_price"]
)
def test_partial_quote_contract_is_never_silently_treated_as_legacy(quote_case, field):
    submit(quote_case)
    state = json.loads(quote_case.service.queue[0][1])["request"]["state"]
    del state[field]
    with pytest.raises(ValueError, match="JEV_SHADOW_QUOTE_CONTRACT_INVALID"):
        prepare_request(state, now_ms=quote_case.db.now, max_age_ms=60000)


def test_missing_quote_has_safe_stale_reason_without_arbitrary_exception_text(
    quote_case, monkeypatch
):
    del quote_case.args["context"]["directional"]["sizing"]["sizing_mark_price"]
    assert submit(quote_case)["reason"] == "JEV_MARKET_SNAPSHOT_EXPIRED"

    def malicious(*args, **kwargs):
        raise ValueError("PRIVATE_MODEL_OR_ACCOUNT_TEXT")

    monkeypatch.setattr(quote_case.service, "_freeze", malicious)
    receipt = submit(quote_case)
    assert receipt["reason"] == "JEV_SHADOW_IDENTITY_INVALID"
    assert "PRIVATE_MODEL_OR_ACCOUNT_TEXT" not in canonical(receipt)


def test_pg_quote_proof_and_coordinator_result_use_original_clocks(
    database, v6_envelope, monkeypatch
):
    now = v6_envelope["observed_at"] + 63325
    args, key, account = quote_args(v6_envelope, now)
    store = BusinessState(database)
    for state_key, payload in (
        (subject.CONTROL_KEY, asdict(AdvisoryPolicy(enabled=True))),
        (key, account),
    ):
        store.change(
            state_key,
            expected_version=0,
            request_key="qa-quote",
            payload=payload,
            reason="isolated quote QA",
        )
    clock, calls = [now], []
    monkeypatch.setattr(subject, "_clock", lambda _: clock[0])
    monkeypatch.setattr(coordination, "_clock", lambda _: clock[0])

    def advisor(policy):
        def query(request, *, timeout_ms):
            calls.append(request)
            clock[0] += 25
            return raw_response()

        return JevAdvisory(
            coordination.JevCoordinator(database, scope=PUBLIC_SCOPE, enabled=True),
            query,
            policy=policy,
            clock_ms=lambda: clock[0],
        )

    service = subject.JevShadow(
        database,
        SimpleNamespace(advisor=advisor),
        scope=SCOPE,
        enabled=True,
        clock_ms=lambda: clock[0],
        stop=threading.Event(),
    )
    monkeypatch.setattr(service, "_start_worker", lambda: None)
    receipt = service.submit(**args, now_ms=now)
    item = service.queue.popleft()
    service._process(*item)
    service._process(*item)
    binding = store.read(service._state_key(subject.BINDING_NAMESPACE, item[0]))
    result = store.read(service._state_key(subject.RESULT_NAMESPACE, item[0]))
    outcome = json.loads(result.payload_json)
    assert binding.version == result.version == 1
    assert outcome["status"] == "OBSERVED" and len(calls) == 1
    assert outcome["candidate_digest"] == receipt["candidate_digest"]
    assert outcome["model_clocks"]["completed_at_ms"] == now + 25
    assert outcome["available_at_original_decision"] is False
    assert (
        json.loads(binding.payload_json)["public_quote_verification"]["status"]
        == "VERIFIED"
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
