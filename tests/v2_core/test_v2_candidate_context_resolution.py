"""Terminal data abstention with real isolated PG, no venue or model access."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.v2_admission import (
    database,
    scheduler_signal,
    strategy_worker,
)
from v2_core.ingress import IntakeRejected
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision, StrategyWorker

__all__ = ["database"]
CAUSES = ("SHORT_RATIO_STALE", "SHORT_RATIO_MISSING")


def seed_task(database, worker, signal_id, cause, *, attempts=2, token=None):
    token = token or str(uuid4())
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks "
            "(consumer,signal_id,attempts,error_code,lease_token,lease_until) "
            "VALUES(%s,%s,%s,%s,%s,clock_timestamp()+interval '60 seconds')",
            (worker.scope.consumer, signal_id, attempts, cause, token),
        )
    return token


def resolve(worker, signal_id, cause, token, **kwargs):
    return worker.reject_candidate_context(
        signal_id, cause=cause, lease_token=token, attempt=2, **kwargs
    )


def due(database, scheduler, signal_id):
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET next_attempt_at=clock_timestamp() "
            "WHERE consumer=%s AND signal_id=%s",
            (scheduler.worker.scope.consumer, signal_id),
        )


def counts(database, signal_id):
    with database() as conn:
        return tuple(
            conn.execute(query, (signal_id,)).fetchone()[0]
            for query in (
                "SELECT count(*) FROM v2_strategy_decisions WHERE signal_id=%s",
                "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s",
                "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s",
            )
        )


@pytest.mark.parametrize("minimum", [None, True, 1, 21, 2.5])
def test_constructor_rejects_unbounded_terminal_policy(minimum):
    with pytest.raises(ValueError, match="bounded candidate context attempts"):
        StrategyScheduler(
            SimpleNamespace(_connect=lambda: None),
            context_provider=lambda _: {},
            context_failure_attempts=minimum,
        )


@pytest.mark.parametrize(
    "cause", ["ENTRY_RESEARCH_CONTEXT_EXPIRED", "ACCOUNT_CONTEXT_STALE", "unknown"]
)
def test_terminal_helper_does_not_accept_other_failure_classes(cause):
    worker = object.__new__(StrategyWorker)
    with pytest.raises(ValueError, match="CANDIDATE_CONTEXT_RESOLUTION_INVALID"):
        resolve(worker, str(uuid4()), cause, str(uuid4()))


@pytest.mark.parametrize("cause", CAUSES)
def test_first_wait_second_identical_failure_resolves_atomically_without_orders(
    database, monkeypatch, cause
):
    worker, runtime = strategy_worker(database)
    signal_id = scheduler_signal(database, "repeat")

    def context(_):
        raise ValueError(cause)

    def forbidden(*_args, **_kwargs):
        pytest.fail("abstention must not evaluate, query JEV or reserve/prepare")

    monkeypatch.setattr(worker, "decide", forbidden)
    monkeypatch.setattr(runtime, "accept_open", forbidden)
    worker.entry_filter = SimpleNamespace(assess=forbidden)
    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(limit=1) == {signal_id: "DEFERRED"}
    assert counts(database, signal_id) == (0, 0, 0)
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1) == {signal_id: "IGNORED"}
    assert counts(database, signal_id) == (1, 1, 0)
    proof = worker.decision(signal_id)["snapshot"]
    assert proof["expires_at_ms"] == 10
    assert proof["features"]["context"] == {}
    assert proof["features"]["evaluation"]["candidate_context_resolution"] == {
        "scope": "CANDIDATE",
        "cause": cause,
        "attempt": 2,
        "minimum_attempts": 2,
        "same_previous_cause": True,
        "context_available": False,
        "execution_authorized": False,
    }
    assert scheduler.last_run_metrics["candidate_resolutions"] == 1
    assert scheduler.last_run_diagnostics["candidate_resolutions"][0]["cause"] == cause
    assert scheduler.run_once(limit=1) == {}
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute(
            "SELECT completed_at IS NOT NULL,lease_token,error_code FROM v2_strategy_tasks "
            "WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone() == (True, None, cause)


def test_changed_error_does_not_count_as_consecutive_missing_data(database):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "changed")
    cause = ["SHORT_RATIO_STALE"]

    def context(_):
        raise ValueError(cause[0])

    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(limit=1)[signal_id] == "DEFERRED"
    cause[0] = "SHORT_RATIO_MISSING"
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1)[signal_id] == "DEFERRED"
    assert counts(database, signal_id) == (0, 0, 0)
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1)[signal_id] == "IGNORED"
    assert (
        worker.decision(signal_id)["snapshot"]["features"]["evaluation"][
            "candidate_context_resolution"
        ]["attempt"]
        == 3
    )


@pytest.mark.parametrize("first_failure", ["CONSUME", "CONTEXT_SUBCLASS"])
def test_prior_matching_text_is_not_proof_of_another_validated_context_wait(
    database, monkeypatch, first_failure
):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "untyped-prior")
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    original_consume = worker.consume

    def bad_consume(*_args, **_kwargs):
        raise ValueError("SHORT_RATIO_STALE")

    def bad_context(_):
        raise IntakeRejected("SHORT_RATIO_STALE")

    if first_failure == "CONSUME":
        monkeypatch.setattr(worker, "consume", bad_consume)
    else:
        scheduler.context_provider = bad_context
    assert scheduler.run_once(limit=1)[signal_id] == "UNAVAILABLE"
    with database() as conn:
        assert conn.execute(
            "SELECT error_code FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone() == (
            ("ValueError" if first_failure == "CONSUME" else "IntakeRejected"),
        )
    monkeypatch.setattr(worker, "consume", original_consume)

    def known_context(_):
        raise ValueError("SHORT_RATIO_STALE")

    scheduler.context_provider = known_context
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1)[signal_id] == "DEFERRED"
    assert counts(database, signal_id) == (0, 0, 0)
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1)[signal_id] == "IGNORED"


def test_explicit_three_attempt_policy_and_research_expiry_remain_bounded(database):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "three")
    reason = ["ENTRY_RESEARCH_CONTEXT_EXPIRED"]

    def context(_):
        raise ValueError(reason[0])

    scheduler = StrategyScheduler(
        worker, context_provider=context, context_failure_attempts=3
    )
    for _ in range(3):
        assert scheduler.run_once(limit=1)[signal_id] == "DEFERRED"
        due(database, scheduler, signal_id)
    assert counts(database, signal_id) == (0, 0, 0)
    reason[0] = "SHORT_RATIO_STALE"
    assert scheduler.run_once(limit=1)[signal_id] == "DEFERRED"
    due(database, scheduler, signal_id)
    assert scheduler.run_once(limit=1)[signal_id] == "IGNORED"


@pytest.mark.parametrize(
    "mutation",
    ["expired_lease", "stolen_lease", "changed_cause", "wrong_attempt", "completed"],
)
def test_only_current_lease_and_persisted_previous_cause_may_terminalize(
    database, mutation
):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "capability")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    clauses = {
        "expired_lease": "lease_until=clock_timestamp()-interval '1 second'",
        "stolen_lease": "lease_token='" + str(uuid4()) + "'",
        "changed_cause": "error_code='SHORT_RATIO_MISSING'",
        "wrong_attempt": "attempts=3",
        "completed": "completed_at=clock_timestamp()",
    }
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET "
            + clauses[mutation]
            + " WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        )
    assert resolve(worker, signal_id, CAUSES[0], token) is None
    assert counts(database, signal_id) == (0, 0, 0)


def test_expiry_at_resolution_is_expired_without_refreshing_original_lifetime(database):
    worker, _ = strategy_worker(database, now=lambda: 10)
    signal_id = scheduler_signal(database, "expired")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    result = resolve(worker, signal_id, CAUSES[0], token)
    assert result["status"] == "EXPIRED"
    assert worker.decision(signal_id)["snapshot"]["expires_at_ms"] == 10
    assert counts(database, signal_id) == (1, 1, 0)


def test_resolution_cannot_claim_another_accounts_task(database):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "other-account")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    peer, _ = strategy_worker(database)
    peer.scope = replace(peer.scope, account_id="different-qa-account")
    assert resolve(peer, signal_id, CAUSES[0], token) is None
    assert counts(database, signal_id) == (0, 0, 0)
    assert resolve(worker, signal_id, CAUSES[0], token)["status"] == "IGNORED"


def test_existing_expired_receipt_is_returned_unchanged(database):
    worker, runtime = strategy_worker(database)
    signal_id = scheduler_signal(database, "receipt")
    runtime.data.signals.complete(
        consumer=worker.scope.consumer,
        signal_id=signal_id,
        outcome="EXPIRED",
        reason="original expiry",
    )
    result = resolve(worker, signal_id, CAUSES[0], str(uuid4()))
    assert result["status"] == "EXPIRED"
    assert result["reason"] == "original expiry"
    assert counts(database, signal_id) == (0, 1, 0)


def test_concurrent_rejection_has_one_immutable_decision_and_receipt(database):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "concurrent-rejection")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: resolve(worker, signal_id, CAUSES[0], token), range(2))
        )
    assert results[0] == results[1]
    assert counts(database, signal_id) == (1, 1, 0)
    with pytest.raises(Exception, match="immutable"), database() as conn:
        conn.execute(
            "UPDATE v2_strategy_decisions SET action='EXPIRED' WHERE signal_id=%s",
            (signal_id,),
        )


@pytest.mark.parametrize("winner", ["OPEN", "ABSTAIN"])
def test_concurrent_open_and_abstention_preserve_first_committed_decision(
    database, winner
):
    worker, runtime = strategy_worker(database)
    signal_id = scheduler_signal(database, "race")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    reached, resume = Event(), Event()
    original_accept = runtime.accept_open

    def decide(*_):
        if winner == "ABSTAIN":
            reached.set()
            assert resume.wait(5)
        return StrategyDecision("OPEN", "valid candidate", "BUY", "0.01")

    def accept(*args, **kwargs):
        reached.set()  # The OPEN decision is already committed before this call.
        assert resume.wait(5)
        return original_accept(*args, **kwargs)

    worker.decide = decide
    if winner == "OPEN":
        runtime.accept_open = accept
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.consume, signal_id, context={})
        try:
            assert reached.wait(5)
            before = worker.decision(signal_id)
            rejection = resolve(worker, signal_id, CAUSES[0], token)
            if winner == "OPEN":
                assert rejection is None
                assert worker.decision(signal_id) == before
            else:
                assert rejection["status"] == "IGNORED"
        finally:
            resume.set()
        outcome = future.result(timeout=5)
    assert outcome["status"] == ("PREPARED" if winner == "OPEN" else "IGNORED")
    assert counts(database, signal_id) == (1, 1, int(winner == "OPEN"))


def test_already_admitted_intent_and_order_remain_identical(database):
    worker, runtime = strategy_worker(database)
    signal_id = scheduler_signal(database, "existing-order")
    opening = worker.consume(signal_id, context={})
    before = runtime.data.trace(opening["intent_id"])
    token = seed_task(database, worker, signal_id, CAUSES[0])
    assert resolve(worker, signal_id, CAUSES[0], token) is None
    assert runtime.data.trace(opening["intent_id"]) == before


@pytest.mark.parametrize(
    "statement", ["v2_decision_evidence", "v2_strategy_decisions", "v2_signal_receipts"]
)
def test_each_transaction_failure_rolls_back_evidence_decision_and_receipt(
    database, monkeypatch, statement
):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "rollback")
    token = seed_task(database, worker, signal_id, CAUSES[0])
    evidence_query = (
        "SELECT evidence_ref,config_digest,strategy_version,config,snapshot "
        "FROM v2_decision_evidence ORDER BY evidence_ref"
    )
    with database() as conn:
        original_evidence = conn.execute(evidence_query).fetchall()

    @contextmanager
    def broken():
        with database() as conn:

            class Proxy:
                def execute(self, query, params=None):
                    if "INSERT INTO " + statement in query:
                        raise ConnectionError("QA transaction failure")
                    return conn.execute(query, params)

            yield Proxy()

    monkeypatch.setattr(worker, "_connect", broken)
    with pytest.raises(ConnectionError, match="QA transaction failure"):
        resolve(worker, signal_id, CAUSES[0], token)
    assert counts(database, signal_id) == (0, 0, 0)
    with database() as conn:
        assert conn.execute(evidence_query).fetchall() == original_evidence
        assert conn.execute(
            "SELECT count(*) FROM v2_decision_evidence WHERE snapshot->>'signal_id'=%s",
            (signal_id,),
        ).fetchone() == (0,)


def test_later_new_signal_can_use_fresh_data_after_previous_abstention(database):
    worker, _ = strategy_worker(database)
    first = scheduler_signal(database, "old")
    token = seed_task(database, worker, first, CAUSES[0])
    assert resolve(worker, first, CAUSES[0], token)["status"] == "IGNORED"
    second = scheduler_signal(database, "fresh")
    assert worker.consume(second, context={})["status"] == "PREPARED"
    assert counts(database, first) == (1, 1, 0)
    assert counts(database, second) == (1, 1, 1)
