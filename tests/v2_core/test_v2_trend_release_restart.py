"""No service or database I/O: release gating is exercised with explicit fakes."""

import json
import sys
from copy import deepcopy

import pytest

from scripts import v2_trend_release_restart as release

BASELINE = {
    "epoch": 12,
    "request_epoch": 12,
    "tenant": "private-tenant",
    "registry": "private-account",
}


def status(phase="DRAINING", **updates):
    return {
        "same_binding": True,
        "epoch": 12 if phase == "DRAINING" else 13,
        "phase": phase,
        "blockers": ["RECOVERING_SOURCE"] if phase == "DRAINING" else [],
        "pending": False,
        "request_epoch": 12 if phase == "DRAINING" else 13,
        "coverage": "ACCOUNT_COVERAGE_CLEAR",
        "coverage_at_ms": 200,
        **updates,
    }


@pytest.fixture
def resumed(monkeypatch, tmp_path):
    protocol = release.protocol
    link = tmp_path / "controller"
    link.symlink_to(release.CANDIDATE)
    monkeypatch.setattr(protocol, "LINK", link)
    monkeypatch.setattr(
        protocol,
        "unit_properties",
        lambda: {"ActiveState": "active", "SubState": "running", "MainPID": "42"},
    )
    monkeypatch.setattr(protocol.time, "time_ns", lambda: 100_000_000)
    monkeypatch.setattr(
        protocol, "run", lambda *_a, **_k: pytest.fail("unexpected external command")
    )
    actions = []

    def database(action, *args, **kwargs):
        actions.append(action)
        if action == "capture":
            assert kwargs == {"capture_phase": "DRAINING"}
            return deepcopy(BASELINE)
        if action == "request":
            assert args == (BASELINE,)
            assert set(kwargs) == {"request_id"}
            return {"epoch": 13}
        if action == "status":
            return status("ACTIVE" if "request" in actions else "DRAINING")
        pytest.fail("unexpected database action")

    monkeypatch.setattr(protocol, "database", database)
    return actions


def test_dry_resume_checks_fresh_management_without_request(resumed, capsys):
    assert release.resume(expected_epoch=12) == 0
    assert resumed == ["capture", "status"]
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "RESUME_PREFLIGHT_PASSED"
    assert event["mutation_authorized"] is False


def test_explicit_resume_checks_coverage_then_requests_same_binding(resumed, capsys):
    assert release.resume(expected_epoch=12, apply=True) == 0
    assert resumed == ["capture", "status", "request", "status"]
    event = json.loads(capsys.readouterr().out)
    assert event == {
        "event": "TREND_CONTROLLER_ACTIVE",
        "epoch": 13,
        "coverage": "ACCOUNT_COVERAGE_CLEAR",
        "same_binding": True,
    }


