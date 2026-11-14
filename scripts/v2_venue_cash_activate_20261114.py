"""Explicit post-release SANDBOX cash-basis activation; dry run by default.

Only the existing account's basis selector changes. No venue call, cash write,
trade, ledger rewrite or risk-limit relaxation is performed. Reverting the
selector requires retaining the compatible policy parser in every reader and
the venue-cash ledger reader for already committed versioned settlements. A
selector rollback does not convert those facts into legacy cash corrections.
"""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = Path("/opt/trade-engine-v2/releases/venue-cash-controller-20261114-v1")
ADMIN = Path("/opt/trade-engine-v2/releases/venue-cash-admin-20261114-v1")
POLICY_HASH = "7dfd0793c919651af6cbce554449984876598ea9e6458867764e141493a4d605"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def reviewed_controller():
    """Require the same full, minimal artifact as the flat-release protocol."""
    release = load("_cash_activation_manifest", "v2_venue_cash_release_20261114.py")
    reader = load(
        "_cash_activation_manifest_reader", "v2_jev_freshness_release_20261108.py"
    )
    if release.CANDIDATE != ROOT:
        raise ValueError("REVIEWED_CONTROLLER_PATH_REQUIRED")
    before, after = reader.manifest(release.PRIOR), reader.manifest(ROOT)
    changed = {key: after[key] for key in after if after[key] != before.get(key)}
    if changed != release.EXPECTED or set(before) - set(after):
        raise ValueError("EXACT_TESTED_MANIFEST_REQUIRED")


def running(unit, expected):
    """Inspect another service user's cwd without reading its environment."""
    if unit not in {
        "trade-v2-execution-controller.service",
        "trade-v2-account-admin.service",
    }:
        raise ValueError("COMPATIBLE_RUNNING_RELEASE_REQUIRED")
    command = ["systemctl", "show", unit, "-p", "MainPID", "--value"]
    try:
        pid = subprocess.check_output(
            command, text=True, timeout=10, stderr=subprocess.DEVNULL
        ).strip()
        if not pid.isascii() or not pid.isdecimal() or not 1 < int(pid) < 2**31:
            raise ValueError
        cwd = subprocess.check_output(
            ["sudo", "-n", "readlink", "-f", "/proc/" + pid + "/cwd"],
            text=True,
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).strip()
        # A restart while inspecting /proc invalidates the observation.
        after = subprocess.check_output(
            command, text=True, timeout=10, stderr=subprocess.DEVNULL
        ).strip()
        if cwd != str(expected) or after != pid:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("COMPATIBLE_RUNNING_RELEASE_REQUIRED") from None


def main():
    execute = sys.argv[1:] == ["--execute"]
    if sys.argv[1:] and not execute:
        raise ValueError("EXPECTED_DRY_RUN_OR_EXECUTE")
    sys.dont_write_bytecode = True
    reviewed_controller()
    for link, release, unit in (
        ("execution-controller", ROOT, "trade-v2-execution-controller.service"),
        ("account-admin", ADMIN, "trade-v2-account-admin.service"),
    ):
        if Path("/opt/trade-engine-v2", link).resolve() != release:
            raise ValueError("COMPATIBLE_POLICY_READERS_REQUIRED")
        if (
            hashlib.sha256(
                (release / "v2_core/runtime_policy.py").read_bytes()
            ).hexdigest()
            != POLICY_HASH
        ):
            raise ValueError("EXACT_POLICY_PARSER_REQUIRED")
        running(unit, release)
    sys.path.insert(0, str(ROOT))
    from services import v2_managed_testnet as managed
    from v2_core import runtime_policy

    for module in (managed, runtime_policy):
        if not Path(module.__file__).resolve().is_relative_to(ROOT):
            raise ValueError("CANDIDATE_IMPORT_ORIGIN_REQUIRED")
    PolicyStore = runtime_policy.PolicyStore
    protocol = load(
        "_cash_activation_protocol",
        "v2_profitability_release_restart.py",
    )
    protocol.CANDIDATE = ROOT
    route = protocol.database("capture")
    target = {
        "target_registry": route["registry"],
        "binding_version": route["binding"],
        "epoch": route["epoch"],
    }
    scope = managed.scope_for(route["tenant"], target)
    if scope.environment != "SANDBOX":
        raise ValueError("SANDBOX_ONLY")
    store = PolicyStore(managed.database(), scope)
    before = store.read()
    desired = {**before.values, "settlement.cash_basis_version": 2}
    if (
        desired["settlement.max_difference_per_fill_usdt"] != "0.000001"
        or desired["settlement.max_difference_episode_usdt"] != "0.0001"
    ):
        raise ValueError("REVIEWED_ANOMALY_LIMITS_REQUIRED")
    if not execute:
        print(
            json.dumps(
                {
                    "event": "CASH_BASIS_ACTIVATION_PREFLIGHT",
                    "policy_version": before.version,
                    "from": before.values["settlement.cash_basis_version"],
                    "to": 2,
                    "writes": False,
                }
            )
        )
        return 0
    if protocol.database("capture") != route:
        raise ValueError("ROUTE_CHANGED")
    after = (
        before
        if before.values == desired
        else store.patch(
            {"settlement.cash_basis_version": 2},
            expected_version=before.version,
            reason="VERIFIED_VENUE_CASH_BASIS_20261114",
        )
    )
    if after.values != desired or store.read() != after:
        raise ValueError("POLICY_VERIFICATION_FAILED")
    print(
        json.dumps(
            {
                "event": "CASH_BASIS_ACTIVATED",
                "policy_version": after.version,
                "cash_basis_version": 2,
                "other_settings_unchanged": True,
                "wallet_tolerance": "0",
                "ledger_rewrite": False,
                "venue_calls": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
