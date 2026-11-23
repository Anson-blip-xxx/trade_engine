"""Narrow SANDBOX follow-up: expected local waits do not waste peer budget.

Uses the exact-manifest graceful restart protocol; no policy or order changes.
Dry run by default, explicit --execute required for the managed deployment.
"""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v2")
CANDIDATE = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v3")
EXPECTED = {
    "services/v2_trading_pipeline.py": "35f80c92e407cc027636944b6b235848745be13b0e34c1f48aa5e10f501b803c",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_queue_reclaim_wait_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_trading_pipeline', 'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
