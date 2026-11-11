"""Exact five-file SANDBOX evidence/lease release; dry-run and flat-only.

The existing lease timeout fix is included. No strategy, exit parameters, order
keys, account bindings or risk limits change. No forced closes are authorized.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/jev-freshness-controller-20261108-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/exit-evidence-controller-20261110-v1")
EXPECTED = {
    "services/v2_directional_exit.py": "68011b84258ccefc288d187db50f237a9adbbec62c23ee19a364018ea97a0e2e",
    "services/v2_directional_exit_market.py": "bfb058d572a1d87a15f101deb62058dfb25fad48bec489f7485609bd7482a5b9",
    "services/v2_exit_research_snapshot.py": "f819606d4e6073fe0393aa37d91494540800f45eab166acb399554c770edc7bd",
    "v2_core/protection.py": "5e133ef7f08a9814fa5f1b784ddb85967e9b6919637a2128f0b495867a8d0216",
    "v2_core/scheduling.py": "d3273142872f728e0cffb0665fd71916ef2b45bc715bf89e6916f8821854294f",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_exit_evidence_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
