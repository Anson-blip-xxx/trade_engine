"""Exact minimal SANDBOX controller artifact; flat-only, dry run by default.

Policy activation and read-only web components are separately verified steps.
Unreleased quant wiring from the workspace is deliberately not in this artifact.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path(
    "/opt/trade-engine-v2/releases/reported-boundary-cash-controller-20261114-v1"
)
CANDIDATE = Path("/opt/trade-engine-v2/releases/venue-cash-controller-20261114-v1")
EXPECTED = {
    "services/v2_settlement_entry.py": "cbbaaba97d1e21a4a473e20f519cb38c68895c8ff10634f0c994ccd7ed6d9ef3",
    "services/v2_directional_cash.py": "562b2fc5f467b1a9478d3a80ec7a9fd8d965219ca4c7d82b715a061b8538762d",
    "services/v2_directional_settlement.py": "d8e48b540360b7908171c1c69f26882f7d194147f8823987aa39f66ad8c60432",
    "services/v2_performance_reporting.py": "055dbbba7f46556a09bb5614afd144a5b8fd3bac22749c5d672b546028457811",
    "services/v2_trading_health.py": "2949582528a0625c50fa3749c3713a4f241271d71e2e2e28c1b76ae2b8e94842",
    "services/v2_trading_pipeline.py": "8244aa7a202acd1d74921e7701152c7aadf5f926a840bf64c59cab6fd926b03e",
    "v2_core/cash_adjustment_basis.py": "6d418326642dcc61339fcefbc4024ac7b2ddf8e8920906b2152df0b2b4ab1168",
    "v2_core/runtime_policy.py": "7dfd0793c919651af6cbce554449984876598ea9e6458867764e141493a4d605",
    "v2_core/settlement_cash_basis.py": "e092001b5547ffb570502fb588d4e34b89b93e971e5436e054ae26012fb2a527",
    "v2_core/telegram.py": "d5dd2b7005b449d64ea8be847325c23d0751cd93f5b43ffd6ec646573306907f",
    "v2_core/ledger.py": "ddc34a53c4f97d284cec37cbf0d2ec59a7e7ef8315ab64769e150ba0be71b5a8",
    "v2_core/venue_cash.py": "0beb1a78e24315bc5b9a46d18ac8916f02a53974fb44cc729cdb62b7f5eed3c7",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_venue_cash_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