def test_epoch_mismatch_never_requests(resumed, capsys):
    assert release.resume(expected_epoch=11, apply=True) == 1
    assert resumed == ["capture"]
    assert "EXPECTED_ROUTE_EPOCH_REQUIRED" in capsys.readouterr().out


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"same_binding": False}, "BINDING_CHANGED"),
        ({"request_epoch": 14}, "CONCURRENT_ROUTE_REQUEST"),
        ({"pending": True}, "CONCURRENT_ROUTE_REQUEST"),
        ({"coverage": "ACCOUNT_COVERAGE_BLOCKED"}, "ROUTE_READINESS_TIMEOUT"),
        ({"coverage_at_ms": 99}, "ROUTE_READINESS_TIMEOUT"),
        ({"blockers": ["WORKER_PROGRESS_STALE"]}, "ROUTE_READINESS_TIMEOUT"),
    ],
)
def test_unsafe_management_blocks_resume(monkeypatch, resumed, capsys, updates, code):
    original = release.protocol.database

    def database(action, *args, **kwargs):
        result = original(action, *args, **kwargs)
        return {**result, **updates} if action == "status" else result

    monkeypatch.setattr(release.protocol, "database", database)
    clock = iter([0, 0, 0, 61])
    monkeypatch.setattr(release.protocol.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(release.protocol.time, "sleep", lambda _seconds: None)
    assert release.resume(expected_epoch=12, apply=True) == 1
    assert "request" not in resumed
    assert code in capsys.readouterr().out


def test_resume_requires_exact_candidate(monkeypatch, resumed, tmp_path, capsys):
    wrong = tmp_path / "wrong-release"
    wrong.symlink_to(release.PRIOR)
    monkeypatch.setattr(release.protocol, "LINK", wrong)
    assert release.resume(expected_epoch=12, apply=True) == 1
    assert resumed == []
    assert "TREND_CANDIDATE_REQUIRED" in capsys.readouterr().out


def test_resume_sanitizes_unexpected_failure(monkeypatch, resumed, capsys):
    def failed(*_args, **_kwargs):
        raise ValueError("SECRET private-account private-tenant")

    monkeypatch.setattr(release.protocol, "database", failed)
    assert release.resume(expected_epoch=12, apply=True) == 1
    output = capsys.readouterr().out
    assert "UNEXPECTED_FAILURE" in output
    assert all(
        value not in output for value in ("SECRET", "private-account", "private-tenant")
    )


def test_deploy_holds_manage_only_and_never_requests(monkeypatch, capsys):
    protocol = release.protocol
    actions = []
    monkeypatch.setattr(protocol, "verify_layout", lambda: actions.append("verify"))
    monkeypatch.setattr(protocol, "journal_cursor", lambda: "cursor")
    monkeypatch.setattr(protocol.time, "time_ns", lambda: 10_000)

    def database(action, *_args, **_kwargs):
        actions.append(action)
        assert action == "capture"
        return deepcopy(BASELINE)

    monkeypatch.setattr(protocol, "database", database)

    def run(command, **_kwargs):
        if "stop" in command:
            actions.append("stop")
        elif "start" in command:
            actions.append("start")
        return json.dumps(
            {
                "__REALTIME_TIMESTAMP": "20",
                "MESSAGE": json.dumps(
                    {
                        "component": "execution-worker-stop",
                        "forced": False,
                        "stopped": True,
                    }
                ),
            }
        )

    monkeypatch.setattr(protocol, "run", run)
    monkeypatch.setattr(
        protocol, "unit_properties", lambda: {"ActiveState": "inactive", "MainPID": "0"}
    )
    monkeypatch.setattr(protocol, "replace_link", lambda: actions.append("switch"))

    def wait(_baseline, *, phase, **_kwargs):
        actions.append(phase)
        assert phase == "DRAINING"
        return status()

    monkeypatch.setattr(protocol, "wait_route", wait)
    assert protocol.execute(apply=True, resume=False) == 0
    assert actions == ["verify", "capture", "stop", "switch", "start", "DRAINING"]
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert event == {"event": "MANAGE_ONLY_HELD", "epoch": 12, "entries_enabled": False}


@pytest.mark.parametrize("execute", [False, True])
def test_main_always_deploys_without_automatic_resume(monkeypatch, execute):
    for key in ("PRIOR", "CANDIDATE", "IMPORT_PROGRAM"):
        monkeypatch.setattr(release.protocol, key, getattr(release.protocol, key))
    monkeypatch.setattr(sys, "argv", ["release", *(["--execute"] if execute else [])])
    calls = []
    monkeypatch.setattr(
        release.protocol, "execute", lambda **kwargs: calls.append(kwargs) or 0
    )
    assert release.main() == 0
    assert calls == [{"apply": execute, "resume": False}]
    assert (
        release.protocol.PRIOR.name == "settlement-entry-witness-controller-20261029-v1"
    )
    assert release.protocol.CANDIDATE.name == "causal-trend-controller-20261030-v2"
    assert "v2_core.settlement_precision" in release.protocol.IMPORT_PROGRAM
    assert "services.v2_directional_cash" in release.protocol.IMPORT_PROGRAM


def test_main_resume_requires_explicit_epoch(monkeypatch):
    for key in ("PRIOR", "CANDIDATE", "IMPORT_PROGRAM"):
        monkeypatch.setattr(release.protocol, key, getattr(release.protocol, key))
    monkeypatch.setattr(sys, "argv", ["release", "--resume", "--execute"])
    monkeypatch.setattr(
        release, "resume", lambda **_kwargs: pytest.fail("must not resume")
    )
    with pytest.raises(SystemExit) as exc:
        release.main()
    assert exc.value.code == 2
