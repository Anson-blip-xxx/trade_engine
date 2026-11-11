"""Exact one-file SANDBOX lease-read fix; flat-only and dry-run by default.

Reuse the audited GET-only inventory, graceful drain, second flat check and
same-binding recovery protocol. Never force-close positions or weaken policy.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/jev-freshness-controller-20261108-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/jev-lease-controller-20261109-v1")
EXPECTED = {
    "v2_core/scheduling.py": "d3273142872f728e0cffb0665fd71916ef2b45bc715bf89e6916f8821854294f",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_jev_lease_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
