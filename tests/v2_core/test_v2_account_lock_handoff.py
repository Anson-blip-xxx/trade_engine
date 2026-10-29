"""Opening/cash/disposition handoff must not rely on client socket cleanup."""

from contextlib import contextmanager

import pytest
from test_v2_directional_cash import SCOPE as CASH_SCOPE
from test_v2_directional_cash import auditor
from test_v2_directional_cash import closed as closed_fixture
from test_v2_intent_admission import database as database_fixture
from test_v2_maintenance_reconciliation import maintenance as maintenance_fixture
from test_v2_maintenance_resolution import prepared
from test_v2_protection_child import SCOPE
from test_v2_protection_child import setup as setup_fixture
from test_v2_venue_readiness import case as opening_fixture

from v2_core.session_lock import session_advisory_lock

database = database_fixture
closed = closed_fixture
setup = setup_fixture
maintenance = maintenance_fixture
opening = opening_fixture


@contextmanager
def retained_connections(database, *, fail_unlock=False):
    owners = []

    class Owner:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def execute(self, statement, *params):
            if fail_unlock and "pg_advisory_unlock(" in statement:
                raise RuntimeError("private driver error")
            return self.conn.execute(statement, *params)

    @contextmanager
    def connect():
        manager = database()
        owner = Owner(manager.__enter__())
        owners.append((manager, owner))
        try:
            yield owner
        except BaseException:
            if not owner.closed:
                owner.rollback()
            raise
        else:
            if not owner.closed:
                owner.commit()

    try:
        yield connect, owners
    finally:
        for manager, owner in reversed(owners):
            if not owner.closed:
                manager.__exit__(None, None, None)


def available(database, scope, *, stage=None):
    key = (stage or "testnet-protocol-account:") + scope.key
    with session_advisory_lock(database, key) as held:
        assert held


@pytest.mark.parametrize("reject_before_submit", [False, True])
def test_opening_releases_on_success_and_preflight_rejection(
    database, opening, reject_before_submit
):
    _data, order, venue, gate, runner, sent = opening
    if reject_before_submit:
        venue.rows["/fapi/v1/symbolConfig"][0]["leverage"] = 20
    with retained_connections(database) as (connect, owners):
        gate.connect = connect
        expected = "REJECTED" if reject_before_submit else "ACKNOWLEDGED"
        assert runner.dispatch(order) == expected
        assert len(sent) == (0 if reject_before_submit else 1)
        assert owners and all(not owner.closed for _, owner in owners)
        available(database, SCOPE)


def test_release_failure_after_submission_remains_unknown_and_never_resends(
    database, opening
):
    _data, order, _venue, gate, runner, sent = opening
    with retained_connections(database, fail_unlock=True) as (connect, owners):
        gate.connect = connect
        # Submission already happened. Unlock failure cannot be reclassified as
        # definitely-not-sent, nor permit a second submission.
        assert runner.dispatch(order) == "UNKNOWN"
        assert len(sent) == 1
        assert owners[0][1].closed
        assert runner.dispatch(order) == "UNKNOWN"
        assert len(sent) == 1


@pytest.mark.parametrize("failure_at", ["CONNECT", "TIMEOUT", "ACQUIRE", "COMMIT"])
def test_lock_acquisition_failure_is_definitely_not_sent(database, opening, failure_at):
    _data, order, venue, gate, runner, sent = opening

    class FaultyOwner:
        def __init__(self, conn):
            self.conn = conn
            self.failed = False

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def execute(self, statement, *params):
            if not self.failed and (
                failure_at == "TIMEOUT"
                and "SET LOCAL" in statement
                or failure_at == "ACQUIRE"
                and "pg_try_advisory_lock(" in statement
            ):
                self.failed = True
                raise RuntimeError("private lock transport detail")
            return self.conn.execute(statement, *params)

        def commit(self):
            if failure_at == "COMMIT" and not self.failed:
                self.failed = True
                raise RuntimeError("private commit transport detail")
            return self.conn.commit()

    @contextmanager
    def connect():
        if failure_at == "CONNECT":
            raise RuntimeError("private connection detail")
        with database() as conn:
            yield FaultyOwner(conn)

    gate.connect = connect
    assert runner.dispatch(order) == "REJECTED"
    assert not sent and not venue.calls
    assert runner.recover(order) == "REJECTED"
    available(database, SCOPE)


@pytest.mark.parametrize("income_failure", [False, True])
def test_cash_audit_releases_before_next_episode_and_stage(
    database, closed, income_failure
):
    _runtime, _episode, income = closed
    income.fail = income_failure
    with retained_connections(database) as (connect, owners):
        stage = auditor(connect, income)
        result = stage.run_once()
        # Cash evidence alone does not authorize final account settlement.
        assert result["status"] == "BLOCKED"
        assert result["settlement_authorized"] is False
        assert next(iter(result["audits"].values()))["status"] == (
            "INCOME_INCOMPLETE" if income_failure else "CASH_MATCHED"
        )
        assert owners and all(not owner.closed for _, owner in owners)
        available(database, CASH_SCOPE)
        available(database, CASH_SCOPE, stage="directional-cash-stage:")
        second = stage.run_once()
        assert second["reason"] == "ACCOUNT_SETTLEMENT_REQUIRED"
        assert len(second["audits"]) == 1


@pytest.mark.parametrize("runner_stopped", [False, True])
def test_audited_disposition_releases_lock_without_weakening_quarantine(
    database, maintenance, runner_stopped
):
    data, spec, _remote, case, worker = prepared(database, maintenance)
    worker.runner_stopped = lambda: runner_stopped
    with retained_connections(database) as (connect, owners):
        worker.connect = connect
        if runner_stopped:
            result = worker.resolve(case)
            assert result["venue_outcome"] == "UNKNOWN"
            assert worker.resolve(case) == result
        else:
            with pytest.raises(ValueError, match="TRADING_RUNNER_NOT_STOPPED"):
                worker.resolve(case)
        assert owners and all(not owner.closed for _, owner in owners)
        available(database, SCOPE)
        trace = data.trace(spec.episode)
        target = next(item for item in trace["orders"] if item["order_id"] == case)
        assert target["status"] == ("RECONCILED" if runner_stopped else "UNKNOWN")
