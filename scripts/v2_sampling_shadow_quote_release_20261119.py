"""Exact shadow-only dual-clock correction; preserve policy and held positions."""

import importlib.util
from pathlib import Path

PRIOR = Path("/opt/trade-engine-v2/releases/sampling-shadow-controller-20261119-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/sampling-shadow-controller-20261119-v2")
EXPECTED = {
    "services/v2_jev_shadow.py": "088eb52cc3dcbe9eb8e98e0ec1a6c2f7611d0da9dd2d42f3325b6fc8ccb42009",
    "v2_core/jev_advisory.py": "f728d71c5ffd66c24c36f8f6899c1bd1315a2022e0d8d6ece91c29b9b83b64ab",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_sampling_shadow_quote_restart_protocol",
        Path(__file__).resolve().with_name("v2_recovery_release_20261115.py"),
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.PRIOR, protocol.CANDIDATE = PRIOR, CANDIDATE
    protocol.EXPECTED = EXPECTED
    protocol.IMPORT_PROGRAM = protocol.IMPORT_PROGRAM.replace(
        "'services.v2_directional_context',",
        "'services.v2_jev_shadow', 'v2_core.jev_advisory', 'services.v2_directional_context',",
    )
    return protocol.main()


if __name__ == "__main__":
    raise SystemExit(main())
