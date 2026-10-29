import json
from pathlib import Path

import pytest
from test_v2_indicator_contract_v3 import minutes

from services.v2_s3_candles import (
    BREAKOUT_VERSION,
    PRICE_VERSION,
    ROLLING_VERSION,
    VERSION,
    build_frame,
)
from services.v2_s3_runtime import evaluate


def expanding_batch():
    rows = minutes(1440)
    for row in rows[-14:]:
        row.update(h="110", l="90")
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": 86400000,
        "candles": {"BTCUSDT": list(reversed(rows))},
    }


@pytest.mark.parametrize(
    "version,expected_event",
    [
        (VERSION, True),
        (PRICE_VERSION, True),
        (ROLLING_VERSION, False),
        (BREAKOUT_VERSION, False),
    ],
)
def test_latest_minute_atr_explicitly_disables_legacy_cross_window_expansion(
    version, expected_event
):
    # A genuine sudden increase: 1426 minutes of TR=2, then 14 of TR=20.
    # Frozen v1/v2 use the first 14 TRs inside each lookback; v3/v4 all use
    # the same latest 14 TRs. This is a release limitation, not a new rule.
    frame = build_frame(
        expanding_batch(), environment="SANDBOX", feature_version=version
    )
    windows = frame["windows"]["BTCUSDT"]
    if version in (ROLLING_VERSION, BREAKOUT_VERSION):
        assert {windows[name]["atr"] for name in ("15m", "1h", "4h", "24h")} == {"20"}
        assert {windows[name]["atr_pct"] for name in ("15m", "1h", "4h", "24h")} == {
            "20"
        }
    else:
        assert float(windows["15m"]["atr_pct"]) == 20
        assert float(windows["1h"]["atr_pct"]) == 2
    events, _ = evaluate(
        frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
    )
    assert any(event["type"] == "ATR_EXPAND" for event in events) is expected_event


def test_candidate_acceptance_records_unresolved_release_constraint():
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy/v2-testnet/indicator-v4-candidate-acceptance-20261028.json"
    )
    report = json.loads(path.read_text())
    assert report["candidate_feature_version"] == BREAKOUT_VERSION
    assert report["release_eligible"] is False
    assert report["new_default_feature_version_enabled"] is False
    assert report["deployed_to_execution"] is False
    restriction = next(
        item
        for item in report["release_restrictions"]
        if item["code"] == "ATR_EXPAND_CROSS_WINDOW_ATR_INCOMPATIBLE"
    )
    assert restriction["status"] == "UNRESOLVED"
    assert restriction["blocks_activation"] is True
    assert restriction["affected_versions"] == [ROLLING_VERSION, BREAKOUT_VERSION]
    assert restriction["observed_fixture"]["s3-closed-1m-v2"]["ATR_EXPAND"] is True
    assert restriction["observed_fixture"]["s3-closed-1m-v4"]["ATR_EXPAND"] is False
