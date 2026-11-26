"""Raw source expiry and unsent lifecycle maintenance, isolated QA PG only."""

from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from uuid import uuid4, uuid5

import psycopg
import pytest

from services import v2_strategy_signal_maintenance as module
from services.v2_execution_guard import create_execution_guard
from services.v2_strategy_signal_maintenance import (
    RAW_ENDED,
    RAW_EXPIRED,
    MaintainedEntryStore,
    SignalMaintenance,
    _raw_outcome,
    run_signal_maintenance,
)
from tests.v2_core import test_v2_strategy_deployment_execution as financial
from tests.v2_core.test_v2_settlement_execution_fence import (
    database,
    fenced,
    route_update,
)
from tests.v2_core.test_v2_strategy_entry_job import ROOT, case
from tests.v2_core.test_v2_strategy_pending_cleanup import pending
from tests.v2_core.test_v2_strategy_plan_admission import counts
from tests.v2_core.test_v2_strategy_prepare import at_database_time
from v2_core.account_risk import RiskReference
from v2_core.evidence import EvidenceStore
from v2_core.orders import Orders
from v2_core.signals import Signals
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_entry_store import StrategyEntryStore
from v2_core.strategy_entry_worker import TypedEntryWorker

__all__ = ["database", "fenced"]


def migration(connect):
    with connect() as conn:
        conn.execute(
            (
                ROOT / "db/migrations/20261126_strategy_maintenance_cursors.sql"
            ).read_text()
        )


def clock(connect):
    with connect() as conn:
        return conn.execute(module._CLOCK_SQL).fetchone()[0]


def raw(connect, *, source="tv_bridge", request_key=None, **changes):
    tick = clock(connect)
    snapshot = {
        "observed_at": tick - 2000,
        "expires_at_ms": tick - 1000,
        "symbol": "BTCUSDT",
        "signal": "TREND_UP_LONG",
        "features": {},
        **changes,
    }
    return Signals(connect).admit(
        source=source,
        environment="SANDBOX",
        request_key=request_key or str(uuid4()),
        snapshot=snapshot,
    ), snapshot


def adapter(fenced, selected, *, connect=None, **changes):
    return SignalMaintenance(
        connect or fenced.admin,
        **{
            "binding": selected.binding,
            "producer": selected.deployment.instance_id,
            "source": "tv_bridge",
            "stop": selected.stop,
            "guard": create_execution_guard(fenced.admin, **fenced.args),
            **changes,
        },
    )


def sweep(fenced, selected, **changes):
    return run_signal_maintenance(
        fenced.admin,
        **{
            "binding": selected.binding,
            "producer": selected.deployment.instance_id,
            "source": "tv_bridge",
            "stop": selected.stop,
            "guard": create_execution_guard(fenced.admin, **fenced.args),
            "limit": 20,
            **changes,
        },
    )


def read(fenced, selected, identity, *, connect=None):
    connect = connect or fenced.admin
    with connect() as conn:
        return MaintainedEntryStore(connect).read(
            conn, scope=selected.worker.scope, source="tv_bridge", signal_id=identity
        )


@pytest.mark.parametrize(
    "now,signal,expected",
    [
        (9, "EVENT_END", None),
        (10, "EVENT_END", ("IGNORED", RAW_ENDED)),
        (19, "TREND_UP_LONG", None),
        (20, "TREND_UP_LONG", ("EXPIRED", RAW_EXPIRED)),
        (20, "EVENT_END", ("EXPIRED", RAW_EXPIRED)),
    ],
)
def test_pure_terminal_uses_original_source_time_only(now, signal, expected):
    assert (
        _raw_outcome({"observed_at": 10, "expires_at_ms": 20, "signal": signal}, now)
        == expected
    )


@pytest.mark.parametrize("value", [True, 0, -1, 21, "1"])
def test_pure_batch_bounds_reject_before_any_port(value):
    with pytest.raises(ValueError, match="BOUNDS_REQUIRED"):
        run_signal_maintenance(
            lambda: pytest.fail("must not connect"),
            binding=None,
            producer="qa",
            source="s3",
            stop=Event(),
            limit=value,
        )


