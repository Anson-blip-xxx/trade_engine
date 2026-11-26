"""Typed preparation recovery; only disposable PG, never exchange ports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import evidence, intent
from tests.v2_core import test_v2_strategy_deployment_execution as execution_fixtures
from tests.v2_core.test_v2_strategy_plan_admission import (
    SCOPE,
    counts,
    generic_intent_without_claim,
    setup,
    signal,
)
from v2_core.account_risk import AccountRiskDenied, RiskReference
from v2_core.intents import AdmissionCode, IntentStore
from v2_core.orders import Orders
from v2_core.service import TradingData
from v2_core.signals import Signals
from v2_core.state import BusinessState
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_plan_admission import opportunity_key
from v2_core.strategy_prepare import StrategyPrepareRecovery

database = database_fixture


def admitted(database, **changes):
    admission, registry, binding, plan = setup(database, **changes)
    result = admission.admit(plan, binding, rationale="QA recovery")
    return registry, binding, plan, result["intent_id"]


def recovery(database, *, scope=SCOPE):
    return StrategyPrepareRecovery(database, scope=scope)


def at_database_time(database, now_ms):
    """Deterministic clock observations with all writes/locks using real QA PG."""

    class ClockConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, *args, **kwargs):
            if (
                query
                == "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
            ):
                return self.conn.execute("SELECT %s::bigint", (now_ms,))
            return self.conn.execute(query, *args, **kwargs)

    @contextmanager
    def connect():
        with database() as conn:
            yield ClockConnection(conn)

    return connect


@pytest.mark.parametrize("scope", [None, {}, "account"])
def test_scope_is_required_before_database(scope):
    with pytest.raises(TypeError, match="PORTS_REQUIRED"):
        recovery(lambda: pytest.fail("must not connect"), scope=scope)


def test_connection_factory_is_explicit():
    with pytest.raises(TypeError, match="PORTS_REQUIRED"):
        recovery(None)


@pytest.mark.parametrize("limit", [True, 0, -1, 101, "1"])
def test_batch_limit_is_bounded_before_database(limit):
    with pytest.raises(ValueError, match="LIMIT_INVALID"):
        recovery(lambda: pytest.fail("must not connect")).run_once(limit=limit)


def test_identifiers_are_checked_before_database():
    port = recovery(lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError):
        port.recover("bad-uuid")
    with pytest.raises(ValueError):
        port.run_once(after="bad-uuid")


def test_recovers_admission_crash_with_original_plan_and_one_order(database):
    _, _, plan, episode = admitted(database)
    before = counts(database)
    result = recovery(database).recover(episode)
    assert result["status"] == "PREPARED"
    assert result["execution_authorized"] is False
    trace = TradingData(database).trace(episode)
    assert trace["decision"]["strategy_plan"] == plan.payload()
    assert trace["status"] == "RECEIVED"
    assert len(trace["orders"]) == 1
    assert trace["orders"][0]["order_id"] == result["order_id"]
    assert trace["orders"][0]["client_order_id"] == result["client_order_id"]
    after = counts(database)
    assert after[0] == before[0] == 1
    assert after[1] == before[1] + 1
    assert after[2:5] == before[2:5]
    assert after[5:] == (1, 0)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_order_events").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM v2_order_events WHERE status<>'PREPARED'"
        ).fetchone() == (0,)


def test_consumed_signal_is_recovered_from_intent_queue(database):
    admission, _, binding, plan = setup(database)
    signal_id = signal(database, plan)
    accepted = admission.admit(plan, binding, rationale="QA", signal_id=signal_id)
    assert (
        Signals(database).pending(
            consumer=binding.consumer,
            environment=SCOPE.environment,
            source="tv_bridge",
        )
        == []
    )
    before = counts(database)
    page = recovery(database).run_once()
    assert len(page["results"]) == 1
    assert page["results"][0]["intent_id"] == accepted["intent_id"]
    assert page["results"][0]["status"] == "PREPARED"
    assert counts(database)[3:5] == before[3:5] == (1, 1)


def test_existing_order_survives_stop_and_expiry_without_new_work(database):
    registry, binding, plan, episode = admitted(database)
    first = recovery(database).recover(episode)
    StrategyDeployments(database, registry).publish(
        replace(binding, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-stop",
    )
    before = counts(database)
    result = recovery(at_database_time(database, plan.expires_at_ms)).recover(episode)
    assert result["status"] == "EXISTING_ORDER"
    assert result["order_status"] == "PREPARED"
    assert result["order_id"] == first["order_id"]
    assert result["client_order_id"] == first["client_order_id"]
    assert counts(database) == before


@pytest.mark.parametrize(
    "status", ["SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "CANCELLED"]
)
def test_existing_order_lifecycle_never_restarts_preparation(database, status):
    binding, _, _, original = execution_fixtures.ready(database)
    episode = original.intent_id
    port = recovery(database, scope=binding.scope)
    first = port.recover(episode)
    orders = Orders(database)
    if status == "CANCELLED":
        orders.transition(
            first["order_id"],
            expected_version=1,
            status=status,
            evidence={"reason": "QA"},
        )
    else:
        orders.transition(
            first["order_id"],
            expected_version=1,
            status="SUBMITTING",
            evidence={},
            risk_reference=RiskReference(
                "SANDBOX", "BTCUSDT", "qa", "100", execution_fixtures.now()
            ),
        )
        if status != "SUBMITTING":
            orders.transition(
                first["order_id"],
                expected_version=2,
                status=status,
                evidence={"source": "QA"},
            )
    before = counts(database)
    result = port.recover(episode)
    assert result["status"] == "EXISTING_ORDER"
    assert result["order_status"] == status
    assert result["order_id"] == first["order_id"]
    assert counts(database) == before


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "REJECTED"])
def test_terminal_intent_does_not_revive(database, status):
    _, _, _, episode = admitted(database)
    assert IntentStore(database).terminate_unstarted(
        episode, status=status, reason="QA"
    )
    before = counts(database)
    result = recovery(database).recover(episode)
    assert result["status"] == "UNCHANGED"
    assert result["intent_status"] == status
    assert counts(database) == before


@pytest.mark.parametrize("mode", ["OFF", "DRAINING", "SHADOW", "ACTIVE"])
def test_current_deployment_fence_denies_without_terminalizing(database, mode):
    registry, binding, _, episode = admitted(database)
    StrategyDeployments(database, registry).publish(
        replace(binding, generation=2, mode=mode),
        expected_generation=1,
        request_id="qa-new-generation",
    )
    before = counts(database)
    with pytest.raises(AccountRiskDenied):
        recovery(database).recover(episode)
    assert counts(database) == before
    assert TradingData(database).trace(episode)["status"] == "RECEIVED"


def test_proven_expiry_is_audited_once_and_keeps_permanent_claim(database):
    _, _, plan, episode = admitted(database)
    port = recovery(at_database_time(database, plan.expires_at_ms))
    before = counts(database)
    result = port.recover(episode)
    assert result["status"] == "EXPIRED"
    assert result["execution_authorized"] is False
    assert port.recover(episode)["intent_status"] == "EXPIRED"
    after = counts(database)
    assert after[0] == before[0]
    assert after[1] == before[1] + 1
    assert after[2:] == before[2:]
    with database() as conn:
        terminal = conn.execute(
            "SELECT payload FROM v2_domain_outbox WHERE intent_id=%s AND event_type='INTENT_TERMINAL:2'",
            (episode,),
        ).fetchone()[0]
    assert terminal["status"] == "EXPIRED"
    assert terminal["reason"] == "STRATEGY_PLAN_EXPIRED_BEFORE_PREPARATION"
    assert BusinessState(database).read(opportunity_key(plan)).version == 1


def test_expiry_cannot_terminalize_a_missing_opportunity_claim(database):
    _, _, plan, episode, _ = generic_intent_without_claim(database)
    before = counts(database)
    with pytest.raises(AccountRiskDenied, match="RECEIPT_INVALID"):
        recovery(at_database_time(database, plan.expires_at_ms)).recover(episode)
    assert counts(database) == before
    assert TradingData(database).trace(episode)["status"] == "RECEIVED"


def test_future_database_time_anomaly_does_not_expire_intent(database):
    _, _, plan, episode = admitted(database)
    before = counts(database)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_EXPIRED"):
        recovery(at_database_time(database, plan.decided_at_ms - 1)).recover(episode)
    assert counts(database) == before
    assert TradingData(database).trace(episode)["status"] == "RECEIVED"


def test_legacy_and_foreign_intents_are_not_prepared(database):
    original = replace(intent(), account_id=SCOPE.account_id)
    assert (
        TradingData(database).accept(original, evidence()).code
        is AdmissionCode.ACCEPTED
    )
    with pytest.raises(ValueError, match="TYPED_PLAN_REQUIRED"):
        recovery(database).recover(original.intent_id)
    _, _, _, foreign = admitted(database, scope=replace(SCOPE, account_id="other"))
    with pytest.raises(ValueError, match="INTENT_NOT_FOUND"):
        recovery(database).recover(foreign)
    assert recovery(database).run_once()["results"] == []
    assert counts(database)[5:] == (0, 0)


def test_concurrent_recovery_prepares_once(database):
    _, _, _, episode = admitted(database)
    barrier = Barrier(4)

    def recover(_):
        barrier.wait(timeout=5)
        return recovery(database).recover(episode)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(recover, range(4)))
    assert [r["status"] for r in results].count("PREPARED") == 1
    assert [r["status"] for r in results].count("EXISTING_ORDER") == 3
    assert len({r["order_id"] for r in results}) == 1
    assert counts(database)[5:] == (1, 0)


def test_prepare_and_expiry_share_the_intent_lock(database):
    _, _, plan, episode = admitted(database)
    barrier = Barrier(2)

    def prepare():
        barrier.wait(timeout=5)
        try:
            return Orders(database).prepare(episode)
        except ValueError:
            return None

    def expire():
        barrier.wait(timeout=5)
        return recovery(at_database_time(database, plan.expires_at_ms)).recover(episode)

    with ThreadPoolExecutor(max_workers=2) as pool:
        opening, terminal = pool.submit(prepare), pool.submit(expire)
        opening, terminal = opening.result(), terminal.result()
    trace = TradingData(database).trace(episode)
    if opening is None:
        assert terminal["status"] == trace["status"] == "EXPIRED"
        assert trace["orders"] == []
    else:
        assert terminal["status"] == "EXISTING_ORDER"
        assert trace["status"] == "RECEIVED"
        assert len(trace["orders"]) == 1


def test_outer_commit_failure_rolls_back_order_and_expiry(database):
    _, _, plan, episode = admitted(database)

    @contextmanager
    def fail_commit():
        with database() as conn:
            yield conn
            raise RuntimeError("QA_COMMIT_FAILURE")

    before = counts(database)
    for connect in (fail_commit, at_database_time(fail_commit, plan.expires_at_ms)):
        with pytest.raises(RuntimeError, match="QA_COMMIT_FAILURE"):
            recovery(connect).recover(episode)
        assert counts(database) == before
        assert TradingData(database).trace(episode)["status"] == "RECEIVED"


@pytest.mark.parametrize("expired", [False, True])
def test_lost_commit_ack_retries_same_durable_result(database, expired):
    _, _, plan, episode = admitted(database)

    @contextmanager
    def lose_ack():
        with database() as conn:
            yield conn
        raise RuntimeError("QA_ACK_LOST")

    connect = at_database_time(lose_ack, plan.expires_at_ms) if expired else lose_ack
    with pytest.raises(RuntimeError, match="QA_ACK_LOST"):
        recovery(connect).recover(episode)
    before = counts(database)
    result = recovery(database).recover(episode)
    assert result["status"] == ("UNCHANGED" if expired else "EXISTING_ORDER")
    if expired:
        assert result["intent_status"] == "EXPIRED"
    assert counts(database) == before


def test_batch_advances_past_bad_claim_and_revisits_it_after_sweep(database):
    admission, _, binding, plan = setup(database)
    plans = [plan, replace(plan, opportunity_id="next-opportunity")]
    candidates = sorted(
        (admission.admit(p, binding, rationale="QA")["intent_id"], p) for p in plans
    )
    bad, bad_plan = candidates[0]
    good, _ = candidates[1]
    BusinessState(database).change(
        opportunity_key(bad_plan),
        expected_version=1,
        request_key="qa-corrupt",
        payload={},
        reason="QA",
    )
    port = recovery(database)
    first = port.run_once(limit=1)
    assert first["results"][0]["intent_id"] == bad
    assert first["results"][0]["status"] == "BLOCKED"
    assert first["next_after"] == bad
    second = port.run_once(limit=1, after=first["next_after"])
    assert second["results"][0]["intent_id"] == good
    assert second["results"][0]["status"] == "PREPARED"
    end = port.run_once(limit=1, after=second["next_after"])
    assert end["results"] == []
    assert end["next_after"] is None
    assert port.run_once(after=end["next_after"])["results"][0]["intent_id"] == bad
    assert TradingData(database).trace(bad)["status"] == "RECEIVED"


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (RuntimeError("postgres://private-diagnostic"), "RuntimeError"),
        (ValueError("postgres://private-diagnostic"), "ValueError"),
        (ValueError("UNRECOGNIZED_PRIVATE_VALUE"), "ValueError"),
        (RuntimeError("STRATEGY_PLAN_INTENT_MISMATCH"), "RuntimeError"),
        (
            ValueError("STRATEGY_PLAN_INTENT_MISMATCH"),
            "STRATEGY_PLAN_INTENT_MISMATCH",
        ),
        (
            ValueError("STRATEGY_PLAN_EVIDENCE_INVALID"),
            "STRATEGY_PLAN_EVIDENCE_INVALID",
        ),
    ],
)
def test_batch_isolates_unknown_failure_without_exposing_details(
    database, monkeypatch, failure, expected_code
):
    _, _, _, episode = admitted(database)
    port = recovery(database)

    def failed(_):
        raise failure

    monkeypatch.setattr(port, "recover", failed)
    before = counts(database)
    page = port.run_once()
    assert page["results"] == [
        {
            "intent_id": episode,
            "status": "ERROR",
            "error_code": expected_code,
            "execution_authorized": False,
        }
    ]
    assert counts(database) == before


def test_batch_page_failure_propagates():
    def failed():
        raise RuntimeError("QA_DB_UNAVAILABLE")

    with pytest.raises(RuntimeError, match="QA_DB_UNAVAILABLE"):
        recovery(failed).run_once()


def test_unknown_identity_does_not_create_work(database):
    before = counts(database)
    with pytest.raises(ValueError, match="INTENT_NOT_FOUND"):
        recovery(database).recover(str(uuid4()))
    assert counts(database) == before
