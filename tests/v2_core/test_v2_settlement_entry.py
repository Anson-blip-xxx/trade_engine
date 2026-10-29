"""Real isolated PG: income lag is not released exposure or fake settlement."""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import Venue
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_settlement_entry import policy_key
from v2_core.account_risk import AccountPolicy
from v2_core.evidence import canonical
from v2_core.state import BusinessState

database = database_fixture
closed = closed_fixture


def setup(database, closed, *, enabled=True, reserved=True, reservation_scope=SCOPE):
    runtime, episode, income = closed
    if reserved:
        # Explicit isolated-QA reservation fixture. Runtime recovery never
        # creates reservations for historical trades or rewrites risk scope.
        with database() as conn:
            conn.execute(
                "INSERT INTO v2_risk_accounts(scope,version) VALUES(%s,1)",
                (reservation_scope.key,),
            )
            conn.execute(
                "INSERT INTO v2_risk_policies(scope,version,config) VALUES(%s,1,%s::jsonb)",
                (
                    reservation_scope.key,
                    canonical(
                        asdict(AccountPolicy("USDT", "10000", 10, 0, "qa", 60000))
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO v2_risk_reservations(episode_id,scope,policy_version,notional,reference) VALUES(%s,%s,1,125,'{}')",
                (episode, reservation_scope.key),
            )
    if enabled is not None:
        BusinessState(database).change(
            policy_key(SCOPE),
            expected_version=0,
            request_key="qa-policy",
            payload={"enabled": enabled},
            reason="QA",
        )
    venue = Venue(income)
    stage = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: 1000
    )
    result = stage.run_once()
    assert result["status"] == "BLOCKED"
    assert result["settlements"][episode]["reason"] == "WAITING_INCOME_LAG"
    proof = stage.coverage.run_once()
    assert proof["status"] == "ACCOUNT_COVERAGE_CLEAR"
    venue.calls.clear()
    return runtime, episode, venue, stage, result, proof


def test_only_verified_cash_lag_allows_other_entry_without_releasing_or_settling(
    database, closed
):
    runtime, episode, venue, stage, result, proof = setup(database, closed)
    before = runtime.data.trace(episode)
    with database() as conn:
        risk_before = conn.execute(
            "SELECT scope,status,notional,released_at FROM v2_risk_reservations WHERE episode_id=%s",
            (episode,),
        ).fetchone()
    answer = stage.entry_clearance(result, proof)
    assert answer is not None
    assert answer["status"] == "ENTRY_CLEAR_PENDING_INCOME"
    assert answer["risk_reservations_retained"] is True
    evidence = answer["evidence"]
    assert evidence["retained_risk_reservations"] and not evidence["cash_complete"]
    assert evidence["coverage_observation_id"] == proof["observation_id"]
    assert evidence["episodes"][0]["episode"] == episode
    assert answer["execution_authorized"] is answer["settlement_authorized"] is False
    assert runtime.data.trace(episode) == before
    with database() as conn:
        assert (
            conn.execute(
                "SELECT scope,status,notional,released_at FROM v2_risk_reservations WHERE episode_id=%s",
                (episode,),
            ).fetchone()
            == risk_before
        )
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_directional_outcomes"
        ).fetchone() == (0,)
    assert not venue.calls and not venue.income.calls


@pytest.mark.parametrize("enabled", [None, False, 1, "true"])
def test_default_off_or_nonboolean_policy_never_changes_old_block(
    database, closed, enabled
):
    _, _, _, stage, result, proof = setup(database, closed, enabled=enabled)
    assert stage.entry_clearance(result, proof) is None


@pytest.mark.parametrize(
    "reason",
    [
        "INCOME_INCOMPLETE",
        "LOCAL_ORDERS_NOT_FINAL",
        "WAITING_CLOSE",
        "CASH_MISMATCH",
        "ACCOUNT_BUSY",
        "UNOWNED_POSITION",
    ],
)
def test_every_nonlag_blocker_is_preserved(database, closed, reason):
    _, episode, _, stage, result, proof = setup(database, closed)
    result["settlements"][episode]["reason"] = reason
    assert stage.entry_clearance(result, proof) is None


