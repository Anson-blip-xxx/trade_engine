"""Real management workflows release locks before backend socket shutdown."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_exit_stage import setup_exit
from test_v2_directional_settlement import baseline
from test_v2_directional_settlement import service as settlement_service
from test_v2_intent_admission import database as database_fixture
from test_v2_opening_cancel import case as cancel_case_fixture
from test_v2_protection_install import case as install_case_fixture
from test_v2_session_lock_release import _available
from test_v2_unprotected_emergency import naked

from v2_core.ledger import Ledger
from v2_core.session_lock import session_advisory_lock

database = database_fixture
closed = closed_fixture
cancel_case = cancel_case_fixture
install_case = install_case_fixture


@pytest.fixture
def deferred_sessions(database, monkeypatch):
    owners, keys = [], []

    @contextmanager
    def retain_after_unlock(connect, key):
        @contextmanager
        def retain_connection():
            manager = connect()
            owner = manager.__enter__()
            owners.append((manager, owner))
            yield owner

        with session_advisory_lock(retain_connection, key) as acquired:
            if acquired:
                keys.append(key)
            yield acquired

    def verify():
        assert keys
        assert all(not owner.closed for _, owner in owners)
        for key in set(keys):
            assert _available(database, key), key

    for module in (
        "v2_core.protection_install",
        "v2_core.opening_cancel",
        "services.v2_directional_exit",
        "services.v2_directional_settlement",
    ):
        monkeypatch.setattr(module + ".session_advisory_lock", retain_after_unlock)
    try:
        yield SimpleNamespace(
            owners=owners, keys=keys, verify=verify, retain=retain_after_unlock
        )
    finally:
        for manager, owner in reversed(owners):
            if not owner.closed:
                owner.rollback()
                owner.execute("SELECT pg_advisory_unlock_all()")
                owner.commit()
            manager.__exit__(None, None, None)


def test_install_stop_handoff_keeps_original_single_write(
    install_case, deferred_sessions
):
    _data, spec, venue, _reference, installer = install_case
    assert installer.ensure(spec)["status"] == "NEW"
    deferred_sessions.verify()
    assert installer.ensure(spec)["status"] == "NEW"
    assert len(venue.writes) == 1
    assert len(deferred_sessions.keys) == 1


def test_rejected_stop_preflight_releases_without_write(
    install_case, deferred_sessions
):
    _data, spec, venue, reference, installer = install_case
    reference["observed_at_ms"] = 1001
    with pytest.raises(ValueError, match="STALE_OR_WRONG_REFERENCE"):
        installer.ensure(spec)
    assert not venue.writes
    deferred_sessions.verify()
    reference["observed_at_ms"] = 1000
    assert installer.ensure(spec)["status"] == "NEW"
    deferred_sessions.verify()
    assert len(venue.writes) == 1


def test_partial_cancel_to_stop_handoff_never_needs_socket_close(
    cancel_case, deferred_sessions
):
    data, order, venue, worker, spec = cancel_case
    assert worker.ensure(order, spec)["status"] == "NEW"
    deferred_sessions.verify()
    assert len(deferred_sessions.keys) == 2
    assert len(venue.deletes) == len(venue.posts) == 1
    assert data.trace(spec.episode)["orders"][0]["status"] == "CANCELLED"
    assert worker.ensure(order, spec)["status"] == "NEW"
    deferred_sessions.verify()
    assert len(venue.deletes) == len(venue.posts) == 1


def test_strategy_exit_releases_stage_and_account_after_reduce_only_post(
    database, deferred_sessions
):
    runtime, opened, venue, stage = setup_exit(
        database, allow_writes=True, mark_price="105.1"
    )
    result = stage.run_once()
    assert result["status"] == "BLOCKED"  # Accepted close remains unfilled.
    assert result["exits"][opened["intent_id"]]["outcome"] == "ACKNOWLEDGED"
    deferred_sessions.verify()
    assert any(
        key.startswith("directional-exit-stage:") for key in deferred_sessions.keys
    )
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    again = stage.run_once()
    assert again["reason"] == "CLOSE_RECOVERY_PENDING"
    deferred_sessions.verify()
    assert len(venue.order_writes) == 1
    assert len(runtime.data.trace(opened["intent_id"])["orders"]) == 2


def test_emergency_exit_releases_without_clearing_unknown_protection(
    database, deferred_sessions
):
    _runtime, opened, venue, stage = naked(database, allow_writes=True)
    result = stage.run_once()
    assert result["status"] == "BLOCKED"
    assert result["emergency_exits"][opened["intent_id"]]["outcome"] == "ACKNOWLEDGED"
    deferred_sessions.verify()
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert len(venue.writes) == 1
    assert stage.run_once()["reason"] == "CLOSE_RECOVERY_PENDING"
    assert len(venue.order_writes) == 1
    deferred_sessions.verify()


def test_settlement_handoff_preserves_cash_proof_and_idempotency(
    database, closed, deferred_sessions
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    stage = settlement_service(database, income)
    result = stage.run_once()
    assert result["status"] == "CLEAR", result
    assert result["settlements"][episode]["status"] == "SETTLED"
    deferred_sessions.verify()
    assert len(deferred_sessions.keys) == 2
    assert stage.run_once()["status"] == "CLEAR"
    deferred_sessions.verify()
    trace = runtime.data.trace(episode)
    assert len(trace["settlements"]) == len(trace["income_allocations"]) == 1


def test_settlement_failed_business_proof_does_not_retain_management_lock(
    database, closed, deferred_sessions
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    stage = settlement_service(database, income, balance="123")
    result = stage.run_once()
    assert result["status"] == "BLOCKED"
    deferred_sessions.verify()
    assert runtime.data.trace(episode)["settlements"] == []


def test_settlement_rollback_then_retry_with_all_guard_backends_still_alive(
    database, closed, deferred_sessions, monkeypatch
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    monkeypatch.setattr(
        "services.v2_directional_cash.session_advisory_lock",
        deferred_sessions.retain,
    )
    original = Ledger.settle

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit lost")

    monkeypatch.setattr(Ledger, "settle", fail_commit)
    first = settlement_service(database, income).run_once()
    assert first["status"] == "BLOCKED", first
    trace = runtime.data.trace(episode)
    assert not trace["cash"]
    assert not trace["income_allocations"]
    assert not trace["settlements"]
    deferred_sessions.verify()

    monkeypatch.setattr(Ledger, "settle", original)
    second = settlement_service(database, income).run_once()
    assert second["status"] == "CLEAR", second
    assert second["settlements"][episode]["status"] == "SETTLED"
    deferred_sessions.verify()
    trace = runtime.data.trace(episode)
    assert len(trace["settlements"]) == len(trace["income_allocations"]) == 1
