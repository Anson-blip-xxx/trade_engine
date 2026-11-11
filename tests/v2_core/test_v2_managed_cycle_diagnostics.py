"""Managed-cycle failure visibility without credentials, network or live state."""

import builtins
import json
from collections import deque
from types import SimpleNamespace

import pytest
from test_v2_managed_guard_wiring import child_case

from services import v2_managed_testnet as managed
from services.v2_execution_controller import ExecutionController

__all__ = ["child_case"]


def _messages(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_exception_arguments_repr_and_external_paths_are_never_formatted(capsys):
    class UnprintableFailure(RuntimeError):
        def __str__(self):
            raise AssertionError("exception text must not be inspected")

        def __repr__(self):
            raise AssertionError("exception representation must not be inspected")

    try:
        raise UnprintableFailure("QA-SECRET api_key=hidden /private/credential")
    except UnprintableFailure as exc:
        managed._CycleDiagnostics(clock=lambda: 1).failed(exc)
    rows = _messages(capsys)
    assert rows == [
        {
            "component": "execution-worker-cycle",
            "status": "DEGRADED",
            "error_code": "EXECUTION_CYCLE_FAILED",
            "error_type": "UnprintableFailure",
            "frames": [],  # Tests and third-party paths are not application locations.
            "consecutive_failures": 1,
            "suppressed_since_last": 0,
        }
    ]
    assert "QA-SECRET" not in json.dumps(rows)


@pytest.mark.parametrize("name", ["private/key=value", "x" * 81, "错误", "1Error"])
def test_uncontrolled_exception_type_names_are_redacted(name):
    error = type(name, (Exception,), {})("never output")
    assert managed._safe_cycle_failure(error) == {
        "error_type": "Exception",
        "frames": [],
    }


def test_repeated_errors_are_bounded_even_when_types_alternate(capsys):
    now = [0]
    reporter = managed._CycleDiagnostics(clock=lambda: now[0])
    reporter.failed(ValueError("secret 1"))
    for i in range(1, 60):
        now[0] = i
        reporter.failed(ConnectionError("secret 2"))
    now[0] = 60
    reporter.failed(TimeoutError("secret 3"))
    rows = _messages(capsys)
    assert len(rows) == 2
    assert [row["consecutive_failures"] for row in rows] == [1, 61]
    assert [row["suppressed_since_last"] for row in rows] == [0, 59]
    assert rows[-1]["error_type"] == "TimeoutError"


def test_recovery_is_logged_once_and_resets_a_new_failure_episode(capsys):
    reporter = managed._CycleDiagnostics(clock=lambda: 10)
    reporter.succeeded()
    assert _messages(capsys) == []
    reporter.failed(ValueError("secret"))
    reporter.failed(ValueError("secret"))
    reporter.succeeded()
    reporter.succeeded()
    reporter.failed(ValueError("secret"))
    rows = _messages(capsys)
    assert [row["status"] for row in rows] == ["DEGRADED", "RECOVERED", "DEGRADED"]
    assert rows[1]["error_code"] == "EXECUTION_CYCLE_RECOVERED"
    assert rows[1]["consecutive_failures"] == 2
    assert rows[1]["suppressed_since_last"] == 1
    assert rows[-1]["consecutive_failures"] == 1


@pytest.mark.parametrize("interval", [True, False, 0, 4, 3601, 60.0, "60"])
def test_diagnostic_rate_limit_is_bounded(interval):
    with pytest.raises(ValueError, match="BOUNDED_CYCLE_DIAGNOSTIC_INTERVAL_REQUIRED"):
        managed._CycleDiagnostics(interval)


def test_broken_journal_output_does_not_fail_the_cycle(monkeypatch):
    def broken(*args, **kwargs):
        raise BrokenPipeError("hidden")

    reporter = managed._CycleDiagnostics(clock=lambda: 1)
    monkeypatch.setattr(builtins, "print", broken)
    reporter.failed(RuntimeError("hidden"))
    reporter.succeeded()
    assert reporter.failures == reporter.suppressed == 0


def test_real_child_reports_failure_and_recovery_without_changing_retry_or_fencing(
    child_case, monkeypatch, capsys
):
    original_create = managed.create_testnet_process
    cycle_count = [0]

    def create(*args, **kwargs):
        process = original_create(*args, **kwargs)
        original_run = process.daemon.run_once

        def run():
            original_run()
            cycle_count[0] += 1
            if cycle_count[0] < 3:
                kwargs["stop"].clear()
                raise RuntimeError("QA-SECRET api_key=never-log-me")

        process.daemon.run_once = run
        return process

    monkeypatch.setattr(managed, "create_testnet_process", create)
    child_case.run()
    assert child_case.sent == ["READY", "DEGRADED", "DEGRADED", "CYCLE"]
    assert child_case.events.count("guard-connect") == 3
    assert child_case.events.count("pipeline-cycle") == 3
    assert child_case.pipeline.enable_entries is True
    rows = _messages(capsys)
    assert [row["status"] for row in rows] == ["DEGRADED", "RECOVERED"]
    assert rows[0]["error_type"] == "RuntimeError"
    assert len(rows[0]["frames"]) == 1
    assert rows[0]["frames"][0]["module"] == "services.v2_managed_testnet"
    assert rows[0]["frames"][0]["function"] == "child_main"
    assert type(rows[0]["frames"][0]["line"]) is int
    assert "QA-SECRET" not in json.dumps(rows)
    assert rows[1]["consecutive_failures"] == 2


def _worker(monkeypatch):
    now, messages = [100], deque()
    monkeypatch.setattr(managed.time, "monotonic", lambda: now[0])
    worker = managed.ChildWorker.__new__(managed.ChildWorker)
    worker.last_progress = worker.last_contact = 100
    worker.progress_timeout = 300
    worker.channel = SimpleNamespace(poll=lambda: bool(messages), recv=messages.popleft)
    worker.process = SimpleNamespace(is_alive=lambda: True)
    return worker, now, messages


def test_degraded_ipc_proves_contact_but_never_success_progress(monkeypatch):
    worker, now, messages = _worker(monkeypatch)
    now[0] = 110
    messages.append("DEGRADED")
    assert worker.alive() and worker.healthy()
    assert (worker.last_progress, worker.last_contact) == (100, 110)
    now[0] = 401
    messages.append("DEGRADED")
    assert worker.alive() and not worker.healthy()
    assert (worker.last_progress, worker.last_contact) == (100, 401)
    now[0] = 402
    messages.append("CYCLE")
    assert worker.alive() and worker.healthy()
    assert (worker.last_progress, worker.last_contact) == (402, 402)


@pytest.mark.parametrize("message", ["READY", "secret", "FAILED:unexpected"])
def test_unrecognized_ipc_never_extends_progress(monkeypatch, message):
    worker, now, messages = _worker(monkeypatch)
    now[0] = 200
    messages.append(message)
    assert worker.alive()
    assert (worker.last_progress, worker.last_contact) == (100, 100)


def test_single_failure_does_not_drain_but_persistent_failure_only_drains_entries(
    monkeypatch,
):
    worker, now, messages = _worker(monkeypatch)
    route = {"phase": "ACTIVE", "epoch": 1, "worker_token": "qa-token"}
    records = []
    controller = ExecutionController.__new__(ExecutionController)
    controller.actual = lambda: route
    controller.desired = lambda: None
    controller.worker = worker
    controller.record = lambda *args, **kwargs: records.append((args, kwargs))

    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics must not restart or stop the management worker")

    controller.start = worker.stop = forbidden
    now[0] = 110
    messages.append("DEGRADED")
    controller.step()
    assert records == []
    now[0] = 401
    messages.append("DEGRADED")
    controller.step()
    assert records == [
        (
            (route, "DRAINING"),
            {"token": "qa-token", "blockers": ["WORKER_PROGRESS_STALE"]},
        )
    ]
    assert controller.worker is worker
