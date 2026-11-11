"""Do not reintroduce retired, side-effectful V1 utilities into the V2 tree.

These are absence checks only: never import old broadcast or auto-push scripts.
Restoring one intentionally requires a new lifecycle/security review.
"""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "relative",
    ("services/s0/market_state_report.py", "scripts/daily_push.sh"),
)
def test_retired_side_effectful_entrypoint_is_not_shipped(relative):
    assert not (Path(__file__).resolve().parents[2] / relative).exists()
