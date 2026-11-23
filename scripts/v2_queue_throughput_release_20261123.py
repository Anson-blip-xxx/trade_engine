"""Reviewed SANDBOX throughput patch, preserving policies and native stops.

Dry run by default. The existing graceful-stop/manage-only/same-binding resume
protocol owns deployment. This only changes bounded observation, serialization
work and redistribution inside the existing cooperative evaluation budget.
"""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v2")
EXPECTED = {
    "v2_core/evidence.py": "22812346257f82d574139e83fafc2d2b3bd6590b6fd560b4b2156069a116e04d",
    "services/v2_trading_pipeline.py": "f8ca4b6fb1169f81b843358d8dd8f264edf3a92806df4e682169ae722cc99ac2",
    "services/v2_signal_queue_health.py": "d4cd90e15390edf2b8d7f4e0c065f437161f3194f16f5e10f29ebe1f11e7ad53",
    "services/v2_trading_health.py": "5418acca88003093d2e6b2f5e5332783c35f92b021b343b1c1bb758f5ed74845",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_queue_throughput_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_trading_health', 'services.v2_signal_queue_health', "
        "'services.v2_trading_pipeline', 'v2_core.evidence', "
        "'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
