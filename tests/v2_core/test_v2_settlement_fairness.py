"""Real PG selection/rotation; venue reconciliation is an explicit test stub.

These tests build immutable journal fills through the real storage APIs. They
verify scheduler fairness and clearance, not exchange cash reconciliation.
"""

from dataclasses import replace
from uuid import UUID

import pytest

from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.intents import AdmissionCode, IntentStore, OpenIntent
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.session_lock import session_advisory_lock

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-settlement-fairness", "SANDBOX", "FUTURES")
VERSION = "directional-admission-v2-1"


def episode(database, number, *, scope=SCOPE, state="closed", lifecycle="legacy"):
    """Seed independent symbols so no fixture bypasses active-slot uniqueness."""
    identity = str(UUID(int=number))
    symbol = f"QA{number}USDT"
    version = VERSION if lifecycle == "legacy" else "qa-other-strategy-v1"
    snapshot = {
        "observed_at": 1,
        "decided_at": 2,
        "source": "qa-settlement-fairness",
        "symbol": symbol,
        "rationale": "explicit journal-only fixture",
        "features": {},
    }
    proof = DecisionEvidence(
        version,
        canonical({"mode": "TESTNET_ADMISSION_ONLY"}),
        canonical(snapshot),
    )
    EvidenceStore(database).put(proof)
    intent = OpenIntent(
        identity,
        scope.exchange,
        scope.account_id,
        scope.environment,
        scope.product,
        "s6",
        f"fairness-{number}",
        symbol,
        "BUY",
        "1",
        version,
        proof.config_digest,
        proof.evidence_ref,
    )
    assert IntentStore(database).admit(intent).code is AdmissionCode.ACCEPTED
    orders, ledger = Orders(database), Ledger(database)
    opening, _ = orders.prepare(identity)
    if state == "unfilled":
        return identity
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=opening,
        exchange_fill_id=str(number * 10 + 1),
        quantity="1",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=10,
        evidence={"source": "qa-journal-fixture"},
    )
    orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        evidence={"source": "qa-journal-fixture", "fills_complete": True},
    )
    if state == "open":
        return identity
    quantity = ".5" if state == "partial" else "1"
    closing, _ = orders.prepare(identity, leg="CLOSE", quantity=quantity)
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id=str(number * 10 + 2),
        quantity=quantity,
        price="110",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=20,
        evidence={"source": "qa-journal-fixture"},
    )
    orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        evidence={"source": "qa-journal-fixture", "fills_complete": True},
    )
    return identity


def financial_snapshot(database):
    with database() as conn:
        return (
            conn.execute(
                "SELECT episode_id::text,status,accounting_revision "
                "FROM v2_episodes ORDER BY episode_id"
            ).fetchall(),
            conn.execute("SELECT count(*) FROM v2_settlements").fetchone()[0],
            conn.execute("SELECT count(*) FROM v2_cash_adjustments").fetchone()[0],
        )


def commit_fixture_settlement(database, identity):
    """Storage contract only; the real venue validator is not under this test."""
    ledger = Ledger(database)
    report = ledger.report(identity, settlement_currency="USDT")
    ledger.settle(
        identity,
        currency="USDT",
        evidence={
            "source": "qa-fairness-persistence-contract",
            "observed_at_ms": 30,
            "ledger_revision": report["accounting_revision"],
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
        },
    )
    return {"status": "SETTLED", "episode": identity, "settlement_authorized": False}


def stage(database, monkeypatch, *, limit=1, behavior=None):
    class NoVenue:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __call__(self, *_args, **_kwargs):
            pytest.fail("fairness tests cannot contact a venue")

    service = DirectionalSettlementStage(
        database, NoVenue(), scope=SCOPE, clock_ms=lambda: 70000, limit=limit
    )
    calls, outcomes = [], []

    def settle(identity):
        calls.append(identity)
        return (
            behavior(identity)
            if behavior is not None
            else {"status": "BLOCKED", "reason": "QA_PERMANENT_CASH_MISMATCH"}
        )

    def record(identity):
        outcomes.append(identity)
        return {"episode_id": identity}

    monkeypatch.setattr(service, "settle", settle)
    monkeypatch.setattr(service, "_record_outcome", record)
    return service, calls, outcomes


def test_bad_first_batch_cannot_starve_later_financial_settlement(
    database, monkeypatch
):
    first, second, good = [episode(database, number) for number in (1, 2, 3)]

    def behavior(identity):
        if identity != good:
            raise ValueError("QA_PERMANENT_CASH_MISMATCH")
        return commit_fixture_settlement(database, identity)

    service, calls, _ = stage(database, monkeypatch, limit=2, behavior=behavior)
    unchanged = financial_snapshot(database)
    initial = service.run_once()
    assert calls == [first, second]
    assert initial["status"] == "BLOCKED"
    assert financial_snapshot(database) == unchanged
    following = service.run_once()
    assert calls == [first, second, good, first]
    assert following["settlements"][good]["status"] == "SETTLED"
    assert following["status"] == "BLOCKED"
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_episodes WHERE episode_id=%s", (good,)
        ).fetchone() == ("SETTLED",)
    service.run_once()
    assert calls.count(good) == 1


@pytest.mark.parametrize("failure", ["blocked", "busy", "exception"])
def test_failure_pages_rotate_and_wrap_without_financial_changes(
    database, monkeypatch, failure
):
    identities = [episode(database, number) for number in (1, 2, 3)]

    def behavior(_identity):
        if failure == "exception":
            raise ValueError("QA_CASH_NOT_VERIFIED")
        return {"status": "BUSY" if failure == "busy" else "BLOCKED"}

    service, calls, _ = stage(database, monkeypatch, behavior=behavior)
    unchanged = financial_snapshot(database)
    for _ in range(5):
        result = service.run_once()
        assert result["status"] == "BLOCKED"
        assert result["settlement_authorized"] is False
        assert len(result["settlements"]) == 1
    assert calls == identities + identities[:2]
    assert financial_snapshot(database) == unchanged


