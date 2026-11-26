"""Typed duplicate receipts drain through the real scheduler and ready mirror.

Only the isolated PostgreSQL fixture is used. No evaluator, model, market data
provider or venue may run while acknowledging a committed duplicate decision.
"""

from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_strategy_plan_admission import setup, signal
from v2_core.scheduling import StrategyScheduler
from v2_core.service import TradingData
from v2_core.strategy import StrategyScope, StrategyWorker
from v2_core.strategy_signal_admission import StrategySignalAdmission

database = database_fixture


def duplicate_case(database, topology="cross-source"):
    admission, registry, binding, plan = setup(database)
    winning_signal = signal(database, plan, source="tv_bridge")
    winner = admission.admit(
        plan, binding, signal_id=winning_signal, rationale="QA original opportunity"
    )
    winner_trace = TradingData(database).trace(winner["intent_id"])
    if topology == "same-signal-other-consumer":
        winning_plan = plan
        _, registry, binding, plan = setup(database, instance="trend-b")
        plan = replace(
            plan,
            observed_at_ms=winning_plan.observed_at_ms,
            decided_at_ms=winning_plan.decided_at_ms,
            expires_at_ms=winning_plan.expires_at_ms,
        )
        candidate, source = winning_signal, "tv_bridge"
    else:
        assert topology == "cross-source"
        candidate, source = signal(database, plan, source="s3"), "s3"
    result = StrategySignalAdmission(database, registry).admit(
        plan,
        binding,
        signal_id=candidate,
        rationale="QA duplicate opportunity",
        features={"input_digest": "b" * 64},
    )
    assert result["status"] == "IGNORED"
    assert result["reason"] == "STRATEGY_OPPORTUNITY_ALREADY_CLAIMED"
    assert "intent_id" not in result
    assert TradingData(database).trace(winner["intent_id"]) == winner_trace
    return binding, plan, candidate, source, winner_trace


def terminal_scheduler(connect, binding, plan, source, *, advisory="gate"):
    def forbidden(*_args, **_kwargs):
        pytest.fail("committed duplicate must not re-evaluate or acquire context")

    runtime = SimpleNamespace(
        data=TradingData(connect), _now=lambda: plan.decided_at_ms + 1
    )
    worker = StrategyWorker(
        runtime,
        StrategyScope(**asdict(plan.scope), producer=binding.instance_id),
        source=source,
        strategy_version=plan.code_version,
        config=binding.config,
        decide=forbidden,
        max_delay_ms=60000,
        **(
            {"entry_filter": SimpleNamespace(assess=forbidden)}
            if advisory == "gate"
            else {"entry_observer": SimpleNamespace(submit=forbidden)}
        ),
    )
    return StrategyScheduler(
        worker, context_provider=forbidden, context_required=forbidden
    )


def business_counts(database):
    with database() as conn:
        return conn.execute(
            """SELECT (SELECT count(*) FROM v2_trade_intents),
            (SELECT count(*) FROM v2_domain_outbox),
            (SELECT count(*) FROM v2_decision_evidence),
            (SELECT count(*) FROM v2_signal_receipts),
            (SELECT count(*) FROM v2_strategy_decisions),
            (SELECT count(*) FROM v2_orders),
            (SELECT count(*) FROM v2_risk_reservations)"""
        ).fetchone()


def task(database, consumer, signal_id):
    with database() as conn:
        return conn.execute(
            """SELECT attempts,completed_at IS NOT NULL,lease_token::text,
            lease_until IS NOT NULL,error_code FROM v2_strategy_tasks
            WHERE consumer=%s AND signal_id=%s""",
            (consumer, signal_id),
        ).fetchone()


def ready_member(database, consumer, signal_id):
    with database() as conn:
        return conn.execute(
            """SELECT EXISTS(SELECT FROM v2_strategy_ready
            WHERE consumer=%s AND signal_id=%s)""",
            (consumer, signal_id),
        ).fetchone()[0]


