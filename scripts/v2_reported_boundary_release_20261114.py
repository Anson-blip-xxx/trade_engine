"""Two-file cash-proof release; SANDBOX, flat-only, dry run by default.

Original fills, income identity, final wallet equality and risk policy remain
unchanged. A reported cash boundary is not proof of venue arithmetic.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/exit-evidence-controller-20261113-v1")
CANDIDATE = Path(
    "/opt/trade-engine-v2/releases/reported-boundary-cash-controller-20261114-v1"
)
EXPECTED = {
    "services/v2_directional_cash.py": "4941261b62552bcd4b80a57d8e2a115f850e9f7bb062d323e6d15d74c4061206",
    "v2_core/settlement_precision.py": "09346595560fda1fb6f2ed6e58741a69798abfd35fda405dbbe75c2149956474",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_boundary_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
