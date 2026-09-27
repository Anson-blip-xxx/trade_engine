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


def test_failed_child_cannot_be_reported_active(controller):
    ctrl, store, console, _state = controller
    request(store, console, add(console)["registry_id"])
    worker = Worker()
    worker.ready_flag = False
    ctrl.spawn = lambda _: worker
    ctrl.step()
    assert (
        ctrl.actual()["phase"] == "BLOCKED"
        and not worker.released
        and not worker.running
    )


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
