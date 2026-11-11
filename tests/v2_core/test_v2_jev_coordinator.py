"""Coordinator tests: pure fake DB first; real PG only in isolated QA fixture."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_jev_coordinator as subject
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.jev_advisory import (
    MODEL,
    RESERVE_TOKENS,
    AdvisoryPolicy,
    JevAdvisory,
    _result,
    parse_response,
    prepare_request,
)
from v2_core.state import BusinessState, StateSnapshot, StateWrite

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-advisory-a", "SANDBOX", "FUTURES")
NOW = 1800000001000


def prepared(now=NOW, symbol="BTCUSDT"):
    return prepare_request(
        {
            "symbol": symbol,
            "observed_at_ms": now,
            "timeframe": "15m",
            "direction": "LONG",
            "price": "100",
            "return_15m_pct": "1",
            "atr_pct": "0.5",
        },
        now_ms=now,
        max_age_ms=60000,
    )


def raw_response(tokens=100):
    return {
        "model": MODEL,
        "answers": {
            name: {"type": "noul", "noul": "0.5"}
            for name in ("trend_supported", "chase_risk", "liquidity_fragile")
        },
        "usage": {"input_tokens": tokens, "output_tokens": 30},
    }


def claim(coordinator, request, *, now=None, **changes):
    current = request["observed_at_ms"] if now is None else now
    args = {
        "request_key": request["request_key"],
        "request": request["request"],
        "now_ms": current,
        "deadline_ms": current + 1500,
        "reserve_tokens": RESERVE_TOKENS,
        "requests_per_minute": 5,
        "tokens_per_day": 500000,
    }
    return coordinator.claim(**{**args, **changes})


def completion(request, grant, *, now, tokens=100, reason=None):
    result = _result(
        "FALLBACK" if reason else "ADVISORY_READY",
        reason or "MODEL_OBSERVED",
        request,
        now_ms=now,
        expires_at_ms=max(now, min(request["valid_until_ms"], now + 30000)),
        response=None if reason else parse_response(raw_response(tokens)),
    )
    return {
        "request_key": request["request_key"],
        "claim_id": grant["claim_id"],
        "result": result,
        "now_ms": now,
        "expires_at_ms": result["expires_at_ms"],
        "charged_input_tokens": tokens,
    }


class MemoryDatabase:
    def __init__(self):
        self.now = NOW
        self.rows = {}
        self.lock = threading.RLock()
        self.allow_lock = True
        self.connections = 0
        self.fail_namespace = None
        self.transactions = []
        self.active_statements = None

    @contextmanager
    def connect(self):
        self.connections += 1
        yield self

    @contextmanager
    def transaction(self):
        with self.lock:
            saved = deepcopy(self.rows)
            previous = self.active_statements
            self.active_statements = []
            self.transactions.append(self.active_statements)
            try:
                yield
            except BaseException:
                self.rows = saved
                raise
            finally:
                self.active_statements = previous

    def execute(self, sql, params=()):
        assert self.active_statements is not None, "operation needs its own transaction"
        self.active_statements.append(sql)
        if sql in {
            "SET LOCAL lock_timeout='50ms'",
            "SET LOCAL statement_timeout='100ms'",
        }:
            result = None
        elif "clock_timestamp()" in sql:
            result = self.now
        elif "pg_try_advisory_xact_lock" in sql:
            assert params == (subject._LOCK,)
            result = self.allow_lock
        else:
            raise AssertionError("Unexpected SQL in pure coordinator test")
        return SimpleNamespace(fetchone=lambda: (result,))


class MemoryState:
    def __init__(self, connect):
        with connect() as self.db:
            pass

    def read(self, key):
        return self.db.rows.get(key.identity)

    def change(self, key, *, expected_version, payload, **_):
        current = self.read(key)
        version = current.version if current else 0
        if key.namespace == self.db.fail_namespace or version != expected_version:
            return StateWrite("STALE", version)
        self.db.rows[key.identity] = StateSnapshot(
            version + 1, canonical(payload), False
        )
        return StateWrite("APPLIED", version + 1)


@pytest.fixture
def memory(monkeypatch):
    monkeypatch.setattr(subject, "BusinessState", MemoryState)
    return MemoryDatabase()


def coordinator(memory, **changes):
    return subject.JevCoordinator(
        memory.connect, **{"scope": SCOPE, "enabled": True, **changes}
    )


def budget_value(memory, namespace, at=None):
    minute, day = subject._window_starts(memory.now if at is None else at)
    key = subject._budget_key(
        namespace, minute if namespace == subject.MINUTE_NAMESPACE else day
    )
    value = memory.rows.get(key.identity)
    return None if value is None else json.loads(value.payload_json)


def test_default_off_never_opens_db(memory):
    store = subject.JevCoordinator(memory.connect, scope=SCOPE)
    assert store.lookup(request_key="invalid", now_ms=None) is None
    assert claim(store, prepared()) == {"status": "BUSY"}
    assert (
        store.complete(
            request_key="", claim_id="", result={}, now_ms=0, expires_at_ms=0
        )
        is False
    )
    assert memory.connections == 0


def test_all_ports_set_local_short_limits_before_read_or_lock(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    assert store.lookup(request_key=request["request_key"], now_ms=memory.now) is None
    memory.now += 100
    assert store.complete(**completion(request, granted, now=memory.now))
    # Three public operations own bounded outer transactions. The fourth is
    # complete's optional diagnostic savepoint and inherits those SET LOCALs;
    # it must not open a fourth connection or weaken the outer limits.
    assert memory.connections == 3
    assert len(memory.transactions) == 4
    assert memory.transactions[-1] == []
    for statements in memory.transactions[:3]:
        assert statements[:2] == [
            "SET LOCAL lock_timeout='50ms'",
            "SET LOCAL statement_timeout='100ms'",
        ]
    assert memory.active_statements is None


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"scope": {}},
        {"requests_per_minute": True},
        {"tokens_per_day": 8191},
        {"max_age_ms": 0},
        {"cache_ttl_ms": 60001},
    ],
)
def test_constructor_rejects_ambiguous_configuration(memory, changes):
    with pytest.raises((TypeError, ValueError)):
        coordinator(memory, **changes)


def test_claim_durable_dedup_no_double_charge(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    assert granted["status"] == "GRANTED"
    assert str(uuid4()) != granted["claim_id"]
    assert claim(store, request) == {"status": "BUSY"}
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 1


def test_expired_claim_never_reclaims_or_refunds(memory):
    request = prepared()
    assert claim(coordinator(memory), request)["status"] == "GRANTED"
    memory.now += 4000
    assert claim(coordinator(memory), request, now=memory.now) == {"status": "BUSY"}
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "private-account"),
        ("api_key", "not-a-real-key"),
        ("orders", []),
        ("price", 1.1),
    ],
)
def test_only_normalized_public_request_is_persisted(memory, field, value):
    request = prepared()
    request["request"]["state"][field] = value
    with pytest.raises(ValueError):
        claim(coordinator(memory), request)
    assert memory.rows == {}


@pytest.mark.parametrize(
    "change",
    [
        {"request_key": "x" * 64},
        {"reserve_tokens": 1},
        {"reserve_tokens": True},
        {"deadline_ms": NOW + 2001},
        {"requests_per_minute": True},
        {"tokens_per_day": 500000.0},
    ],
)
def test_invalid_claim_does_not_write(memory, change):
    with pytest.raises(ValueError):
        claim(coordinator(memory), prepared(), **change)
    assert memory.rows == {}


def test_request_model_questions_and_key_are_bound(memory):
    for mutate in (
        lambda req: req["request"].update(model="other"),
        lambda req: req["request"].update(questions={}),
        lambda req: req.update(request_key="0" * 64),
    ):
        request = prepared()
        mutate(request)
        with pytest.raises(ValueError):
            claim(coordinator(memory), request)
    assert memory.rows == {}


def test_global_budget_across_accounts_and_environments(memory):
    a = coordinator(memory, tokens_per_day=RESERVE_TOKENS)
    b = coordinator(
        memory, scope=replace(SCOPE, account_id="qa-advisory-b", environment="LIVE")
    )
    assert claim(a, prepared())["status"] == "GRANTED"
    assert claim(b, prepared())["status"] == "BUDGET_EXHAUSTED"
    assert b.lookup(request_key=prepared()["request_key"], now_ms=memory.now) is None
    assert budget_value(memory, subject.DAY_NAMESPACE)["limit"] == RESERVE_TOKENS


def test_minute_rate_limit_has_no_refund_and_resets_by_db_time(memory):
    store = coordinator(memory, requests_per_minute=1)
    first = prepared()
    granted = claim(store, first)
    memory.now += 10
    assert store.complete(**completion(first, granted, now=memory.now, tokens=1))
    assert claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "BUDGET_EXHAUSTED"
    memory.now += 60000
    assert claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "GRANTED"


def test_forged_client_time_cannot_open_future_budget_window(memory):
    store = coordinator(memory, requests_per_minute=1)
    assert claim(store, prepared())["status"] == "GRANTED"
    assert claim(store, prepared(symbol="ETHUSDT"), now=memory.now + 60000) == {
        "status": "BUSY"
    }
    assert budget_value(memory, subject.MINUTE_NAMESPACE)["used"] == 1


def test_success_cache_survives_rpc_deadline_but_not_market_expiry(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = completion(request, granted, now=memory.now)
    assert store.complete(**args) is True
    memory.now += 3000
    assert (
        store.lookup(request_key=request["request_key"], now_ms=memory.now)
        == args["result"]
    )
    assert claim(store, request, now=memory.now) == {
        "status": "CACHED",
        "result": args["result"],
    }
    memory.now += 30000
    assert store.lookup(request_key=request["request_key"], now_ms=0) is None
    assert claim(store, request, now=memory.now) == {"status": "BUSY"}


def test_usage_refund_exactly_once_and_only_owned_scope(memory):
    a, request = coordinator(memory), prepared()
    granted = claim(a, request)
    memory.now += 100
    args = completion(request, granted, now=memory.now)
    other = coordinator(memory, scope=replace(SCOPE, account_id="other"))
    assert other.complete(**args) is False
    assert a.complete(**{**args, "claim_id": str(uuid4())}) is False
    assert a.complete(**args) is True
    assert a.complete(**args) is False
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 100


def test_unknown_usage_retains_reservation_and_failure_is_not_cache(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 2000
    assert store.complete(
        **completion(
            request, granted, now=memory.now, tokens=None, reason="DEADLINE_EXPIRED"
        )
    )
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS
    assert store.lookup(request_key=request["request_key"], now_ms=memory.now) is None
    assert claim(store, request, now=memory.now) == {"status": "BUSY"}


def test_known_overspend_is_charged_not_capped_or_refunded(memory):
    store, request = coordinator(memory, tokens_per_day=RESERVE_TOKENS * 2), prepared()
    granted = claim(store, request)
    memory.now += 100
    assert store.complete(
        **completion(
            request,
            granted,
            now=memory.now,
            tokens=20000,
            reason="TOKEN_RESERVATION_EXCEEDED",
        )
    )
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 20000
    assert claim(store, prepared(memory.now, "ETHUSDT"))["status"] == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize(
    "reason,tokens,elapsed",
    [
        ("MODEL_TIMEOUT", 1, 100),
        ("MODEL_UNAVAILABLE", 1, 100),
        ("TOKEN_RESERVATION_EXCEEDED", 8192, 100),
        ("TOKEN_RESERVATION_EXCEEDED", None, 100),
        ("DEADLINE_EXPIRED", 100, 100),
        ("MODEL_UNAVAILABLE", None, 2000),
    ],
)
def test_fallback_cannot_forge_usage_refund_or_deadline(
    memory, reason, tokens, elapsed
):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += elapsed
    with pytest.raises(ValueError):
        store.complete(
            **completion(request, granted, now=memory.now, tokens=tokens, reason=reason)
        )
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


@pytest.mark.parametrize("field", ["execution_authorized", "risk_override"])
def test_fallback_authorization_requires_literal_false(memory, field):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = completion(
        request, granted, now=memory.now, tokens=None, reason="MODEL_UNAVAILABLE"
    )
    args["result"][field] = 0
    with pytest.raises(ValueError):
        store.complete(**args)
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


def test_claim_cannot_extend_snapshot_with_rpc_deadline(memory):
    request = prepared()
    memory.now += 59900
    assert claim(coordinator(memory), request, now=memory.now) == {"status": "BUSY"}
    assert memory.rows == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_authorized", True),
        ("risk_override", True),
        ("decision_effect", "BUY"),
        ("model", "other"),
        ("extra", "instruction"),
    ],
)
def test_poisoned_result_rejected_without_refund(memory, field, value):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = completion(request, granted, now=memory.now)
    args["result"][field] = value
    with pytest.raises(ValueError):
        store.complete(**args)
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


def test_usage_mismatch_and_late_ready_result_rejected(memory):
    store, request = coordinator(memory), prepared()
    granted = claim(store, request)
    memory.now += 100
    args = completion(request, granted, now=memory.now)
    with pytest.raises(ValueError):
        store.complete(**{**args, "charged_input_tokens": 0})
    memory.now += 1500
    with pytest.raises(ValueError):
        store.complete(**completion(request, granted, now=memory.now))
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == RESERVE_TOKENS


def test_utc8_day_rollover_and_late_refund_to_original_day(memory):
    midnight = subject._window_starts(NOW)[1] + 86400000
    memory.now = midnight - 1000
    store = coordinator(memory)
    request = prepared(memory.now)
    granted = claim(store, request)
    memory.now = midnight + 100
    next_request = prepared(memory.now, "ETHUSDT")
    assert claim(store, next_request)["status"] == "GRANTED"
    assert store.complete(**completion(request, granted, now=memory.now, tokens=200))
    assert budget_value(memory, subject.DAY_NAMESPACE, midnight - 1)["used"] == 200
    assert (
        budget_value(memory, subject.DAY_NAMESPACE, midnight)["used"] == RESERVE_TOKENS
    )


def test_busy_lock_does_not_write(memory):
    memory.allow_lock = False
    assert claim(coordinator(memory), prepared()) == {"status": "BUSY"}
    assert memory.rows == {}


def test_cas_failure_rolls_back_all_budget_writes(memory):
    memory.fail_namespace = subject.REQUEST_NAMESPACE
    with pytest.raises(ValueError, match="CAS_CONFLICT"):
        claim(coordinator(memory), prepared())
    assert memory.rows == {}


def test_component_to_coordinator_end_to_end_without_sdk_or_network(memory):
    calls = []

    def query(request, timeout_ms):
        calls.append((deepcopy(request), timeout_ms))
        memory.now += 50
        return raw_response()

    model = JevAdvisory(
        coordinator(memory),
        query,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: memory.now,
    )
    features = prepared()["request"]["state"]
    result = model.evaluate(features)
    assert result["status"] == "ADVISORY_READY"
    assert result["execution_authorized"] is False
    assert model.evaluate(features) == result
    assert len(calls) == 1
    assert calls[0][1] <= 1500
    assert budget_value(memory, subject.DAY_NAMESPACE)["used"] == 100


def test_pg_concurrent_claim_has_one_grant_and_one_reservation(database):
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim(store, request), range(8)))
    assert sum(result["status"] == "GRANTED" for result in results) == 1
    assert {result["status"] for result in results} <= {"GRANTED", "BUSY"}
    day = subject._window_starts(now)[1]
    saved = BusinessState(database).read(
        subject._budget_key(subject.DAY_NAMESPACE, day)
    )
    assert json.loads(saved.payload_json)["used"] == RESERVE_TOKENS


def test_pg_global_budget_independent_of_account_scope(database):
    a = subject.JevCoordinator(
        database, scope=SCOPE, enabled=True, tokens_per_day=RESERVE_TOKENS
    )
    b = subject.JevCoordinator(
        database,
        scope=replace(SCOPE, account_id="other", environment="LIVE"),
        enabled=True,
    )
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    assert claim(a, request)["status"] == "GRANTED"
    assert claim(b, request)["status"] == "BUDGET_EXHAUSTED"


def test_pg_completion_refund_is_cas_and_cache_usable(database):
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    granted = claim(store, request)
    with database() as conn:
        finished = subject._clock(conn)
    args = completion(request, granted, now=finished)
    assert store.complete(**args) is True
    assert store.complete(**args) is False
    assert (
        store.lookup(request_key=request["request_key"], now_ms=finished)
        == args["result"]
    )
    saved = BusinessState(database).read(
        subject._budget_key(subject.DAY_NAMESPACE, subject._window_starts(now)[1])
    )
    assert json.loads(saved.payload_json)["used"] == 100


def pg_business_snapshot(database):
    with database() as conn:
        return (
            conn.execute(
                "SELECT state_id::text,version,payload,deleted FROM v2_business_state ORDER BY state_id"
            ).fetchall(),
            conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0],
        )


def test_pg_claim_row_lock_falls_back_without_rpc_or_partial_quota(database):
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    assert claim(store, prepared(now))["status"] == "GRANTED"
    original = pg_business_snapshot(database)
    day_key = subject._budget_key(subject.DAY_NAMESPACE, subject._window_starts(now)[1])

    def forbidden_query(*args, **kwargs):
        pytest.fail("failed durable claim must never call the model")

    model = JevAdvisory(
        store,
        forbidden_query,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: time.time_ns() // 1000000,
    )
    with database() as holder:
        holder.execute(
            "SELECT state_id FROM v2_business_state WHERE state_id=%s FOR UPDATE",
            (day_key.identity,),
        )
        started = time.monotonic()
        result = model.evaluate(prepared(now, "ETHUSDT")["request"]["state"])
        assert time.monotonic() - started < 1.5
        assert result["status"] == "FALLBACK"
        assert result["reason"] == "COORDINATOR_UNAVAILABLE"
    # Minute counters/history are written before the locked daily row. They
    # must disappear too; a timeout must not leave any partial reservation.
    assert pg_business_snapshot(database) == original


def test_pg_completion_row_lock_rolls_back_refund_and_preserves_claim(database):
    import psycopg

    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    with database() as conn:
        now = subject._clock(conn)
    request = prepared(now)
    grant = claim(store, request)
    original = pg_business_snapshot(database)
    with database() as holder:
        holder.execute(
            "SELECT state_id FROM v2_business_state WHERE state_id=%s FOR UPDATE",
            (store.request_state_key(request["request_key"]).identity,),
        )
        finished = subject._clock(holder)
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            store.complete(**completion(request, grant, now=finished))
        assert time.monotonic() - started < 1.5
    # The refund precedes the claimed request CAS; both must roll back when
    # the final publication cannot take its row lock within 50ms.
    assert pg_business_snapshot(database) == original


def test_pg_lookup_statement_timeout_does_not_inherit_long_factory_limit(
    database, monkeypatch
):
    import psycopg

    class DelayedState(BusinessState):
        def read(self, key):
            with self._connect() as conn:
                conn.execute("SELECT pg_sleep(0.5)")
            return super().read(key)

    original = pg_business_snapshot(database)
    monkeypatch.setattr(subject, "BusinessState", DelayedState)
    store = subject.JevCoordinator(database, scope=SCOPE, enabled=True)
    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        store.lookup(request_key=prepared()["request_key"], now_ms=NOW)
    assert time.monotonic() - started < 1.5
    assert pg_business_snapshot(database) == original
