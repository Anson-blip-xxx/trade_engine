"""Explicit position-preserving SANDBOX restart; read-only dry run by default.

Uses the established graceful-stop / manage-only / same-binding resume protocol.
Owned positions are not closed, orders are not cancelled, cash/risk policy is not
changed. Native protection must also be independently checked before execution.
Only the controller's exact reviewed minimal artifact may be installed. Failure
before switching may restore the old process in management-only mode, never OPEN.
"""

import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNITS = (
    "trade-v2-execution-controller.service",
    "trade-v2-testnet-daemon.service",
    "trade-v2-testnet-watchdog.service",
)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def compatible_admin(activation):
    admin = activation.ADMIN
    link = Path("/opt/trade-engine-v2/account-admin")
    if not link.is_symlink() or link.resolve() != admin:
        raise ValueError("COMPATIBLE_POLICY_READERS_REQUIRED")
    if (
        hashlib.sha256((admin / "v2_core/runtime_policy.py").read_bytes()).hexdigest()
        != activation.POLICY_HASH
    ):
        raise ValueError("EXACT_POLICY_PARSER_REQUIRED")
    activation.running("trade-v2-account-admin.service", admin)


def runners_stopped(protocol):
    """MainPID=0 is insufficient: all controller and legacy cgroups must be empty."""
    for unit in UNITS:
        output = protocol.run(
            [
                "systemctl",
                "show",
                "--no-pager",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "ControlGroup",
            ]
        )
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if any(
            props.get(key) != value
            for key, value in {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
            }.items()
        ):
            return False
        group_name = "/system.slice/" + unit
        if props.get("ControlGroup") not in ("", group_name):
            return False
        group = Path("/sys/fs/cgroup") / group_name.lstrip("/")
        if group.is_symlink():
            return False
        if group.exists():
            for path in [group, *group.rglob("*")]:
                if path.is_symlink():
                    return False
                if path.is_dir() and (path / "cgroup.procs").read_text().strip():
                    return False
    return True


def guarded_execute(
    protocol, *, policy_check, readers_check, candidate_check, stopped, apply=False
):
    """Add bounded checks to the tested protocol, not a parallel restart engine."""
    original_database, original_run = protocol.database, protocol.run
    original_switch = protocol.replace_link
    state = {"baseline": None, "stop_requested": False}

    def route_unchanged(baseline):
        observed = original_database("status", baseline)
        if (
            observed.get("same_binding") is not True
            or observed.get("pending") is not False
            or observed.get("epoch") != baseline["epoch"]
            or observed.get("request_epoch") != baseline["request_epoch"]
        ):
            raise protocol.ReleaseError("CONCURRENT_ROUTE_REQUEST")

    def database(action, baseline=None, **extra):
        if action == "request":
            policy_check(state["baseline"])
            readers_check()
            candidate_check()
        result = original_database(action, baseline, **extra)
        if action == "capture":
            state["baseline"] = result
            policy_check(result)
        return result

    def run(command, **kwargs):
        if list(command) == ["sudo", "-n", "systemctl", "stop", protocol.UNIT]:
            baseline = state["baseline"]
            policy_check(baseline)
            readers_check()
            if original_database("capture") != baseline:
                raise protocol.ReleaseError("CONCURRENT_ROUTE_REQUEST")
            state["stop_requested"] = True
        return original_run(command, **kwargs)

    def switch():
        if not stopped():
            raise protocol.ReleaseError("ALL_EXECUTION_PROCESSES_MUST_BE_STOPPED")
        route_unchanged(state["baseline"])
        policy_check(state["baseline"])
        readers_check()
        if not stopped():
            raise protocol.ReleaseError("EXECUTOR_RESTARTED_DURING_CHECK")
        original_switch()

    protocol.database, protocol.run, protocol.replace_link = database, run, switch
    try:
        result = protocol.execute(apply=apply, resume=True)
        if result == 0:
            policy_check(state["baseline"])
            if apply:
                candidate_check()
            protocol.emit("POLICY_COMPARISON", unchanged=True, cash_activation=False)
        elif state["stop_requested"] and protocol.LINK.resolve() == protocol.PRIOR:
            # Never undo a switched candidate or resume entries on an error.
            # Retaining management matters more than installing this release.
            try:
                if not stopped():
                    raise protocol.ReleaseError("OLD_MANAGEMENT_NOT_RESTARTED")
                route_unchanged(state["baseline"])
                # The old parser may not understand a concurrently activated
                # basis selector. Restore it only with its original policy.
                policy_check(state["baseline"])
                started_ms = time.time_ns() // 1_000_000
                original_run(
                    ["sudo", "-n", "systemctl", "start", protocol.UNIT], timeout=30
                )
                protocol.wait_route(
                    state["baseline"], phase="DRAINING", since_ms=started_ms, timeout=60
                )
                protocol.emit("PRIOR_MANAGEMENT_RESTORED", entries_enabled=False)
            except Exception:  # noqa: BLE001 - never disclose provider or account data
                protocol.emit(
                    "PRIOR_MANAGEMENT_REQUIRES_ATTENTION", entries_enabled=False
                )
        return result
    except Exception:  # noqa: BLE001 - sanitized policy failure after protocol return
        protocol.emit("MANAGED_RELEASE_CHECK_FAILED", automatic_rollback=False)
        return 1
    finally:
        protocol.database, protocol.run, protocol.replace_link = (
            original_database,
            original_run,
            original_switch,
        )


