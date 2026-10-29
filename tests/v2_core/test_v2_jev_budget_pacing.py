"""Local daily spending is paced, never expanded or mistaken for model approval."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from test_v2_jev_coordinator import (
    SCOPE,
    budget_value,
    claim,
    completion,
    coordinator,
    prepared,
)
from test_v2_jev_coordinator import database as database_fixture
from test_v2_jev_coordinator import memory as memory_fixture
from test_v2_jev_entry_context import POLICY, binding, publish
from test_v2_jev_entry_context import case as context_case_fixture

from services import v2_jev_coordinator as subject
from services.v2_jev_entry_context import JevEntryContext
from services.v2_jev_reference import CONTROL_KEY
from v2_core.evidence import canonical
from v2_core.jev_advisory import RESERVE_TOKENS, AdvisoryPolicy, JevAdvisory
from v2_core.state import BusinessState, StateSnapshot

memory = memory_fixture
context_case = context_case_fixture
database = database_fixture
DAY_MS = 86400000


def midnight(db):
    db.now = subject._window_starts(db.now)[1]
    return db.now


def shared_control(db, **changes):
    policy = AdvisoryPolicy(enabled=True, token_budget_pacing=True, **changes)
    db.rows[CONTROL_KEY.identity] = StateSnapshot(1, canonical(asdict(policy)), False)
    return policy


def day_value(now, used=0, limit=500000):
    value = subject._budget(
        None, kind="utc8-day", start=subject._window_starts(now)[1], limit=limit
    )
    return {**value, "used": used}


def set_day(db, used, limit=500000):
    value = day_value(db.now, used, limit)
    key = subject._budget_key(subject.DAY_NAMESPACE, value["window_start_ms"])
    db.rows[key.identity] = StateSnapshot(1, canonical(value), False)
    return key


@pytest.mark.parametrize(
    "changes",
    [
        {"token_budget_pacing": 1},
        {"token_budget_pacing": "true"},
        {"token_pacing_burst_tokens": True},
        {"token_pacing_burst_tokens": 8191},
        {"token_pacing_burst_tokens": 131073},
    ],
)
def test_policy_and_coordinator_validate_pacing(memory, changes):
    with pytest.raises(ValueError):
        AdvisoryPolicy(**changes)
    with pytest.raises(ValueError):
        coordinator(memory, **changes)


def test_old_control_payload_defaults_off_and_budget_shape_unchanged(memory):
    old = asdict(AdvisoryPolicy(enabled=True))
    del old["token_budget_pacing"], old["token_pacing_burst_tokens"]
    assert AdvisoryPolicy(**old).token_budget_pacing is False
    memory.rows[CONTROL_KEY.identity] = StateSnapshot(1, canonical(old), False)
    midnight(memory)
    assert claim(coordinator(memory), prepared(memory.now))["status"] == "GRANTED"
    assert set(budget_value(memory, subject.DAY_NAMESPACE)) == {
        "schema",
        "budget_key",
        "kind",
        "window_start_ms",
        "used",
        "limit",
    }


@pytest.mark.parametrize("elapsed", [0, 1, 60000, DAY_MS // 2, DAY_MS - 1])
def test_linear_cap_exact_boundary_reserves_headroom(memory, elapsed):
    day = midnight(memory)
    now = day + elapsed
    released = 16384 + (500000 - 16384) * elapsed // DAY_MS
    before = day_value(now, released - RESERVE_TOKENS)
    assert subject._token_budget_reason(before, now=now, pacing=True) is None
    assert (
        subject._token_budget_reason(
            {**before, "used": before["used"] + 1}, now=now, pacing=True
        )
        == "TOKEN_BUDGET_PACED"
    )


def test_pacing_never_adds_burst_to_hard_cap(memory):
    now = midnight(memory) + DAY_MS - 1
    value = day_value(now, 500000 - RESERVE_TOKENS + 1)
    assert (
        subject._token_budget_reason(value, now=now, pacing=True, burst_tokens=131072)
        == "TOKEN_BUDGET_EXHAUSTED"
    )


def test_small_daily_limit_clamps_burst_and_retains_full_reservation(memory):
    now = midnight(memory)
    value = day_value(now, limit=RESERVE_TOKENS)
    assert subject._token_budget_reason(value, now=now, pacing=True) is None
    assert (
        subject._token_budget_reason({**value, "used": 1}, now=now, pacing=True)
        == "TOKEN_BUDGET_EXHAUSTED"
    )


def test_paced_claim_is_read_only_and_creates_no_request(memory):
    midnight(memory)
    shared_control(memory)
    set_day(memory, RESERVE_TOKENS + 1)
    before = deepcopy(memory.rows)
    assert claim(coordinator(memory), prepared(memory.now)) == {
        "status": "TOKEN_BUDGET_PACED"
    }
    assert memory.rows == before


def test_atomic_pacing_is_shared_across_accounts_and_environments(memory):
    midnight(memory)
    shared_control(memory)
    a = coordinator(memory)
    b = coordinator(
        memory, scope=replace(SCOPE, account_id="qa-other", environment="LIVE")
    )
    requests = [
        (a, prepared(memory.now, "BTCUSDT")),
        (b, prepared(memory.now, "ETHUSDT")),
        (a, prepared(memory.now, "BNBUSDT")),
        (b, prepared(memory.now, "SOLUSDT")),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: claim(*item), requests))
    assert [r["status"] for r in results].count("GRANTED") == 2
    assert [r["status"] for r in results].count("TOKEN_BUDGET_PACED") == 2
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 2 * RESERVE_TOKENS
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 2


def test_cache_and_existing_claim_are_checked_before_pacing(memory):
    midnight(memory)
    shared_control(memory)
    store, request = coordinator(memory), prepared(memory.now)
    grant = claim(store, request)
    assert claim(store, request)["status"] == "BUSY"
    memory.now += 100
    args = completion(request, grant, now=memory.now, tokens=819)
    assert store.complete(**args)
    set_day(memory, 490000)
    before = deepcopy(memory.rows)
    assert claim(store, request, now=memory.now) == {
        "status": "CACHED",
        "result": args["result"],
    }
    assert memory.rows == before


def test_unknown_usage_keeps_reserve_and_no_auto_retry(memory):
    midnight(memory)
    shared_control(memory, token_pacing_burst_tokens=RESERVE_TOKENS)
    store, request = coordinator(memory), prepared(memory.now)
    grant = claim(store, request)
    memory.now += 1400
    assert store.complete(
        **completion(
            request, grant, now=memory.now, tokens=None, reason="MODEL_TIMEOUT"
        )
    )
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS
    assert claim(store, request, now=memory.now)["status"] == "BUSY"
    assert (
        claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "TOKEN_BUDGET_PACED"
    )


def test_known_usage_refund_preserves_remaining_day_budget(memory):
    midnight(memory)
    shared_control(memory)
    store, request = coordinator(memory), prepared(memory.now)
    grant = claim(store, request)
    memory.now += 100
    assert store.complete(**completion(request, grant, now=memory.now, tokens=819))
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 819
    assert claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "GRANTED"
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 819 + RESERVE_TOKENS


def test_pacing_reopens_only_when_real_db_time_releases_capacity(memory):
    day = midnight(memory)
    shared_control(memory)
    set_day(memory, 250000)
    store = coordinator(memory)
    assert claim(store, prepared(memory.now))["status"] == "TOKEN_BUDGET_PACED"
    # Faking client time cannot pick a later pacing/calendar window.
    assert claim(store, prepared(memory.now), now=day + DAY_MS // 2)["status"] == "BUSY"
    memory.now = day + DAY_MS // 2
    assert claim(store, prepared(memory.now))["status"] == "GRANTED"
    assert (
        budget_value(memory, subject.DAY_NAMESPACE)["used"] == 250000 + RESERVE_TOKENS
    )


def test_utc8_day_rollover_keeps_old_day_unchanged(memory):
    day = midnight(memory)
    shared_control(memory)
    old = set_day(memory, 492241)
    before = memory.rows[old.identity]
    memory.now = day + DAY_MS - 1
    assert (
        claim(coordinator(memory), prepared(memory.now))["status"] == "BUDGET_EXHAUSTED"
    )
    memory.now = day + DAY_MS
    assert claim(coordinator(memory), prepared(memory.now))["status"] == "GRANTED"
    assert memory.rows[old.identity] == before
    assert subject._window_starts(memory.now)[1] == day + DAY_MS
    assert (day + 28800000) % DAY_MS == 0


def test_control_pacing_cannot_be_relaxed_by_process_or_call(memory):
    midnight(memory)
    shared_control(memory, token_pacing_burst_tokens=8192)
    set_day(memory, 1)
    assert (
        claim(
            coordinator(memory, token_budget_pacing=False),
            prepared(memory.now),
            token_budget_pacing=False,
            token_pacing_burst_tokens=131072,
        )["status"]
        == "TOKEN_BUDGET_PACED"
    )


def test_corrupt_shared_control_is_not_availability_or_budget_refund(memory):
    midnight(memory)
    memory.rows[CONTROL_KEY.identity] = StateSnapshot(
        1, canonical({"enabled": True, "token_budget_pacing": "yes"}), False
    )
    before = deepcopy(memory.rows)
    with pytest.raises(ValueError):
        claim(coordinator(memory), prepared(memory.now))
    assert memory.rows == before


def test_advisory_pacing_returns_explicit_fallback_without_provider(memory):
    midnight(memory)
    shared_control(memory)
    set_day(memory, 8193)
    advisor = JevAdvisory(
        coordinator(memory),
        lambda *_args, **_kwargs: pytest.fail("paced provider call"),
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: memory.now,
    )
    result = advisor.evaluate(prepared(memory.now)["request"]["state"])
    assert result["status"] == "FALLBACK" and result["reason"] == "TOKEN_BUDGET_PACED"
    assert result["hypotheses"] is None and result["execution_authorized"] is False


@pytest.mark.parametrize("mode,status", [("BASELINE", "FALLBACK"), ("SKIP", "SKIP")])
def test_entry_pacing_respects_policy_and_never_queues(context_case, mode, status):
    case = context_case
    shared_control(case.db)
    now = case.db.now
    day = subject._window_starts(now)[1]
    released = 16384 + (500000 - 16384) * (now - day) // DAY_MS
    key = set_day(case.db, released - RESERVE_TOKENS + 1)
    before = case.db.rows[key.identity]
    case.service = JevEntryContext(
        case.db.connect,
        case.reference,
        scope=case.service.scope,
        policy=replace(POLICY, unavailable_action=mode),
    )
    result = case.service.assess(**case.args)
    evidence = json.loads(result.evidence_json)
    assert result.status == status and not case.queued
    assert case.db.rows[key.identity] == before
    if mode == "BASELINE":
        assert result.decision is case.args["decision"]
        assert evidence["fallback_cause"] == "TOKEN_BUDGET_PACED"
        assert evidence["hypotheses"] is None and evidence["risk_override"] is False
    else:
        assert result.decision.action != "OPEN"


def test_existing_ready_veto_cannot_become_pacing_baseline(context_case):
    case = context_case
    shared_control(case.db)
    case.service = JevEntryContext(
        case.db.connect,
        case.reference,
        scope=case.service.scope,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )
    assert case.service.assess(**case.args).status == "PENDING"
    publish(case, chase="0.9")
    set_day(case.db, 490000)
    result = case.service.assess(**case.args)
    assert result.status == "VETO" and result.reason == "JEV_CHASE_RISK_HIGH"
    assert len(case.queued) == 1


def test_racing_pacing_wakes_unclaimed_wait_without_sleep(context_case, monkeypatch):
    case = context_case
    shared_control(case.db)
    case.service = JevEntryContext(
        case.db.connect,
        case.reference,
        scope=case.service.scope,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )
    assert case.service.assess(**case.args).status == "PENDING"
    before_binding = binding(case)[1]
    now = case.db.now
    day = subject._window_starts(now)[1]
    released = 16384 + (500000 - 16384) * (now - day) // DAY_MS
    set_day(case.db, released - RESERVE_TOKENS + 1)
    monkeypatch.setattr(
        "services.v2_jev_entry_context.time.sleep",
        lambda _: pytest.fail("paced wait slept"),
    )
    assert (
        case.service.wait_pending(
            signal_id=case.args["signal_id"],
            consumer=case.args["consumer"],
            max_wait_ms=1500,
        )
        is True
    )
    result = case.service.assess(**case.args)
    assert result.status == "FALLBACK"
    assert json.loads(result.evidence_json)["fallback_cause"] == "TOKEN_BUDGET_PACED"
    assert binding(case)[1] == before_binding and len(case.queued) == 1


def test_pg_concurrent_pacing_preserves_original_budget_schema(database):
    connect = database
    with connect() as conn:
        now = subject._clock(conn)
    day = subject._window_starts(now)[1]
    control = AdvisoryPolicy(enabled=True, token_budget_pacing=True)
    state = BusinessState(connect)
    assert (
        state.change(
            CONTROL_KEY,
            expected_version=0,
            request_key="qa-pacing-control",
            payload=asdict(control),
            reason="isolated QA",
        ).code
        == "APPLIED"
    )
    # Leave exactly one full reservation available under the real DB clock.
    released = 16384 + (500000 - 16384) * (now - day) // DAY_MS
    day_key = subject._budget_key(subject.DAY_NAMESPACE, day)
    initial = day_value(now, released - RESERVE_TOKENS)
    assert (
        state.change(
            day_key,
            expected_version=0,
            request_key="qa-pacing-day",
            payload=initial,
            reason="isolated QA",
        ).code
        == "APPLIED"
    )
    stores = [
        subject.JevCoordinator(
            connect, scope=replace(SCOPE, account_id=f"qa-paced-{i}"), enabled=True
        )
        for i in range(4)
    ]
    requests = [
        prepared(now, symbol) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: claim(*item), zip(stores, requests)))
    assert [r["status"] for r in results].count("GRANTED") == 1
    assert all(
        r["status"] in {"GRANTED", "BUSY", "TOKEN_BUDGET_PACED"} for r in results
    )
    result = json.loads(state.read(day_key).payload_json)
    assert result == {**initial, "used": initial["used"] + RESERVE_TOKENS}


def test_pg_paced_denial_creates_no_claim_and_no_budget_mutation(database):
    connect = database
    with connect() as conn:
        now = subject._clock(conn)
    state = BusinessState(connect)
    control = AdvisoryPolicy(enabled=True, token_budget_pacing=True)
    state.change(
        CONTROL_KEY,
        expected_version=0,
        request_key="qa-control",
        payload=asdict(control),
        reason="isolated QA",
    )
    day = subject._window_starts(now)[1]
    released = 16384 + (500000 - 16384) * (now - day) // DAY_MS
    # A 4096-token margin tolerates fixture setup while keeping the hard cap
    # available except in the final minutes of the UTC+8 day.
    used = released - RESERVE_TOKENS + 4096
    if used + RESERVE_TOKENS > 500000:
        pytest.skip("paced-only PG fixture crosses UTC+8 end-of-day hard cap")
    day_key = subject._budget_key(subject.DAY_NAMESPACE, day)
    state.change(
        day_key,
        expected_version=0,
        request_key="qa-day",
        payload=day_value(now, used),
        reason="isolated QA",
    )
    before = state.read(day_key)
    store, request = (
        subject.JevCoordinator(connect, scope=SCOPE, enabled=True),
        prepared(now),
    )
    assert claim(store, request)["status"] == "TOKEN_BUDGET_PACED"
    assert state.read(day_key) == before
    assert state.read(store.request_state_key(request["request_key"])) is None
