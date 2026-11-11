"""No real systemd, database, network or release mutations in these tests."""

from types import SimpleNamespace

import pytest

from scripts.v2_flat_controller_release import execute


class ReleaseError(RuntimeError):
    pass


@pytest.fixture
def ports():
    calls, events = [], []
    state = {
        "running": True,
        "link": "old",
        "stop_proved": True,
        "epoch": 8,
        "same_binding": True,
        "pending": False,
    }
    baseline = {"epoch": 8, "request_epoch": 8}

    def database(action, *_args, **_kwargs):
        calls.append(action)
        if action == "capture":
            return dict(baseline)
        if action == "request":
            return {"epoch": 9}
        return {
            "epoch": state["epoch"],
            "request_epoch": state["epoch"],
            "same_binding": state["same_binding"],
            "pending": state["pending"],
        }

    def run(command, **_kwargs):
        if "stop" in command:
            calls.append("stop")
            state["running"] = False
        if "start" in command:
            calls.append("start")
            state["running"] = True
        return "safe journal"

    def switch():
        calls.append("switch")
        state["link"] = "new"

    def wait(_baseline, *, phase, **_kwargs):
        calls.append(phase)
        return {
            "phase": phase,
            "epoch": 9 if phase == "ACTIVE" else 8,
            "coverage": "ACCOUNT_COVERAGE_CLEAR",
        }

    protocol = SimpleNamespace(
        verify_layout=lambda: calls.append("verify"),
        database=database,
        run=run,
        emit=lambda event, **kw: events.append((event, kw)),
        UNIT="fake.service",
        journal_cursor=lambda: "cursor",
        ReleaseError=ReleaseError,
        graceful_stop_proved=lambda *_a, **_kw: state["stop_proved"],
        unit_properties=lambda: {
            "ActiveState": "active" if state["running"] else "inactive",
            "SubState": "running" if state["running"] else "dead",
            "MainPID": "7" if state["running"] else "0",
        },
        LINK=SimpleNamespace(resolve=lambda: state["link"]),
        PRIOR="old",
        replace_link=switch,
        wait_route=wait,
    )

    def gate():
        calls.append("flat")
        return {"status": "FLAT_VERIFIED"}

    return SimpleNamespace(
        protocol=protocol,
        calls=calls,
        events=events,
        state=state,
        gate=gate,
        stopped=lambda: not state["running"],
    )


def test_dry_run_is_read_only(ports):
    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: ports.gate,
            runners_stopped=ports.stopped,
        )
        == 0
    )
    assert ports.calls == ["verify", "capture", "flat"]


def test_flat_is_rechecked_after_stop_before_switch(ports):
    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: ports.gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 0
    )
    assert ports.calls == [
        "verify",
        "capture",
        "flat",
        "stop",
        "flat",
        "switch",
        "start",
        "DRAINING",
        "request",
        "ACTIVE",
    ]


@pytest.mark.parametrize("fails_on", [1, 2])
def test_nonflat_or_unavailable_gate_never_switches(ports, fails_on):
    n = 0

    def gate():
        nonlocal n
        n += 1
        if n == fails_on:
            raise ValueError("SENSITIVE_PROVIDER_ERROR")
        return {"status": "FLAT_VERIFIED"}

    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert "switch" not in ports.calls and "request" not in ports.calls
    assert ports.state["running"] is True and ports.state["link"] == "old"
    assert "SENSITIVE_PROVIDER_ERROR" not in str(ports.events)
    if fails_on == 2:
        assert ports.events[-1][1]["old_management"] == "MANAGE_ONLY_VERIFIED"


@pytest.mark.parametrize("changed", ["same_binding", "epoch", "pending"])
def test_changed_route_never_restored_or_requested(ports, changed):
    n = 0

    def gate():
        nonlocal n
        n += 1
        if n == 2:
            ports.state[changed] = {"same_binding": False, "epoch": 9, "pending": True}[
                changed
            ]
            raise ValueError("changed route")
        return {"status": "FLAT_VERIFIED"}

    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert not set(ports.calls) & {"switch", "start", "request"}


def test_unproved_stop_preserves_old_link_and_restores_management_only(ports):
    ports.state["stop_proved"] = False
    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: ports.gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert "switch" not in ports.calls and "request" not in ports.calls
    assert ports.state["running"] is True


def test_executor_restarted_during_gate_prevents_switch(ports):
    n = 0

    def gate():
        nonlocal n
        n += 1
        if n == 2:
            ports.state["running"] = True
        return {"status": "FLAT_VERIFIED"}

    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert "switch" not in ports.calls and "request" not in ports.calls


def test_candidate_recovery_failure_never_implicitly_rolls_back(ports):
    def wait(*_a, **_kw):
        raise ReleaseError("ROUTE_READINESS_TIMEOUT")

    ports.protocol.wait_route = wait
    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: ports.gate,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert ports.state["link"] == "new" and ports.state["running"] is True
    assert "request" not in ports.calls
    assert ports.events[-1][1]["old_management"] == "NOT_ATTEMPTED"


@pytest.mark.parametrize("proof", [None, {}, {"status": "BLOCKED"}])
def test_gate_must_explicitly_confirm_flat(ports, proof):
    assert (
        execute(
            ports.protocol,
            prepare_gate=lambda _: lambda: proof,
            runners_stopped=ports.stopped,
            apply=True,
        )
        == 1
    )
    assert "stop" not in ports.calls and "switch" not in ports.calls
