"""Worker handle ownership only; no DB, network or running service calls."""

import threading
from types import SimpleNamespace

import pytest

from services.v2_worker_group import WorkerGroup


class Worker:
    def __init__(
        self, name, events, *, ready=True, stop=True, alive=True, healthy=True
    ):
        self.name, self.events = name, events
        self.ready_result, self.stop_result = ready, stop
        self.alive_result, self.healthy_result = alive, healthy
        self.release_error = self.request_error = None
        self.startup_failure = None

    def call(self, action, result):
        self.events.append((self.name, action))
        if isinstance(result, Exception):
            raise result
        return result

    def ready(self):
        return self.call("ready", self.ready_result)

    def release(self):
        return self.call("release", self.release_error)

    def alive(self):
        return self.call("alive", self.alive_result)

    def healthy(self):
        return self.call("healthy", self.healthy_result)

    def request_stop(self):
        return self.call("request_stop", self.request_error)

    def stop(self):
        return self.call("stop", self.stop_result)


def pair(**changes):
    events = []
    first, second = (
        Worker("management", events),
        Worker("settlement", events, **changes),
    )
    return SimpleNamespace(
        events=events,
        first=first,
        second=second,
        group=WorkerGroup([first, second]),
    )


def test_every_member_ready_before_any_release_and_release_is_once_only():
    case = pair()
    assert case.group.ready() is True
    case.group.release()
    case.group.release()
    assert case.events == [
        ("management", "ready"),
        ("settlement", "ready"),
        ("management", "release"),
        ("settlement", "release"),
    ]
    assert case.group.entries_enabled is False
    assert case.group.startup_failure is None
    assert case.group.members == (case.first, case.second)
    assert case.group.stop_confirmations == (None, None)


def test_release_before_readiness_or_after_stop_is_forbidden():
    case = pair()
    with pytest.raises(RuntimeError, match="^WORKER_GROUP_NOT_READY$"):
        case.group.release()
    assert case.events == []
    case.group.request_stop()
    with pytest.raises(RuntimeError, match="^WORKER_GROUP_CLOSING$"):
        case.group.release()
    assert not case.group.ready() and not case.group.alive()


@pytest.mark.parametrize(
    "result", [False, None, 1, "ready", RuntimeError("private failure")]
)
def test_readiness_failure_retains_handles_broadcasts_stop_and_never_releases(result):
    case = pair(ready=result)
    assert case.group.ready() is False
    assert case.group.startup_failure == "WORKER_START_FAILED"
    assert case.group.members == (case.first, case.second)
    assert case.events[-2:] == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
    ]
    with pytest.raises(RuntimeError, match="WORKER_GROUP_CLOSING"):
        case.group.release()
    assert case.group.stop()
    assert not any(action == "release" for _, action in case.events)


@pytest.mark.parametrize(
    "code", ["EXECUTION_GUARD_PERMISSION_DENIED", "PRIVATE_PROVIDER_DIAGNOSTIC", None]
)
def test_startup_diagnostics_only_expose_known_fixed_codes(code):
    case = pair(ready=False)
    case.second.startup_failure = code
    assert case.group.ready() is False
    expected = (
        code if code == "EXECUTION_GUARD_PERMISSION_DENIED" else "WORKER_START_FAILED"
    )
    assert case.group.startup_failure == expected


def test_release_failure_stops_already_released_peers_but_keeps_all_handles():
    case = pair()
    case.second.release_error = RuntimeError("private credential value")
    assert case.group.ready()
    with pytest.raises(RuntimeError, match="^WORKER_GROUP_RELEASE_FAILED$") as raised:
        case.group.release()
    assert "private" not in str(raised.value)
    assert case.events[-2:] == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
    ]
    assert case.group.members == (case.first, case.second)
    assert case.group.stop() is True


@pytest.mark.parametrize(
    "result,confirmation",
    [
        (False, False),
        (None, None),
        (1, None),
        (RuntimeError("private stop error"), None),
    ],
)
def test_unknown_stop_never_discards_handle_or_claims_group_stopped(
    result, confirmation
):
    case = pair(stop=result)
    assert case.group.stop() is False
    assert case.group.stop_confirmations == (True, confirmation)
    assert case.group.members == (case.first, case.second)
    assert case.events == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
        ("management", "stop"),
        ("settlement", "stop"),
    ]
    case.events.clear()
    case.second.stop_result = True
    assert case.group.stop() is True
    assert case.events == [("settlement", "request_stop"), ("settlement", "stop")]
    assert case.group.stop_confirmations == (True, True)


def test_first_stop_exception_does_not_skip_other_member_stop():
    case = pair()
    case.first.stop_result = OSError("private exception")
    assert not case.group.stop()
    assert case.group.stop_confirmations == (None, True)
    assert case.events[-1] == ("settlement", "stop")


