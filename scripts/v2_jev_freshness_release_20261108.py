"""Reviewed flat-only SANDBOX release; GET-only dry run unless --execute.

Reuses the tested graceful release protocol. No credentials or account IDs are
embedded or printed. Load existing systemd credentials only into process memory.
"""

import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path(
    "/opt/trade-engine-v2/releases/entry-clearance-quality-controller-20261107-v1"
)
CANDIDATE = Path("/opt/trade-engine-v2/releases/jev-freshness-controller-20261108-v1")
EXPECTED = {
    "v2_core/shared_market.py": "c8573047aafdc12aa2a59b68b414d644f128f7408b8c72960a0daba93b661fc7",
    "services/v2_jev_entry_context.py": "0b81c403d810e110ed6c47149bbcefdfdc4a34e378d544d70bf0d93065416eec",
    "v2_core/scheduling.py": "5df869b405ba8791c1663d1e8f8ea04f52688bb21fb349b6a1981be404c9946d",
    "services/v2_trading_health.py": "7724e4271d47558868fc3b0836c535541c3d30eeadce5e86ca9ebcf7f343c7dc",
}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("SYMLINK_IN_RELEASE")
        if path.is_file() and not {"__pycache__", ".pytest_cache"} & set(path.parts):
            result[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def main():
    if os.environ.get("V2_SHARED_MARKET") != "true":
        raise ValueError("SHARED_HOST_BUDGET_REQUIRED")
    if os.environ.get("CREDENTIALS_DIRECTORY") != (
        "/run/credentials/trade-v2-execution-controller.service"
    ):
        raise ValueError("EXISTING_CONTROLLER_CREDENTIAL_MOUNT_REQUIRED")
    apply = sys.argv[1:] == ["--execute"]
    if sys.argv[1:] and not apply:
        raise ValueError("EXPECTED_DRY_RUN_OR_EXECUTE")
    before, after = manifest(PRIOR), manifest(CANDIDATE)
    if {
        key: after[key] for key in after if after[key] != before.get(key)
    } != EXPECTED or set(before) - set(after):
        raise ValueError("EXACT_TESTED_MANIFEST_REQUIRED")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(CANDIDATE))
    protocol = load(
        "_freshness_release_protocol", "v2_profitability_release_restart.py"
    )
    flat = load("_freshness_flat_gate", "v2_flat_release_gate.py")
    orchestrator = load("_freshness_orchestrator", "v2_flat_controller_release.py")
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.IMPORT_PROGRAM = r"""
import importlib
from pathlib import Path
root = Path.cwd().resolve()
for name in ("services.v2_execution_supervisor", "services.v2_managed_testnet",
             "services.v2_jev_entry_context", "services.v2_trading_health",
             "v2_core.shared_market", "v2_core.scheduling"):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(root)
from v2_core.scheduling import JEV_MARKET_REFRESH_RETRY, JEV_QUOTA_RETRY
assert JEV_MARKET_REFRESH_RETRY == "JEV_MARKET_REFRESH_PENDING"
assert JEV_QUOTA_RETRY == "JEV_RECHECK_ADMISSION_BUDGET_UNAVAILABLE"
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
"""
    from services import v2_managed_testnet as managed
    from v2_core.account_inventory import AccountInventory
    from v2_core.runtime_policy import PolicyStore

    state = {}

    def prepare_gate(baseline):
        target = {
            "target_registry": baseline["registry"],
            "binding_version": baseline["binding"],
            "epoch": baseline["epoch"],
        }
        scope = managed.scope_for(baseline["tenant"], target)
        request = managed.readonly_request(baseline["tenant"], target, scope)
        if request.environment != "SANDBOX" or any(
            getattr(request, name) is not False
            for name in (
                "_trading",
                "_cancel",
                "_order_cancel",
                "_protection",
                "_settings",
            )
        ):
            raise protocol.ReleaseError("SANDBOX_GET_ONLY_REQUIRED")
        inventory = AccountInventory(
            request,
            scope=scope,
            clock_ms=lambda: time.time_ns() // 1000000,
            combined_account_config=True,
            read_workers=1,
        )
        state["scope"] = scope
        state["policy"] = PolicyStore(managed.database(), scope).read()

        def gate():
            try:
                return flat.verify_flat(
                    connect=managed.database(),
                    inventory=inventory,
                    scope=scope,
                    baseline=baseline,
                    clock_ms=lambda: time.time_ns() // 1000000,
                    route_role="trade_v2_execution_operator",
                )
            except flat.FlatGateError as exc:
                raise protocol.ReleaseError(str(exc)) from None

        return gate

    def runners_stopped():
        for unit in (
            protocol.UNIT,
            "trade-v2-testnet-daemon.service",
            "trade-v2-testnet-watchdog.service",
        ):
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
            props = dict(
                line.split("=", 1) for line in output.splitlines() if "=" in line
            )
            expected = {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
            }
            if any(props.get(key) != value for key, value in expected.items()):
                return False
            group_name = "/system.slice/" + unit
            if props.get("ControlGroup") not in ("", group_name):
                return False
            group = Path("/sys/fs/cgroup") / group_name.lstrip("/")
            if group.is_symlink():
                return False
            if group.exists():
                for path in [group / "cgroup.procs", *group.glob("**/*/cgroup.procs")]:
                    if path.is_symlink() or path.read_text().strip():
                        return False
        return True

    result = orchestrator.execute(
        protocol,
        prepare_gate=prepare_gate,
        runners_stopped=runners_stopped,
        apply=apply,
    )
    if result == 0 and apply:
        policy = PolicyStore(managed.database(), state["scope"]).read()
        unchanged = policy == state["policy"]
        print(
            json.dumps(
                {
                    "event": "POLICY_COMPARISON",
                    "unchanged": unchanged,
                    "version": policy.version,
                }
            ),
            flush=True,
        )
        if not unchanged:
            return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())