def test_pending_selection_keeps_scope_lifecycle_and_closed_fill_filters(
    database, monkeypatch
):
    expected = episode(database, 1)
    episode(database, 2, scope=replace(SCOPE, account_id="another-account"))
    episode(database, 3, scope=replace(SCOPE, environment="LIVE"))
    episode(database, 4, state="unfilled")
    episode(database, 5, state="open")
    episode(database, 6, state="partial")
    episode(database, 7, lifecycle="unrelated")
    service, calls, _ = stage(database, monkeypatch, limit=20)
    unchanged = financial_snapshot(database)
    result = service.run_once()
    assert calls == [expected]
    assert set(result["settlements"]) == {expected}
    assert financial_snapshot(database) == unchanged


def test_successful_rotated_page_does_not_hide_earlier_pending_episode(
    database, monkeypatch
):
    bad, good = episode(database, 1), episode(database, 2)
    service, calls, _ = stage(
        database,
        monkeypatch,
        behavior=lambda identity: commit_fixture_settlement(database, identity),
    )
    service._settlement_after = bad
    result = service.run_once()
    assert calls == [good]
    assert result["settlements"][good]["status"] == "SETTLED"
    assert result["status"] == "BLOCKED"
    assert service._pending_episodes(after="", limit=20) == [(bad,)]


def test_success_result_without_committed_fact_is_not_clear(database, monkeypatch):
    identity = episode(database, 1)
    service, calls, _ = stage(
        database, monkeypatch, behavior=lambda _: {"status": "SETTLED"}
    )
    result = service.run_once()
    assert calls == [identity]
    assert result["status"] == "BLOCKED"
    assert service._pending_episodes(after="", limit=1) == [(identity,)]


def test_only_a_fully_drained_financial_set_reaches_clear(database, monkeypatch):
    identities = [episode(database, number) for number in (1, 2, 3)]
    service, calls, _ = stage(
        database,
        monkeypatch,
        behavior=lambda identity: commit_fixture_settlement(database, identity),
    )
    assert service.run_once()["status"] == "BLOCKED"
    assert service.run_once()["status"] == "BLOCKED"
    assert service.run_once()["status"] == "CLEAR"
    assert calls == identities
    empty = service.run_once()
    assert empty["status"] == "CLEAR"
    assert empty["settlements"] == {}
    assert calls == identities


def test_restart_starts_safe_scan_from_beginning_without_persisted_cursor(
    database, monkeypatch
):
    first, second = episode(database, 1), episode(database, 2)
    service, calls, _ = stage(database, monkeypatch)
    service.run_once()
    service.run_once()
    assert calls == [first, second]
    assert service._settlement_after == second
    recreated, repeated, _ = stage(database, monkeypatch)
    assert recreated._settlement_after == recreated._outcome_after == ""
    recreated.run_once()
    assert repeated == [first]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_business_state").fetchone() == (0,)


def test_outcome_and_financial_rotation_hints_are_independent(database, monkeypatch):
    settled = episode(database, 1)
    commit_fixture_settlement(database, settled)
    first, second = episode(database, 2), episode(database, 3)
    service, calls, outcomes = stage(database, monkeypatch)
    service._outcome_after = second
    service.run_once()
    assert outcomes == [settled]
    assert calls == [first]
    assert service._outcome_after == settled
    assert service._settlement_after == first
    service.run_once()
    assert calls == [first, second]


def test_outer_stage_contention_does_not_advance_unattempted_work(
    database, monkeypatch
):
    episode(database, 1)
    service, calls, _ = stage(database, monkeypatch)
    with session_advisory_lock(
        database, "directional-settlement-stage:" + SCOPE.key
    ) as acquired:
        assert acquired
        assert service.run_once() == {"status": "BLOCKED", "reason": "BUSY"}
    assert service._settlement_after == ""
    assert calls == []


def test_postcommit_error_remains_blocked_but_does_not_repeat_settlement(
    database, monkeypatch
):
    identity = episode(database, 1)

    def behavior(target):
        commit_fixture_settlement(database, target)
        raise ConnectionError("QA_RESPONSE_LOST_AFTER_COMMIT")

    service, calls, _ = stage(database, monkeypatch, behavior=behavior)
    first = service.run_once()
    assert first["status"] == "BLOCKED"
    assert service._pending_episodes(after="", limit=1) == []
    assert service.run_once()["status"] == "CLEAR"
    assert calls == [identity]
    assert financial_snapshot(database)[1] == 1


def test_failed_global_recheck_cannot_return_clear(database, monkeypatch):
    identity = episode(database, 1)
    service, calls, _ = stage(
        database,
        monkeypatch,
        behavior=lambda target: commit_fixture_settlement(database, target),
    )
    original = service._pending_episodes
    reads = []

    def fail_recheck(*, after, limit):
        reads.append((after, limit))
        if len(reads) == 2:
            raise ConnectionError("QA_GLOBAL_RECHECK_UNAVAILABLE")
        return original(after=after, limit=limit)

    monkeypatch.setattr(service, "_pending_episodes", fail_recheck)
    with pytest.raises(ConnectionError, match="QA_GLOBAL_RECHECK_UNAVAILABLE"):
        service.run_once()
    assert calls == [identity]
    assert financial_snapshot(database)[1] == 1
    assert service._settlement_after == identity
