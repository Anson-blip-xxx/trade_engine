"""Real owner threads with explicit fake process ports; no DB or network."""

import json
import threading
import time
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.v2_entry_worker import EntryPorts, EntryWorker
from services.v2_management_runtime import ManagementBinding
from services.v2_worker_group import WorkerGroup
from v2_core.account_risk import AccountScope
from v2_core.bounded_job import BoundedJobPolicy


def wait_until(check, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.005)
    pytest.fail("QA_ENTRY_OWNER_CONDITION_NOT_REACHED")


def binding():
    return ManagementBinding(
        AccountScope("BINANCE", "qa-entry-owner", "SANDBOX", "FUTURES"),
        str(uuid4()),
        str(uuid4()),
        1,
        7,
        str(uuid4()),
    )


def config(**changes):
    return {
        "V2_ENTRY_WORK_MS": "1000",
        "V2_ENTRY_STOP_MS": "10",
        "V2_ENTRY_TERMINATE_MS": "10",
        "V2_ENTRY_KILL_MS": "10",
        "V2_ENTRY_POLL_MS": "10",
        "V2_ENTRY_IDLE_MS": "10",
        "V2_ENTRY_READY_MS": "100",
        "V2_ENTRY_STOP_JOIN_MS": "20",
        "V2_ENTRY_PROGRESS_TIMEOUT_MS": "1000",
        **changes,
    }


def target(_stop, _binding, _source):
    pytest.fail("fake process owner must never execute a batch locally")


class FakeJob:
    def __init__(self, case, job_target, policy):
        assert job_target is target
        assert type(policy) is BoundedJobPolicy
        self.case, self.policy = case, policy
        self.hold_stop = self.raise_poll = self.raise_start = self.raise_stop = False
        self.state = {
            "job_id": None,
            "status": "IDLE",
            "stop_confirmed": True,
            "execution_authorized": False,
            "outcome_requires_reconciliation": False,
        }
        self.lock = threading.Lock()

    def record(self, method, args=()):
        self.case.calls.append((method, threading.get_ident(), self, args))

    def start(self, *args):
        self.record("start", args)
        with self.lock:
            self.state.update(
                job_id=str(uuid4()),
                status="RUNNING",
                stop_confirmed=False,
                outcome_requires_reconciliation=True,
            )
            self.case.starts.append((self, args))
            if self.raise_start:
                raise OSError("PRIVATE_START_DETAIL")
            return dict(self.state)

    def poll(self):
        self.record("poll")
        with self.lock:
            if self.raise_poll:
                raise RuntimeError("PRIVATE_POLL_DETAIL")
            return dict(self.state)

    def stop(self):
        self.record("stop")
        with self.lock:
            if self.raise_stop:
                raise RuntimeError("PRIVATE_STOP_DETAIL")
            self.state.update(
                status="STOP_UNCONFIRMED" if self.hold_stop else "STOPPED",
                stop_confirmed=not self.hold_stop,
            )
            return dict(self.state)

    def finish(self, status="COMPLETED"):
        with self.lock:
            self.state.update(status=status, stop_confirmed=True)


@pytest.fixture
def workers():
    cases = []

    def create(
        *, sources=("tv_bridge", "s3", "quant"), values=None, clock=None, factory=None
    ):
        selected = binding()
        case = SimpleNamespace(binding=selected, calls=[], starts=[], jobs=[])
        case.ports = EntryPorts(selected, target, (selected,), sources)

        def make(job_target, *, policy):
            case.calls.append(("factory", threading.get_ident(), None, ()))
            job = FakeJob(case, job_target, policy)
            case.jobs.append(job)
            if factory is not None:
                factory(case, job)
            return job

        case.worker = EntryWorker(
            case.ports,
            config() if values is None else values,
            job_factory=make,
            **({"clock": clock} if clock else {}),
        )
        cases.append(case)
        return case

    yield create
    for case in cases:
        for job in case.jobs:
            job.hold_stop = job.raise_start = job.raise_poll = job.raise_stop = False
            job.finish("STOPPED")
        case.worker.request_stop()
        wait_until(case.worker.stop)


def start(case):
    assert case.worker.ready()
    case.worker.release()
    wait_until(lambda: bool(case.starts))
    return case.starts[0][0]


