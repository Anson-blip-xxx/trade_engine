"""Keep new terminal entry checks understandable in the daily review."""

import pytest

from services.v2_daily_mover_report import _REASON_TEXT
from services.v2_directional_replay import MARKET_ONLY_REJECTIONS


@pytest.mark.parametrize(
    "reason,label",
    [
        ("TREND_BREAKOUT_BODY_INVALIDATED", "突破确认实体已被回吐"),
        ("TREND_BREAKOUT_PRICE_UNCONFIRMED", "突破现价证据未确认"),
    ],
)
def test_entry_quality_rejections_have_explicit_review_labels(reason, label):
    assert reason in MARKET_ONLY_REJECTIONS
    assert _REASON_TEXT[reason] == label
