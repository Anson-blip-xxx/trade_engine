"""Local shutdown and lease-retention ordering; no PostgreSQL or services."""

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from services import v2_execution_supervisor as supervisor
from services.v2_execution_supervisor import shutdown_controller, shutdown_worker


def case(
    *, stopped=True, phase="ACTIVE", request_error=None, db_error=None, write_error=None
):
    events = []
    state = SimpleNamespace(stopped=stopped, stop_confirmed=False)
    actual = {"phase": phase, "worker_token": "fake-frozen-token", "epoch": 7}

    def request_stop():
        events.append("request-stop")
        if request_error:
            raise request_error

    def stop():
        events.append("stop")
        if isinstance(state.stopped, Exception):
            raise state.stopped
        state.stop_confirmed = state.stopped is True
        return state.stopped

    def read():
        assert state.stop_confirmed, "DB read preceded local stop confirmation"
        events.append("actual")
        if db_error:
            raise db_error
        return actual

    def record(target, phase, **kwargs):
        assert state.stop_confirmed
        events.append("record")
        assert target is actual and phase == "DRAINING"
        assert kwargs == {
            "token": "fake-frozen-token",
            "blockers": ["CONTROLLER_RESTART_RECOVERY"],
        }
        if write_error:
            raise write_error

    def forbidden(*_args, **_kwargs):
        pytest.fail("shutdown must not authorize, switch or restart a worker")

    worker = SimpleNamespace(request_stop=request_stop, stop=stop)
    controller = SimpleNamespace(
        worker=worker,
        actual=read,
        record=record,
        spawn=forbidden,
        start=forbidden,
        preflight=forbidden,
        desired=forbidden,
        step=forbidden,
    )
    return SimpleNamespace(
        events=events, state=state, worker=worker, controller=controller
    )


@pytest.mark.parametrize("phase", ["ACTIVE", "STARTING", "DRAINING"])
def test_local_stop_precedes_all_db_work_and_preserves_recovery_marker(phase):
    item = case(phase=phase)
    result = shutdown_worker(item.controller)
    assert item.events == ["request-stop", "stop", "actual", "record"]
    assert result == {
        "status": "STOPPED",
        "stop_confirmed": True,
        "route_recorded": True,
        "request_stop_failed": False,
        "error_code": None,
        "execution_authorized": False,
    }
    assert item.controller.worker is item.worker


@pytest.mark.parametrize("phase", ["STOPPED", "BLOCKED"])
def test_confirmed_stop_does_not_change_other_route_phases(phase):
    item = case(phase=phase)
    result = shutdown_worker(item.controller)
    assert item.events == ["request-stop", "stop", "actual"]
    assert result["stop_confirmed"] and not result["route_recorded"]


@pytest.mark.parametrize("error_port", ["db_error", "write_error"])
def test_db_failure_cannot_skip_local_stop_or_expose_payload(error_port):
    item = case(**{error_port: RuntimeError("PRIVATE_DATABASE_DSN")})
    result = shutdown_worker(item.controller)
    assert item.events[:2] == ["request-stop", "stop"]
    assert result["stop_confirmed"] is True and result["status"] == "STOPPED"
    assert result["error_code"] == "CONTROLLER_STOP_ROUTE_UNRECORDED"
    assert "PRIVATE" not in str(result)


def test_hanging_route_read_occurs_only_after_worker_has_confirmed_stop():
    item = case()
    entered, release = threading.Event(), threading.Event()
    results = []

    def blocked_read():
        assert item.state.stop_confirmed
        entered.set()
        assert release.wait(3)

    item.controller.actual = blocked_read
    thread = threading.Thread(
        target=lambda: results.append(shutdown_worker(item.controller))
    )
    thread.start()
    try:
        assert entered.wait(2)
        assert item.events == ["request-stop", "stop"]
        assert item.state.stop_confirmed and results == []
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results[0]["stop_confirmed"]


@pytest.mark.parametrize(
    "stopped", [False, None, 1, "yes", RuntimeError("PRIVATE_STOP_EXCEPTION")]
)
def test_unconfirmed_stop_retains_handle_and_never_calls_database(stopped):
    item = case(stopped=stopped)
    result = shutdown_worker(item.controller)
    assert item.events == ["request-stop", "stop"]
    assert item.controller.worker is item.worker
    assert result["status"] == "STOP_UNCONFIRMED" and not result["stop_confirmed"]
    assert not result["route_recorded"] and not result["execution_authorized"]
    expected = (
        "WORKER_STOP_FAILED"
        if isinstance(stopped, Exception)
        else "SOURCE_PROCESS_NOT_STOPPED"
    )
    assert result["error_code"] == expected and "PRIVATE" not in str(result)