@pytest.mark.parametrize("queue_mode", ["READY", "CANONICAL"])
@pytest.mark.parametrize("topology", ["cross-source", "same-signal-other-consumer"])
@pytest.mark.parametrize("advisory", ["gate", "shadow"])
def test_committed_duplicate_is_acked_without_context_or_model(
    database, queue_mode, topology, advisory
):
    binding, plan, candidate, source, winner_trace = duplicate_case(database, topology)
    scheduler = terminal_scheduler(database, binding, plan, source, advisory=advisory)
    before = business_counts(database)
    assert before[:2] == (1, 1) and before[-2:] == (0, 0)
    with database() as conn:
        if queue_mode == "CANONICAL":
            conn.execute("UPDATE v2_strategy_ready_control SET enabled=false")
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES (%s,%s)",
            (binding.consumer, candidate),
        )
    assert ready_member(database, binding.consumer, candidate)
    assert scheduler.run_once(limit=1) == {candidate: "IGNORED"}
    assert scheduler.last_run_diagnostics["queue_source"]["mode"] == queue_mode
    assert scheduler.last_run_diagnostics["failures"] == []
    assert task(database, binding.consumer, candidate) == (1, True, None, False, None)
    assert not ready_member(database, binding.consumer, candidate)
    assert scheduler.run_once(limit=1) == {}
    assert business_counts(database) == before
    assert TradingData(database).trace(winner_trace["intent_id"]) == winner_trace


def test_terminal_duplicate_does_not_steal_another_live_task_lease(database):
    binding, plan, candidate, source, _ = duplicate_case(database)
    scheduler = terminal_scheduler(database, binding, plan, source)
    lease = str(uuid4())
    with database() as conn:
        conn.execute(
            """INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until)
            VALUES (%s,%s,%s,clock_timestamp()+interval '10 minutes')""",
            (binding.consumer, candidate, lease),
        )
    before = business_counts(database)
    assert scheduler.run_once(limit=1) == {}
    assert task(database, binding.consumer, candidate) == (0, False, lease, True, None)
    assert ready_member(database, binding.consumer, candidate)
    with database() as conn:
        conn.execute(
            """UPDATE v2_strategy_tasks
            SET lease_until=clock_timestamp()-interval '1 second'
            WHERE consumer=%s AND signal_id=%s""",
            (binding.consumer, candidate),
        )
    assert scheduler.run_once(limit=1) == {candidate: "IGNORED"}
    assert task(database, binding.consumer, candidate) == (1, True, None, False, None)
    assert not ready_member(database, binding.consumer, candidate)
    assert business_counts(database) == before


@pytest.mark.parametrize("failure", ["before-ack", "after-ack-commit"])
def test_duplicate_ack_failure_is_recoverable_without_business_replay(
    database, failure
):
    binding, plan, candidate, source, _ = duplicate_case(database)
    armed = [True]

    @contextmanager
    def interrupted_database():
        ack = [False]
        with database() as conn:

            class Connection:
                def execute(self, query, *args, **kwargs):
                    if (
                        armed[0]
                        and isinstance(query, str)
                        and "completed_at=CASE WHEN EXISTS" in query
                    ):
                        armed[0] = False
                        ack[0] = True
                        if failure == "before-ack":
                            raise RuntimeError("QA_DUPLICATE_ACK_BEFORE_COMMIT")
                    return conn.execute(query, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield Connection()
        if ack[0] and failure == "after-ack-commit":
            raise RuntimeError("QA_DUPLICATE_ACK_COMMIT_ACK_LOST")

    scheduler = terminal_scheduler(interrupted_database, binding, plan, source)
    before = business_counts(database)
    assert scheduler.run_once(limit=1) == {candidate: "UNAVAILABLE"}
    assert scheduler.last_run_diagnostics["failures"][-1]["stage"] == "ACK"
    assert business_counts(database) == before
    if failure == "before-ack":
        pending = task(database, binding.consumer, candidate)
        assert pending[:2] == (1, False) and pending[2] is not None and pending[3]
        assert ready_member(database, binding.consumer, candidate)
        with database() as conn:
            conn.execute(
                """UPDATE v2_strategy_tasks
                SET lease_until=clock_timestamp()-interval '1 second'
                WHERE consumer=%s AND signal_id=%s""",
                (binding.consumer, candidate),
            )
        assert scheduler.run_once(limit=1) == {candidate: "IGNORED"}
        expected_attempts = 2
    else:
        assert scheduler.run_once(limit=1) == {}
        expected_attempts = 1
    assert task(database, binding.consumer, candidate) == (
        expected_attempts,
        True,
        None,
        False,
        None,
    )
    assert not ready_member(database, binding.consumer, candidate)
    assert business_counts(database) == before
