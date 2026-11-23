"""Bounded queue observation release; preserve SANDBOX policy and positions.

Default is a read-only dry run. --execute uses the established graceful stop,
manage-only coverage check, and same-binding resume protocol. No risk changes,
synthetic orders, position closes, history deletion or LIVE switch.
"""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/sampling-shadow-controller-20261119-v2")
CANDIDATE = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v1")
EXPECTED = {
    "services/v2_trading_health.py": "3dbc3bc9aec9498648667ad4e229755a4d67163f0631328edf2952c5464b517c",
    "services/v2_signal_queue_health.py": "cbe4bac8c777d1ff7456969f891eccb1382b4e2a084bc6dbfdac0ad993213cb9",
    "v2_core/telegram.py": "3808969af9508c7b5731aeb1eba4d0d19185dac33d0d38ef63253f7c4254ee6e",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_queue_health_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_trading_health', 'services.v2_signal_queue_health', "
        "'v2_core.telegram', 'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
