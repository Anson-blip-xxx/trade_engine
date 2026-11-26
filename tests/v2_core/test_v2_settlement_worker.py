"""Real-thread supervision with explicit fake selection and disposable-job ports.

No PostgreSQL, credentials or subprocess is opened here. These tests prove the
controller worker lifecycle and scheduling contract, not financial settlement.
"""

import json
import threading
import time
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from services.v2_management_runtime import ManagementBinding
from services.v2_settlement_worker import SettlementPorts, SettlementWorker
from v2_core.account_risk import AccountScope
from v2_core.bounded_job import BoundedJobPolicy


def wait_until(predicate, *, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("QA_WORKER_CONDITION_NOT_REACHED")


def config(**changes):
    return {
        "V2_CONTROLLER_ENABLE_ENTRIES": "false",
        "V2_SETTLEMENT_WORK_MS": "1000",
        "V2_SETTLEMENT_STOP_MS": "10",
        "V2_SETTLEMENT_TERMINATE_MS": "10",
        "V2_SETTLEMENT_KILL_MS": "10",
        "V2_SETTLEMENT_POLL_MS": "10",
        "V2_SETTLEMENT_IDLE_MS": "10",
        "V2_SETTLEMENT_READY_MS": "50",
        "V2_SETTLEMENT_STOP_JOIN_MS": "30",
        "V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS": "30",
        **changes,
    }


class FakeJob:
    """Only the worker thread invokes the job API; tests control its result."""

    def __init__(self, case, target, policy):
        assert callable(target)
        assert type(policy) is BoundedJobPolicy
        self.case, self.target, self.policy = case, target, policy
        self.state = {
            "job_id": None,
            "status": "IDLE",
            "stop_confirmed": True,
            "execution_authorized": False,
            "outcome_requires_reconciliation": False,
        }
        self.hold_stop = False
        self.raise_poll = False
        self.started = False
        self.lock = threading.Lock()

    def _record(self, name, args=()):
        self.case.events.append((name, threading.get_ident(), self, args))

    def start(self, *args):
        self._record("start", args)
        with self.lock:
            assert self.state["stop_confirmed"], "a live job cannot be replaced"
            self.started = True
            self.state = {
                "job_id": str(uuid4()),
                "status": "RUNNING",
                "stop_confirmed": False,
                "execution_authorized": False,
                "outcome_requires_reconciliation": True,
            }
            self.case.starts.append((self, args))
            return dict(self.state)

    def poll(self):
        self._record("poll")
        with self.lock:
            if self.raise_poll:
                raise RuntimeError("PRIVATE_JOB_POLL_DETAIL")
            return dict(self.state)

    def stop(self):
        self._record("stop")
        with self.lock:
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

    def create(*, select=None, bootstrap=None, values=None, clock=None):
        scope = AccountScope("BINANCE", "qa-settlement-worker", "SANDBOX", "FUTURES")
        binding = ManagementBinding(
            scope, str(uuid4()), str(uuid4()), 1, 7, str(uuid4())
        )
        target = {
            "target_registry": binding.registry_id,
            "binding_version": binding.binding_version,
            "epoch": binding.epoch,
            "worker_token": binding.worker_token,
        }
        case = SimpleNamespace(
            binding=binding,
            target=target,
            events=[],
            selections=[],
            starts=[],
            jobs=[],
            cleanup_events=[],
            bootstrap_calls=[],
        )

        def selector(kind, after):
            case.selections.append((kind, after, threading.get_ident()))
            return None if select is None else select(case, kind, after)

        ports = SettlementPorts(binding, selector, (binding,))
        case.ports = ports

        def initialize(tenant, selected, supplied):
            case.bootstrap_calls.append(
                (tenant, selected, supplied, threading.get_ident())
            )
            return ports if bootstrap is None else bootstrap(case)

        def factory(job_target, *, policy):
            job = FakeJob(case, job_target, policy)
            case.jobs.append(job)
            return job

        case.worker = SettlementWorker(
            binding.tenant_id,
            target,
            config() if values is None else values,
            bootstrap=initialize,
            job_factory=factory,
            **({"clock": clock} if clock is not None else {}),
        )
        cases.append(case)
        return case

    yield create
    for case in cases:
        for event in case.cleanup_events:
            event.set()
        for job in case.jobs:
            job.hold_stop = job.raise_poll = False
            job.finish("STOPPED")
        case.worker.request_stop()
        wait_until(case.worker.stop)


def one_episode(_case, kind, _after):
    return str(UUID(int=1)) if kind == "FINANCIAL" else None


def release_and_start(case):
    assert case.worker.ready()
    case.worker.release()
    wait_until(lambda: bool(case.starts))
    return case.starts[0][0]


def test_ports_are_immutable_and_worker_never_enables_entries(workers):
    case = workers()
    assert case.worker.entries_enabled is False
    with pytest.raises(FrozenInstanceError):
        case.ports.job_args = ()
    assert case.worker.ready()
    assert case.worker.alive()
    assert case.worker.snapshot()["execution_authorized"] is False


def test_bootstrap_ready_is_not_release_and_control_reads_do_not_select(workers):
    case = workers(select=one_episode)
    assert case.worker.ready()
    assert len(case.bootstrap_calls) == 1
    assert case.bootstrap_calls[0][0] == case.binding.tenant_id
    assert case.bootstrap_calls[0][1] == case.target
    assert case.bootstrap_calls[0][3] != threading.get_ident()
    for _ in range(20):
        case.worker.alive()
        case.worker.healthy()
        case.worker.snapshot()
    time.sleep(0.04)
    assert not case.selections and not case.starts
    case.worker.release()
    wait_until(lambda: bool(case.starts))
    job, args = case.starts[0]
    assert args == (case.binding, "FINANCIAL", str(UUID(int=1)))
    assert job.policy == BoundedJobPolicy(1, 0.01, 0.01, 0.01)


def test_single_background_owner_polls_active_job_without_selection(workers):
    case = workers(select=one_episode)
    job = release_and_start(case)
    wait_until(lambda: sum(event[0] == "poll" for event in case.events) >= 3)
    selected = list(case.selections)
    calls = len(case.events)
    time.sleep(0.05)
    assert case.selections == selected
    assert len(case.events) > calls
    assert len(case.starts) == 1
    assert {event[1] for event in case.events} == {case.selections[0][2]}
    assert case.selections[0][2] != threading.get_ident()
    assert case.worker.snapshot()["stop_confirmed"] is False
    job.finish()
    wait_until(lambda: len(case.selections) > len(selected))


@pytest.mark.parametrize(
    "status", ["COMPLETED", "BLOCKED", "BUSY", "FAILED", "TIMEOUT"]
)
def test_each_lane_rotates_after_any_confirmed_job_outcome(workers, status):
    identities = {
        "FINANCIAL": [str(UUID(int=1)), str(UUID(int=3))],
        "OUTCOME": [str(UUID(int=2)), str(UUID(int=4))],
    }

    def select(_case, kind, after):
        return next(
            (item for item in identities[kind] if item > after), identities[kind][0]
        )

    case = workers(select=select)
    release_and_start(case)
    observed = []
    for index in range(6):
        wait_until(lambda index=index: len(case.starts) > index)
        job, args = case.starts[index]
        observed.append(args[-2:])
        job.finish(status)
    for kind, expected in identities.items():
        assert [identity for lane, identity in observed if lane == kind] == [
            *expected,
            expected[0],
        ]
    by_lane = {
        kind: [after for lane, after, _ in case.selections if lane == kind]
        for kind in identities
    }
    for kind, expected in identities.items():
        assert by_lane[kind][:3] == ["", *expected]


def test_empty_lane_does_not_starve_financial_work(workers):
    case = workers(select=one_episode)
    release_and_start(case)
    for index in range(3):
        wait_until(lambda index=index: len(case.starts) > index)
        job, args = case.starts[index]
        assert args[-2:] == ("FINANCIAL", str(UUID(int=1)))
        job.finish("BLOCKED")
    assert any(kind == "OUTCOME" for kind, _, _ in case.selections)


def test_pending_bootstrap_is_not_ready_and_stop_cannot_drop_its_thread(workers):
    entered, resume = threading.Event(), threading.Event()

    def bootstrap(case):
        case.cleanup_events.append(resume)
        entered.set()
        resume.wait(3)
        return case.ports

    case = workers(bootstrap=bootstrap)
    assert entered.wait(1)
    assert not case.worker.ready()
    with pytest.raises(ValueError, match="SETTLEMENT_WORKER_NOT_READY"):
        case.worker.release()
    before = time.monotonic()
    case.worker.request_stop()
    assert time.monotonic() - before < 0.1
    assert case.worker.stop() is False
    assert not case.starts and not case.selections
    resume.set()
    wait_until(case.worker.stop)
    with pytest.raises(ValueError, match="SETTLEMENT_WORKER_NOT_READY"):
        case.worker.release()
    time.sleep(0.03)
    assert not case.starts


def test_blocked_selector_does_not_block_controller_reads_or_confirm_stop(workers):
    entered, resume = threading.Event(), threading.Event()

    def select(case, kind, after):
        case.cleanup_events.append(resume)
        entered.set()
        resume.wait(3)
        return str(UUID(int=1))

    case = workers(select=select)
    assert case.worker.ready()
    case.worker.release()
    assert entered.wait(1)
    before = time.monotonic()
    case.worker.alive()
    case.worker.healthy()
    case.worker.snapshot()
    case.worker.request_stop()
    assert time.monotonic() - before < 0.1
    assert case.worker.stop() is False
    assert not case.starts
    resume.set()
    wait_until(case.worker.stop)
    assert not case.starts, "a selection returned after stop must not create a job"


def test_unconfirmed_job_stop_preserves_ownership_without_selection_or_restart(workers):
    case = workers(select=one_episode)
    job = release_and_start(case)
    job.hold_stop = True
    selected = list(case.selections)
    case.worker.request_stop()
    assert case.worker.stop() is False
    wait_until(lambda: any(event[0] == "stop" for event in case.events))
    snapshot = case.worker.snapshot()
    assert snapshot["stop_confirmed"] is False
    assert snapshot["execution_authorized"] is False
    with pytest.raises(ValueError, match="SETTLEMENT_WORKER_NOT_READY"):
        case.worker.release()
    assert case.worker.stop() is False
    assert len(case.starts) == 1 and case.selections == selected
    assert all(event[1] != threading.get_ident() for event in case.events)
    job.hold_stop = False
    job.finish("STOPPED")
    wait_until(case.worker.stop)


def test_stop_before_release_is_idempotent_and_never_selects(workers):
    case = workers(select=one_episode)
    assert case.worker.ready()
    case.worker.request_stop()
    wait_until(case.worker.stop)
    assert case.worker.stop() is True
    with pytest.raises(ValueError, match="SETTLEMENT_WORKER_NOT_READY"):
        case.worker.release()
    time.sleep(0.03)
    assert not case.selections and not case.starts
    assert not case.worker.alive()


def test_bootstrap_failure_is_fixed_diagnostic_and_never_ready(workers, capsys):
    def fail(_case):
        raise RuntimeError("PRIVATE_BOOTSTRAP_DETAIL")

    case = workers(bootstrap=fail)
    assert case.worker.ready() is False
    wait_until(lambda: not case.worker.alive())
    assert not case.starts and not case.selections
    captured = capsys.readouterr()
    assert (
        "PRIVATE_BOOTSTRAP"
        not in json.dumps(case.worker.snapshot()) + captured.out + captured.err
    )
    assert case.worker.stop()


@pytest.mark.parametrize(
    "field", ["tenant_id", "registry_id", "binding_version", "epoch", "worker_token"]
)
def test_bootstrap_cannot_ack_a_changed_account_route_binding(workers, field):
    def wrong_binding(case):
        changed = 999 if field in {"binding_version", "epoch"} else str(uuid4())
        return replace(case.ports, binding=replace(case.binding, **{field: changed}))

    case = workers(bootstrap=wrong_binding)
    assert case.worker.ready() is False
    wait_until(lambda: not case.worker.alive())
    assert case.worker.startup_failure == "WORKER_START_FAILED"
    assert not case.starts and not case.selections


@pytest.mark.parametrize("result", [None, {}, SimpleNamespace()])
def test_bootstrap_requires_explicit_typed_ports(workers, result):
    case = workers(bootstrap=lambda _case: result)
    assert not case.worker.ready()
    wait_until(lambda: not case.worker.alive())
    assert not case.starts
    assert case.worker.startup_failure == "WORKER_START_FAILED"


def test_permission_denial_preserves_only_fixed_startup_code(workers):
    class PermissionDenied(RuntimeError):
        sqlstate = "42501"

    def fail(_case):
        raise PermissionDenied("PRIVATE_ROLE_DETAILS")

    case = workers(bootstrap=fail)
    assert not case.worker.ready()
    wait_until(lambda: not case.worker.alive())
    assert case.worker.startup_failure == "EXECUTION_GUARD_PERMISSION_DENIED"
    assert "PRIVATE_ROLE" not in json.dumps(case.worker.snapshot())


@pytest.mark.parametrize(
    "identity", ["", "bad-id", 1, True, {}, str(UUID(int=10)).upper()]
)
def test_bad_selected_identity_never_reaches_a_job(workers, identity):
    case = workers(select=lambda *_: identity)
    assert case.worker.ready()
    case.worker.release()
    wait_until(lambda: len(case.selections) >= 2)
    assert case.worker.alive()
    assert not case.starts


def test_poll_failure_cannot_drop_an_unconfirmed_job_or_schedule_another(workers):
    case = workers(select=one_episode)
    job = release_and_start(case)
    job.hold_stop = True
    selected = list(case.selections)
    job.raise_poll = True
    wait_until(lambda: any(event[0] == "stop" for event in case.events))
    assert case.worker.alive()
    assert case.worker.snapshot()["stop_confirmed"] is False
    assert "PRIVATE_JOB_POLL" not in json.dumps(case.worker.snapshot())
    assert case.worker.stop() is False
    assert case.selections == selected
    assert len(case.starts) == 1
    job.raise_poll = job.hold_stop = False
    job.finish("STOPPED")
    wait_until(case.worker.stop)


def test_selector_failure_is_local_retried_and_not_successful_progress(workers):
    current = [100.0]

    def fail(_case, kind, after):
        raise RuntimeError("PRIVATE_SELECTOR_DETAIL")

    case = workers(select=fail, clock=lambda: current[0])
    assert case.worker.ready()
    case.worker.release()
    wait_until(lambda: len(case.selections) >= 2)
    assert not case.starts
    assert case.worker.alive()
    current[0] = 131.0
    assert case.worker.healthy() is False
    assert "PRIVATE_SELECTOR" not in json.dumps(case.worker.snapshot())


def test_completed_job_and_empty_queue_refresh_progress_but_failure_does_not(workers):
    current = [100.0]
    case = workers(select=one_episode, clock=lambda: current[0])
    job = release_and_start(case)
    current[0] = 131.0
    assert case.worker.healthy() is False
    job.finish("FAILED")
    wait_until(lambda: len(case.starts) >= 2)
    assert case.worker.healthy() is False
    assert case.worker.snapshot()["progress_fresh"] == {
        "FINANCIAL": False,
        "OUTCOME": True,
    }
    case.starts[1][0].finish("COMPLETED")
    wait_until(case.worker.healthy)


def test_repeated_outcome_failure_is_diagnostic_not_financial_health(workers):
    current = [100.0]

    def outcome_only(_case, kind, _after):
        return str(UUID(int=2)) if kind == "OUTCOME" else None

    case = workers(select=outcome_only, clock=lambda: current[0])
    job = release_and_start(case)
    assert case.starts[0][1][-2] == "OUTCOME"
    for index in range(3):
        current[0] += 31.0
        job.finish("FAILED")
        wait_until(lambda index=index: len(case.starts) >= index + 2)
        job = case.starts[index + 1][0]
        assert case.worker.healthy() is True
        assert case.worker.snapshot()["progress_fresh"] == {
            "FINANCIAL": True,
            "OUTCOME": False,
        }
        assert all(args[-2] == "OUTCOME" for _, args in case.starts)


def test_idle_queue_health_uses_local_contact_without_new_job(workers):
    current = [100.0]
    case = workers(clock=lambda: current[0])
    assert case.worker.ready()
    case.worker.release()
    wait_until(lambda: len(case.selections) >= 2)
    count = len(case.selections)
    current[0] = 131.0
    wait_until(lambda: len(case.selections) > count + 1)
    assert case.worker.healthy()
    assert not case.starts


@pytest.mark.parametrize(
    "key,value",
    [
        ("V2_CONTROLLER_ENABLE_ENTRIES", "true"),
        ("V2_CONTROLLER_ENABLE_ENTRIES", True),
        ("V2_ENVIRONMENT", "LIVE"),
        ("V2_SETTLEMENT_WORK_MS", "99"),
        ("V2_SETTLEMENT_WORK_MS", "300001"),
        ("V2_SETTLEMENT_STOP_MS", "9"),
        ("V2_SETTLEMENT_STOP_MS", "10001"),
        ("V2_SETTLEMENT_TERMINATE_MS", "9"),
        ("V2_SETTLEMENT_KILL_MS", "10001"),
        ("V2_SETTLEMENT_POLL_MS", "9"),
        ("V2_SETTLEMENT_POLL_MS", "251"),
        ("V2_SETTLEMENT_IDLE_MS", "10001"),
        ("V2_SETTLEMENT_READY_MS", "30001"),
        ("V2_SETTLEMENT_STOP_JOIN_MS", "1001"),
        ("V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS", "29"),
        ("V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS", "3601"),
        ("V2_SETTLEMENT_WORK_MS", 100),
        ("V2_SETTLEMENT_WORK_MS", "NaN"),
        ("V2_SETTLEMENT_WORK_MS", " 100"),
        ("V2_SETTLEMENT_WORK_MS", "١٠٠"),
    ],
)
def test_invalid_worker_configuration_rejected_before_bootstrap(workers, key, value):
    called = []
    with pytest.raises((TypeError, ValueError)):
        workers(
            values=config(**{key: value}),
            bootstrap=lambda case: called.append(True) or case.ports,
        )
    assert called == []
