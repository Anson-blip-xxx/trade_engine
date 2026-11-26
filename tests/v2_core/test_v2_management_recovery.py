"""Offline timeout recovery: real controller and child lifecycle, fake persistence.

No PostgreSQL connection, subprocess, exchange client, or service is started.
The in-memory route records expose the authority present at every stop, spawn,
and release boundary; database fencing itself is covered by controller PG tests.
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_testnet as managed
from services.v2_execution_controller import ExecutionController


class RecoveryCase:
    def __init__(self, monkeypatch, *, phase="ACTIVE", stop_mode="graceful"):
        self.now = 100.0
        self.events, self.spawned = [], []
        self.route = {
            "epoch": 7,
            "target_registry": str(uuid4()),
            "binding_version": 3,
            "phase": phase,
            "worker_token": str(uuid4()),
            "blockers": [],
        }
        self.original = deepcopy(self.route)
        self.desired = deepcopy(self.route)
        self.spawn_ready = True
        self.spawn_stop_mode = "graceful"
        self.fail_record = None
        monkeypatch.setattr(managed.time, "monotonic", lambda: self.now)
        self.controller = ExecutionController(
            lambda: pytest.fail("offline lifecycle test attempted database I/O"),
            tenant_id=str(uuid4()),
            preflight=lambda target: self.check("preflight", target),
            source_clear=lambda target: self.check("source-clear", target),
            spawn=self.spawn,
            monotonic=lambda: self.now,
        )
        self.controller.actual = lambda: deepcopy(self.route)
        self.controller.desired = lambda: deepcopy(self.desired)
        self.controller.record = self.record
        self.old = self.worker("old", stop_mode=stop_mode)
        self.old.last_contact = self.old.last_progress = 0.0
        self.controller.worker = self.old

    def check(self, name, target):
        self.events.append((name, target["target_registry"]))
        return []

    def record(self, target, phase, *, token=None, blockers=()):
        if phase == self.fail_record:
            raise ConnectionError("fake persistence unavailable")
        self.route = {
            **target,
            "phase": phase,
            "worker_token": token,
            "blockers": list(blockers),
        }
        self.events.append(("record", phase, token))

    def observe(self, action, name):
        self.events.append(
            (action, name, self.route["phase"], self.route["worker_token"])
        )

    def worker(self, name, *, stop_mode, ready=True):
        pending = ["READY"] if name != "old" and ready else []
        process = SimpleNamespace(running=True, stopping=False, stop_mode=stop_mode)
        process.is_alive = lambda: process.running

        def request_stop():
            process.stopping = True
            self.observe("stop", name)

        def join(_timeout):
            if process.stopping and process.stop_mode == "graceful":
                process.running = False

        def terminate():
            self.observe("terminate", name)
            if process.stop_mode == "terminate":
                process.running = False

        def kill():
            self.observe("kill", name)
            if process.stop_mode == "kill":
                process.running = False

        process.join, process.terminate, process.kill = join, terminate, kill
        worker = managed.ChildWorker.__new__(managed.ChildWorker)
        worker.mode = "MANAGEMENT_ONLY"
        worker.entries_enabled = False
        worker.last_contact = worker.last_progress = self.now
        worker.progress_timeout, worker.stop_grace = 30, 1
        worker.process = process
        worker.channel = SimpleNamespace(
            poll=lambda _timeout=0: bool(pending), recv=lambda: pending.pop(0)
        )
        worker.stop_event = SimpleNamespace(set=request_stop)
        worker.release_event = SimpleNamespace(
            set=lambda: self.observe("release", name)
        )
        worker.pending = pending
        return worker

    def spawn(self, target):
        self.observe("spawn", "replacement")
        worker = self.worker(
            "replacement",
            stop_mode=self.spawn_stop_mode,
            ready=self.spawn_ready,
        )
        self.spawned.append((deepcopy(target), worker))
        return worker


@pytest.mark.parametrize("phase", ["ACTIVE", "DRAINING", "STARTING"])
@pytest.mark.parametrize("stop_mode", ["graceful", "terminate", "kill"])
def test_timeout_revokes_before_stop_then_replaces_only_in_draining(
    monkeypatch, phase, stop_mode
):
    case = RecoveryCase(monkeypatch, phase=phase, stop_mode=stop_mode)
    assert case.old.process.is_alive() and not case.old.alive()
    case.controller.step()

    token = case.route["worker_token"]
    assert token and token != case.original["worker_token"]
    assert case.route["phase"] == "DRAINING"
    assert case.route["blockers"] == ["RECOVERING_SOURCE"]
    assert not case.old.process.is_alive()
    assert len(case.spawned) == 1
    target, replacement = case.spawned[0]
    for key in ("epoch", "target_registry", "binding_version"):
        assert target[key] == case.original[key]
    assert target["worker_token"] == token
    assert case.controller.worker is replacement
    assert case.events[:2] == [
        ("record", "STOPPED", None),
        ("stop", "old", "STOPPED", None),
    ]
    spawn_at = case.events.index(("spawn", "replacement", "STARTING", token))
    assert case.events[spawn_at - 1] == ("record", "STARTING", token)
    assert case.events[spawn_at + 1 :] == [
        ("record", "DRAINING", token),
        ("release", "replacement", "DRAINING", token),
    ]
    assert not any(event[0] in {"preflight", "source-clear"} for event in case.events)
    case.controller.step()
    assert len(case.spawned) == 1


def test_failed_revocation_never_stops_or_replaces_owned_worker(monkeypatch):
    case = RecoveryCase(monkeypatch)
    case.fail_record = "STOPPED"
    with pytest.raises(ConnectionError):
        case.controller.step()
    assert case.route == case.original
    assert case.old.process.is_alive()
    assert case.controller.worker is case.old
    assert case.events == [] and case.spawned == []


@pytest.mark.parametrize("recovering", [False, True])
def test_direct_start_cannot_discard_a_retained_worker_handle(monkeypatch, recovering):
    case = RecoveryCase(monkeypatch)
    next_target = {
        **case.desired,
        "epoch": case.original["epoch"] + 1,
        "target_registry": str(uuid4()),
    }
    assert case.controller.start(next_target, recovering=recovering) is False
    assert case.controller.worker is case.old and case.old.process.is_alive()
    assert case.route == case.original
    assert case.events == [] and case.spawned == []


@pytest.mark.parametrize("newer_request", [False, True])
def test_failed_stop_retains_revoked_worker_and_never_starts_another(
    monkeypatch, newer_request
):
    case = RecoveryCase(monkeypatch, stop_mode="never")
    case.controller.step()
    assert case.route["phase"] == "BLOCKED"
    assert case.route["worker_token"] is None
    assert case.route["blockers"] == ["SOURCE_PROCESS_NOT_STOPPED"]
    assert case.controller.worker is case.old and case.old.process.is_alive()
    if newer_request:
        case.desired = {
            **case.desired,
            "epoch": case.original["epoch"] + 1,
            "target_registry": str(uuid4()),
        }
    case.controller.step()
    assert case.spawned == []
    assert case.controller.worker is case.old and case.old.process.is_alive()
    assert case.route["worker_token"] is None
    assert not any(event[0] == "preflight" for event in case.events)


def test_eventually_confirmed_stop_recovers_original_source_before_new_request(
    monkeypatch,
):
    case = RecoveryCase(monkeypatch, stop_mode="never")
    case.controller.step()
    case.desired = {
        **case.desired,
        "epoch": case.original["epoch"] + 1,
        "target_registry": str(uuid4()),
    }
    case.old.process.stop_mode = "graceful"
    case.controller.step()
    assert not case.old.process.is_alive()
    assert len(case.spawned) == 1
    target, replacement = case.spawned[0]
    assert target["target_registry"] == case.original["target_registry"]
    assert target["epoch"] == case.original["epoch"]
    assert case.route["phase"] == "DRAINING"
    assert case.route["worker_token"] not in {None, case.original["worker_token"]}
    assert case.controller.worker is replacement
    assert not any(event[0] == "preflight" for event in case.events)


def test_responsive_degraded_worker_keeps_owned_management_without_replacement(
    monkeypatch,
):
    case = RecoveryCase(monkeypatch)
    case.old.pending.append("DEGRADED")
    case.controller.step()
    assert case.route["phase"] == "DRAINING"
    assert case.route["worker_token"] == case.original["worker_token"]
    assert case.route["blockers"] == ["WORKER_PROGRESS_STALE"]
    assert case.old.process.is_alive()
    assert case.controller.worker is case.old and case.spawned == []
    assert case.events == [("record", "DRAINING", case.original["worker_token"])]


def test_failed_replacement_readiness_is_fenced_before_stop_and_not_released(
    monkeypatch,
):
    case = RecoveryCase(monkeypatch)
    case.spawn_ready = False
    case.controller.step()
    assert len(case.spawned) == 1
    target, replacement = case.spawned[0]
    assert target["worker_token"] != case.original["worker_token"]
    assert not case.old.process.is_alive() and not replacement.process.is_alive()
    assert case.route["phase"] == "BLOCKED"
    assert case.route["worker_token"] is None
    assert case.controller.worker is None
    assert case.events[-2:] == [
        ("record", "BLOCKED", None),
        ("stop", "replacement", "BLOCKED", None),
    ]
    assert not any(event[0] == "release" for event in case.events)
