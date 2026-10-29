from pathlib import Path
from uuid import uuid4

import pytest
from test_v2_credential_console import add
from test_v2_execution_routes import case, database, request, routes, setup

from services.v2_execution_controller import ExecutionController

__all__ = ["case", "database", "routes", "setup"]


class Worker:
    def __init__(self):
        self.running = True
        self.released = False
        self.ready_flag = True
        self.stop_ok = True

    def ready(self):
        return self.ready_flag

    def release(self):
        self.released = True

    def alive(self):
        return self.running

    def stop(self):
        if self.stop_ok:
            self.running = False
        return self.stop_ok


def test_start_failure_retries_bounded_manage_only(controller):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    now = [100.0]
    ctrl.monotonic = lambda: now[0]
    spawn = ctrl.spawn

    def failing(target):
        worker = spawn(target)
        worker.ready_flag = False
        return worker

    ctrl.spawn = failing
    ctrl.step()
    assert ctrl.actual()["phase"] == "BLOCKED"
    ctrl.step()
    assert len(state["workers"]) == 1
    now[0] += 30
    ctrl.step()
    assert len(state["workers"]) == 2
    now[0] += 30
    ctrl.step()
    assert len(state["workers"]) == 2
    ctrl.spawn = spawn
    now[0] += 30
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"
    assert ctrl.actual()["blockers"] == ["RECOVERING_SOURCE"]
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"


@pytest.mark.parametrize(
    "blocker", ["EXECUTION_GUARD_PERMISSION_DENIED", "SOURCE_PROCESS_NOT_STOPPED"]
)
def test_blocked_unsafe_start_is_not_retried(controller, blocker):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    ctrl.record(ctrl.desired(), "BLOCKED", blockers=["WORKER_START_FAILED", blocker])
    ctrl.step()
    assert not state["workers"]


@pytest.fixture
def controller(routes):
    store, console = routes
    with console.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260928_execution_controller.sql"
            ).read_text()
        )
    state = {"preflight": [], "source": [], "workers": []}

    def spawn(target):
        worker = Worker()
        state["workers"].append((target, worker))
        return worker

    ctrl = ExecutionController(
        console.connect,
        tenant_id=console.tenant,
        preflight=lambda _: state["preflight"],
        source_clear=lambda _: state["source"],
        spawn=spawn,
    )
    return ctrl, store, console, state


def test_real_state_transitions_require_ready_and_flat_source(controller):
    ctrl, store, console, state = controller
    a = add(console)["registry_id"]
    b = add(console)["registry_id"]
    request(store, console, a)
    ctrl.step()
    assert ctrl.actual()["phase"] == "ACTIVE" and state["workers"][0][1].released
    request(store, console, b, expected_epoch=1)
    state["source"] = ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING" and len(state["workers"]) == 1
    state["source"] = []
    ctrl.step()
    assert not state["workers"][0][1].running
    assert ctrl.actual()["phase"] == "ACTIVE" and ctrl.actual()["target_registry"] == b
    assert len(state["workers"]) == 2 and state["workers"][1][1].released


def test_preflight_failure_never_starts_worker(controller):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    state["preflight"] = ["TARGET_PREFLIGHT_FAILED"]
    ctrl.step()
    ctrl.step()
    assert ctrl.actual()["phase"] == "BLOCKED" and not state["workers"]


def test_target_failure_keeps_healthy_source_running(controller):
    ctrl, store, console, state = controller
    a = add(console)["registry_id"]
    b = add(console)["registry_id"]
    request(store, console, a)
    ctrl.step()
    original = ctrl.actual()
    state["preflight"] = ["NO_AVAILABLE_BALANCE"]
    request(store, console, b, expected_epoch=1)
    ctrl.step()
    ctrl.step()
    assert (
        ctrl.actual() == original
        and len(state["workers"]) == 1
        and state["workers"][0][1].running
    )
    pending = store.inspect(console.tenant, "SANDBOX")["pending_request"]
    assert pending["phase"] == "REJECTED" and pending["blockers"] == [
        "NO_AVAILABLE_BALANCE"
    ]


def test_restart_recovers_manage_only(controller):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    ctrl.step()
    token = ctrl.actual()["worker_token"]
    state["workers"][0][1].running = False
    ctrl.step()
    assert (
        ctrl.actual()["phase"] == "DRAINING" and ctrl.actual()["worker_token"] != token
    )
    assert state["workers"][1][1].released


def test_source_process_must_confirm_stop(controller):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    ctrl.step()
    request(store, console, add(console)["registry_id"], expected_epoch=1)
    state["workers"][0][1].stop_ok = False
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING" and len(state["workers"]) == 1
    assert ctrl.actual()["blockers"] == ["SOURCE_PROCESS_NOT_STOPPED"]