@pytest.mark.parametrize(
    "defect",
    [
        "outcome_error",
        "episode_error",
        "mixed",
        "empty",
        "unknown_episode",
        "top_error",
        "caller_permission",
        "missing_proof",
        "caller_tamper",
        "stale_proof",
        "future_proof",
        "changed_orders",
        "changed_fill",
        "lag_expired",
        "future_fill",
    ],
)
def test_incomplete_or_changed_evidence_never_isolates_the_block(
    database, closed, defect
):
    runtime, episode, _, stage, result, proof = setup(database, closed)
    if defect == "outcome_error":
        result["outcomes"][episode] = {"status": "BLOCKED", "error_code": "FAILED"}
    elif defect == "episode_error":
        result["settlements"][episode]["error_code"] = "FAILED"
    elif defect == "mixed":
        result["settlements"][str(uuid4())] = {"status": "BLOCKED", "reason": "UNKNOWN"}
    elif defect == "empty":
        result["settlements"] = {}
    elif defect == "unknown_episode":
        result["settlements"] = {str(uuid4()): result["settlements"][episode]}
    elif defect == "top_error":
        result["error_code"] = "FAILED"
    elif defect == "caller_permission":
        result["settlement_authorized"] = True
    elif defect == "missing_proof":
        proof = None
    elif defect == "caller_tamper":
        proof = {**proof, "execution_authorized": True}
    elif defect == "stale_proof":
        stage.coverage.inventory.clock_ms = lambda: 6001
    elif defect == "future_proof":
        stage.coverage.inventory.clock_ms = lambda: 999
    elif defect == "changed_orders":
        from test_v2_account_risk import prepare

        prepare(database, symbol="ETHUSDT")
    elif defect == "changed_fill":
        order = next(
            o for o in runtime.data.trace(episode)["orders"] if o["leg"] == "CLOSE"
        )
        runtime.data.ledger.record_fill(
            order_id=order["order_id"],
            exchange_fill_id="2",
            quantity="1.25",
            price="92",
            fee="0.046",
            fee_currency="USDT",
            occurred_at_ms=10,
            evidence={"source": "qa-additional-fill-proof"},
        )
    elif defect == "lag_expired":
        # Fresh coverage cannot make a stale pending-cash episode an income lag.
        stage.clock = stage.coverage.inventory.clock_ms = lambda: 60010
        proof = stage.coverage.run_once()
    elif defect == "future_fill":
        stage.clock = lambda: 9
    assert stage.entry_clearance(result, proof) is None


@pytest.mark.parametrize("wrong_scope", [False, True])
def test_risk_reservation_must_be_present_held_and_exact_scope(
    database, closed, wrong_scope
):
    _, _, _, stage, result, proof = setup(
        database,
        closed,
        reserved=wrong_scope,
        reservation_scope=replace(SCOPE, account_id="other") if wrong_scope else SCOPE,
    )
    assert stage.entry_clearance(result, proof) is None


def test_second_fence_rejects_facts_changed_during_pg_checks(
    database, closed, monkeypatch
):
    runtime, episode, _, stage, result, proof = setup(database, closed)
    reuse = stage.coverage.reuse_after_protection
    calls = []

    def changing(saved):
        calls.append(True)
        if len(calls) == 2:
            runtime.data.ledger.adjustment(
                episode_id=episode,
                source_id="qa-change",
                amount_text="-0.1",
                currency="USDT",
                kind="FUNDING",
                occurred_at_ms=11,
                evidence={"source": "qa"},
            )
        return reuse(saved)

    monkeypatch.setattr(stage.coverage, "reuse_after_protection", changing)
    assert stage.entry_clearance(result, proof) is None


def test_released_reservation_cannot_be_claimed_held_by_coverage(database, closed):
    _, episode, _, stage, result, proof = setup(database, closed)
    with database() as conn:
        conn.execute(
            "UPDATE v2_risk_reservations SET status='RELEASED',released_at=clock_timestamp(),release_reason='ABORTED' WHERE episode_id=%s",
            (episode,),
        )
    assert stage.coverage.reuse_after_protection(proof) is not None
    assert stage.entry_clearance(result, proof) is None


def test_second_fence_rejects_policy_changed_during_checks(
    database, closed, monkeypatch
):
    _, _, _, stage, result, proof = setup(database, closed)
    reuse = stage.coverage.reuse_after_protection
    calls = []

    def changing(saved):
        calls.append(True)
        if len(calls) == 2:
            BusinessState(database).change(
                policy_key(SCOPE),
                expected_version=1,
                request_key="qa-off",
                payload={"enabled": False},
                reason="QA",
            )
        return reuse(saved)

    monkeypatch.setattr(stage.coverage, "reuse_after_protection", changing)
    assert stage.entry_clearance(result, proof) is None