def test_ready_is_not_release_and_no_target_runs_in_owner(workers):
    case = workers()
    assert case.worker.ready()
    assert case.worker.entries_enabled is False
    with pytest.raises(FrozenInstanceError):
        case.ports.sources = ("s2",)
    for _ in range(10):
        assert case.worker.alive() and case.worker.healthy()
        state = case.worker.snapshot()
        assert state["trading_health"] == "NOT_EVALUATED"
        assert state["health_scope"] == "PROCESS_OWNERSHIP_ONLY"
        assert not state["execution_authorized"] and not state["entry_dispatch_enabled"]
        assert all(
            not lane["completed_batch_fresh"] for lane in state["lanes"].values()
        )
    time.sleep(0.04)
    assert not case.calls
    job = start(case)
    assert case.starts[0][1] == (case.binding, "tv_bridge")
    assert job.policy == BoundedJobPolicy(1, 0.01, 0.01, 0.01)


def test_active_job_only_polled_locally_by_single_owner(workers):
    case = workers()
    start(case)
    wait_until(lambda: sum(call[0] == "poll" for call in case.calls) >= 4)
    before = len(case.calls)
    for _ in range(20):
        case.worker.alive()
        case.worker.healthy()
        case.worker.snapshot()
    time.sleep(0.04)
    assert len(case.calls) > before
    assert len(case.jobs) == len(case.starts) == 1
    assert {call[1] for call in case.calls} == {case.worker._thread.ident}
    assert case.worker._thread.ident != threading.get_ident()


@pytest.mark.parametrize(
    "status", ["COMPLETED", "BUSY", "BLOCKED", "FAILED", "TIMEOUT"]
)
def test_sources_rotate_after_empty_failed_or_completed_batch(workers, status):
    case = workers()
    start(case)
    for index in range(6):
        wait_until(lambda index=index: len(case.starts) > index)
        job, args = case.starts[index]
        assert args[-1] == case.ports.sources[index % 3]
        job.finish(status)
    assert case.worker.healthy()
    assert case.worker.snapshot()["trading_health"] == "NOT_EVALUATED"


def test_factory_failure_advances_lane_and_does_not_disable_management(workers):
    def fail_first(case, _job):
        if len(case.jobs) == 1:
            raise RuntimeError("PRIVATE_FACTORY_DETAIL")

    case = workers(factory=fail_first)
    start(case)
    assert case.starts[0][1][-1] == "s3"
    state = case.worker.snapshot()
    assert state["lanes"]["tv_bridge"]["last_status"] == "FAILED"
    assert case.worker.healthy()
    assert "PRIVATE" not in json.dumps(state)


@pytest.mark.parametrize("failure", ["start", "poll", "stop"])
def test_unknown_stop_keeps_original_handle_and_prevents_next_lane(workers, failure):
    def inject(_case, job):
        job.hold_stop = True
        job.raise_start = failure == "start"
        job.raise_poll = failure in {"poll", "stop"}
        job.raise_stop = failure == "stop"

    case = workers(factory=inject)
    job = start(case)
    wait_until(lambda: any(call[0] == "stop" for call in case.calls))
    time.sleep(0.04)
    assert case.worker.alive() and case.worker.healthy()
    assert case.worker.snapshot()["stop_confirmed"] is False
    assert case.worker.stop() is False
    assert len(case.jobs) == len(case.starts) == 1
    assert case.worker._job is job
    assert "PRIVATE" not in json.dumps(case.worker.snapshot())
    job.raise_poll = job.raise_stop = job.raise_start = job.hold_stop = False
    wait_until(case.worker.stop)
    assert case.worker.snapshot()["stop_confirmed"] is True


def test_malformed_stop_boolean_is_not_confirmed_death(workers):
    case = workers()
    job = start(case)
    job.hold_stop = job.raise_stop = True
    with job.lock:
        job.state.update(status="COMPLETED", stop_confirmed=1)
    wait_until(lambda: case.worker.snapshot()["status"] == "STOP_UNCONFIRMED")
    assert len(case.starts) == 1
    assert case.worker.stop() is False
    job.raise_stop = job.hold_stop = False
    wait_until(case.worker.stop)


def test_confirmed_failed_job_may_rotate_after_cleanup(workers):
    def fail_first(case, job):
        job.raise_start = len(case.jobs) == 1

    case = workers(factory=fail_first)
    start(case)
    wait_until(lambda: len(case.starts) >= 2)
    assert [args[-1] for _, args in case.starts[:2]] == ["tv_bridge", "s3"]
    stop_index = next(i for i, call in enumerate(case.calls) if call[0] == "stop")
    second_start_index = next(
        i
        for i, call in enumerate(case.calls)
        if call[0] == "start" and call[2] is case.starts[1][0]
    )
    assert stop_index < second_start_index
    assert case.worker.snapshot()["lanes"]["tv_bridge"]["last_failure"] == "FAILED"


