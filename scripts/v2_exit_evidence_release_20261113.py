"""Exact nine-file exit evidence release; SANDBOX, flat-only, default dry run.

Uses the previously tested stop/recheck/same-binding recovery protocol. This
release does not change exit rules, entry thresholds, leverage or model gates.
The campaign research reader is already compatible and remains untouched.
Includes the compatible offline JEV report module, not a new scheduled job.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/weekly-correctness-controller-20261111-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/exit-evidence-controller-20261113-v1")
EXPECTED = {
    "services/v2_integration_effectiveness.py": "91ba7bf5df76f20a0adb5f5cb802195b1305d22b64e36721356fbf96c3bffc15",
    "services/v2_exit_research_snapshot.py": "f819606d4e6073fe0393aa37d91494540800f45eab166acb399554c770edc7bd",
    "services/v2_jev_entry_context.py": "6bf849a0cef5375b54eebc05634906ccc2445cbf5d8871ffbbdc23295a19f94c",
    "services/v2_directional_exit.py": "f108dfd821e4a7732c8e866ee7aa018808a3cac729d653f1cc99f7f07500b9e1",
    "services/v2_directional_exit_market.py": "26763b5336ab1f2e25bdb3521f05dc030851842b1699acae362e74703a755ce3",
    "v2_core/account_coverage.py": "a44362a043b69964642186b085f4d0b9ba055970bd9f310b341cf09c9ccf22de",
    "v2_core/strategy_observations.py": "3051f3a61e657f2b7e257014037ad724719f28957a2a77b6be42e09114c4c6c4",
    "v2_core/public_market.py": "5bae5a3dbc30aa739629a5cd18c6520e7b3b1170d1ea499fd2834e017d8ed212",
    "v2_core/protection.py": "5e133ef7f08a9814fa5f1b784ddb85967e9b6919637a2128f0b495867a8d0216",
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