def test_request_stop_exception_still_broadcasts_and_attempts_all_stops():
    case = pair()
    case.first.request_error = OSError("private hint error")
    assert case.group.stop()
    assert case.events == [
        ("management", "request_stop"),
        ("settlement", "request_stop"),
        ("management", "stop"),
        ("settlement", "stop"),
    ]


def test_broadcast_precedes_a_blocking_member_stop_without_claiming_nonblocking():
    case = pair()
    entered, release = threading.Event(), threading.Event()
    result = []

    def blocking_stop():
        case.events.append(("management", "stop"))
        entered.set()
        assert release.wait(3)
        return True

    case.first.stop = blocking_stop
    thread = threading.Thread(target=lambda: result.append(case.group.stop()))
    thread.start()
    try:
        assert entered.wait(2)
        assert case.events[:2] == [
            ("management", "request_stop"),
            ("settlement", "request_stop"),
        ]
        assert result == [] and case.group.stop_confirmations == (None, None)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and result == [True]


def test_partial_factory_failure_returns_group_retaining_every_created_handle():
    events = []
    first = Worker("management", events)

    def fail_before_spawn():
        raise ValueError("private factory config")

    group = WorkerGroup.create([lambda: first, fail_before_spawn])
    assert group.members == (first,)
    assert group.startup_failure == "WORKER_START_FAILED"
    assert group.ready() is False
    assert events == [("management", "request_stop")]
    assert group.stop() is True


def test_factory_failure_before_any_handle_can_confirm_empty_owned_set():
    def fail_before_spawn():
        raise ValueError("no child was created")

    group = WorkerGroup.create([fail_before_spawn])
    assert not group.ready() and group.members == ()
    assert group.startup_failure == "WORKER_START_FAILED"
    assert group.stop() is True


def test_invalid_returned_factory_handle_is_retained_as_unknown_until_stopped():
    incomplete = SimpleNamespace()
    group = WorkerGroup.create([lambda: incomplete])
    assert group.members == (incomplete,)
    assert group.startup_failure == "WORKER_START_FAILED"
    assert group.stop() is False and group.stop_confirmations == (None,)
    incomplete.stop = lambda: True
    assert group.stop() is True


def test_duplicate_factory_handle_does_not_lose_or_double_stop_existing_owner():
    events = []
    member = Worker("same", events)
    group = WorkerGroup.create([lambda: member, lambda: member])
    assert group.members == (member,)
    assert not group.ready() and group.stop()
    assert events.count(("same", "stop")) == 1


@pytest.mark.parametrize("method", ["alive", "healthy"])
@pytest.mark.parametrize(
    "result", [False, None, 1, RuntimeError("private liveness error")]
)
def test_member_liveness_and_health_require_explicit_true(method, result):
    case = pair()
    setattr(case.second, method + "_result", result)
    assert getattr(case.group, method)() is False
    assert case.group.members == (case.first, case.second)


def test_healthy_group_preserves_primary_management_when_other_health_is_degraded():
    case = pair(healthy=False)
    assert case.group.alive() is True
    assert case.group.healthy() is False
    assert not any(action == "request_stop" for _, action in case.events)


def test_legacy_member_without_optional_ports_remains_supported():
    member = SimpleNamespace(
        ready=lambda: True, release=lambda: None, alive=lambda: True, stop=lambda: True
    )
    group = WorkerGroup([member], entries_enabled=True)
    assert group.entries_enabled and group.ready() and group.alive() and group.healthy()
    group.release()
    assert group.stop()


def test_present_but_invalid_health_port_is_not_silently_treated_as_healthy():
    case = pair()
    case.second.healthy = True
    assert case.group.healthy() is False
    assert case.group.alive() is True


def test_completed_stop_is_idempotent_without_touching_closed_member_handles():
    case = pair()
    assert case.group.stop()
    previous = tuple(case.events)
    assert case.group.stop()
    assert tuple(case.events) == previous
    assert case.group.stop_confirmations == (True, True)


@pytest.mark.parametrize(
    "members", [[], (), None, "workers", [None], [SimpleNamespace()], [None] * 9]
)
def test_invalid_explicit_members_are_rejected(members):
    with pytest.raises(ValueError):
        WorkerGroup(members)


def test_explicit_duplicate_and_mutating_input_list_cannot_change_owned_members():
    case = pair()
    with pytest.raises(ValueError, match="EXPLICIT_WORKER_HANDLES_REQUIRED"):
        WorkerGroup([case.first, case.first])
    source = [case.first]
    group = WorkerGroup(source)
    source.append(case.second)
    assert group.members == (case.first,)


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_explicit_entry_mode_validation_precedes_all_factory_calls(flag):
    with pytest.raises(ValueError, match="EXPLICIT_WORKER_ENTRY_MODE_REQUIRED"):
        WorkerGroup.create(
            [lambda: pytest.fail("must not construct any worker")], entries_enabled=flag
        )


@pytest.mark.parametrize("factories", [[], None, [None], [lambda: None] * 9])
def test_factory_list_validated_before_starting_any_member(factories):
    with pytest.raises(ValueError):
        WorkerGroup.create(factories)