def test_stop_before_release_starts_nothing_and_release_cannot_revive(workers):
    case = workers()
    assert case.worker.ready()
    wait_until(case.worker.stop)
    assert not case.starts
    assert not case.worker.alive() and not case.worker.healthy()
    assert not case.worker.ready()
    with pytest.raises(ValueError, match="ENTRY_WORKER_NOT_READY"):
        case.worker.release()


def test_batch_progress_is_separate_from_management_health(workers):
    now = [100.0]
    case = workers(clock=lambda: now[0])
    job = start(case)
    now[0] = 200.0
    assert case.worker.healthy()
    assert all(
        not lane["completed_batch_fresh"]
        for lane in case.worker.snapshot()["lanes"].values()
    )
    job.finish("COMPLETED")
    wait_until(lambda: len(case.starts) >= 2)
    state = case.worker.snapshot()
    assert state["lanes"]["tv_bridge"]["completed_batch_fresh"] is True
    assert state["lanes"]["s3"]["completed_batch_fresh"] is False
    case.starts[1][0].finish("FAILED")
    wait_until(lambda: len(case.starts) >= 3)
    now[0] = 202.0
    assert (
        case.worker.snapshot()["lanes"]["tv_bridge"]["completed_batch_fresh"] is False
    )
    assert case.worker.healthy()


def test_worker_group_keeps_management_running_on_entry_failure(workers):
    case = workers()
    management = SimpleNamespace(
        ready=lambda: True,
        release=lambda: None,
        alive=lambda: True,
        healthy=lambda: True,
        request_stop=lambda: None,
        stop=lambda: True,
    )
    group = WorkerGroup([management, case.worker])
    assert group.ready()
    group.release()
    wait_until(lambda: bool(case.starts))
    case.starts[0][0].finish("FAILED")
    wait_until(lambda: len(case.starts) >= 2)
    assert group.healthy() and group.alive()
    case.starts[1][0].hold_stop = True
    assert group.stop() is False
    assert group.stop_confirmations == (True, False)
    case.starts[1][0].hold_stop = False
    wait_until(group.stop)


@pytest.mark.parametrize(
    "sources", [(), ("s2", "s2"), ("unknown",), ("s2",) * 5, ["s2"], (1,)]
)
def test_sources_are_an_explicit_small_closed_set(sources):
    with pytest.raises(ValueError, match="EXPLICIT_ENTRY_PORTS_REQUIRED"):
        EntryPorts(binding(), target, (), sources)


@pytest.mark.parametrize(
    "change",
    [
        {"job_target": "module:function"},
        {"job_args": []},
        {"job_args": (None,) * 17},
        {"binding": None},
    ],
)
def test_ports_reject_dynamic_imports_and_unbounded_arguments(change):
    ports = EntryPorts(binding(), target, (), ("s2",))
    with pytest.raises(ValueError, match="EXPLICIT_ENTRY_PORTS_REQUIRED"):
        replace(ports, **change)


@pytest.mark.parametrize(
    "key,value",
    [
        ("V2_ENVIRONMENT", "LIVE"),
        ("V2_ENTRY_WORK_MS", "99"),
        ("V2_ENTRY_WORK_MS", "300001"),
        ("V2_ENTRY_WORK_MS", True),
        ("V2_ENTRY_WORK_MS", " 100"),
        ("V2_ENTRY_WORK_MS", "١٠٠"),
        ("V2_ENTRY_WORK_MS", "NaN"),
        ("V2_ENTRY_STOP_MS", "9"),
        ("V2_ENTRY_TERMINATE_MS", "10001"),
        ("V2_ENTRY_KILL_MS", "0"),
        ("V2_ENTRY_POLL_MS", "251"),
        ("V2_ENTRY_IDLE_MS", "10001"),
        ("V2_ENTRY_READY_MS", "30001"),
        ("V2_ENTRY_STOP_JOIN_MS", "1001"),
        ("V2_ENTRY_PROGRESS_TIMEOUT_MS", "99"),
    ],
)
def test_invalid_entry_config_rejected_before_any_owner_is_created(key, value):
    called = []
    with pytest.raises(ValueError):
        EntryWorker(
            EntryPorts(binding(), target, (), ("s2",)),
            config(**{key: value}),
            job_factory=lambda *_args, **_kwargs: called.append(True),
        )
    assert called == []


def test_settlement_and_controller_budgets_cannot_override_entry_policy(workers):
    case = workers(
        values=config(
            V2_SETTLEMENT_WORK_MS="300000", V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS="999"
        )
    )
    job = start(case)
    assert job.policy.work_seconds == 1
