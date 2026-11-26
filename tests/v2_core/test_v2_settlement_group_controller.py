"""Controller PG state transitions with real group and settlement health logic.

Settlement lifecycle is passive here: no owner thread, process, job, credentials
or exchange client is started. This isolates the health-to-handover contract;
it is not an end-to-end financial settlement test.
"""

import threading
from types import SimpleNamespace

from test_v2_credential_console import add
from test_v2_execution_controller import (
    Worker,
    case,
    controller,
    database,
    routes,
    setup,
)
from test_v2_execution_routes import request

from services.v2_settlement_worker import SettlementWorker
from services.v2_worker_group import WorkerGroup

__all__ = ["case", "controller", "database", "routes", "setup"]


class PassiveSettlement(SettlementWorker):
    """Retain production healthy/snapshot while faking only lifecycle ports."""

    def __init__(self):
        self.running = True
        self.released = False
        self.stop_ok = True
        self.stop_calls = 0
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread = SimpleNamespace(is_alive=lambda: self.running)
        self.clock = lambda: 100.0
        self.progress_timeout = 30
        self._last_success = {"FINANCIAL": 100.0, "OUTCOME": 100.0}
        self._status, self._kind, self._job_id = "IDLE", "FINANCIAL", None
        self._job = None

    def ready(self):
        return self.running

    def release(self):
        self.released = True

    def request_stop(self):
        self.stop_requested = True

    def stop(self):
        self.stop_calls += 1
        self.request_stop()
        if self.stop_ok:
            self.running = False
        return self.stop_ok


def install_composite(ctrl, state):
    groups = []

    def spawn(target):
        if groups:
            assert groups[-1].stop_confirmations == (True, True)
        management, settlement = Worker(), PassiveSettlement()
        group = WorkerGroup([management, settlement], entries_enabled=False)
        groups.append(group)
        state["workers"].append((target, group))
        return group

    ctrl.spawn = spawn
    return groups


def test_stale_outcome_allows_handover_only_after_source_clear_and_confirmed_stop(
    controller,
):
    ctrl, store, console, state = controller
    groups = install_composite(ctrl, state)
    source = add(console)["registry_id"]
    target = add(console)["registry_id"]
    request(store, console, source)
    ctrl.step()
    old = ctrl.actual()
    assert old["phase"] == "ACTIVE"
    group = groups[0]
    management, settlement = group.members
    settlement._last_success["OUTCOME"] = 0.0
    assert settlement.snapshot()["progress_fresh"] == {
        "FINANCIAL": True,
        "OUTCOME": False,
    }
    assert group.healthy() is True
    assert group.entries_enabled is False
    request(store, console, target, expected_epoch=1)
    state["source"] = ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    ctrl.step()
    assert ctrl.actual()["phase"] == "DRAINING"
    assert ctrl.actual()["blockers"] == state["source"]
    assert len(groups) == 1 and settlement.stop_calls == 0

    state["source"] = []
    ctrl.step()
    actual = ctrl.actual()
    assert actual["phase"] == "ACTIVE"
    assert actual["target_registry"] == target and actual["epoch"] == 2
    assert actual["worker_token"] != old["worker_token"]
    assert group.stop_confirmations == (True, True)
    assert settlement.stop_calls == 1 and not management.running
    assert settlement.snapshot()["stop_confirmed"] is True
    assert len(groups) == 2
    assert all(member.released for member in groups[1].members)


def test_stale_financial_cannot_be_hidden_by_fresh_outcome_or_management(controller):
    ctrl, store, console, state = controller
    groups = install_composite(ctrl, state)
    source = add(console)["registry_id"]
    target = add(console)["registry_id"]
    request(store, console, source)
    ctrl.step()
    original = ctrl.actual()
    group = groups[0]
    management, settlement = group.members
    settlement._last_success["FINANCIAL"] = 0.0
    assert settlement.snapshot()["progress_fresh"] == {
        "FINANCIAL": False,
        "OUTCOME": True,
    }
    assert management.alive() is True and group.healthy() is False
    source_checks = []
    ctrl.source_clear = lambda actual: source_checks.append(actual) or []
    request(store, console, target, expected_epoch=1)
    ctrl.step()
    actual = ctrl.actual()
    assert actual["phase"] == "DRAINING"
    assert actual["blockers"] == ["WORKER_PROGRESS_STALE"]
    assert actual["target_registry"] == source and actual["epoch"] == 1
    assert actual["worker_token"] == original["worker_token"]
    assert len(groups) == 1 and settlement.stop_calls == 0
    assert not source_checks
