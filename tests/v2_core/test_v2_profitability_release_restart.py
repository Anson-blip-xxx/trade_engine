import json
from copy import deepcopy

import pytest

from scripts import v2_profitability_release_restart as release

BASELINE = {
    "epoch": 10,
    "request_epoch": 10,
    "tenant": "private-tenant",
    "registry": "private-account",
}


def stopped(*, forced=False, stopped=True, at=20):
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(at),
            "MESSAGE": json.dumps(
                {
                    "component": "execution-worker-stop",
                    "forced": forced,
                    "stopped": stopped,
                }
            ),
        }
    )


@pytest.mark.parametrize(
    "output,expected",
    [
        (stopped(), True),
        (stopped(at=9), False),
        (stopped(forced=True), False),
        (stopped(stopped=False), False),
        (stopped() + "\n" + stopped(forced=True), False),
        ("invalid\n{}", False),
    ],
)
def test_stop_requires_fresh_explicit_unforced_receipt(output, expected):
    assert release.graceful_stop_proved(output, since_us=10) is expected


def status(phase="DRAINING", **updates):
    result = {
        "same_binding": True,
        "epoch": 10 if phase == "DRAINING" else 11,
        "phase": phase,
        "blockers": ["RECOVERING_SOURCE"] if phase == "DRAINING" else [],
        "pending": False,
        "request_epoch": 10 if phase == "DRAINING" else 11,
        "coverage": "ACCOUNT_COVERAGE_CLEAR",
        "coverage_at_ms": 200,
    }
    return {**result, **updates}


@pytest.mark.parametrize("phase", ["DRAINING", "ACTIVE"])
def test_wait_requires_expected_phase_and_fresh_coverage(monkeypatch, phase):
    monkeypatch.setattr(release, "database", lambda *_args, **_kwargs: status(phase))
    assert (
        release.wait_route(BASELINE, phase=phase, since_ms=100, timeout=1)["phase"]
        == phase
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"same_binding": False}, "BINDING_CHANGED"),
        ({"request_epoch": 12}, "CONCURRENT_ROUTE_REQUEST"),
        ({"pending": True}, "CONCURRENT_ROUTE_REQUEST"),
        ({"epoch": 12}, "ROUTE_EPOCH_ADVANCED_UNEXPECTEDLY"),
    ],
)
def test_wait_never_overrides_changed_binding_or_request(monkeypatch, change, code):
    monkeypatch.setattr(
        release, "database", lambda *_args, **_kwargs: {**status(), **change}
    )
    with pytest.raises(release.ReleaseError, match=code):
        release.wait_route(BASELINE, phase="DRAINING", since_ms=100, timeout=1)


@pytest.mark.parametrize(
    "change",
    [
        {"coverage": "ACCOUNT_COVERAGE_BLOCKED"},
        {"coverage_at_ms": 99},
        {"blockers": ["WORKER_PROGRESS_STALE"]},
        {"phase": "BLOCKED"},
    ],
)
def test_wait_does_not_accept_old_or_blocked_management(monkeypatch, change):
    clock = iter([0, 0, 0, 2])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(release.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(release, "database", lambda *_args, **_kwargs: {**status(), **change})
    with pytest.raises(release.ReleaseError, match="ROUTE_READINESS_TIMEOUT"):
        release.wait_route(BASELINE, phase="DRAINING", since_ms=100, timeout=1)


def test_dry_run_never_stops_switches_or_requests(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(release, "verify_layout", lambda: calls.append("verify"))

    def database(action, *_args, **_kwargs):
        calls.append(action)
        return deepcopy(BASELINE)

    monkeypatch.setattr(release, "database", database)
    monkeypatch.setattr(
        release, "run", lambda *_args, **_kwargs: pytest.fail("unexpected command")
    )
    monkeypatch.setattr(
        release, "replace_link", lambda: pytest.fail("unexpected mutation")
    )
    assert release.execute() == 0
    assert calls == ["verify", "capture"]
    output = capsys.readouterr().out
    assert "private-tenant" not in output and "private-account" not in output
    assert json.loads(output)["mutation_authorized"] is False


def test_failed_stop_does_not_switch_or_resume(monkeypatch, capsys):
    actions = []
    monkeypatch.setattr(release, "verify_layout", lambda: None)
    monkeypatch.setattr(release, "journal_cursor", lambda: "cursor")

    def database(action, *_args, **_kwargs):
        actions.append(action)
        return deepcopy(BASELINE) if action == "capture" else status()

    monkeypatch.setattr(release, "database", database)
    monkeypatch.setattr(release, "run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        release, "unit_properties", lambda: {"ActiveState": "active", "MainPID": "1"}
    )
    monkeypatch.setattr(release, "replace_link", lambda: pytest.fail("must not switch"))
    assert release.execute(apply=True) == 1
    assert actions == ["capture", "status"]
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert event["stage"] == "GRACEFUL_STOP"
    assert event["code"] == "CONTROLLER_NOT_STOPPED"
    assert event["actual_phase"] == "DRAINING"


def test_execute_orders_graceful_switch_recovery_then_request(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(release, "verify_layout", lambda: calls.append("verify"))
    monkeypatch.setattr(release, "journal_cursor", lambda: "cursor")
    monkeypatch.setattr(release.time, "time_ns", lambda: 10_000)

    def database(action, *_args, **_kwargs):
        calls.append(action)
        return deepcopy(BASELINE) if action == "capture" else {"epoch": 11}

    monkeypatch.setattr(release, "database", database)

    def run(command, **_kwargs):
        if "stop" in command:
            calls.append("stop")
        elif "start" in command:
            calls.append("start")
        return stopped()

    monkeypatch.setattr(release, "run", run)
    properties = iter(
        [
            {"ActiveState": "inactive", "MainPID": "0"},
            {"ActiveState": "active", "SubState": "running", "MainPID": "50"},
        ]
    )
    monkeypatch.setattr(release, "unit_properties", lambda: next(properties))
    monkeypatch.setattr(release, "replace_link", lambda: calls.append("switch"))

    def wait(_baseline, *, phase, **_kwargs):
        calls.append(phase)
        return status(phase)

    monkeypatch.setattr(release, "wait_route", wait)
    assert release.execute(apply=True) == 0
    assert calls == [
        "verify",
        "capture",
        "stop",
        "switch",
        "start",
        "DRAINING",
        "request",
        "ACTIVE",
    ]
    output = capsys.readouterr().out
    assert "private-tenant" not in output and "private-account" not in output
    assert json.loads(output.splitlines()[-1])["same_binding"] is True


def test_unexpected_child_failure_does_not_leak_error(monkeypatch, capsys):
    monkeypatch.setattr(
        release, "verify_layout", lambda: (_ for _ in ()).throw(ValueError("SECRET"))
    )
    assert release.execute() == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert "UNEXPECTED_FAILURE" in output
