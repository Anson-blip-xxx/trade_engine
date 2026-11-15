"""Exact SANDBOX health-timer hotfix using the reviewed managed restart path."""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/recovery-controller-20261115-v2")
CANDIDATE = Path("/opt/trade-engine-v2/releases/recovery-controller-20261115-v3")
# Exact artifact: 479 isolated regressions, including delayed-query clocks.
EXPECTED = {
    "services/v2_trading_health.py": "e59973b68e66f9c2072b2fd61d0017fad58abd2b3283fadf9bff01a1cbcaef84",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_health_lane_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_trading_health', 'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
