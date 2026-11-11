"""Cross-package research integration; excluded from execution-only candidates."""

import pytest

from services.v2_exit_research_snapshot import build_exit_research_snapshot
from tests.v2_core.test_v2_exit_research_snapshot import inputs
from v2_core.trend_runner import StagnationShadowPolicy, stagnation_shadow


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("raw_rows", [False, True])
def test_collected_snapshot_is_consumable_by_research_shadow(side, raw_rows):
    values = inputs(side, raw_rows=raw_rows)
    result = build_exit_research_snapshot(**values)
    compared = stagnation_shadow(
        {"opened_at_ms": values["opened_at_ms"], "snapshots": [result["snapshot"]]},
        policy=StagnationShadowPolicy(mode="SHADOW"),
    )
    assert compared["status"] == "SHADOW_ELIGIBLE_PREFIX"
    assert compared["baseline_code_identity_proven"] is False
    assert compared["execution_authorized"] is compared["promotion_authorized"] is False


def test_research_shadow_still_refuses_legacy_twenty_hour_warmup():
    values = inputs(raw_rows=True)
    values["hourly"]["rows"] = values["hourly"]["rows"][-20:]
    result = build_exit_research_snapshot(**values)
    compared = stagnation_shadow(
        {"opened_at_ms": values["opened_at_ms"], "snapshots": [result["snapshot"]]},
        policy=StagnationShadowPolicy(mode="SHADOW"),
    )
    assert compared["status"] == "UNAVAILABLE"
    assert compared["reason"] == "HOURLY_TREND_WARMUP_OR_STALE"
