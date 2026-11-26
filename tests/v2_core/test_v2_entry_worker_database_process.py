"""Direct entry owner + spawn + real isolated PG; never runtime/network ports.

All fault injection is inside disposable QA transactions. A process reaped by
the owner is not treated as proof that PostgreSQL has rolled its work back.
"""

import multiprocessing
import os
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from services import v2_execution_guard
from services.v2_entry_worker import EntryPorts, EntryWorker
from services.v2_strategy_entry_job import EntryJobSpec, run_entry_work
from tests.v2_core.test_v2_settlement_execution_fence import database, fenced
from tests.v2_core.test_v2_settlement_job_process import _qa_connection
from tests.v2_core.test_v2_strategy_entry_job import case
from tests.v2_core.test_v2_strategy_plan_admission import counts
from v2_core.bounded_job import BoundedProcessJob
from v2_core.evidence import canonical, digest
from v2_core.session_lock import session_advisory_lock
from v2_core.signals import Signals
from v2_core.strategy_plan_admission import opportunity_intent_id

__all__ = ["database", "fenced"]


def qa_entry_job(
    stop, dsn, schema, role, spec, now, mode, entered, first, identity, source
):
    """Trusted top-level spawn target, bound to one explicitly passed QA DB."""
    connect = _qa_connection(dsn, schema)
    v2_execution_guard.EXECUTION_GUARD_ROLE = role

    @contextmanager
    def fault_connection():
        with connect() as conn:
            yield conn
            if (
                mode == "held-transaction"
                and source == "tv_bridge"
                and first.value == 1
            ):
                # Detect an INSERT by this exact transaction, not a committed
                # row seen by a later reader. No production logic is replaced.
                written = conn.execute(
                    "SELECT 1 FROM v2_trade_intents WHERE signal_id=%s "
                    "AND xmin=pg_current_xact_id()::text::xid",
                    (identity,),
                ).fetchone()
                if written:
                    conn.execute(
                        "INSERT INTO qa_fence_commits VALUES ('entry-uncommitted')"
                    )
                    first.value = 0
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    entered.set()
                    # Hold a real admission transaction while the client is
                    # stuck. SIGKILL must close it; no sleep SQL is left running
                    # independently in a backend after process death.
                    while True:
                        time.sleep(0.05)

    result = run_entry_work(
        fault_connection if mode == "held-transaction" else connect,
        binding=spec.binding,
        registry=spec.registry(),
        producer=spec.producer,
        source=source,
        stop=stop,
        clock_ms=lambda: now,
        limit=spec.limit,
        budget_ms=spec.budget_ms,
        history_page_size=spec.history_page_size,
        history_budget_ms=spec.history_budget_ms,
    )
    if mode == "lost-result" and first.value == 1:
        assert result["status"] == "COMPLETED"
        first.value = 0
        entered.set()
        # Admission, preparation and queue ACK are committed, but no result
        # frame can reach BoundedProcessJob. The next batch reads PG facts.
        os._exit(0)
    entered.set()
    return result["status"]


