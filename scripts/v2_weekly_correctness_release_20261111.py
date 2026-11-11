"""Exact three-file SANDBOX JEV veto release; flat-only, dry-run by default.

Reuses the reviewed stop/recheck/same-binding recovery protocol. No threshold,
budget, order permission or expiry changes. Read-only reports deploy separately.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/ready-queue-controller-20261110-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/weekly-correctness-controller-20261111-v1")
EXPECTED = {
    "services/v2_jev_entry_context.py": "1153899124dad7051d9602b97fb7a6c071f6440a78db0ebfc8c448ba560f6488",
    "v2_core/strategy.py": "4792ac57caed7408b40f3b0ed2e64370523083029bb5fa79da79393fe017400f",
    "v2_core/scheduling.py": "1a46854a40c5c0c29177254fce2fa8ab4b285215af722381b2571494d2424a51",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_weekly_correctness_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
