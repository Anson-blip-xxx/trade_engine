"""Pure restart-port tests: no venue, database, systemd or credential access."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import v2_venue_cash_managed_restart_20261114 as release


@pytest.fixture
def ports(monkeypatch):
    protocol = release.load(
        "_cash_managed_test_protocol", "v2_profitability_release_restart.py"
    )
    calls, events = [], []
    baseline = {
        "epoch": 8,
        "request_epoch": 8,
        "tenant": "secret-tenant",
        "registry": "secret-registry",
    }
    state = {
        "running": True,
        "link": "old",
        "forced": False,
        "leftover": False,
        "pending": False,
        "same_binding": True,
        "epoch": 8,
        "policy_calls": 0,
        "reader_calls": 0,
        "policy_fail": 0,
        "reader_fail": 0,
        "candidate_calls": 0,
        "candidate_fail": 0,
    }

    def database(action, *_args, **_kwargs):
        calls.append(action)
        if action == "capture":
            return dict(baseline)
        if action == "request":
            return {"epoch": 9}
        return {
            "same_binding": state["same_binding"],
            "pending": state["pending"],
            "epoch": state["epoch"],
            "request_epoch": state["epoch"],
            "phase": "DRAINING",
        }

    def run(command, **_kwargs):
        if "stop" in command:
            calls.append("stop")
            state["running"] = False
        elif "start" in command:
            calls.append("start")
            state["running"] = True
        else:
            assert "journalctl" in command
        return "safe journal"

    def switch():
        calls.append("switch")
        state["link"] = "new"

    def wait(_baseline, *, phase, **_kwargs):
        calls.append(phase)
        if state.get("coverage_failure"):
            raise protocol.ReleaseError("ROUTE_READINESS_TIMEOUT")
        return {
            "phase": phase,
            "epoch": 9 if phase == "ACTIVE" else 8,
            "coverage": "ACCOUNT_COVERAGE_CLEAR",
        }

    def policy_check(actual):
        assert actual == baseline
        state["policy_calls"] += 1
        if state["policy_calls"] == state["policy_fail"]:
            raise protocol.ReleaseError("POLICY_CHANGED_DURING_RELEASE")

    def readers_check():
        state["reader_calls"] += 1
        if state["reader_calls"] == state["reader_fail"]:
            raise ValueError("secret-parser-error")

    def candidate_check():
        state["candidate_calls"] += 1
        if state["candidate_calls"] == state["candidate_fail"]:
            raise ValueError("COMPATIBLE_RUNNING_RELEASE_REQUIRED")

    monkeypatch.setattr(protocol, "verify_layout", lambda: calls.append("verify"))
    monkeypatch.setattr(protocol, "database", database)
    monkeypatch.setattr(protocol, "run", run)
    monkeypatch.setattr(protocol, "replace_link", switch)
    monkeypatch.setattr(protocol, "wait_route", wait)
    monkeypatch.setattr(
        protocol, "emit", lambda event, **kw: events.append((event, kw))
    )
    monkeypatch.setattr(protocol, "journal_cursor", lambda: "cursor")
    monkeypatch.setattr(
        protocol, "graceful_stop_proved", lambda *_a, **_kw: not state["forced"]
    )
    monkeypatch.setattr(
        protocol,
        "unit_properties",
        lambda: {
            "ActiveState": "active" if state["running"] else "inactive",
            "SubState": "running" if state["running"] else "dead",
            "MainPID": "7" if state["running"] else "0",
        },
    )
    monkeypatch.setattr(
        protocol, "LINK", SimpleNamespace(resolve=lambda: state["link"])
    )
    monkeypatch.setattr(protocol, "PRIOR", "old")
    options = {
        "policy_check": policy_check,
        "readers_check": readers_check,
        "candidate_check": candidate_check,
        "stopped": lambda: not state["running"] and not state["leftover"],
    }
    return SimpleNamespace(
        protocol=protocol, state=state, calls=calls, events=events, options=options
    )


def test_default_dry_run_does_not_stop_switch_resume_or_change_policy(ports):
    assert release.guarded_execute(ports.protocol, **ports.options) == 0
    assert ports.calls == ["verify", "capture"]
    assert ports.state["running"] and ports.state["link"] == "old"
    assert "secret-" not in str(ports.events)


def test_position_preserving_restart_uses_manage_only_then_same_binding_resume(ports):
    originals = (
        ports.protocol.database,
        ports.protocol.run,
        ports.protocol.replace_link,
    )
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 0
    assert ports.calls == [
        "verify",
        "capture",
        "capture",
        "stop",
        "status",
        "switch",
        "start",
        "DRAINING",
        "request",
        "ACTIVE",
    ]
    assert ports.state["policy_calls"] == 5
    assert ports.state["reader_calls"] == 3
    assert ports.state["candidate_calls"] == 2
    assert (
        ports.protocol.database,
        ports.protocol.run,
        ports.protocol.replace_link,
    ) == originals
    assert "secret-" not in str(ports.events)
    assert ports.events[-1] == (
        "POLICY_COMPARISON",
        {"unchanged": True, "cash_activation": False},
    )


def test_forced_or_unproved_stop_never_switches_and_restores_only_old_management(ports):
    ports.state["forced"] = True
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert "switch" not in ports.calls and "request" not in ports.calls
    assert ports.state["running"] and ports.state["link"] == "old"
    assert ports.events[-1] == ("PRIOR_MANAGEMENT_RESTORED", {"entries_enabled": False})


def test_leftover_executor_prevents_switch_and_restoration(ports):
    ports.state["leftover"] = True
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert not {"switch", "start", "request"} & set(ports.calls)


def test_changed_policy_prevents_incompatible_prior_parser_restoration(ports):
    ports.state["forced"] = True
    ports.state["policy_fail"] = 3
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert not {"switch", "start", "request"} & set(ports.calls)
    assert ports.events[-1] == (
        "PRIOR_MANAGEMENT_REQUIRES_ATTENTION",
        {"entries_enabled": False},
    )


@pytest.mark.parametrize(
    "field,value", [("pending", True), ("same_binding", False), ("epoch", 9)]
)
def test_concurrent_route_never_switches_or_restores_old_binding(ports, field, value):
    ports.state[field] = value
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert not {"switch", "start", "request"} & set(ports.calls)


@pytest.mark.parametrize("failure", [1, 2, 3, 4, 5])
def test_policy_change_cannot_be_reported_as_success_or_activate_cash_basis(
    ports, failure
):
    ports.state["policy_fail"] = failure
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    if failure <= 2:
        assert "stop" not in ports.calls
    if failure <= 3:
        assert "switch" not in ports.calls
    if failure <= 4:
        assert "request" not in ports.calls
    assert not any(event == "POLICY_COMPARISON" for event, _ in ports.events)


def test_parser_recheck_failure_restores_old_management_without_sensitive_error(ports):
    ports.state["reader_fail"] = 2
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert "switch" not in ports.calls and "request" not in ports.calls
    assert ports.state["running"]
    assert "secret-" not in str(ports.events)


def test_coverage_failure_after_switch_never_rolls_back_or_requests_entries(ports):
    ports.state["coverage_failure"] = True
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    assert ports.state["link"] == "new" and ports.state["running"]
    assert "request" not in ports.calls
    assert ports.calls.count("switch") == 1


@pytest.mark.parametrize("failure", [1, 2])
def test_actual_candidate_process_required_before_resume_and_success(ports, failure):
    ports.state["candidate_fail"] = failure
    assert release.guarded_execute(ports.protocol, **ports.options, apply=True) == 1
    if failure == 1:
        assert "request" not in ports.calls
    assert not any(event == "POLICY_COMPARISON" for event, _ in ports.events)


@pytest.mark.parametrize(
    "args", [["--force"], ["--execute", "--force"], ["--execute", "--execute"]]
)
def test_unreviewed_cli_flags_are_rejected_before_access(monkeypatch, args):
    monkeypatch.setattr(release.sys, "argv", ["restart.py", *args])
    monkeypatch.setattr(
        release, "load", lambda *_: pytest.fail("must not load runtime")
    )
    with pytest.raises(ValueError, match="EXPECTED_DRY_RUN_OR_EXECUTE"):
        release.main()


@pytest.mark.parametrize(
    "env,error",
    [
        ({}, "SHARED_HOST_BUDGET_REQUIRED"),
        ({"V2_SHARED_MARKET": "true"}, "EXISTING_CONTROLLER_CREDENTIAL_MOUNT_REQUIRED"),
        (
            {"V2_SHARED_MARKET": "true", "CREDENTIALS_DIRECTORY": "/different"},
            "EXISTING_CONTROLLER_CREDENTIAL_MOUNT_REQUIRED",
        ),
    ],
)
def test_sandbox_existing_credential_context_is_required(monkeypatch, env, error):
    monkeypatch.setattr(release.sys, "argv", ["restart.py"])
    monkeypatch.setattr(release.os, "environ", env)
    monkeypatch.setattr(
        release, "load", lambda *_: pytest.fail("must not load runtime")
    )
    with pytest.raises(ValueError, match=error):
        release.main()


@pytest.fixture
def cgroups(monkeypatch, tmp_path):
    for unit in release.UNITS:
        group = tmp_path / "system.slice" / unit
        group.mkdir(parents=True)
        (group / "cgroup.procs").write_text("")
    state = {
        "MainPID": "0",
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "ControlGroup": "",
    }
    monkeypatch.setattr(
        release,
        "Path",
        lambda value: tmp_path if value == "/sys/fs/cgroup" else Path(value),
    )
    calls = []

    def run(command):
        calls.append(command)
        return "\n".join(key + "=" + value for key, value in state.items())

    return SimpleNamespace(
        root=tmp_path, protocol=SimpleNamespace(run=run), state=state, calls=calls
    )


def test_all_three_cgroups_must_be_empty(cgroups):
    assert release.runners_stopped(cgroups.protocol)
    assert len(cgroups.calls) == 3
    child = cgroups.root / "system.slice" / release.UNITS[0] / "worker"
    child.mkdir()
    (child / "cgroup.procs").write_text("123\n")
    assert not release.runners_stopped(cgroups.protocol)


@pytest.mark.parametrize(
    "key,value",
    [
        ("MainPID", "2"),
        ("LoadState", "not-found"),
        ("ActiveState", "active"),
        ("SubState", "failed"),
        ("ControlGroup", "/elsewhere"),
    ],
)
def test_unverified_unit_status_is_not_stopped(cgroups, key, value):
    cgroups.state[key] = value
    assert not release.runners_stopped(cgroups.protocol)


def test_symlinked_cgroup_subtree_is_rejected(cgroups):
    group = cgroups.root / "system.slice" / release.UNITS[0]
    (group / "worker").symlink_to(cgroups.root, target_is_directory=True)
    assert not release.runners_stopped(cgroups.protocol)
