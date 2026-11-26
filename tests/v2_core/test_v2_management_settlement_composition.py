"""Opt-in worker composition and spawn ownership through pure fake OS ports.

No PostgreSQL, credentials, real subprocess, service or venue is accessed. The
real TestnetPorts/ChildWorker/WorkerGroup composition remains under test.
"""

import builtins
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_testnet as managed
from services import v2_settlement_worker as settlement
from services.v2_worker_group import WorkerGroup

MODE = "MANAGEMENT_WITH_SETTLEMENT"


def settings(**changes):
    return {
        "V2_CONTROLLER_WORKER_MODE": MODE,
        "V2_CONTROLLER_ENABLE_ENTRIES": "false",
        "V2_CONTROLLER_STOP_GRACE_SECONDS": "1",
        **changes,
    }


def target():
    return {
        "target_registry": str(uuid4()),
        "binding_version": 2,
        "epoch": 7,
        "worker_token": str(uuid4()),
    }


class Member:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.ready_result = self.alive_result = self.stop_result = True
        self.stop_requested = False
        self.release_error = None
        self.startup_failure = None

    def ready(self):
        self.events.append((self.name, "ready"))
        return self.ready_result

    def release(self):
        self.events.append((self.name, "release"))
        if self.release_error is not None:
            raise self.release_error

    def alive(self):
        return self.alive_result

    def healthy(self):
        return True

    def request_stop(self):
        self.events.append((self.name, "request_stop"))
        self.stop_requested = True

    def stop(self):
        self.events.append((self.name, "stop"))
        return self.stop_result


@pytest.fixture
def factories(monkeypatch):
    case = SimpleNamespace(events=[], child_calls=[], settlement_calls=[])
    case.management = Member("management", case.events)
    case.settlement = Member("settlement", case.events)
    case.child_error = case.settlement_error = None

    def child(tenant, selected, values):
        case.child_calls.append((tenant, deepcopy(selected), deepcopy(values)))
        if case.child_error is not None:
            raise case.child_error
        return case.management

    def other(tenant, selected, values):
        case.settlement_calls.append((tenant, deepcopy(selected), deepcopy(values)))
        if case.settlement_error is not None:
            raise case.settlement_error
        return case.settlement

    monkeypatch.setattr(managed, "ChildWorker", child)
    monkeypatch.setattr(settlement, "SettlementWorker", other)
    return case


@pytest.mark.parametrize("mode", [None, "LEGACY", "MANAGEMENT_ONLY"])
def test_existing_default_and_single_worker_modes_do_not_construct_settlement(
    factories, mode
):
    values = {"V2_CONTROLLER_ENABLE_ENTRIES": "false"}
    if mode is not None:
        values["V2_CONTROLLER_WORKER_MODE"] = mode
    tenant, selected = str(uuid4()), target()
    port = managed.TestnetPorts(tenant, values)
    assert port.spawn(selected) is factories.management
    assert factories.child_calls == [(tenant, selected, values)]
    assert factories.settlement_calls == []
    assert factories.events == []


def test_legacy_existing_entry_setting_is_not_changed_by_opt_in_feature(factories):
    values = {"V2_CONTROLLER_ENABLE_ENTRIES": "true"}
    tenant, selected = str(uuid4()), target()
    result = managed.TestnetPorts(tenant, values).spawn(selected)
    assert result is factories.management
    assert factories.child_calls[0][2] == values
    assert factories.settlement_calls == []


def test_composite_mode_constructs_direct_siblings_with_same_binding_and_no_entries(
    factories,
):
    tenant, selected, values = str(uuid4()), target(), settings()
    before = deepcopy((selected, values))
    port = managed.TestnetPorts(tenant, values)
    result = port.spawn(selected)
    assert type(result) is WorkerGroup
    assert result.members == (factories.management, factories.settlement)
    assert result.entries_enabled is False
    assert factories.child_calls == [
        (tenant, selected, {**values, "V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY"})
    ]
    assert factories.settlement_calls == [(tenant, selected, values)]
    assert (selected, values) == before
    assert port.values == values
    assert factories.events == [], "construction must not imply ready or release"


def test_composite_ready_precedes_both_releases_and_release_is_idempotent(factories):
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    with pytest.raises(RuntimeError, match="WORKER_GROUP_NOT_READY"):
        group.release()
    assert group.ready() is True
    group.release()
    group.release()
    assert factories.events == [
        ("management", "ready"),
        ("settlement", "ready"),
        ("management", "release"),
        ("settlement", "release"),
    ]


@pytest.mark.parametrize("member", ["management", "settlement"])
def test_readiness_failure_retains_both_composed_workers_and_broadcasts_stop(
    factories, member
):
    getattr(factories, member).ready_result = False
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    assert group.ready() is False
    assert group.members == (factories.management, factories.settlement)
    assert factories.events[-2:] == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
    ]
    assert not any(action == "release" for _, action in factories.events)
    assert group.stop() is True