def test_raw_expiry_is_audited_recoverable_and_never_an_order(fenced):
    selected = case(fenced)
    identity, _ = raw(fenced.admin)
    before = counts(fenced.admin)
    result = adapter(fenced, selected).maintain(identity)
    assert result["status"] == "EXPIRED"
    assert result["reason"] == RAW_EXPIRED
    assert result["execution_authorized"] is result["venue_flat_verified"] is False
    assert result["trading_health"] == "NOT_EVALUATED"
    state = read(fenced, selected, identity)
    assert state.receipt.outcome == "EXPIRED" and state.candidate is None
    after = counts(fenced.admin)
    assert after[0] == before[0] == after[5] == after[6] == 0
    assert after[2] == before[2] + 1 and after[3] == before[3] + 1
    assert adapter(fenced, selected).maintain(identity)["status"] == "RECOVERY_REQUIRED"
    assert counts(fenced.admin) == after
    worker = TypedEntryWorker(
        fenced.admin,
        selected.registry,
        scope=selected.worker.scope,
        source="tv_bridge",
        store=MaintainedEntryStore(fenced.admin),
        clock_ms=lambda: 0,
    )
    assert worker.consume(identity, context={})["status"] == "EXPIRED"
    with (
        fenced.admin() as conn,
        pytest.raises(ValueError, match="TERMINAL_EVIDENCE_INVALID"),
    ):
        StrategyEntryStore(fenced.admin).read(
            conn, scope=selected.worker.scope, source="tv_bridge", signal_id=identity
        )


@pytest.mark.parametrize("observation", ["UNKNOWN", "NO_SETUP", "NO_BUDGET"])
def test_unknown_and_no_setup_are_waits_until_actual_source_expiry(fenced, observation):
    selected = case(fenced)
    tick = clock(fenced.admin)
    identity, snapshot = raw(
        fenced.admin, expires_at_ms=tick + 60000, features={"observation": observation}
    )
    before = counts(fenced.admin)
    assert adapter(fenced, selected).maintain(identity)["status"] == "DEFERRED"
    assert counts(fenced.admin) == before
    advanced = at_database_time(fenced.admin, snapshot["expires_at_ms"])
    result = adapter(fenced, selected, connect=advanced).maintain(identity)
    assert (result["status"], result["reason"]) == ("EXPIRED", RAW_EXPIRED)
    assert (
        read(fenced, selected, identity, connect=advanced).receipt.reason == RAW_EXPIRED
    )


def test_future_event_end_is_not_consumed_until_observed(fenced):
    selected = case(fenced)
    tick = clock(fenced.admin)
    identity, snapshot = raw(
        fenced.admin,
        observed_at=tick + 60000,
        expires_at_ms=tick + 120000,
        signal="EVENT_END",
    )
    assert adapter(fenced, selected).maintain(identity)["status"] == "DEFERRED"
    assert read(fenced, selected, identity).receipt is None
    advanced = at_database_time(fenced.admin, snapshot["observed_at"])
    result = adapter(fenced, selected, connect=advanced).maintain(identity)
    assert (result["status"], result["reason"]) == ("IGNORED", RAW_ENDED)
    assert (
        read(fenced, selected, identity, connect=advanced).receipt.outcome == "IGNORED"
    )


def test_off_deployment_and_draining_route_allow_expiry_not_entry(fenced):
    selected = case(fenced)
    identity, _ = raw(fenced.admin)
    StrategyDeployments(fenced.admin, selected.registry).publish(
        replace(selected.deployment, generation=2, mode="OFF"),
        expected_generation=1,
        request_id=str(uuid4()),
    )
    route_update(fenced, "SET phase='DRAINING'")
    assert adapter(fenced, selected).maintain(identity)["status"] == "EXPIRED"
    # Recovery binds historical deployment, not whatever is active now.
    StrategyDeployments(fenced.admin, selected.registry).publish(
        replace(selected.deployment, generation=3, mode="ACTIVE"),
        expected_generation=2,
        request_id=str(uuid4()),
    )
    assert read(fenced, selected, identity).receipt.outcome == "EXPIRED"


