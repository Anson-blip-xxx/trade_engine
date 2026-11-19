"""Exact SANDBOX shadow-observer release using the managed restart protocol.

Only the reviewed four-file artifact may be switched. Budget CAS is separate;
the restart preserves current policy, native protection and account binding.
"""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/recovery-controller-20261115-v3")
CANDIDATE = Path("/opt/trade-engine-v2/releases/sampling-shadow-controller-20261119-v1")
EXPECTED = {
    "services/v2_directional_admission.py": "6b789def5421a7239b2a1d96cccf0ba85bb0bd9ffdab69ee0bac0b9a7778b0b0",
    "services/v2_jev_shadow.py": "161cb787b44938d6d2bd686b2f52220a5de2ea0716fb9f06b742c8e2896c9902",
    "services/v2_testnet_daemon_entry.py": "66debe95e0e927a6cf2f8bfec7a5d72772a0d49d90aa309f1a2d0cf95478482e",
    "v2_core/strategy.py": "7b42719608d6c3ba14bd7d6ac89c366feecc347e7bd854efd64faa547e8e7e12",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_sampling_shadow_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_jev_shadow', 'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
