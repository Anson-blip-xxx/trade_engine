"""Typed frozen candidates through the real scheduler; no execution transport."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.v2_core import test_v2_strategy_deployment_execution as execution_fixtures
from tests.v2_core.test_v2_strategy_plan_admission import counts, setup, signal
from v2_core import entry_controls, liquidity_controls
from v2_core.account_risk import RiskReference
from v2_core.evidence import canonical, digest
from v2_core.orders import Orders
from v2_core.scheduling import StrategyScheduler
from v2_core.signals import Signals
from v2_core.strategy import StrategyScope
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_entry_store import StrategyEntryStore
from v2_core.strategy_entry_worker import FrozenEntryCandidate, TypedEntryWorker
from v2_core.strategy_execution_context import execution_context

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]


def frozen(database, *, action="OPEN", financial=False):
    if financial:
        binding, plan, _, _ = execution_fixtures.material()
        registry = execution_fixtures.REGISTRY
        execution_fixtures.publish(database, binding)
        execution_fixtures.risk(database)
    else:
        _, registry, binding, plan = setup(database)
    identity = signal(database, plan)
    with database() as conn:
        conn.execute(
            (ROOT / "db/migrations/20261126_strategy_entry_candidates.sql").read_text()
        )
        snapshot = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (identity,)
        ).fetchone()[0]
    store = StrategyEntryStore(database)
    features = (
        {
            "execution": execution_context(
                plan,
                entry_controls=entry_controls.read(database, plan.scope).snapshot(),
                liquidity_controls=liquidity_controls.read(
                    database, plan.scope
                ).snapshot(),
                policy={
                    "entry.reentry_enabled": False,
                    "entry.price_guard_enabled": False,
                    "entry.max_adverse_price_fraction": "0.01",
                    "entry.max_stop_risk_increase": "1.10",
                },
            )
        }
        if action == "OPEN"
        else {}
    )
    candidate = FrozenEntryCandidate(
        binding=binding,
        signal_id=identity,
        source="tv_bridge",
        signal_digest=digest(canonical(snapshot)),
        action=action,
        rationale="QA frozen entry",
        observed_at_ms=plan.observed_at_ms,
        decided_at_ms=plan.decided_at_ms,
        expires_at_ms=plan.expires_at_ms,
        features_json=canonical(features),
        plan=plan if action == "OPEN" else None,
    )
    store.freeze(candidate)
    scope = StrategyScope(**asdict(plan.scope), producer=binding.instance_id)
    worker = TypedEntryWorker(
        database,
        registry,
        scope=scope,
        source="tv_bridge",
        store=store,
        clock_ms=lambda: plan.decided_at_ms + 1,
    )
    return registry, binding, plan, identity, worker


def scheduler(worker):
    def forbidden(*_args, **_kwargs):
        pytest.fail("frozen/recovery inputs must not acquire fresh context")

    return StrategyScheduler(
        worker, context_provider=forbidden, context_required=forbidden
    )


def task(database, worker, identity):
    with database() as conn:
        return conn.execute(
            "SELECT attempts,completed_at IS NOT NULL,error_code,lease_token "
            "FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, identity),
        ).fetchone()


@pytest.mark.parametrize("queue_mode", ["READY", "CANONICAL"])
@pytest.mark.parametrize("action", ["OPEN", "IGNORED"])
def test_frozen_plan_resolves_and_scheduler_acks(database, queue_mode, action):
    _, _, _, identity, worker = frozen(database, action=action)
    with database() as conn:
        if queue_mode == "CANONICAL":
            conn.execute("UPDATE v2_strategy_ready_control SET enabled=false")
    queue = scheduler(worker)
    expected = "PREPARED" if action == "OPEN" else "IGNORED"
    assert queue.run_once(limit=1) == {identity: expected}
    assert queue.last_run_diagnostics["failures"] == []
    assert queue.last_run_diagnostics["queue_source"]["mode"] == queue_mode
    assert task(database, worker, identity) == (1, True, None, None)
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status<>'PREPARED'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (
            1 if action == "OPEN" else 0,
        )
    before = counts(database)
    assert queue.run_once(limit=1) == {}
    assert counts(database) == before


def test_current_deployment_refusal_does_not_ack_or_rewrite_candidate(database):
    registry, binding, plan, identity, worker = frozen(database)
    StrategyDeployments(database, registry).publish(
        replace(binding, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-off",
    )
    queue = scheduler(worker)
    result = queue.run_once(limit=1)
    assert result[identity] in {"UNAVAILABLE", "DEFERRED"}
    assert task(database, worker, identity)[1] is False
    assert counts(database)[0] == counts(database)[5] == 0
    with database() as conn:
        stored = worker.store.read(
            conn, scope=worker.scope, source=worker.source, signal_id=identity
        )
    assert stored.candidate.plan.plan_digest == plan.plan_digest
    assert stored.receipt is None


def test_legacy_bulk_expiry_is_rejected_before_database():
    class FrozenWorker:
        requires_audited_expiry = True

        @staticmethod
        def _connect():
            pytest.fail("legacy expiry must not open a database connection")

    queue = StrategyScheduler(FrozenWorker(), context_provider=lambda _: {})
    with pytest.raises(ValueError, match="TYPED_ENTRY_AUDITED_EXPIRY_REQUIRED"):
        queue.expire_pending(limit=1)


def test_live_lease_is_not_taken_by_typed_consumer(database):
    _, _, _, identity, worker = frozen(database)
    token = str(uuid4())
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) "
            "VALUES(%s,%s,%s,clock_timestamp()+interval '10 minutes')",
            (worker.scope.consumer, identity, token),
        )
    assert scheduler(worker).run_once(limit=1) == {}
    row = task(database, worker, identity)
    assert row[:3] == (0, False, None) and str(row[3]) == token
    assert counts(database)[0] == counts(database)[5] == 0


def due(database, worker, identity):
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET next_attempt_at=clock_timestamp() "
            "WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, identity),
        )


def test_unproven_raw_receipt_cannot_ack_typed_task(database):
    _, _, _, identity, worker = frozen(database)
    Signals(database).complete(
        consumer=worker.scope.consumer,
        signal_id=identity,
        outcome="IGNORED",
        reason="unproven raw receipt",
    )
    # Discovery normally excludes terminal receipts; inject the existing task
    # whose result was corrupted after it had already entered the queue.
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES(%s,%s)",
            (worker.scope.consumer, identity),
        )
    queue = scheduler(worker)
    assert queue.run_once(limit=1) == {identity: "UNAVAILABLE"}
    row = task(database, worker, identity)
    assert row[0:2] == (1, False) and row[2] and row[3] is None
    assert queue.last_run_diagnostics["failures"][0]["stage"] == "READ"
    assert counts(database)[0] == counts(database)[5] == 0


@pytest.mark.parametrize("stage", ["admission", "preparation", "terminal"])
def test_lost_business_commit_ack_requires_verified_replay(
    database, monkeypatch, stage
):
    action = "IGNORED" if stage == "terminal" else "OPEN"
    _, _, _, identity, worker = frozen(database, action=action)
    owner, method = {
        "admission": (worker.admission, "admit"),
        "preparation": (worker.preparation, "recover"),
        "terminal": (worker.store, "terminal"),
    }[stage]
    original = getattr(owner, method)
    armed = [True]

    def lost_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if armed[0]:
            armed[0] = False
            raise RuntimeError("QA_BUSINESS_COMMIT_ACK_LOST")
        return result

    monkeypatch.setattr(owner, method, lost_ack)
    queue = scheduler(worker)
    assert queue.run_once(limit=1) == {identity: "UNAVAILABLE"}
    assert task(database, worker, identity)[1] is False
    before = counts(database)
    due(database, worker, identity)
    expected = {
        "admission": "PREPARED",
        "preparation": "EXISTING_ORDER",
        "terminal": "IGNORED",
    }[stage]
    assert queue.run_once(limit=1) == {identity: expected}
    assert task(database, worker, identity) == (2, True, None, None)
    after = counts(database)
    assert after[0] == before[0] == (0 if stage == "terminal" else 1)
    assert after[5] == (0 if stage == "terminal" else 1)
    if stage != "admission":
        assert after == before


def test_drain_after_read_keeps_task_for_verified_retry(database, monkeypatch):
    _, _, _, identity, worker = frozen(database, action="IGNORED")
    assert worker.consume(identity, context={})["status"] == "IGNORED"
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES(%s,%s)",
            (worker.scope.consumer, identity),
        )
    stop = [False]
    original = worker._read

    def read_then_drain(*args):
        result = original(*args)
        stop[0] = True
        return result

    monkeypatch.setattr(worker, "_read", read_then_drain)
    queue = scheduler(worker)
    assert queue.run_once(limit=1, stop_requested=lambda: stop[0]) == {
        identity: "DRAINED"
    }
    assert task(database, worker, identity) == (1, False, None, None)
    monkeypatch.setattr(worker, "_read", original)
    assert queue.run_once(limit=1) == {identity: "IGNORED"}
    assert task(database, worker, identity) == (2, True, None, None)


def test_order_free_unknown_intent_is_not_completed_preparation(database):
    _, binding, plan, identity, worker = frozen(database)
    with database() as conn:
        candidate = worker.store.read(
            conn, scope=worker.scope, source=worker.source, signal_id=identity
        ).candidate
    accepted = worker.admission.admit(
        plan,
        binding,
        signal_id=identity,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    with database() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET status='UNKNOWN',version=version+1 "
            "WHERE intent_id=%s",
            (accepted["intent_id"],),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES(%s,%s)",
            (worker.scope.consumer, identity),
        )
    assert scheduler(worker).run_once(limit=1) == {identity: "UNCHANGED"}
    assert task(database, worker, identity)[1] is False
    assert counts(database)[5] == 0


def test_existing_unknown_order_ack_does_not_mean_order_resolved(database):
    _, _, _, identity, worker = frozen(database, financial=True)
    original = worker.consume(identity, context={})
    orders = Orders(database)
    orders.transition(
        original["order_id"],
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference(
            "SANDBOX", "BTCUSDT", "qa", "100", execution_fixtures.now()
        ),
    )
    orders.transition(
        original["order_id"], expected_version=2, status="UNKNOWN", evidence={}
    )
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES(%s,%s)",
            (worker.scope.consumer, identity),
        )
    before = counts(database)
    assert scheduler(worker).run_once(limit=1) == {identity: "EXISTING_ORDER"}
    assert task(database, worker, identity) == (1, True, None, None)
    assert counts(database) == before
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (original["order_id"],)
        ).fetchone() == ("UNKNOWN",)


@pytest.mark.parametrize("phase", ["rollback", "committed_ack_lost"])
def test_typed_scheduler_ack_failure_does_not_repeat_business_work(database, phase):
    _, _, _, identity, worker = frozen(database)
    armed = [True]

    @contextmanager
    def interrupted_database():
        affected = [False]
        with database() as conn:

            class Connection:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, query, *args, **kwargs):
                    cursor = conn.execute(query, *args, **kwargs)
                    if armed[0] and "completed_at=CASE WHEN EXISTS" in query:
                        armed[0] = False
                        affected[0] = True
                        if phase == "rollback":
                            raise RuntimeError("QA_SCHEDULER_ACK_ROLLBACK")
                    return cursor

            yield Connection()
        if affected[0] and phase == "committed_ack_lost":
            raise RuntimeError("QA_SCHEDULER_ACK_COMMIT_ACK_LOST")

    worker._connect = interrupted_database
    queue = scheduler(worker)
    assert queue.run_once(limit=1) == {identity: "UNAVAILABLE"}
    before = counts(database)
    assert before[0] == before[5] == 1
    assert queue.last_run_diagnostics["failures"][-1]["stage"] == "ACK"
    if phase == "rollback":
        assert task(database, worker, identity)[1] is False
        with database() as conn:
            conn.execute(
                "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE consumer=%s AND signal_id=%s",
                (worker.scope.consumer, identity),
            )
        assert queue.run_once(limit=1) == {identity: "EXISTING_ORDER"}
    else:
        assert queue.run_once(limit=1) == {}
    assert task(database, worker, identity)[1] is True
    assert counts(database) == before