@pytest.mark.parametrize("cause", ["STOPPED", "epoch", "token"])
def test_revoked_owner_cannot_terminalize_or_checkpoint(fenced, cause):
    selected = case(fenced)
    migration(fenced.admin)
    identity, _ = raw(fenced.admin)
    if cause == "STOPPED":
        route_update(fenced, "SET phase='STOPPED',worker_token=NULL")
    elif cause == "epoch":
        route_update(fenced, "SET epoch=epoch+1")
    else:
        route_update(fenced, "SET worker_token=%s", (str(uuid4()),))
    before = counts(fenced.admin)
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        adapter(fenced, selected).maintain(identity)
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        sweep(fenced, selected)
    assert counts(fenced.admin) == before
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_maintenance_cursors"
        ).fetchone() == (0,)


def test_original_frozen_candidate_wins_even_when_expired(fenced):
    selected = case(fenced)
    advanced = at_database_time(fenced.admin, selected.plan.expires_at_ms + 1)
    before = counts(fenced.admin)
    assert (
        adapter(fenced, selected, connect=advanced).maintain(selected.identity)[
            "status"
        ]
        == "RECOVERY_REQUIRED"
    )
    assert (
        read(fenced, selected, selected.identity, connect=advanced).candidate
        is not None
    )
    assert counts(fenced.admin) == before


def test_original_intent_receipt_wins_over_raw_expiry(fenced):
    selected = pending(fenced)
    advanced = at_database_time(fenced.admin, selected.plan.expires_at_ms + 1)
    before = counts(fenced.admin)
    result = adapter(fenced, selected, connect=advanced).maintain(selected.identity)
    assert (result["status"], result["outcome"]) == ("RECOVERY_REQUIRED", "INTENT")
    assert (
        read(fenced, selected, selected.identity, connect=advanced).receipt.intent_id
        == selected.episode
    )
    assert counts(fenced.admin) == before


def test_wrong_source_is_not_a_receipt_in_this_lane(fenced):
    selected = case(fenced)
    identity, _ = raw(fenced.admin, source="s3")
    before = counts(fenced.admin)
    with pytest.raises(ValueError, match="SIGNAL_SCOPE_MISMATCH"):
        adapter(fenced, selected).maintain(identity)
    assert counts(fenced.admin) == before


def test_bare_receipt_without_original_audit_is_not_recovered(fenced):
    selected = case(fenced)
    identity, _ = raw(fenced.admin)
    Signals(fenced.admin).complete(
        consumer=selected.worker.scope.consumer,
        signal_id=identity,
        outcome="EXPIRED",
        reason=RAW_EXPIRED,
    )
    with pytest.raises(ValueError, match="DURABLE_STATE_CONFLICT"):
        adapter(fenced, selected).maintain(identity)


def test_self_consistent_hash_without_published_deployment_is_rejected(fenced):
    selected = case(fenced)
    identity, snapshot = raw(fenced.admin)
    forged = replace(selected.deployment, generation=2)
    proof = module._raw_proof(
        forged,
        snapshot,
        signal_id=identity,
        source="tv_bridge",
        decided_at=clock(fenced.admin),
        phase="ACTIVE",
    )
    EvidenceStore(fenced.admin).put(proof)
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action) VALUES(%s,%s,%s,%s,'EXPIRED')",
            (
                str(uuid5(module._NAMESPACE, forged.consumer + ":" + identity)),
                forged.consumer,
                identity,
                proof.evidence_ref,
            ),
        )
    Signals(fenced.admin).complete(
        consumer=forged.consumer,
        signal_id=identity,
        outcome="EXPIRED",
        reason=RAW_EXPIRED,
    )
    with pytest.raises(ValueError, match="RAW_TERMINAL_EVIDENCE_INVALID"):
        read(fenced, selected, identity)


