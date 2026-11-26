"""Typed unsent cleanup against isolated PG; no exchange or running services."""

from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from services.v2_execution_guard import create_execution_guard
from tests.v2_core import test_v2_strategy_deployment_execution as financial
from tests.v2_core.test_v2_settlement_execution_fence import (
    database,
    fenced,
    route_update,
)
from tests.v2_core.test_v2_strategy_entry_job import case
from tests.v2_core.test_v2_strategy_plan_admission import counts
from tests.v2_core.test_v2_strategy_prepare import at_database_time
from v2_core.account_risk import AccountScope, RiskReference
from v2_core.orders import Orders
from v2_core.state import BusinessState
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_pending_cleanup import StrategyPendingCleanup
from v2_core.strategy_plan_admission import StrategyPlanAdmission, opportunity_key
from v2_core.strategy_prepare import StrategyPrepareRecovery

__all__ = ["database", "fenced"]


def pending(fenced, *, prepared=True):
    selected = case(fenced)
    with fenced.admin() as conn:
        candidate = selected.worker.store.read(
            conn,
            scope=selected.worker.scope,
            source="tv_bridge",
            signal_id=selected.identity,
        ).candidate
    admitted = selected.worker.admission.admit(
        selected.plan,
        selected.deployment,
        signal_id=selected.identity,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    selected.episode = admitted["intent_id"]
    selected.order = None
    if prepared:
        selected.order = StrategyPrepareRecovery(
            fenced.admin, scope=selected.plan.scope
        ).recover(selected.episode)["order_id"]
    return selected


def port(fenced, *, connect=None, scope=None, guard=None):
    return StrategyPendingCleanup(
        fenced.admin if connect is None else connect,
        scope=financial.SCOPE if scope is None else scope,
        commit_guard=guard
        or create_execution_guard(fenced.admin, **fenced.args).require_transaction,
    )


def withdraw(fenced, selected, cause):
    connect = fenced.admin
    if cause == "DRAINING":
        route_update(fenced, "SET phase='DRAINING'")
    elif cause == "EXPIRED":
        connect = at_database_time(fenced.admin, selected.plan.expires_at_ms + 1)
    else:
        StrategyDeployments(fenced.admin, selected.registry).publish(
            replace(
                selected.deployment,
                generation=2,
                mode="OFF" if cause == "OFF" else "ACTIVE",
            ),
            expected_generation=1,
            request_id=str(uuid4()),
        )
    return port(fenced, connect=connect)


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("cause", ["DRAINING", "OFF", "REPLACED", "EXPIRED"])
def test_only_unsent_requests_are_audited_and_replay_original_identity(
    fenced, prepared, cause
):
    selected = pending(fenced, prepared=prepared)
    adapter = withdraw(fenced, selected, cause)
    before = counts(fenced.admin)
    result = adapter.cleanup(selected.episode)
    terminal = "EXPIRED" if cause == "EXPIRED" and not prepared else "CANCELLED"
    assert result["status"] == terminal
    assert result["execution_authorized"] is result["venue_flat_verified"] is False
    committed = counts(fenced.admin)
    assert committed[0] == before[0] == 1
    assert committed[2:] == before[2:]
    replay = adapter.cleanup(selected.episode)
    assert replay["status"] == "ALREADY_TERMINATED"
    assert replay["intent_status"] == terminal
    assert counts(fenced.admin) == committed
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_trade_intents").fetchall() == [
            (terminal,)
        ]
        assert conn.execute("SELECT status FROM v2_episodes").fetchall() == (
            [("ABORTED",)] if prepared else []
        )
        assert conn.execute(
            "SELECT status,version FROM v2_order_events ORDER BY version"
        ).fetchall() == ([("PREPARED", 1), ("CANCELLED", 2)] if prepared else [])
        assert conn.execute(
            "SELECT count(*) FROM v2_domain_outbox WHERE event_type='STRATEGY_PENDING_CLEANUP'"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_fills").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


@pytest.mark.parametrize("prepared", [False, True])
def test_current_active_plan_is_not_cancelled(fenced, prepared):
    selected = pending(fenced, prepared=prepared)
    before = counts(fenced.admin)
    assert port(fenced).cleanup(selected.episode)["status"] == "UNCHANGED"
    assert counts(fenced.admin) == before


@pytest.mark.parametrize("change", ["STOPPED", "STARTING", "token", "epoch"])
def test_lost_management_authority_cannot_cancel(fenced, change):
    selected = pending(fenced)
    before = counts(fenced.admin)
    if change in {"STOPPED", "STARTING"}:
        route_update(
            fenced,
            "SET phase=%s,worker_token=CASE WHEN %s='STOPPED' THEN NULL ELSE worker_token END",
            (change, change),
        )
    elif change == "token":
        route_update(fenced, "SET worker_token=%s", (str(uuid4()),))
    else:
        route_update(fenced, "SET epoch=epoch+1")
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        port(fenced).cleanup(selected.episode)
    assert counts(fenced.admin) == before


def test_missing_transaction_phase_is_not_a_cached_management_permit(fenced):
    selected = pending(fenced)
    guard = create_execution_guard(fenced.admin, **fenced.args)

    def incomplete(conn, **kwargs):
        guard.require_transaction(conn, **kwargs)

    with pytest.raises(ValueError, match="TRANSACTION_FENCE_REQUIRED"):
        port(fenced, guard=incomplete).cleanup(selected.episode)


@pytest.mark.parametrize("status", ["SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"])
def test_submitted_or_ambiguous_order_keeps_identity_and_held_risk(fenced, status):
    selected = pending(fenced)
    orders = Orders(fenced.admin)
    assert orders.transition(
        selected.order,
        expected_version=1,
        status="SUBMITTING",
        evidence={"origin": "ISOLATED_QA"},
        risk_reference=RiskReference(
            "SANDBOX", "BTCUSDT", "qa", "100", financial.now()
        ),
        require_account_risk=True,
    )
    if status != "SUBMITTING":
        assert orders.transition(
            selected.order,
            expected_version=2,
            status=status,
            evidence={"origin": "ISOLATED_QA"},
            exchange_order_id="qa-venue-1" if status == "ACKNOWLEDGED" else None,
        )
    before = counts(fenced.admin)
    adapter = withdraw(fenced, selected, "DRAINING")
    assert adapter.cleanup(selected.episode)["status"] == "RECONCILIATION_REQUIRED"
    assert counts(fenced.admin) == before
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT order_id::text,status FROM v2_orders"
        ).fetchall() == [(selected.order, status)]
        assert conn.execute("SELECT status FROM v2_risk_reservations").fetchall() == [
            ("HELD",)
        ]
        assert conn.execute("SELECT status FROM v2_episodes").fetchall() == [
            ("ACTIVE",)
        ]