def await_condition(check, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    pytest.fail("QA_ENTRY_DATABASE_OWNER_BOUND_EXCEEDED")


def make_owner(fenced, selected, mode, *, sources=("tv_bridge",), work_ms="5000"):
    with fenced.admin() as conn:
        dsn, schema = (
            conn.info.dsn,
            conn.execute("SELECT current_schema()").fetchone()[0],
        )
    registry = selected.registry
    spec = EntryJobSpec(
        selected.binding,
        selected.deployment.instance_id,
        tuple(registry.specs.values()),
        tuple(registry.entry_handlers),
        tuple(registry.exit_handlers),
        tuple(registry.plan_schemas),
        limit=1,
        budget_ms=1000,
        history_page_size=10,
        history_budget_ms=500,
    )
    context = multiprocessing.get_context("spawn")
    entered, first = context.Event(), context.RawValue("b", 1)
    first_done, all_done = threading.Event(), threading.Event()
    jobs, starts, completions, threads = [], [], [], set()
    holder = []

    class ObservedJob(BoundedProcessJob):
        finished = False

        def start(self, *args):
            threads.add(threading.get_ident())
            self.source = args[-1]
            starts.append(self.source)
            return super().start(*args)

        def poll(self):
            threads.add(threading.get_ident())
            result = super().poll()
            if (
                result["stop_confirmed"]
                and result["job_id"] is not None
                and not self.finished
            ):
                self.finished = True
                completions.append((self.source, result))
                first_done.set()
                if len(completions) >= 2:
                    # Pure local hint only: observer never opens a connection
                    # or calls the process job API from another thread.
                    holder[0].request_stop()
                    all_done.set()
            return result

    def factory(target, *, policy):
        job = ObservedJob(target, policy=policy)
        jobs.append(job)
        return job

    worker = EntryWorker(
        EntryPorts(
            selected.binding,
            qa_entry_job,
            (
                dsn,
                schema,
                fenced.role,
                spec,
                selected.worker.runtime._now(),
                mode,
                entered,
                first,
                selected.identity,
            ),
            sources,
        ),
        {
            "V2_ENTRY_WORK_MS": work_ms,
            "V2_ENTRY_STOP_MS": "50",
            "V2_ENTRY_TERMINATE_MS": "50",
            "V2_ENTRY_KILL_MS": "1000",
            "V2_ENTRY_POLL_MS": "10",
            "V2_ENTRY_IDLE_MS": "1000",
            "V2_ENTRY_STOP_JOIN_MS": "20",
        },
        job_factory=factory,
    )
    holder.append(worker)
    return {
        "worker": worker,
        "entered": entered,
        "first_done": first_done,
        "all_done": all_done,
        "jobs": jobs,
        "starts": starts,
        "completions": completions,
        "threads": threads,
    }


def durable_ids(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT i.intent_id::text,o.order_id::text,o.client_order_id,o.status "
            "FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id"
        ).fetchall()


@pytest.mark.parametrize("mode", ["normal", "lost-result"])
def test_real_entry_batch_commit_and_missing_ipc_replay_original_identity(fenced, mode):
    selected = case(fenced)
    owned = make_owner(fenced, selected, mode)
    worker = owned["worker"]
    try:
        assert worker.ready()
        assert not owned["jobs"]
        worker.release()
        # No controller poll drives deadlines or progress in these waits.
        assert owned["first_done"].wait(10)
        assert owned["completions"][0][1]["status"] == (
            "COMPLETED" if mode == "normal" else "FAILED"
        )
        first = durable_ids(fenced.admin)
        first_counts = counts(fenced.admin)
        assert len(first) == 1
        assert first[0][0] == opportunity_intent_id(selected.plan)
        assert first[0][3] == "PREPARED"
        assert owned["all_done"].wait(10)
        await_condition(worker.stop)
        assert owned["starts"] == ["tv_bridge", "tv_bridge"]
        assert owned["completions"][1][1]["status"] == "COMPLETED"
        assert durable_ids(fenced.admin) == first
        assert counts(fenced.admin) == first_counts
        with fenced.admin() as conn:
            assert conn.execute("SELECT count(*) FROM v2_order_events").fetchone() == (
                1,
            )
            assert conn.execute(
                "SELECT count(*) FROM v2_orders WHERE status<>'PREPARED'"
            ).fetchone() == (0,)
        assert owned["threads"] == {worker._thread.ident}
        assert all(job._process is None for job in owned["jobs"])
    finally:
        worker.request_stop()
        await_condition(worker.stop)


def confirmed_rollback(connect, identity):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            with connect() as conn:
                conn.execute(
                    "LOCK TABLE v2_trade_intents,qa_fence_commits IN ACCESS EXCLUSIVE MODE NOWAIT"
                )
                assert conn.execute(
                    "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s",
                    (identity,),
                ).fetchone() == (0,)
                assert conn.execute(
                    "SELECT count(*) FROM qa_fence_commits"
                ).fetchone() == (0,)
                assert conn.execute(
                    "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s",
                    (identity,),
                ).fetchone() == (0,)
                return
        except psycopg.errors.LockNotAvailable:
            time.sleep(0.01)
    pytest.fail("QA_ENTRY_PG_TRANSACTION_DID_NOT_END_AFTER_KILL")


def test_stuck_uncommitted_admission_is_killed_rolled_back_and_next_lane_runs(fenced):
    selected = case(fenced)
    with fenced.admin() as conn:
        original = selected.worker.store.read(
            conn,
            scope=selected.worker.scope,
            source="tv_bridge",
            signal_id=selected.identity,
        )
    # Same opportunity, different source: the rolled-back TV admission must
    # leave no winner claim preventing this real candidate from preparing.
    s3_identity = Signals(fenced.admin).admit(
        source="s3",
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot=original.snapshot,
    )
    selected.worker.store.freeze(
        replace(
            original.candidate,
            signal_id=s3_identity,
            source="s3",
            signal_digest=digest(canonical(original.snapshot)),
        )
    )
    owned = make_owner(
        fenced,
        selected,
        "held-transaction",
        sources=("tv_bridge", "s3"),
        work_ms="2000",
    )
    worker = owned["worker"]
    try:
        assert worker.ready()
        worker.release()
        assert owned["entered"].wait(8), (
            "child never held the synthetic admission transaction"
        )
        assert owned["first_done"].wait(8)
        assert owned["completions"][0][1]["status"] == "TIMEOUT"
        confirmed_rollback(fenced.admin, selected.identity)
        assert owned["all_done"].wait(8)
        await_condition(worker.stop)
        assert owned["starts"] == ["tv_bridge", "s3"]
        assert owned["completions"][1][1]["status"] == "COMPLETED"
        winner = opportunity_intent_id(selected.plan)
        with fenced.admin() as conn:
            assert conn.execute(
                "SELECT intent_id::text,signal_id::text FROM v2_trade_intents"
            ).fetchall() == [(winner, s3_identity)]
            assert conn.execute(
                "SELECT signal_id::text,outcome,intent_id::text FROM v2_signal_receipts"
            ).fetchall() == [(s3_identity, "INTENT", winner)]
            assert conn.execute(
                "SELECT completed_at IS NOT NULL FROM v2_strategy_tasks "
                "WHERE consumer=%s AND signal_id=%s",
                (selected.worker.scope.consumer, s3_identity),
            ).fetchone() == (True,)
            assert conn.execute(
                "SELECT episode_id::text,status FROM v2_orders"
            ).fetchall() == [(winner, "PREPARED")]
            assert conn.execute(
                "SELECT e.status,e.version FROM v2_order_events e "
                "JOIN v2_orders o USING(order_id) WHERE o.episode_id=%s",
                (winner,),
            ).fetchall() == [("PREPARED", 1)]
            assert conn.execute("SELECT count(*) FROM v2_order_events").fetchone() == (
                1,
            )
        with session_advisory_lock(
            fenced.admin, "typed-entry-batch:" + selected.worker.scope.consumer
        ) as acquired:
            assert acquired, "the previous backend retained batch ownership"
        with fenced.admin() as conn:
            conn.execute("SET LOCAL lock_timeout='500ms'")
            conn.execute("UPDATE v2_execution_routes SET blockers=blockers")
        assert all(job._process is None for job in owned["jobs"])
    finally:
        worker.request_stop()
        await_condition(worker.stop)
