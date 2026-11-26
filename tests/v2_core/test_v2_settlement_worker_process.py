"""Real owner-thread + spawn + fenced PG job; synthetic GET-only venue.

No controller main-loop poll drives the job deadline. Child DSN and TCP denial
are inherited from the existing dedicated QA helper, never runtime configuration.
"""

import multiprocessing
import threading
import time

import pytest
from test_v2_managed_settlement_work import binding_for
from test_v2_settlement_execution_fence import database, fenced
from test_v2_settlement_job_process import (
    _confirmed_lock_release,
    _material,
    _qa_connection,
    _Venue,
)
from test_v2_strategy_settlement import closed_plan

from services import v2_execution_guard
from services.v2_execution_guard import create_execution_guard
from services.v2_settlement_worker import (
    SettlementPorts,
    SettlementWorker,
    run_settlement_work,
)
from v2_core.bounded_job import BoundedProcessJob

__all__ = ["database", "fenced"]


def qa_job(stop, dsn, schema, role, binding, material, mode, entered, kind, episode):
    connect = _qa_connection(dsn, schema)
    v2_execution_guard.EXECUTION_GUARD_ROLE = role
    request = _Venue(
        material["rows"], material["income"], material["symbol"], mode, entered
    )
    return run_settlement_work(
        connect,
        request,
        binding=binding,
        guard=create_execution_guard(connect, **binding.guard_args),
        kind=kind,
        episode=episode,
        stop=stop,
        clock_ms=lambda: material["clock"],
    )


def await_condition(check, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    pytest.fail("QA_OWNER_BOUND_EXCEEDED")


@pytest.mark.parametrize("mode", ["normal", "stuck-income"])
def test_owner_supervises_real_fenced_job_without_controller_poll(fenced, mode):
    case = closed_plan(fenced.admin)
    binding = binding_for(fenced)
    dsn, schema, material = _material(fenced.admin, case)
    entered = multiprocessing.get_context("spawn").Event()
    selected, jobs, completed = [], [], threading.Event()
    owner_threads = set()

    class ObservedJob(BoundedProcessJob):
        def poll(self):
            owner_threads.add(threading.get_ident())
            result = super().poll()
            if result["stop_confirmed"]:
                self.final = result
                completed.set()
            return result

    def factory(_target, *, policy):
        job = ObservedJob(qa_job, policy=policy)
        jobs.append(job)
        return job

    def select(kind, after):
        owner_threads.add(threading.get_ident())
        selected.append((kind, after))
        # Exactly one explicit obligation: after it, leave the owner idle so
        # observation cannot race a second synthetic job's account lock.
        if kind == "FINANCIAL" and not after:
            return case.episode
        return None

    ports = SettlementPorts(
        binding,
        select,
        (dsn, schema, fenced.role, binding, material, mode, entered),
    )
    worker = SettlementWorker(
        binding.tenant_id,
        {
            "target_registry": binding.registry_id,
            "binding_version": binding.binding_version,
            "epoch": binding.epoch,
            "worker_token": binding.worker_token,
        },
        {
            "V2_SETTLEMENT_WORK_MS": "4000",
            "V2_SETTLEMENT_STOP_MS": "50",
            "V2_SETTLEMENT_TERMINATE_MS": "50",
            "V2_SETTLEMENT_KILL_MS": "1000",
            "V2_SETTLEMENT_POLL_MS": "10",
            "V2_SETTLEMENT_IDLE_MS": "10",
        },
        bootstrap=lambda *_: ports,
        job_factory=factory,
    )
    try:
        assert worker.ready()
        assert not jobs
        worker.release()
        if mode == "stuck-income":
            assert entered.wait(4), "spawn did not reach synthetic stuck GET"
        # Simulate a controller blocked in unrelated I/O: no group alive,
        # health, stop or job.poll is invoked by this controlling thread.
        assert completed.wait(8), "independent deadline owner did not finish"
        assert len(jobs) == 1
        assert owner_threads == {worker._thread.ident}
        result = jobs[0].final
        assert result["stop_confirmed"] is True
        assert result["execution_authorized"] is False
        assert result["status"] == ("COMPLETED" if mode == "normal" else "TIMEOUT")
        _confirmed_lock_release(fenced.admin)
        trace = case.data.trace(case.episode)
        assert len(trace["settlements"]) == (1 if mode == "normal" else 0)
        assert trace["account_risk"]["status"] == (
            "RELEASED" if mode == "normal" else "HELD"
        )
        await_condition(lambda: ("FINANCIAL", case.episode) in selected)
    finally:
        worker.request_stop()
        await_condition(worker.stop)
    assert worker.snapshot()["stop_confirmed"] is True