def test_order_free_unknown_is_not_assumed_unsent(fenced):
    selected = pending(fenced, prepared=False)
    with fenced.admin() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET status='UNKNOWN',version=version+1 WHERE intent_id=%s",
            (selected.episode,),
        )
    before = counts(fenced.admin)
    assert (
        withdraw(fenced, selected, "DRAINING").cleanup(selected.episode)["status"]
        == "RECONCILIATION_REQUIRED"
    )
    assert counts(fenced.admin) == before


def test_prepared_label_cannot_hide_submission_history(fenced):
    selected = pending(fenced)
    # Controlled QA corruption: the immutable journal contradicts the root.
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_order_events(event_id,order_id,version,status,evidence) VALUES (%s,%s,2,'SUBMITTING','{}')",
            (str(uuid4()), selected.order),
        )
    before = counts(fenced.admin)
    with pytest.raises(ValueError, match="ORDER_HISTORY_INVALID"):
        withdraw(fenced, selected, "DRAINING").cleanup(selected.episode)
    assert counts(fenced.admin) == before


def test_old_cancelled_order_without_cleanup_audit_is_not_replayed_as_verified(fenced):
    selected = pending(fenced)
    assert Orders(fenced.admin).transition(
        selected.order,
        expected_version=1,
        status="CANCELLED",
        evidence={"reason": "OTHER_OWNER_QA"},
    )
    with pytest.raises(ValueError, match="AUDIT_REQUIRED"):
        port(fenced).cleanup(selected.episode)


def test_forged_cleanup_audit_cannot_replace_actual_cancel_evidence(fenced):
    selected = pending(fenced)
    assert Orders(fenced.admin).transition(
        selected.order,
        expected_version=1,
        status="CANCELLED",
        evidence={"reason": "OTHER_OWNER_QA"},
    )
    with fenced.admin() as conn:
        conn.execute(
            'INSERT INTO v2_domain_outbox(event_id,intent_id,event_type,payload) VALUES (%s,%s,\'STRATEGY_PENDING_CLEANUP\',\'{"schema":"strategy-pending-cleanup-v1","data_revision":1}\')',
            (str(uuid4()), selected.episode),
        )
    with pytest.raises(ValueError, match="AUDIT_INVALID"):
        port(fenced).cleanup(selected.episode)


def test_permanent_opportunity_proof_is_required_even_while_draining(fenced):
    selected = pending(fenced)
    BusinessState(fenced.admin).change(
        opportunity_key(selected.plan),
        expected_version=1,
        request_key="qa-corrupt-claim",
        payload={},
        reason="ISOLATED_QA_CORRUPTION",
    )
    before = counts(fenced.admin)
    with pytest.raises(Exception, match="STRATEGY_OPPORTUNITY_RECEIPT_INVALID"):
        withdraw(fenced, selected, "DRAINING").cleanup(selected.episode)
    assert counts(fenced.admin) == before


