"""Real independent owner + direct spawn children; local fake batches only."""

import json
import multiprocessing
import os
import signal
import threading
import time
from uuid import uuid4

import pytest

from services.v2_entry_worker import EntryPorts, EntryWorker
from services.v2_management_runtime import ManagementBinding
from v2_core.account_risk import AccountScope
from v2_core.bounded_job import BoundedProcessJob


def local_batch(stop, binding, mode, entered, identity, source):
    """Only local values/events; no DB, token, exchange or production imports."""
    assert binding.scope.environment == "SANDBOX"
    assert source == "tv_bridge"
    identity[0], identity[1] = os.getpid(), os.getppid()
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    entered.set()
    if mode in {"COMPLETED", "BUSY", "BLOCKED"}:
        return mode
    if mode == "failure":
        raise RuntimeError("PRIVATE_LOCAL_BATCH_FAILURE")
    if mode == "malformed":
        return {"private": "PRIVATE_LOCAL_BATCH_RESULT"}
    if mode == "crash":
        os._exit(7)
    if mode == "cooperative":
        assert stop.wait(10)
        return "COMPLETED"
    if mode == "stubborn":
        time.sleep(20)
        return "COMPLETED"
    raise AssertionError("unexpected QA mode")


def wait_until(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    pytest.fail("QA_ENTRY_PROCESS_STOP_NOT_CONFIRMED")


@pytest.fixture
def real_owners():
    owned = []

    def create(mode, *, work_ms="2000"):
        context = multiprocessing.get_context("spawn")
        entered, identity = context.Event(), context.RawArray("q", 2)
        done = threading.Event()
        jobs, owner_threads, starts = [], set(), []
        binding = ManagementBinding(
            AccountScope("BINANCE", "qa-entry-process", "SANDBOX", "FUTURES"),
            str(uuid4()),
            str(uuid4()),
            1,
            2,
            str(uuid4()),
        )

        class ObservedJob(BoundedProcessJob):
            def start(self, *args):
                owner_threads.add(threading.get_ident())
                starts.append(args)
                return super().start(*args)

            def poll(self):
                owner_threads.add(threading.get_ident())
                result = super().poll()
                if result["stop_confirmed"] and result["job_id"] is not None:
                    self.final = result
                    done.set()
                return result

        def factory(target, *, policy):
            job = ObservedJob(target, policy=policy)
            jobs.append(job)
            return job

        worker = EntryWorker(
            EntryPorts(
                binding, local_batch, (binding, mode, entered, identity), ("tv_bridge",)
            ),
            {
                "V2_ENTRY_WORK_MS": work_ms,
                "V2_ENTRY_STOP_MS": "50",
                "V2_ENTRY_TERMINATE_MS": "50",
                "V2_ENTRY_KILL_MS": "1000",
                "V2_ENTRY_POLL_MS": "10",
                "V2_ENTRY_IDLE_MS": "10000",
                "V2_ENTRY_STOP_JOIN_MS": "20",
            },
            job_factory=factory,
        )
        case = {
            "worker": worker,
            "entered": entered,
            "identity": identity,
            "done": done,
            "jobs": jobs,
            "owner_threads": owner_threads,
            "starts": starts,
        }
        owned.append(case)
        return case

    yield create
    for case in owned:
        case["worker"].request_stop()
        wait_until(case["worker"].stop)
        assert all(job._process is None for job in case["jobs"])


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("COMPLETED", "COMPLETED"),
        ("BUSY", "BUSY"),
        ("BLOCKED", "BLOCKED"),
        ("failure", "FAILED"),
        ("malformed", "FAILED"),
        ("crash", "FAILED"),
    ],
)
def test_real_batch_runs_as_direct_child_and_only_status_crosses_ipc(
    real_owners, mode, expected, capfd
):
    case = real_owners(mode)
    owner = case["worker"]
    assert owner.ready()
    assert not case["jobs"] and not case["entered"].is_set()
    owner.release()
    assert case["entered"].wait(4)
    # No controlling-thread poll/alive/health call drives completion.
    assert case["done"].wait(6)
    assert len(case["jobs"]) == len(case["starts"]) == 1
    result = case["jobs"][0].final
    assert result["status"] == expected
    assert result["stop_confirmed"] is True
    assert result["execution_authorized"] is False
    assert result["outcome_requires_reconciliation"] is True
    assert case["identity"][0] != os.getpid()
    assert case["identity"][1] == os.getpid()
    assert case["owner_threads"] == {owner._thread.ident}
    assert owner.healthy()
    captured = capfd.readouterr()
    assert "PRIVATE_LOCAL_BATCH" not in captured.out + captured.err + json.dumps(
        owner.snapshot()
    )


def test_stuck_batch_times_out_and_is_reaped_without_controller_poll(real_owners):
    case = real_owners("stubborn", work_ms="1500")
    owner = case["worker"]
    assert owner.ready()
    owner.release()
    assert case["entered"].wait(4)
    assert case["done"].wait(6), "local owner did not enforce absolute job budget"
    assert case["jobs"][0].final["status"] == "TIMEOUT"
    assert case["jobs"][0]._process is None
    assert len(case["jobs"]) == 1
    assert owner.alive() and owner.healthy()
    assert owner.snapshot()["trading_health"] == "NOT_EVALUATED"
    assert owner.snapshot()["lanes"]["tv_bridge"]["completed_batch_fresh"] is False


def test_stop_cooperation_reaps_child_before_reporting_owner_stopped(real_owners):
    case = real_owners("cooperative")
    owner = case["worker"]
    assert owner.ready()
    owner.release()
    assert case["entered"].wait(4)
    owner.request_stop()
    wait_until(owner.stop)
    assert case["jobs"][0].final["status"] == "STOPPED"
    assert case["jobs"][0]._process is None
    assert owner.snapshot()["stop_confirmed"] is True
    assert not owner.alive() and not owner.healthy()
    assert len(case["jobs"]) == 1


def test_stop_before_release_never_spawns_even_real_factory(real_owners):
    case = real_owners("COMPLETED")
    assert case["worker"].ready()
    wait_until(case["worker"].stop)
    assert case["jobs"] == []
    assert not case["entered"].is_set()