def test_lost_commit_ack_recovers_one_raw_decision(fenced):
    selected = case(fenced)
    identity, _ = raw(fenced.admin)

    @contextmanager
    def lost():
        with fenced.admin() as conn:
            yield conn
        raise OSError("synthetic commit acknowledgement lost")

    with pytest.raises(OSError, match="acknowledgement"):
        adapter(fenced, selected, connect=lost).maintain(identity)
    committed = counts(fenced.admin)
    assert adapter(fenced, selected).maintain(identity)["status"] == "RECOVERY_REQUIRED"
    assert counts(fenced.admin) == committed


def test_raw_expiry_wins_exact_final_producer_freeze_race(fenced, monkeypatch):
    from services.v2_strategy_candidate_producer import CandidateProducer
    from services.v2_strategy_entry_job import entry_transaction
    from tests.v2_core.test_v2_strategy_candidate_producer import scene

    selected = scene(fenced)
    original = selected.producer
    producer = CandidateProducer(
        fenced.admin,
        binding=selected.binding,
        registry=financial.REGISTRY,
        instance_id=selected.deployment.instance_id,
        source="tv_bridge",
        market_reader=original.market,
        account_reader=original.account,
        execution_reader=original.execution,
        stop=original.stop,
        guard=original.guard,
        store_factory=MaintainedEntryStore,
    )
    freeze = producer._freeze
    advanced = at_database_time(fenced.admin, selected.snapshot["expires_at_ms"])

    def last_moment(candidate):
        # Exactly after prepare's last durable read, before freeze's signal lock.
        # Only the QA clock is advanced; all proof/locks/writes use isolated PG.
        producer.connect = advanced
        producer.store = MaintainedEntryStore(advanced)
        producer.write = entry_transaction(
            advanced, binding=selected.binding, guard=producer.guard, stop=producer.stop
        )
        maintenance = SignalMaintenance(
            advanced,
            binding=selected.binding,
            producer=selected.deployment.instance_id,
            source="tv_bridge",
            stop=producer.stop,
            guard=producer.guard,
        )
        assert maintenance.maintain(selected.identity)["status"] == "EXPIRED"
        return freeze(candidate)

    monkeypatch.setattr(producer, "_freeze", last_moment)
    result = producer.prepare(selected.identity)
    assert (result["status"], result["receipt"]) == ("RECOVERY_REQUIRED", "EXPIRED")
    assert producer._state(selected.identity).candidate is None
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_entry_candidates"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT outcome FROM v2_signal_receipts").fetchall() == [
            ("EXPIRED",)
        ]


def test_route_fence_is_held_until_audit_commit_and_stop_rolls_back(
    fenced, monkeypatch
):
    selected = case(fenced)
    identity, _ = raw(fenced.admin)
    original = module._raw_proof

    def stop_after_proof(*args, **kwargs):
        proof = original(*args, **kwargs)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced, "SET phase='DRAINING'")
        selected.stop.set()
        return proof

    before = counts(fenced.admin)
    monkeypatch.setattr(module, "_raw_proof", stop_after_proof)
    with pytest.raises(ValueError, match="MAINTENANCE_STOPPED"):
        adapter(fenced, selected).maintain(identity)
    assert counts(fenced.admin) == before
    selected.stop.clear()
    monkeypatch.setattr(module, "_raw_proof", original)
    assert adapter(fenced, selected).maintain(identity)["status"] == "EXPIRED"