def test_composite_broadcasts_before_first_stop_and_retains_unconfirmed_member(
    factories,
):
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())

    def management_stop():
        assert factories.settlement.stop_requested
        factories.events.append(("management", "stop"))
        return True

    factories.management.stop = management_stop
    factories.settlement.stop_result = False
    assert group.stop() is False
    assert group.stop_confirmations == (True, False)
    assert group.members == (factories.management, factories.settlement)
    assert factories.events == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
        ("management", "stop"),
        ("settlement", "stop"),
    ]
    factories.settlement.stop_result = True
    assert group.stop() is True


def test_second_factory_failure_preserves_first_handle_until_confirmed_stop(factories):
    factories.settlement_error = RuntimeError("PRIVATE_SECOND_FACTORY_ERROR")
    factories.management.stop_result = False
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    assert group.members == (factories.management,)
    assert group.startup_failure == "WORKER_START_FAILED"
    assert group.ready() is False
    assert factories.management.stop_requested
    assert group.stop() is False
    assert group.members == (factories.management,)
    factories.management.stop_result = True
    assert group.stop() is True


def test_first_factory_failure_does_not_construct_second_worker(factories):
    factories.child_error = RuntimeError("PRIVATE_FIRST_FACTORY_ERROR")
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    assert group.members == ()
    assert not group.ready()
    assert factories.settlement_calls == []
    assert group.stop()


def test_second_release_failure_broadcasts_stop_to_already_released_management(
    factories,
):
    factories.settlement.release_error = RuntimeError("PRIVATE_RELEASE_ERROR")
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    assert group.ready()
    with pytest.raises(RuntimeError, match="WORKER_GROUP_RELEASE_FAILED") as failure:
        group.release()
    assert "PRIVATE_RELEASE" not in str(failure.value)
    assert factories.management.stop_requested and factories.settlement.stop_requested
    assert group.members == (factories.management, factories.settlement)
    assert group.stop()


@pytest.mark.parametrize("flag", ["true", "TRUE", "False", True, False, None, ""])
def test_composite_rejects_every_nonliteral_disabled_entry_value_before_factories(
    factories, flag
):
    with pytest.raises(ValueError, match="MANAGEMENT_ONLY_ENTRIES_DISABLED_REQUIRED"):
        managed.TestnetPorts(
            str(uuid4()), settings(V2_CONTROLLER_ENABLE_ENTRIES=flag)
        ).spawn(target())
    assert factories.child_calls == factories.settlement_calls == []


@pytest.mark.parametrize("mode", [None, "", "UNKNOWN", " LEGACY", True])
def test_unknown_mode_does_not_fall_back_to_legacy(factories, mode):
    with pytest.raises(ValueError, match="EXPLICIT_CONTROLLER_WORKER_MODE_REQUIRED"):
        managed.TestnetPorts(
            str(uuid4()), settings(V2_CONTROLLER_WORKER_MODE=mode)
        ).spawn(target())
    assert factories.child_calls == factories.settlement_calls == []


class Channel:
    def __init__(self, messages=()):
        self.messages, self.sent, self.polls = list(messages), [], []
        self.closed = False

    def poll(self, timeout=None):
        self.polls.append(timeout)
        return bool(self.messages)

    def recv(self):
        if not self.messages:
            raise EOFError
        return self.messages.pop(0)

    def send(self, value):
        self.sent.append(value)

    def close(self):
        self.closed = True


class Process:
    def __init__(self, *, target, args, mode):
        self.target, self.args, self.mode = target, args, mode
        self.pid = None
        self.running = False
        self.closed = False
        self.calls = []
        self.terminate_works = True
        self.kill_works = True
        self.on_join = None

    def start(self):
        self.calls.append("start")
        if self.mode != "no-pid":
            self.pid, self.running = 1234, True
        if self.mode != "success":
            raise RuntimeError("PRIVATE_PROCESS_START_ERROR")

    def is_alive(self):
        return self.running

    def join(self, timeout):
        self.calls.append(("join", timeout))
        if self.on_join is not None:
            self.on_join()

    def terminate(self):
        self.calls.append("terminate")
        if self.terminate_works:
            self.running = False

    def kill(self):
        self.calls.append("kill")
        if self.kill_works:
            self.running = False

    def close(self):
        self.closed = True


@pytest.fixture
def spawn_context(monkeypatch):
    case = SimpleNamespace(mode="success", methods=[], cells=[], process=None)
    case.channel, case.child = Channel(["READY"]), Channel()

    class Context:
        def RawValue(self, typecode, value):
            assert (typecode, value) == ("b", 0)
            cell = SimpleNamespace(value=value)
            case.cells.append(cell)
            return cell

        def Event(self):
            pytest.fail("child control hints must not use process-shared Event locks")

        def Pipe(self, *, duplex):
            assert duplex is False
            return case.channel, case.child

        def Process(self, *, target, args):
            case.process = Process(target=target, args=args, mode=case.mode)
            return case.process

    def context(method):
        case.methods.append(method)
        assert method == "spawn"
        return Context()

    monkeypatch.setattr(managed.multiprocessing, "get_context", context)
    return case


