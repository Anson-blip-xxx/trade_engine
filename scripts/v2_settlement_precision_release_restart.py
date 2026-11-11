"""Controller-only precision hotfix, using the existing guarded release protocol.

Dry run by default. This does not deploy the unactivated strategy candidate,
change account/environment, skip reconciliation, or restart the market hub.
Run from the repository with ``python3 -m scripts.v2_settlement_precision_release_restart``.
"""

from scripts import v2_profitability_release_restart as protocol

PRIOR = protocol.ROOT / "releases/profitability-foundation-controller-20261026-v1"
CANDIDATE = protocol.ROOT / "releases/settlement-entry-witness-controller-20261029-v1"
IMPORT_PROGRAM = r'''
import importlib
from pathlib import Path
for name in (
    "services.v2_execution_supervisor", "services.v2_directional_cash",
    "services.v2_directional_settlement", "services.v2_settlement_entry",
    "v2_core.settlement_precision",
):
    source = Path(importlib.import_module(name).__file__)
    assert source.resolve().is_relative_to(Path.cwd().resolve())
    compile(source.read_bytes(), str(source), "exec")
from v2_core.trend_runner import TrendRunnerPolicy
assert TrendRunnerPolicy().mode == "OFF"
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
'''


def main():
    protocol.PRIOR = PRIOR
    protocol.CANDIDATE = CANDIDATE
    protocol.IMPORT_PROGRAM = IMPORT_PROGRAM
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