def test_persistent_cursor_advances_before_poison_and_wraps(fenced, monkeypatch):
    selected = case(fenced)
    migration(fenced.admin)
    first, _ = raw(fenced.admin, request_key="!qa-first")
    second, _ = raw(fenced.admin, request_key="!qa-second")
    original = module.SignalMaintenance.maintain

    def poison(self, identity):
        if identity == first:
            # Even the failed item's hint must already be durable.
            assert self._cursor()[0] == "!qa-first"
            raise ValueError("ISOLATED_POISON")
        return original(self, identity)

    monkeypatch.setattr(module.SignalMaintenance, "maintain", poison)
    result = sweep(fenced, selected, limit=1)
    assert result["signals"][first]["status"] == "BLOCKED"
    assert adapter(fenced, selected)._cursor()[0] == "!qa-first"
    result = sweep(fenced, selected, limit=1)
    assert result["signals"][second]["status"] == "EXPIRED"
    for _ in range(5):
        sweep(fenced, selected, limit=1)
        if adapter(fenced, selected)._cursor()[0] is None:
            break
    assert adapter(fenced, selected)._cursor()[0] is None
    assert read(fenced, selected, first).receipt is None
    assert sweep(fenced, selected, limit=1)["signals"][first]["status"] == "BLOCKED"


def test_cursor_loss_or_reconstruction_is_only_a_hint(fenced):
    selected = case(fenced)
    migration(fenced.admin)
    identity, _ = raw(fenced.admin, request_key="!qa-expired")
    sweep(fenced, selected, limit=1)
    committed = counts(fenced.admin)
    with fenced.admin() as conn:
        conn.execute("DELETE FROM v2_strategy_maintenance_cursors")
    result = sweep(fenced, selected, limit=1)
    assert result["signals"] == {}
    assert read(fenced, selected, identity).receipt.outcome == "EXPIRED"
    assert counts(fenced.admin) == committed


def test_next_first_lane_survives_interrupted_source_work(fenced, monkeypatch):
    selected = case(fenced)
    migration(fenced.admin)
    raw(fenced.admin, request_key="!qa-interrupt")
    original = module.SignalMaintenance.maintain

    def interrupt(self, identity):
        raise KeyboardInterrupt("synthetic process loss")

    monkeypatch.setattr(module.SignalMaintenance, "maintain", interrupt)
    with pytest.raises(KeyboardInterrupt):
        sweep(fenced, selected, limit=1)
    assert adapter(fenced, selected)._cursor()[2] == "cleanup"
    monkeypatch.setattr(module.SignalMaintenance, "maintain", original)
    seen = []
    original_page = module.SignalMaintenance._intent_page

    def cleanup_first(self, *args):
        seen.append("cleanup")
        return original_page(self, *args)

    monkeypatch.setattr(module.SignalMaintenance, "_intent_page", cleanup_first)
    sweep(fenced, selected, limit=1)
    assert seen == ["cleanup"]
    assert adapter(fenced, selected)._cursor()[2] == "signals"


def test_draining_sweep_cancels_only_original_unsent_prepared(fenced):
    selected = pending(fenced)
    migration(fenced.admin)
    route_update(fenced, "SET phase='DRAINING'")
    result = sweep(fenced, selected)
    assert result["cleanup"][selected.episode]["status"] == "CANCELLED"
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_orders").fetchall() == [
            ("CANCELLED",)
        ]
        assert conn.execute(
            "SELECT count(*) FROM v2_order_events WHERE status='SUBMITTING'"
        ).fetchone() == (0,)
    # The original receipt remains INTENT, not a fabricated EXPIRED source receipt.
    assert read(fenced, selected, selected.identity).receipt.outcome == "INTENT"


def test_unknown_order_is_not_released_by_lifecycle_sweep(fenced):
    selected = pending(fenced)
    migration(fenced.admin)
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
    assert orders.transition(
        selected.order,
        expected_version=2,
        status="UNKNOWN",
        evidence={"origin": "ISOLATED_QA"},
    )
    route_update(fenced, "SET phase='DRAINING'")
    before = counts(fenced.admin)
    result = sweep(fenced, selected)
    assert result["cleanup"][selected.episode]["status"] == "RECONCILIATION_REQUIRED"
    assert counts(fenced.admin) == before
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_risk_reservations").fetchall() == [
            ("HELD",)
        ]
        assert conn.execute("SELECT status FROM v2_orders").fetchall() == [("UNKNOWN",)]
