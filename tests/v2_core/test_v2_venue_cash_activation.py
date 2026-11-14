"""Activation uses mocked filesystem/process/database ports; no runtime access."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import v2_venue_cash_activate_20261114 as activation
from v2_core.evidence import canonical
from v2_core.runtime_policy import PolicySnapshot, resolve


@pytest.fixture
def ports(monkeypatch):
    import services
    from v2_core import runtime_policy

    encoded = b"reviewed-parser-only-unit-fixture"
    route = {
        "tenant": "private-unit-tenant",
        "registry": "private-unit-registry",
        "binding": 3,
        "epoch": 12,
        "account": "private-unit-account",
    }
    state = {
        "pid": "123",
        "cwd_mismatch": False,
        "link_mismatch": False,
        "policy_bytes": encoded,
        "environment": "SANDBOX",
        "route_changed": False,
        "cas_failure": False,
        "post_patch_changed": False,
        "current": PolicySnapshot(7, canonical(resolve())),
        "captures": 0,
        "process_calls": [],
        "patches": [],
    }

    class FakePath:
        def __init__(self, *parts):
            self.path = Path(*parts)

        def resolve(self):
            if self.path.parts[:2] == ("/", "proc"):
                return (
                    Path("/wrong/process")
                    if state["cwd_mismatch"]
                    else state["process_cwd"]
                )
            if self.path.is_relative_to(Path(str(activation.ROOT))):
                return self.path
            if state["link_mismatch"]:
                return Path("/wrong/release")
            return {
                Path("/opt/trade-engine-v2/execution-controller"): activation.ROOT,
                Path("/opt/trade-engine-v2/account-admin"): activation.ADMIN,
            }.get(self.path, self.path)

    class ReleasePath:
        def __init__(self, actual):
            self.actual = actual

        def __str__(self):
            return str(self.actual)

        def __fspath__(self):
            return str(self.actual)

        def __truediv__(self, suffix):
            assert suffix == "v2_core/runtime_policy.py"
            return SimpleNamespace(read_bytes=lambda: state["policy_bytes"])

        def __eq__(self, other):
            return self is other

    root = ReleasePath(activation.ROOT)
    admin = ReleasePath(activation.ADMIN)
    monkeypatch.setattr(activation, "ROOT", root)
    monkeypatch.setattr(activation, "ADMIN", admin)
    monkeypatch.setattr(activation, "Path", FakePath)
    monkeypatch.setattr(activation, "reviewed_controller", lambda: None)
    monkeypatch.setattr(activation, "POLICY_HASH", hashlib.sha256(encoded).hexdigest())
    monkeypatch.setattr(activation.sys, "argv", ["activate.py"])
    monkeypatch.setattr(activation.sys, "path", list(activation.sys.path))
    monkeypatch.setattr(activation.sys, "dont_write_bytecode", True)

    def process(command, **kwargs):
        assert kwargs == {
            "text": True,
            "timeout": 10,
            "stderr": activation.subprocess.DEVNULL,
        }
        state["process_calls"].append(command)
        if command[:4] == ["sudo", "-n", "readlink", "-f"]:
            assert command[4] == "/proc/" + state["pid"] + "/cwd"
            if state.get("readlink_failure"):
                raise PermissionError("SENSITIVE_PROVIDER_FAILURE")
            return (
                "/wrong/process" if state["cwd_mismatch"] else str(state["process_cwd"])
            )
        assert command[:2] == ["systemctl", "show"]
        assert command[-3:] == ["-p", "MainPID", "--value"]
        state["process_cwd"] = (
            root if command[2] == "trade-v2-execution-controller.service" else admin
        )
        if state.get("pid_changed") and len(state["process_calls"]) == 3:
            return "124"
        return state["pid"]

    monkeypatch.setattr(activation.subprocess, "check_output", process)

    def capture(action):
        assert action == "capture", (
            "activation must never request a new execution route"
        )
        state["captures"] += 1
        result = deepcopy(route)
        if state["route_changed"] and state["captures"] > 1:
            result["epoch"] += 1
        return result

    protocol = SimpleNamespace(database=capture)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
    monkeypatch.setattr(
        activation.importlib.util, "spec_from_file_location", lambda *_: spec
    )
    monkeypatch.setattr(
        activation.importlib.util, "module_from_spec", lambda _: protocol
    )

    managed = SimpleNamespace(
        __file__=str(root) + "/services/v2_managed_testnet.py",
        scope_for=lambda tenant, target: SimpleNamespace(
            environment=state["environment"]
        ),
        database=lambda: object(),
    )
    monkeypatch.setattr(services, "v2_managed_testnet", managed, raising=False)
    monkeypatch.setattr(
        runtime_policy, "__file__", str(root) + "/v2_core/runtime_policy.py"
    )

    class Store:
        def __init__(self, *_):
            pass

        def read(self):
            return state["current"]

        def patch(self, changes, *, expected_version, reason):
            state["patches"].append((changes, expected_version, reason))
            if state["cas_failure"] or expected_version != state["current"].version:
                raise ValueError("POLICY_VERSION_CONFLICT")
            after = PolicySnapshot(
                expected_version + 1,
                canonical(resolve({**state["current"].values, **changes})),
            )
            state["current"] = (
                PolicySnapshot(after.version + 1, after.values_json)
                if state["post_patch_changed"]
                else after
            )
            return after

    monkeypatch.setattr(runtime_policy, "PolicyStore", Store)
    return state


def test_activation_dry_run_reads_but_never_patches_or_logs_account_ids(ports, capsys):
    assert activation.main() == 0
    assert ports["patches"] == []
    assert ports["captures"] == 1
    assert len(ports["process_calls"]) == 6
    output = capsys.readouterr().out
    assert "private-unit" not in output
    assert json.loads(output)["writes"] is False


def test_execute_changes_only_basis_selector_with_expected_policy_version(
    ports, monkeypatch, capsys
):
    before = ports["current"]
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    assert activation.main() == 0
    assert ports["captures"] == 2
    assert ports["patches"] == [
        ({"settlement.cash_basis_version": 2}, 7, "VERIFIED_VENUE_CASH_BASIS_20261114")
    ]
    assert ports["current"].values == {
        **before.values,
        "settlement.cash_basis_version": 2,
    }
    output = json.loads(capsys.readouterr().out)
    assert output["other_settings_unchanged"] is True
    assert output["wallet_tolerance"] == "0"
    assert output["ledger_rewrite"] is output["venue_calls"] is False


def test_repeat_activation_is_idempotent(ports, monkeypatch):
    ports["current"] = PolicySnapshot(
        8, canonical(resolve({"settlement.cash_basis_version": 2}))
    )
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    assert activation.main() == 0
    assert ports["patches"] == []
    assert ports["current"].version == 8


@pytest.mark.parametrize(
    "arguments",
    [["--force"], ["--execute", "--force"], ["--execute", "--execute"], ["--dry-run"]],
)
def test_unknown_cli_arguments_cannot_write(ports, monkeypatch, arguments):
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", *arguments])
    with pytest.raises(ValueError, match="EXPECTED_DRY_RUN_OR_EXECUTE"):
        activation.main()
    assert ports["process_calls"] == ports["patches"] == []


@pytest.mark.parametrize(
    "pid",
    ["", "0", "1", "-1", "not-a-pid", "１２３", "2147483648", "123/../../environ"],
)
def test_running_pid_is_required_before_policy_access(ports, pid):
    ports["pid"] = pid
    with pytest.raises(ValueError, match="COMPATIBLE_RUNNING_RELEASE_REQUIRED"):
        activation.main()
    assert ports["captures"] == 0
    assert ports["patches"] == []


@pytest.mark.parametrize("failure", ["pid_changed", "readlink_failure"])
def test_cross_user_cwd_failure_or_restart_is_sanitized(ports, failure):
    ports[failure] = True
    with pytest.raises(ValueError, match="^COMPATIBLE_RUNNING_RELEASE_REQUIRED$"):
        activation.main()
    assert ports["captures"] == 0
    assert ports["patches"] == []


def test_running_rejects_unapproved_service_before_sudo(ports):
    with pytest.raises(ValueError, match="COMPATIBLE_RUNNING_RELEASE_REQUIRED"):
        activation.running("other.service", activation.ADMIN)
    assert ports["process_calls"] == []


@pytest.mark.parametrize(
    "failure,error",
    [
        ("cwd_mismatch", "COMPATIBLE_RUNNING_RELEASE_REQUIRED"),
        ("link_mismatch", "COMPATIBLE_POLICY_READERS_REQUIRED"),
        ("policy_bytes", "EXACT_POLICY_PARSER_REQUIRED"),
        ("environment", "SANDBOX_ONLY"),
        ("route_changed", "ROUTE_CHANGED"),
    ],
)
def test_unproved_reader_scope_or_route_never_activates(
    ports, monkeypatch, failure, error
):
    ports[failure] = (
        b"changed"
        if failure == "policy_bytes"
        else "LIVE"
        if failure == "environment"
        else True
    )
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    with pytest.raises(ValueError, match=error):
        activation.main()
    assert ports["patches"] == []


@pytest.mark.parametrize(
    "key",
    [
        "settlement.max_difference_per_fill_usdt",
        "settlement.max_difference_episode_usdt",
    ],
)
def test_unreviewed_anomaly_limits_require_new_review(ports, monkeypatch, key):
    ports["current"] = PolicySnapshot(7, canonical(resolve({key: "0.01"})))
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    with pytest.raises(ValueError, match="REVIEWED_ANOMALY_LIMITS_REQUIRED"):
        activation.main()
    assert ports["patches"] == []


def test_policy_cas_conflict_is_not_retried_or_overridden(ports, monkeypatch):
    ports["cas_failure"] = True
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    with pytest.raises(ValueError, match="POLICY_VERSION_CONFLICT"):
        activation.main()
    assert len(ports["patches"]) == 1
    assert ports["current"].version == 7


def test_concurrent_post_commit_change_is_not_reported_as_success(
    ports, monkeypatch, capsys
):
    ports["post_patch_changed"] = True
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    with pytest.raises(ValueError, match="POLICY_VERIFICATION_FAILED"):
        activation.main()
    assert "CASH_BASIS_ACTIVATED" not in capsys.readouterr().out


@pytest.mark.parametrize("module", ["managed", "runtime_policy"])
def test_preimported_workspace_module_cannot_authorize_activation(
    ports, monkeypatch, module
):
    import services
    from v2_core import runtime_policy

    target = services.v2_managed_testnet if module == "managed" else runtime_policy
    monkeypatch.setattr(target, "__file__", "/workspace/not-the-reviewed-release.py")
    monkeypatch.setattr(activation.sys, "argv", ["activate.py", "--execute"])
    with pytest.raises(ValueError, match="CANDIDATE_IMPORT_ORIGIN_REQUIRED"):
        activation.main()
    assert ports["captures"] == 0
    assert ports["patches"] == []


def test_manifest_failure_prevents_even_process_and_policy_preflight(
    ports, monkeypatch
):
    def refuse():
        raise ValueError("EXACT_TESTED_MANIFEST_REQUIRED")

    monkeypatch.setattr(activation, "reviewed_controller", refuse)
    with pytest.raises(ValueError, match="EXACT_TESTED_MANIFEST_REQUIRED"):
        activation.main()
    assert ports["process_calls"] == ports["patches"] == []
    assert ports["captures"] == 0


@pytest.fixture
def manifest_ports(monkeypatch):
    prior = Path("/virtual/prior")
    state = {
        "before": {"unchanged.py": "stable", "modified.py": "old"},
        "after": {
            "unchanged.py": "stable",
            "modified.py": "new",
            "added.py": "new-file",
        },
        "candidate": activation.ROOT,
        "error": None,
        "calls": [],
    }

    def manifest(root):
        state["calls"].append(root)
        if state["error"]:
            raise ValueError(state["error"])
        return state["before"] if root == prior else state["after"]

    def load(name, filename):
        if filename == "v2_venue_cash_release_20261114.py":
            return SimpleNamespace(
                PRIOR=prior,
                CANDIDATE=state["candidate"],
                EXPECTED={"modified.py": "new", "added.py": "new-file"},
            )
        assert filename == "v2_jev_freshness_release_20261108.py"
        return SimpleNamespace(manifest=manifest)

    monkeypatch.setattr(activation, "load", load)
    return state


def test_activation_revalidates_exact_prior_to_candidate_manifest(manifest_ports):
    activation.reviewed_controller()
    assert len(manifest_ports["calls"]) == 2


@pytest.mark.parametrize(
    "defect",
    [
        "missing_changed_file",
        "extra_file",
        "changed_unrelated_file",
        "deleted_file",
        "wrong_hash",
    ],
)
def test_incomplete_or_expanded_artifact_cannot_activate(manifest_ports, defect):
    after = manifest_ports["after"]
    if defect == "missing_changed_file":
        del after["added.py"]
    elif defect == "extra_file":
        after["unreviewed.py"] = "new"
    elif defect == "changed_unrelated_file":
        after["unchanged.py"] = "not-stable"
    elif defect == "deleted_file":
        del after["unchanged.py"]
    elif defect == "wrong_hash":
        after["modified.py"] = "different-implementation"
    with pytest.raises(ValueError, match="EXACT_TESTED_MANIFEST_REQUIRED"):
        activation.reviewed_controller()


def test_manifest_root_must_be_the_reviewed_controller(manifest_ports):
    manifest_ports["candidate"] = Path("/unreviewed/release")
    with pytest.raises(ValueError, match="REVIEWED_CONTROLLER_PATH_REQUIRED"):
        activation.reviewed_controller()
    assert manifest_ports["calls"] == []


def test_symlink_or_unreadable_manifest_failure_propagates(manifest_ports):
    manifest_ports["error"] = "SYMLINK_IN_RELEASE"
    with pytest.raises(ValueError, match="SYMLINK_IN_RELEASE"):
        activation.reviewed_controller()
