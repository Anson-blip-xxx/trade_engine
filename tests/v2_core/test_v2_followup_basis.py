"""Final-flat market return is not the VWAP of previous partial reductions."""

from decimal import Decimal
from pathlib import Path

import pytest

from services.v2_trade_path_review import post_flat_path
from services.v2_trade_review import build_review
from v2_core.directional_outcomes import LEGACY_FOLLOWUP_BASIS
from v2_core.quant_indicators import MINUTE, Bar


def bar(t, close="100", high=None, low=None):
    return Bar(t, close, high or close, low or close, close, "1")


def test_legacy_followup_is_labelled_without_reinterpreting_or_rewriting_it():
    original = {
        "id": "a",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "source": "s3",
        "decided_ms": 0,
        "opened_ms": 1,
        "closed_ms": 1000,
        "close_known_ms": 1001,
        "net_pnl": "1",
        "maintenance": False,
        "followup_60_return_pct": "20",
    }
    row = build_review([original], start_ms=0, now_ms=4000000)["trades"][0]
    assert row["followup_60_return_pct"] == "20"
    assert row["followup_60_basis"] == LEGACY_FOLLOWUP_BASIS
    assert row["post_flat_60_market_return_pct"] is None
    assert "followup_60_basis" not in original


def test_first_full_minute_anchor_omits_flat_boundary_and_anchor_intrabar_extremes():
    rows = [
        bar(MINUTE, "100", "300", "20"),
        bar(2 * MINUTE, "110", "120", "90"),
        bar(3 * MINUTE, "105"),
    ]
    result = post_flat_path(
        rows, side="BUY", closed_ms=1000, as_of_ms=4 * MINUTE, horizon_minutes=3
    )
    assert result["status"] == "OBSERVED"
    assert result["basis"] == "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE"
    assert result["anchor_ms"] == 2 * MINUTE
    assert result["anchor_delay_ms"] == 119000
    assert Decimal(result["return_pct"]) == 5
    assert Decimal(result["favorable_excursion_pct"]) == 20
    assert Decimal(result["adverse_excursion_pct"]) == -10
    assert result["horizon_delay_ms"] == 59000
    assert result["market_return_not_trade_pnl"] and not result["costs_included"]


def test_sell_direction_and_exact_timed_mark():
    anchor = {
        "kind": "MARKET_MARK",
        "t": MINUTE,
        "price": "100",
        "observed_ms": MINUTE + 10,
        "source": "exchange-mark-event",
    }
    rows = [bar(MINUTE, "90", "110", "80"), bar(2 * MINUTE, "85")]
    result = post_flat_path(
        rows,
        side="SELL",
        closed_ms=MINUTE,
        as_of_ms=3 * MINUTE,
        horizon_minutes=2,
        anchor=anchor,
    )
    assert result["status"] == "OBSERVED"
    assert result["basis"] == "EXPLICIT_TIMED_MARK_AFTER_FLAT"
    assert Decimal(result["return_pct"]) == 15
    assert Decimal(result["favorable_excursion_pct"]) == 20
    assert Decimal(result["adverse_excursion_pct"]) == -10


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "ALL_CLOSE_FILL_VWAP"},
        {"price": "NaN"},
        {"price": "0"},
        {"t": 59999},
        {"t": 180001},
        {"observed_ms": 59999},
        {"observed_ms": 300001},
        {"source": ""},
        {"t": True},
    ],
)
def test_invalid_or_future_anchor_is_unknown(changes):
    anchor = {
        "kind": "MARKET_MARK",
        "t": MINUTE,
        "price": "100",
        "observed_ms": MINUTE + 10,
        "source": "exchange-mark",
        **changes,
    }
    result = post_flat_path(
        [bar(MINUTE), bar(2 * MINUTE)],
        side="BUY",
        closed_ms=MINUTE,
        as_of_ms=300000,
        horizon_minutes=2,
        anchor=anchor,
    )
    assert result["status"] == "INVALID_ANCHOR_OR_PATH"
    assert result["return_pct"] is None


@pytest.mark.parametrize(
    "times",
    [[], [MINUTE], [MINUTE, MINUTE], [2 * MINUTE, MINUTE], [0, MINUTE, 2 * MINUTE]],
)
def test_missing_duplicate_or_outside_path_is_not_filled(times):
    result = post_flat_path(
        [bar(t) for t in times],
        side="BUY",
        closed_ms=MINUTE,
        as_of_ms=3 * MINUTE,
        horizon_minutes=2,
    )
    assert result["status"] == "INCOMPLETE_OR_INVALID_MINUTE_PATH"
    assert result["return_pct"] is None


def test_unobserved_horizon_and_one_bar_no_post_anchor_remain_unknown():
    assert (
        post_flat_path(
            [], side="BUY", closed_ms=MINUTE, as_of_ms=2 * MINUTE, horizon_minutes=2
        )["status"]
        == "HORIZON_NOT_YET_OBSERVABLE"
    )
    assert (
        post_flat_path(
            [bar(MINUTE)],
            side="BUY",
            closed_ms=MINUTE,
            as_of_ms=2 * MINUTE,
            horizon_minutes=1,
        )["status"]
        == "NO_POST_ANCHOR_OBSERVATION"
    )


def test_bad_parameters_fail_explicitly():
    with pytest.raises(ValueError, match="PARAMETERS"):
        post_flat_path([], side="OTHER", closed_ms=0, as_of_ms=1)


def test_web_followup_cannot_claim_post_flat_or_sell_too_early_return():
    source = (Path(__file__).resolve().parents[2] / "web/v2_strategy/app.js").read_text()
    assert "T+60 相对全部平仓成交均价" in source
    assert "非清仓后收益，不能据此判断卖飞" in source
    assert 't.followup_60_basis !== "ALL_CLOSE_FILL_VWAP_NOT_FINAL_FLAT_MARK"' in source
    assert "观测口径未知，不展示收益" in source
    assert "平仓后 60 分钟方向涨幅" not in source