@pytest.mark.parametrize(
    "failure",
    [None, "EXECUTION_GUARD_PERMISSION_DENIED", "qa-private-sql-token"],
)
def test_failed_child_cannot_be_reported_active(controller, failure):
    ctrl, store, console, _state = controller
    request(store, console, add(console)["registry_id"])
    worker = Worker()
    worker.ready_flag = False
    worker.startup_failure = failure
    ctrl.spawn = lambda _: worker
    ctrl.step()
    assert (
        ctrl.actual()["phase"] == "BLOCKED"
        and not worker.released
        and not worker.running
    )
    expected = ["WORKER_START_FAILED"]
    if failure == "EXECUTION_GUARD_PERMISSION_DENIED":
        expected.append(failure)
    assert ctrl.actual()["blockers"] == expected
    with console.connect() as connection:
        assert connection.execute(
            "SELECT blockers FROM v2_execution_controller_events WHERE phase='BLOCKED' ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == (expected,)


def test_controller_never_consumes_live_request(controller):
    ctrl, store, console, state = controller
    live = add(console, environment="LIVE")["registry_id"]
    store.request(
        console.tenant,
        "LIVE",
        target_registry=live,
        binding_version=1,
        expected_epoch=0,
        request_id=str(uuid4()),
    )
    ctrl.step()
    assert ctrl.actual() is None and not state["workers"]


def test_stalled_worker_cannot_keep_opening(controller):
    ctrl, store, console, state = controller
    request(store, console, add(console)["registry_id"])
    ctrl.step()
    state["workers"][0][1].healthy = lambda: False
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"
    assert ctrl.actual()["blockers"] == ["WORKER_PROGRESS_STALE"]


def recovering(controller):
    ctrl, store, console, state = controller
    registry = add(console)["registry_id"]
    request(store, console, registry)
    ctrl.step()
    state["workers"][0][1].running = False
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"
    state["source"] = ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    state["preflight"] = ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    return registry


def test_same_binding_requires_explicit_request_and_two_coverage_checks(controller):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    previous = ctrl.actual()
    calls = []

    def check(target):
        calls.append(state["workers"][-1][1].running)
        assert target["target_registry"] == registry
        return []

    ctrl.resume_preflight = check
    ctrl.step()
    assert ctrl.actual() == previous and not calls
    request(store, console, registry, expected_epoch=1)
    ctrl.step()
    assert calls == [True, False]
    assert ctrl.actual()["phase"] == "ACTIVE" and ctrl.actual()["epoch"] == 2
    assert ctrl.actual()["worker_token"] != previous["worker_token"]
    assert len(state["workers"]) == 3 and state["workers"][-1][1].released
    ctrl.step()
    assert len(state["workers"]) == 3 and calls == [True, False]


@pytest.mark.parametrize(
    "failure", [["MISSING_CONFIRMED_STOP"], None, False, "error", "exception"]
)
def test_failed_resume_does_not_stop_current_manager(controller, failure):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    request(store, console, registry, expected_epoch=1)

    def check(_):
        if failure == "exception":
            raise RuntimeError("private-transport-details")
        return failure

    ctrl.resume_preflight = check
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"
    assert len(state["workers"]) == 2 and state["workers"][-1][1].running
    assert "private" not in str(ctrl.actual())


def test_resume_waits_for_old_process_exit(controller):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    request(store, console, registry, expected_epoch=1)
    ctrl.resume_preflight = lambda _: []
    state["workers"][-1][1].stop_ok = False
    ctrl.step()
    assert ctrl.actual()["blockers"] == ["SOURCE_PROCESS_NOT_STOPPED"]
    assert len(state["workers"]) == 2


def test_resume_coverage_change_restores_management_not_opening(controller):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    request(store, console, registry, expected_epoch=1)
    results = iter([[], ["VENUE_LEDGER_POSITION_MISMATCH"]])
    ctrl.resume_preflight = lambda _: next(results)
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING" and ctrl.actual()["epoch"] == 1
    assert state["workers"][-1][1].released


def test_failed_resume_start_falls_back_to_manage_only(controller):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    request(store, console, registry, expected_epoch=1)
    ctrl.resume_preflight = lambda _: []
    spawn = ctrl.spawn

    def failing_once(target):
        worker = spawn(target)
        worker.ready_flag = len(state["workers"]) != 3
        return worker

    ctrl.spawn = failing_once
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING" and ctrl.actual()["epoch"] == 2
    assert state["workers"][-1][1].running and state["workers"][-1][1].released
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"  # another explicit request required


def test_failed_start_without_confirmed_stop_never_spawns_fallback(controller):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    request(store, console, registry, expected_epoch=1)
    ctrl.resume_preflight = lambda _: []
    spawn = ctrl.spawn

    def stuck(target):
        worker = spawn(target)
        worker.ready_flag = worker.stop_ok = False
        return worker

    ctrl.spawn = stuck
    ctrl.step()
    assert ctrl.actual()["phase"] == "BLOCKED"
    assert "SOURCE_PROCESS_NOT_STOPPED" in ctrl.actual()["blockers"]
    assert len(state["workers"]) == 3 and ctrl.worker is state["workers"][-1][1]


@pytest.mark.parametrize("changed", ["account", "binding"])
def test_resume_cannot_bypass_different_account_or_credential_handover(
    controller, changed
):
    registry = recovering(controller)
    ctrl, store, console, state = controller
    if changed == "account":
        registry = add(console)["registry_id"]
        request(store, console, registry, expected_epoch=1)
    else:
        actual = ctrl.actual()
        # A changed desired binding cannot use the same-binding fast path.
        ctrl.desired = lambda: {**actual, "epoch": 2, "binding_version": 2}
    ctrl.resume_preflight = lambda _: pytest.fail("cross-binding resume forbidden")
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING" and len(state["workers"]) == 2