IMPORT_PROGRAM = r"""
import importlib
from pathlib import Path
root = Path.cwd().resolve()
for name in ("services.v2_execution_supervisor", "services.v2_managed_testnet",
             "services.v2_directional_cash", "services.v2_directional_settlement",
             "v2_core.ledger", "v2_core.runtime_policy", "v2_core.venue_cash",
             "v2_core.settlement_cash_basis"):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(root)
from v2_core.runtime_policy import resolve
defaults = resolve()
assert defaults["settlement.cash_basis_version"] == 1
assert defaults["settlement.max_difference_per_fill_usdt"] == "0.000001"
assert defaults["settlement.max_difference_episode_usdt"] == "0.0001"
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
"""


def main():
    apply = sys.argv[1:] == ["--execute"]
    if sys.argv[1:] and not apply:
        raise ValueError("EXPECTED_DRY_RUN_OR_EXECUTE")
    if os.environ.get("V2_SHARED_MARKET") != "true":
        raise ValueError("SHARED_HOST_BUDGET_REQUIRED")
    if (
        os.environ.get("CREDENTIALS_DIRECTORY")
        != "/run/credentials/trade-v2-execution-controller.service"
    ):
        raise ValueError("EXISTING_CONTROLLER_CREDENTIAL_MOUNT_REQUIRED")
    sys.dont_write_bytecode = True
    activation = load("_cash_managed_activation", "v2_venue_cash_activate_20261114.py")
    release = load("_cash_managed_manifest", "v2_venue_cash_release_20261114.py")
    protocol = load("_cash_managed_protocol", "v2_profitability_release_restart.py")
    protocol.PRIOR, protocol.CANDIDATE = release.PRIOR, release.CANDIDATE
    protocol.IMPORT_PROGRAM = IMPORT_PROGRAM

    def readers_check():
        activation.reviewed_controller()
        compatible_admin(activation)

    readers_check()
    activation.running(protocol.UNIT, release.PRIOR)
    sys.path.insert(0, str(release.CANDIDATE))
    from services import v2_managed_testnet as managed
    from v2_core import runtime_policy

    for module in (managed, runtime_policy):
        if not Path(module.__file__).resolve().is_relative_to(release.CANDIDATE):
            raise ValueError("CANDIDATE_IMPORT_ORIGIN_REQUIRED")
    state = {}

    def policy_check(baseline):
        target = {
            "target_registry": baseline["registry"],
            "binding_version": baseline["binding"],
            "epoch": baseline["epoch"],
        }
        scope = managed.scope_for(baseline["tenant"], target)
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ):
            raise protocol.ReleaseError("SANDBOX_FUTURES_ONLY")
        current = runtime_policy.PolicyStore(managed.database(), scope).read()
        if current.version <= 0:
            raise protocol.ReleaseError("EXISTING_POLICY_REQUIRED")
        if not state:
            state.update(scope=scope, policy=current)
        if scope != state["scope"] or current != state["policy"]:
            raise protocol.ReleaseError("POLICY_CHANGED_DURING_RELEASE")

    return guarded_execute(
        protocol,
        policy_check=policy_check,
        readers_check=readers_check,
        candidate_check=lambda: activation.running(protocol.UNIT, release.CANDIDATE),
        stopped=lambda: runners_stopped(protocol),
        apply=apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
