"""Disposable real spawn processes and deterministic escalation; no DB/network."""

import multiprocessing
import os
import signal
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from v2_core.bounded_job import BoundedJobPolicy, BoundedProcessJob, _StopFlag


def _return(_stop, value):
    return value


def _failure(_stop):
    raise RuntimeError("PRIVATE_CHILD_ARGUMENT_MUST_NOT_ESCAPE")


def _crash(_stop, code):
    os._exit(code)


def _cooperate(stop, entered, wait_timeout=10):
    entered.set()
    assert stop.wait(wait_timeout) is True
    assert stop.is_set() is True
    return "COMPLETED"


def _ignore_stop(_stop, entered):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    entered.set()
    time.sleep(10)


def _delay_exit(_stop, entered):
    from multiprocessing.util import Finalize

    Finalize(None, time.sleep, args=(10,), exitpriority=0)
    entered.set()
    return "COMPLETED"


def finish(job, *, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = job.poll()
        if result["stop_confirmed"]:
            return result
        time.sleep(0.01)
    pytest.fail("disposable job failed to stop within test deadline")


@pytest.fixture
def real_jobs():
    jobs = []

    def create(target, *, policy=None):
        job = BoundedProcessJob(
            target, policy=policy or BoundedJobPolicy(2, 0.1, 0.1, 1)
        )
        jobs.append(job)
        return job

    yield create
    for job in jobs:
        # Test cleanup owns only processes explicitly created by this fixture.
        if job._process is not None:
            job._process.kill()
            job._process.join(3)
            job.poll()


@pytest.mark.parametrize(
    "value", [0, -1, True, None, "1", float("nan"), float("inf"), 3601, 10**1000]
)
@pytest.mark.parametrize("field", range(4))
def test_policy_requires_finite_positive_bounded_values(value, field):
    values = [1, 1, 1, 1]
    values[field] = value
    with pytest.raises(ValueError, match="^BOUNDED_JOB_POLICY_REQUIRED$"):
        BoundedJobPolicy(*values)


def test_policy_total_and_immutability():
    with pytest.raises(ValueError, match="BOUNDED_JOB_POLICY_REQUIRED"):
        BoundedJobPolicy(1000, 1000, 1000, 1000)
    policy = BoundedJobPolicy(1, 1, 1, 1)
    with pytest.raises(FrozenInstanceError):
        policy.work_seconds = 2


@pytest.mark.parametrize("result", ["COMPLETED", "BLOCKED", "BUSY"])
def test_real_spawn_returns_only_fixed_outcome_and_can_start_fresh(real_jobs, result):
    job = real_jobs(_return)
    initial = job.start(result)
    assert initial["status"] == "RUNNING"
    assert initial["stop_confirmed"] is False
    completed = finish(job)
    assert completed == {**initial, "status": result, "stop_confirmed": True}
    assert completed["execution_authorized"] is False
    assert completed["outcome_requires_reconciliation"] is True
    next_job = job.start("BUSY")
    assert next_job["job_id"] != initial["job_id"]
    assert finish(job)["status"] == "BUSY"


@pytest.mark.parametrize(
    "result", [None, True, "READY", "FAILED", {"private": "must not cross IPC"}]
)
def test_real_invalid_result_is_fixed_failure(real_jobs, result):
    job = real_jobs(_return)
    job.start(result)
    assert finish(job)["status"] == "FAILED"


def test_real_exception_text_is_not_exposed(real_jobs, capfd):
    job = real_jobs(_failure)
    job.start()
    result = finish(job)
    captured = capfd.readouterr()
    assert result["status"] == "FAILED"
    assert "PRIVATE_CHILD_ARGUMENT" not in str(result) + captured.out + captured.err


@pytest.mark.parametrize("code", [0, 7])
def test_real_exit_without_protocol_result_is_failure(real_jobs, code):
    job = real_jobs(_crash)
    job.start(code)
    assert finish(job)["status"] == "FAILED"


@pytest.mark.parametrize("wait_timeout", [None, 10])
def test_real_cooperative_stop_never_reports_late_success(real_jobs, wait_timeout):
    entered = multiprocessing.get_context("spawn").Event()
    job = real_jobs(_cooperate)
    job.start(entered, wait_timeout)
    assert entered.wait(3)
    requested = job.stop()
    assert requested["status"] in {"STOPPING", "STOPPED"}
    assert finish(job)["status"] == "STOPPED"


def test_real_waiting_child_kill_cannot_strand_parent_stop_flag(real_jobs):
    entered = multiprocessing.get_context("spawn").Event()
    job = real_jobs(_cooperate)
    job.start(entered, None)
    assert entered.wait(3)
    flag, process = job._stop_event, job._process
    process.kill()
    process.join(3)
    assert not process.is_alive()
    # No semaphore/condition shared with the dead child is acquired here.
    flag.set()
    assert flag.is_set() and flag.wait(0) is True
    assert finish(job)["status"] == "FAILED"


def test_real_deadline_cooperative_return_is_still_timeout(real_jobs):
    entered = multiprocessing.get_context("spawn").Event()
    job = real_jobs(_cooperate, policy=BoundedJobPolicy(0.8, 0.2, 0.1, 1))
    job.start(entered, None)
    assert entered.wait(3)
    assert finish(job)["status"] == "TIMEOUT"


def test_real_ignored_stop_and_sigterm_escalate_to_kill(real_jobs):
    entered = multiprocessing.get_context("spawn").Event()
    job = real_jobs(_ignore_stop, policy=BoundedJobPolicy(2, 0.03, 0.03, 1))
    job.start(entered)
    process = job._process
    assert entered.wait(3)
    job.stop()
    result = finish(job)
    assert result["status"] == "STOPPED" and result["stop_confirmed"] is True
    assert process._closed


def test_real_result_without_exit_is_not_completion_and_hits_deadline(real_jobs):
    entered = multiprocessing.get_context("spawn").Event()
    job = real_jobs(_delay_exit, policy=BoundedJobPolicy(1, 0.03, 0.03, 1))
    job.start(entered)
    assert entered.wait(3)
    assert job.poll()["status"] == "RUNNING"
    assert finish(job)["status"] == "TIMEOUT"


class FakeProcess:
    def __init__(self, *, start_error=False, partial=False):
        self.pid = None
        self.exitcode = None
        self.alive = False
        self.calls = []
        self.start_error, self.partial = start_error, partial

    def start(self):
        if not self.start_error or self.partial:
            self.pid, self.alive = 12345, True
        if self.start_error:
            raise RuntimeError("secret spawn error")

    def join(self, *, timeout):
        self.calls.append(("join", timeout))

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.calls.append(("terminate",))

    def kill(self):
        self.calls.append(("kill",))

    def close(self):
        self.calls.append(("close",))


class FakeChannel:
    def __init__(self, payload=b"COMPLETED"):
        self.payload, self.closed, self.reads = payload, False, 0

    def poll(self, timeout):
        assert timeout == 0
        return True

    def recv_bytes(self, *, maxlength):
        self.reads += 1
        assert maxlength == 64
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class FakeStopCell:
    value = 0

    @property
    def called(self):
        return self.value == 1


def fake_job(*, process=None, payload=b"COMPLETED"):
    process = process or FakeProcess()
    channel, child = FakeChannel(payload), FakeChannel()
    now, stop = SimpleNamespace(value=0), FakeStopCell()

    def raw_value(typecode, value):
        assert (typecode, value) == ("b", 0)
        return stop

    context = SimpleNamespace(
        RawValue=raw_value,
        Pipe=lambda **_: (channel, child),
        Process=lambda **_: process,
    )
    job = BoundedProcessJob(
        _return,
        policy=BoundedJobPolicy(1, 2, 3, 4),
        clock=lambda: now.value,
        context=context,
    )
    return SimpleNamespace(
        job=job, process=process, channel=channel, child=child, now=now, stop=stop
    )


def test_idle_does_no_process_work():
    case = fake_job()
    expected = {
        "job_id": None,
        "status": "IDLE",
        "stop_confirmed": True,
        "execution_authorized": False,
        "outcome_requires_reconciliation": False,
    }
    assert case.job.poll() == case.job.stop() == expected
    assert case.process.calls == []


def test_only_owned_child_and_zero_timeout_joins_are_used():
    case = fake_job()
    initial = case.job.start()
    assert case.child.closed
    with pytest.raises(RuntimeError, match="^BOUNDED_JOB_NOT_STOPPED$"):
        case.job.start()
    assert case.job.poll()["job_id"] == initial["job_id"]
    case.now.value = 1
    assert case.job.poll()["status"] == "STOPPING"
    assert case.stop.called and case.channel.reads == 0
    case.now.value = 3
    case.job.poll()
    assert ("terminate",) in case.process.calls
    case.now.value = 6
    case.job.poll()
    assert ("kill",) in case.process.calls
    case.now.value = 10
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    assert case.job._process is case.process and not case.channel.closed
    assert all(call == ("join", 0) for call in case.process.calls if call[0] == "join")
    with pytest.raises(RuntimeError, match="BOUNDED_JOB_NOT_STOPPED"):
        case.job.start()
    case.process.alive, case.process.exitcode = False, -9
    result = case.job.poll()
    assert result["status"] == "TIMEOUT" and result["stop_confirmed"]
    assert case.channel.reads == 0 and case.channel.closed


def test_late_poll_does_not_extend_any_deadline():
    case = fake_job()
    case.job.start()
    case.now.value = 100
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    assert case.process.calls.count(("terminate",)) == 1
    assert case.process.calls.count(("kill",)) == 1
    case.job.poll()
    assert case.process.calls.count(("kill",)) == 1


def test_explicit_stop_uses_its_own_absolute_stop_deadline():
    case = fake_job()
    case.job.start()
    case.now.value = 0.5
    assert case.job.stop()["status"] == "STOPPING"
    case.now.value = 2.4
    case.job.poll()
    assert ("terminate",) not in case.process.calls
    case.now.value = 20
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    case.process.alive, case.process.exitcode = False, -9
    assert case.job.poll()["status"] == "STOPPED"


def test_unobserved_normal_exit_after_deadline_is_conservatively_timeout():
    case = fake_job()
    case.job.start()
    case.process.alive, case.process.exitcode = False, 0
    case.now.value = 2
    assert case.job.poll()["status"] == "TIMEOUT"
    assert case.channel.reads == 0


@pytest.mark.parametrize(
    "payload",
    [b"READY", b"", b"COMPLETED" * 10, EOFError(), OSError("private IPC error")],
)
def test_invalid_or_missing_postexit_ipc_is_fixed_failure(payload):
    case = fake_job(payload=payload)
    case.job.start()
    case.process.alive, case.process.exitcode = False, 0
    assert case.job.poll()["status"] == "FAILED"


def test_spawn_failure_without_child_closes_owned_channels():
    case = fake_job(process=FakeProcess(start_error=True))
    result = case.job.start("private argument")
    assert result["status"] == "FAILED" and result["stop_confirmed"]
    assert case.channel.closed and case.child.closed
    assert "private" not in str(result)


def test_partial_spawn_failure_retains_child_until_reaped():
    case = fake_job(process=FakeProcess(start_error=True, partial=True))
    assert case.job.start()["status"] == "STOPPING"
    assert case.job._process is case.process and case.stop.called
    case.now.value = 20
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    case.process.alive, case.process.exitcode = False, -9
    assert case.job.poll()["status"] == "FAILED"


def test_exitcode_unknown_is_not_stop_confirmation():
    case = fake_job()
    case.job.start()
    case.process.alive = False
    case.now.value = 20
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    assert case.job._process is case.process and case.channel.reads == 0


def test_join_failure_does_not_drop_process_ownership():
    case = fake_job()
    case.job.start()

    def broken_join(**_):
        raise OSError("private process error")

    case.process.join = broken_join
    case.now.value = 20
    assert case.job.poll()["status"] == "STOP_UNCONFIRMED"
    assert case.job._process is case.process


@pytest.mark.parametrize(
    "clock",
    [lambda: -1, lambda: True, lambda: float("nan"), lambda: "0", lambda: 10**1000],
)
def test_invalid_clock_is_rejected_before_spawning(clock):
    job = BoundedProcessJob(_return, policy=BoundedJobPolicy(1, 1, 1, 1), clock=clock)
    with pytest.raises(ValueError, match="BOUNDED_JOB_CLOCK_INVALID"):
        job.start()
    assert job._process is None


def test_regressing_clock_is_rejected():
    case = fake_job()
    case.job.start()
    case.now.value = 0.5
    case.job.poll()
    case.now.value = 0.4
    with pytest.raises(ValueError, match="BOUNDED_JOB_CLOCK_INVALID"):
        case.job.poll()


@pytest.mark.parametrize("method", ["fork", "forkserver"])
def test_supplied_real_context_must_not_inherit_process_connections(method):
    with pytest.raises(ValueError, match="^BOUNDED_JOB_SPAWN_REQUIRED$"):
        BoundedProcessJob(
            _return,
            policy=BoundedJobPolicy(1, 1, 1, 1),
            context=multiprocessing.get_context(method),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"target": None},
        {"policy": {"work_seconds": 1}},
        {"clock": None},
    ],
)
def test_constructor_requires_explicit_typed_dependencies(changes):
    with pytest.raises(ValueError, match="^EXPLICIT_BOUNDED_JOB_REQUIRED$"):
        BoundedProcessJob(
            **{
                "target": _return,
                "policy": BoundedJobPolicy(1, 1, 1, 1),
                "clock": time.monotonic,
                **changes,
            }
        )


@pytest.mark.parametrize(
    "timeout", [-1, True, "1", float("nan"), float("inf"), 10**1000]
)
def test_stop_flag_rejects_invalid_wait_without_acquiring_any_lock(timeout):
    flag = _StopFlag(FakeStopCell())
    with pytest.raises(ValueError, match="^BOUNDED_JOB_STOP_WAIT_INVALID$"):
        flag.wait(timeout)


def test_stop_flag_wait_timeout_and_latched_parent_set():
    flag = _StopFlag(FakeStopCell())
    assert flag.is_set() is False
    assert flag.wait(0) is False
    assert flag.wait(0.001) is False
    flag.set()
    assert flag.is_set() is True
    assert flag.wait() is True
    assert flag.wait(0) is True
