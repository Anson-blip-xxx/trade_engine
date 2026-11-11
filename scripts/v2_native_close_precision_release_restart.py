"""Controller-only native close precision repair; guarded SANDBOX restart.

Dry run by default. Uses the established graceful stop, manage-only recovery,
same-account readiness and resume protocol. No trade or risk policy overrides.
"""

from scripts import v2_profitability_release_restart as protocol

PRIOR = protocol.ROOT / "releases/mover-symbol-controller-20261102-v1"
CANDIDATE = protocol.ROOT / "releases/native-close-precision-controller-20261105-v1"
IMPORT_PROGRAM = r"""
import importlib
from pathlib import Path
for name in (
    "services.v2_execution_supervisor", "services.v2_directional_cash",
    "services.v2_directional_settlement", "services.v2_settlement_entry",
    "v2_core.settlement_precision", "v2_core.native_entry_precision",
):
    source = Path(importlib.import_module(name).__file__)
    assert source.resolve().is_relative_to(Path.cwd().resolve())
    compile(source.read_bytes(), str(source), "exec")
from v2_core.trend_runner import TrendRunnerPolicy
assert TrendRunnerPolicy().mode == "OFF"
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
"""


def main():
    protocol.PRIOR = PRIOR
    protocol.CANDIDATE = CANDIDATE
    protocol.IMPORT_PROGRAM = IMPORT_PROGRAM
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
