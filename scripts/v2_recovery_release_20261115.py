"""Reviewed SANDBOX controller recovery release; dry run unless --execute.

Preserves positions, binding and policy; resumes only after fresh manage-only
account coverage. Uses the previously tested graceful restart protocol.
"""

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_recovery_restart_helpers",
    Path(__file__).resolve().with_name("v2_venue_cash_managed_restart_20261114.py"),
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
compatible_admin = _helpers.compatible_admin
guarded_execute = _helpers.guarded_execute
load = _helpers.load
runners_stopped = _helpers.runners_stopped

PRIOR = Path("/opt/trade-engine-v2/releases/venue-cash-controller-20261114-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/recovery-controller-20261115-v1")
# Filled from the exact candidate after its independent regression run.
EXPECTED = {
    "services/v2_directional_context.py": "59993ec0f48e918bf8f87ffe65352373bafa1f83884725aba8497c4161784217",
    "services/v2_signal_maintenance.py": "675eb2f484e98e175e5fe6b16393657b03968efd255aad5f1ab2ac21e6b0c539",
    "v2_core/binance.py": "c3549783e582b376a67252d4626674996cefffa5d3fb4957ed2e03994a098063",
    "v2_core/runner.py": "cbda07704c190efe6d06461fc9dd17906c613f3d78cb0f3563f4951bd0b41aa5",
    "v2_core/scheduling.py": "73d95fd0ea07ba857c3ecaf87a4252d64dedc28dfb78bccd615b331484ac332e",
}
IMPORT_PROGRAM = r"""
import importlib
from pathlib import Path
root = Path.cwd().resolve()
for name in ('services.v2_execution_supervisor', 'services.v2_managed_testnet',
             'services.v2_directional_context', 'services.v2_signal_maintenance',
             'v2_core.binance', 'v2_core.runner', 'v2_core.scheduling'):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(root)
print('IMPORTS_AND_SAFE_DEFAULTS_VERIFIED')
"""


def reviewed_manifest(reader):
    before, after = reader.manifest(PRIOR), reader.manifest(CANDIDATE)
    changed = {key: after[key] for key in after if after[key] != before.get(key)}
    if not EXPECTED or changed != EXPECTED or set(before) - set(after):
        raise ValueError("EXACT_TESTED_MANIFEST_REQUIRED")


def main():
    apply = sys.argv[1:] == ["--execute"]
    if sys.argv[1:] and not apply:
        raise ValueError("EXPECTED_DRY_RUN_OR_EXECUTE")
    if (
        os.environ.get("V2_SHARED_MARKET") != "true"
        or os.environ.get("CREDENTIALS_DIRECTORY")
        != "/run/credentials/trade-v2-execution-controller.service"
    ):
        raise ValueError("EXISTING_SHARED_CONTROLLER_ENVIRONMENT_REQUIRED")
    sys.dont_write_bytecode = True
    reader = load("_recovery_manifest", "v2_jev_freshness_release_20261108.py")
    activation = load("_recovery_parser", "v2_venue_cash_activate_20261114.py")
    protocol = load("_recovery_protocol", "v2_profitability_release_restart.py")
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.IMPORT_PROGRAM = IMPORT_PROGRAM

    def readers_check():
        reviewed_manifest(reader)
        compatible_admin(activation)
        if (
            hashlib.sha256(
                (CANDIDATE / "v2_core/runtime_policy.py").read_bytes()
            ).hexdigest()
            != activation.POLICY_HASH
        ):
            raise ValueError("UNCHANGED_POLICY_PARSER_REQUIRED")

    readers_check()
    activation.running(protocol.UNIT, PRIOR)
    sys.path.insert(0, str(CANDIDATE))
    from services import v2_managed_testnet as managed
    from v2_core import runtime_policy

    for module in (managed, runtime_policy):
        if not Path(module.__file__).resolve().is_relative_to(CANDIDATE):
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
        if current.version <= 0 or current.values["settlement.cash_basis_version"] != 2:
            raise protocol.ReleaseError("EXISTING_VENUE_CASH_POLICY_REQUIRED")
        if not state:
            state.update(scope=scope, policy=current)
        if scope != state["scope"] or current != state["policy"]:
            raise protocol.ReleaseError("POLICY_CHANGED_DURING_RELEASE")

    return guarded_execute(
        protocol,
        policy_check=policy_check,
        readers_check=readers_check,
        candidate_check=lambda: activation.running(protocol.UNIT, CANDIDATE),
        stopped=lambda: runners_stopped(protocol),
        apply=apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