def child_values(**changes):
    return settings(V2_CONTROLLER_WORKER_MODE="MANAGEMENT_ONLY", **changes)


def test_child_uses_raw_control_hints_closes_child_pipe_and_retains_ready_behavior(
    spawn_context,
):
    tenant, selected, values = str(uuid4()), target(), child_values()
    worker = managed.ChildWorker(tenant, selected, values)
    assert spawn_context.methods == ["spawn"]
    assert len(spawn_context.cells) == 2
    assert spawn_context.child.closed
    assert not spawn_context.channel.closed
    assert spawn_context.process.target is managed.child_main
    assert spawn_context.process.args[:3] == (tenant, selected, values)
    assert worker.ready() is True
    assert worker.entries_enabled is False
    assert not worker.stop_event.is_set() and not worker.release_event.is_set()
    worker.release()
    assert worker.release_event.is_set()
    worker.request_stop()
    assert worker.stop_event.is_set()
    assert spawn_context.process.calls == ["start"]
    assert worker.stop() is True
    assert spawn_context.process.calls == [
        "start",
        ("join", 1),
        "terminate",
        ("join", 5),
    ]


def test_partial_spawn_retains_failed_handle_and_ready_never_consumes_ipc(
    spawn_context,
):
    spawn_context.mode = "partial"
    worker = managed.ChildWorker(str(uuid4()), target(), child_values())
    assert worker.process is spawn_context.process
    assert worker.process.pid == 1234 and worker.process.is_alive()
    assert worker.startup_failure == "WORKER_START_FAILED"
    assert worker.stop_event.is_set()
    assert worker.ready() is False
    assert spawn_context.channel.polls == []
    assert spawn_context.child.closed and not spawn_context.channel.closed
    assert worker.stop() is True
    assert worker.process is spawn_context.process


def test_partial_spawn_unconfirmed_stop_does_not_hide_owned_process(spawn_context):
    spawn_context.mode = "partial"
    worker = managed.ChildWorker(str(uuid4()), target(), child_values())
    worker.process.terminate_works = worker.process.kill_works = False
    assert worker.stop() is False
    assert worker.process is spawn_context.process
    assert worker.process.is_alive() and worker.stop_event.is_set()
    worker.process.kill_works = True
    assert worker.stop() is True


def test_no_pid_spawn_failure_closes_owned_resources_and_raises_fixed_code(
    spawn_context, capsys
):
    spawn_context.mode = "no-pid"
    with pytest.raises(RuntimeError, match="^WORKER_SPAWN_FAILED$") as failure:
        managed.ChildWorker(str(uuid4()), target(), child_values())
    assert spawn_context.process.pid is None
    assert spawn_context.process.closed
    assert spawn_context.channel.closed and spawn_context.child.closed
    assert spawn_context.process.calls == ["start"]
    captured = capsys.readouterr()
    assert (
        "PRIVATE_PROCESS_START" not in str(failure.value) + captured.out + captured.err
    )


def test_partial_management_spawn_composition_retains_and_stops_both_siblings(
    spawn_context, monkeypatch
):
    spawn_context.mode = "partial"
    events = []
    other = Member("settlement", events)
    monkeypatch.setattr(settlement, "SettlementWorker", lambda *_: other)
    group = managed.TestnetPorts(str(uuid4()), settings()).spawn(target())
    assert len(group.members) == 2
    first = group.members[0]
    assert type(first) is managed.ChildWorker
    assert group.members[1] is other
    assert not group.ready()
    assert first.stop_event.is_set() and other.stop_requested
    spawn_context.process.on_join = lambda: (
        pytest.fail("second sibling must receive stop before first join")
        if not other.stop_requested
        else None
    )
    assert group.stop()
    assert group.stop_confirmations == (True, True)


def test_direct_composite_child_is_rejected_before_any_process_construction(
    spawn_context,
):
    with pytest.raises(ValueError, match="DIRECT_WORKER_GROUP_OWNER_REQUIRED"):
        managed.ChildWorker(str(uuid4()), target(), settings())
    assert spawn_context.methods == []
    assert spawn_context.process is None


def test_child_main_cannot_construct_legacy_or_nested_workers_for_composite_mode(
    monkeypatch, capsys
):
    imports = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"redis", "clickhouse_connect"}:
            imports.append(name)
            pytest.fail("composite child must reject before legacy dependency imports")
        return original_import(name, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("direct composite child must not initialize any business port")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    for name in (
        "scope_for",
        "private_for",
        "database",
        "create_testnet_process",
        "_management_child",
    ):
        monkeypatch.setattr(managed, name, forbidden)
    channel = Channel()
    managed.child_main(
        str(uuid4()),
        target(),
        settings(),
        SimpleNamespace(),
        SimpleNamespace(),
        channel,
    )
    assert channel.sent == ["FAILED"]
    assert channel.closed and imports == []
    captured = capsys.readouterr()
    assert "execution-worker-failure" in captured.out