def test_foreign_account_intent_is_not_mutated_or_selected(fenced):
    selected = pending(fenced, prepared=False)
    foreign_scope = AccountScope("BINANCE", "qa-foreign-cleanup", "SANDBOX", "FUTURES")
    binding = replace(selected.deployment, scope=foreign_scope)
    plan = replace(selected.plan, scope=foreign_scope)
    StrategyDeployments(fenced.admin, selected.registry).publish(
        binding, expected_generation=0, request_id=str(uuid4())
    )
    foreign = StrategyPlanAdmission(fenced.admin, selected.registry).admit(
        plan, binding, rationale="isolated foreign QA"
    )["intent_id"]
    adapter = withdraw(fenced, selected, "DRAINING")
    with pytest.raises(ValueError, match="INTENT_NOT_FOUND"):
        adapter.cleanup(foreign)
    page = adapter.run_once()
    assert [result["intent_id"] for result in page["results"]] == [selected.episode]
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT status FROM v2_trade_intents WHERE intent_id=%s", (foreign,)
        ).fetchone() == ("RECEIVED",)


def test_failed_commit_ack_replays_own_terminal_audit_without_duplicate_event(fenced):
    selected = pending(fenced)
    withdraw(fenced, selected, "DRAINING")

    @contextmanager
    def lost_ack():
        with fenced.admin() as conn:
            yield conn
        raise OSError("synthetic commit ACK lost")

    with pytest.raises(OSError, match="commit ACK"):
        port(fenced, connect=lost_ack).cleanup(selected.episode)
    before = counts(fenced.admin)
    assert port(fenced).cleanup(selected.episode)["status"] == "ALREADY_TERMINATED"
    assert counts(fenced.admin) == before


def test_stop_after_cancellation_rolls_back_whole_business_transaction(
    fenced, monkeypatch
):
    selected = pending(fenced)
    adapter = withdraw(fenced, selected, "DRAINING")
    before = counts(fenced.admin)
    stop = Event()
    transition = Orders.transition

    def stopping(*args, **kwargs):
        value = transition(*args, **kwargs)
        stop.set()
        return value

    monkeypatch.setattr(Orders, "transition", stopping)
    with pytest.raises(ValueError, match="STRATEGY_CLEANUP_STOPPED"):
        adapter.cleanup(selected.episode, stop_requested=stop.is_set)
    assert counts(fenced.admin) == before
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_orders").fetchall() == [
            ("PREPARED",)
        ]


def test_failed_first_page_advances_hint_and_never_proves_flat(fenced):
    selected = pending(fenced, prepared=False)
    another = StrategyPlanAdmission(fenced.admin, selected.registry).admit(
        replace(selected.plan, opportunity_id=str(uuid4())),
        selected.deployment,
        rationale="second QA",
    )["intent_id"]
    first, second = sorted((selected.episode, another))
    with fenced.admin() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET status='UNKNOWN',version=version+1 WHERE intent_id=%s",
            (first,),
        )
    adapter = withdraw(fenced, selected, "DRAINING")
    page = adapter.run_once(limit=1)
    assert page["results"][0]["status"] == "RECONCILIATION_REQUIRED"
    assert page["next_after"] == first
    following = adapter.run_once(limit=1, after=page["next_after"])
    assert following["results"][0]["intent_id"] == second
    assert following["results"][0]["status"] == "CANCELLED"
    assert following["venue_flat_verified"] is False
    assert adapter.run_once(limit=1, after=second)["next_after"] is None
    assert adapter.run_once(limit=1)["results"][0]["intent_id"] == first


def test_restricted_business_role_restored_and_route_fence_held_until_commit(fenced):
    selected = pending(fenced)
    withdraw(fenced, selected, "DRAINING")
    with fenced.admin() as conn:
        tables = (
            "v2_trade_intents",
            "v2_decision_evidence",
            "v2_orders",
            "v2_order_events",
            "v2_episodes",
            "v2_fills",
            "v2_business_state",
            "v2_state_history",
            "v2_signal_receipts",
            "v2_domain_outbox",
            "v2_risk_reservations",
            "v2_settlements",
        )
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE ON {} TO {}").format(
                sql.SQL(",").join(map(sql.Identifier, tables)),
                sql.Identifier(fenced.worker),
            )
        )
    real_guard = create_execution_guard(fenced.restricted, **fenced.args)

    def checked(conn, **kwargs):
        phase = real_guard.require_transaction(conn, **kwargs)
        assert phase == "DRAINING"
        assert conn.execute("SELECT current_user").fetchone() == (fenced.worker,)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced)
        return phase

    assert (
        port(fenced, connect=fenced.restricted, guard=checked).cleanup(
            selected.episode
        )["status"]
        == "CANCELLED"
    )
    route_update(fenced)


@pytest.mark.parametrize("limit", [True, 0, 101, "1"])
def test_batch_bounds_checked_before_database(limit):
    def forbidden():
        pytest.fail("invalid input must not connect")

    adapter = StrategyPendingCleanup(
        forbidden, scope=financial.SCOPE, commit_guard=forbidden
    )
    with pytest.raises(ValueError, match="LIMIT_INVALID"):
        adapter.run_once(limit=limit)