def test_request_hint_failure_still_calls_real_stop():
    item = case(request_error=RuntimeError("PRIVATE_HINT_FAILURE"))
    result = shutdown_worker(item.controller)
    assert item.events == ["request-stop", "stop", "actual", "record"]
    assert result["request_stop_failed"] is True and result["stop_confirmed"] is True


def test_legacy_worker_without_optional_request_stop_still_stops_first():
    item = case()
    del item.worker.request_stop
    result = shutdown_worker(item.controller)
    assert item.events == ["stop", "actual", "record"]
    assert result["stop_confirmed"]


def test_no_local_worker_never_needs_database():
    item = case()
    item.controller.worker = None
    result = shutdown_worker(item.controller)
    assert result["status"] == "IDLE" and result["stop_confirmed"]
    assert item.events == []


def test_unconfirmed_retries_remain_inside_lease_without_db_or_busy_loop(capsys):
    item = case(stopped=False)
    lease = SimpleNamespace(held=False)
    waits = []

    @contextmanager
    def held_lease():
        lease.held = True
        try:
            yield
        finally:
            assert item.state.stop_confirmed
            lease.held = False

    def wait(seconds):
        assert lease.held and item.controller.worker is item.worker
        assert not any(event in {"actual", "record"} for event in item.events)
        waits.append(seconds)
        if len(waits) == 3:
            item.state.stopped = True

    with held_lease():
        result = shutdown_controller(item.controller, wait=wait)
    assert result["status"] == "STOPPED" and not lease.held
    assert waits == [0.2, 0.2, 0.2]
    assert item.events[-2:] == ["actual", "record"]
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [message["status"] for message in messages] == [
        "STOP_UNCONFIRMED",
        "STOPPED",
    ]


def test_stop_exception_retries_locally_without_raw_error(capsys):
    item = case(stopped=RuntimeError("PRIVATE_RETRY_FAILURE"))

    def wait(seconds):
        assert seconds == 0.2 and item.events == ["request-stop", "stop"]
        assert item.controller.worker is item.worker
        item.state.stopped = True

    assert shutdown_controller(item.controller, wait=wait)["stop_confirmed"]
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    assert "WORKER_STOP_FAILED" in output


def test_logging_failure_does_not_release_unknown_handle_or_skip_retry(monkeypatch):
    item = case(stopped=False)

    def broken_print(*_args, **_kwargs):
        raise OSError("PRIVATE_LOG_SINK_FAILURE")

    monkeypatch.setattr(supervisor, "print", broken_print, raising=False)

    def wait(_seconds):
        assert item.controller.worker is item.worker
        item.state.stopped = True

    assert shutdown_controller(item.controller, wait=wait)["stop_confirmed"]


def test_main_finally_invokes_shutdown_before_lease_context_exits(monkeypatch):
    events = []

    class Lease:
        def execute(self, query, _params=None):
            if "pg_try_advisory_lock" in query:
                return SimpleNamespace(fetchone=lambda: (True,))
            events.append("lease-ping")
            return self

        def commit(self):
            pass

    @contextmanager
    def connect():
        events.append("lease-enter")
        try:
            yield Lease()
        finally:
            events.append("lease-exit")

    def failed_step():
        events.append("controller-step")
        raise RuntimeError("fake controller loop failure")

    controller = SimpleNamespace(step=failed_step)
    monkeypatch.setenv("V2_CONTROLLER_TENANT", "fake-tenant")
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive"),
    )
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_: None)
    monkeypatch.setattr(supervisor, "database", lambda: connect)
    monkeypatch.setattr(
        supervisor, "ExecutionController", lambda *_args, **_kwargs: controller
    )
    monkeypatch.setattr(
        supervisor,
        "TestnetPorts",
        lambda *_: SimpleNamespace(
            preflight=None, source_clear=None, spawn=None, resume_preflight=None
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "build_managed_health",
        lambda *_: SimpleNamespace(
            step=lambda _: pytest.fail("health must not run after failed step")
        ),
    )

    def shutdown(actual_controller):
        assert actual_controller is controller
        assert events[-1] == "controller-step" and "lease-exit" not in events
        events.append("local-shutdown")

    monkeypatch.setattr(supervisor, "shutdown_controller", shutdown)
    with pytest.raises(RuntimeError, match="fake controller loop failure"):
        supervisor.main()
    assert events[-2:] == ["local-shutdown", "lease-exit"]
