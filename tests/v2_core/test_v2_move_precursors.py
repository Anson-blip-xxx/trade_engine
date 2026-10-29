import json
from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest

from v2_core.move_precursors import MINUTE, analyze_move_precursor

START = 1_800_000_000_000


def bars(*, width=MINUTE):
    baseline = [
        {"o": "100", "h": "105", "l": "95", "c": "100", "v": "10"} for _ in range(3)
    ]
    recent = [
        {"o": "100", "h": "101", "l": "100", "c": "101", "v": "20"},
        {"o": "101", "h": "102", "l": "101", "c": "102", "v": "20"},
        {"o": "102", "h": "103", "l": "102", "c": "103", "v": "20"},
    ]
    return [{"t": START + i * width, **row} for i, row in enumerate(baseline + recent)]


def analyze(rows=None, **kwargs):
    return analyze_move_precursor(
        bars() if rows is None else rows,
        **{
            "symbol": "TESTUSDT",
            "side": "BUY",
            "cutoff_ms": START + 6 * MINUTE,
            "window_bars": 3,
            **kwargs,
        },
    )


def test_equal_closed_windows_observe_contraction_volume_and_direction():
    result = analyze()
    assert result["status"] == "OBSERVED"
    assert result["features"] == {
        "range_ratio": "0.3",
        "volume_ratio": "2",
        "directional_return_pct": "3",
        "favorable_close_step_pct": "100",
        "undefined_ratios": [],
        "volume_unit": "BASE_ASSET",
    }
    assert result["windows"]["baseline"]["end_ms"] == START + 3 * MINUTE
    assert result["windows"]["recent"]["start_ms"] == START + 3 * MINUTE
    assert result["windows"]["recent"]["end_ms"] == START + 6 * MINUTE


def test_future_bar_including_cutoff_open_does_not_change_any_output():
    rows = bars()
    expected = analyze(rows)
    rows.extend(
        [
            {"t": START + 6 * MINUTE, "o": "not known yet", "symbol": "WRONGUSDT"},
            {"t": START + 7 * MINUTE, "h": "1000000000000000000"},
            {"t": START - MINUTE, "c": "invalid outside window"},
        ]
    )
    assert analyze(rows) == expected


def test_later_outcome_cannot_choose_direction_or_modify_input_digest():
    rows = bars()
    saved = deepcopy(rows)
    up, down = analyze(rows), analyze(rows, side="SELL")
    assert up["features"]["range_ratio"] == down["features"]["range_ratio"]
    assert up["features"]["volume_ratio"] == down["features"]["volume_ratio"]
    assert down["features"]["directional_return_pct"] == "-3"
    assert down["features"]["favorable_close_step_pct"] == "0"
    assert up["input_digest"] != down["input_digest"]
    assert rows == saved


def test_sell_has_the_same_positive_directional_features_for_falling_prices():
    rows = bars()
    for index, row in enumerate(rows[3:]):
        opening = 100 - index
        row.update(
            o=str(opening), h=str(opening), l=str(opening - 1), c=str(opening - 1)
        )
    assert analyze(rows, side="SELL")["features"] == analyze()["features"]


def test_hourly_input_keeps_real_interval_and_same_dimensionless_features():
    result = analyze(
        bars(width=60 * MINUTE),
        cutoff_ms=START + 6 * 60 * MINUTE,
        interval_minutes=60,
    )
    assert result["features"] == analyze()["features"]
    assert result["source_interval_minutes"] == 60
    assert result["windows"]["recent"]["start_ms"] == START + 3 * 60 * MINUTE


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "MISSING_OR_PARTIAL_WINDOW"),
        ("duplicate", "DUPLICATE_BAR"),
        ("misaligned", "MISALIGNED_BAR"),
        ("ohlc", "INVALID_OHLC"),
        ("volume", "INVALID_DECIMAL"),
        ("field", "MISSING_BAR_FIELD"),
        ("symbol", "BAR_SYMBOL_MISMATCH"),
        ("source", "BAR_SOURCE_MISMATCH"),
        ("environment", "BAR_SOURCE_MISMATCH"),
    ],
)
def test_bad_source_is_unknown_without_synthesized_features(change, reason):
    rows = bars()
    if change == "missing":
        rows.pop(1)
    elif change == "duplicate":
        rows.append(deepcopy(rows[1]))
    elif change == "misaligned":
        rows[1]["t"] += 1
    elif change == "ohlc":
        rows[1]["h"] = "99"
    elif change == "volume":
        rows[1]["v"] = "-1"
    elif change == "field":
        del rows[1]["c"]
    elif change == "symbol":
        rows[1]["symbol"] = "OTHERUSDT"
    elif change == "source":
        rows[1]["market_source"] = "BINANCE_SPOT_LIVE"
    else:
        rows[1]["environment"] = "SANDBOX"
    result = analyze(rows)
    assert result["status"] == "UNKNOWN" and result["reason"] == reason
    assert result["features"] is None and result["input_digest"] is None
    assert all(h["status"] == "UNKNOWN" for h in result["hypotheses"])


def test_flat_and_zero_volume_baseline_stays_unknown_for_ratios_only():
    rows = bars()
    for row in rows[:3]:
        row.update(h="100", l="100", v="0")
    features = analyze(rows)["features"]
    assert features["range_ratio"] is None and features["volume_ratio"] is None
    assert features["undefined_ratios"] == [
        "ZERO_BASELINE_RANGE_PCT_OF_OPEN",
        "ZERO_BASELINE_BASE_VOLUME",
    ]
    assert features["directional_return_pct"] == "3"


def test_no_spot_inventory_oi_liquidation_or_intent_inferred_from_futures_bars():
    result = analyze()
    hypotheses = {h["id"]: h for h in result["hypotheses"]}
    assert hypotheses["PRICE_VOLUME_STRUCTURE"]["status"] == "DESCRIPTIVE_ONLY"
    for key in (
        "SPOT_DIRECTIONAL_FLOW",
        "LIQUIDITY_ABSORPTION",
        "DERIVATIVES_POSITIONING",
    ):
        assert hypotheses[key]["status"] == "UNKNOWN"
        assert hypotheses[key]["required_evidence"] and hypotheses[key]["falsification"]
    assert not ({"authorized", "signal", "probability", "confidence"} & result.keys())
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cutoff_ms": START + 6 * MINUTE - 1},
        {"interval_minutes": True},
        {"interval_minutes": 2},
        {"window_bars": 1},
        {"window_bars": 241},
        {"side": "LONG"},
        {"market_source": "UNKNOWN"},
        {"cutoff_ms": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError, match="PRECURSOR_"):
        analyze(**kwargs)


@pytest.mark.parametrize("value", [True, 1.2, "NaN", "Infinity", "1e100", "1e-100"])
def test_non_decimal_or_unbounded_numbers_never_enter_evidence(value):
    rows = bars()
    rows[0]["v"] = value
    assert analyze(rows)["status"] == "UNKNOWN"


def test_sort_order_decimal_context_and_equivalent_notation_do_not_change_result():
    rows = bars()
    expected = analyze(rows)
    for row in rows:
        row["v"] = Decimal(row["v"])
        row["o"] = str(Decimal(row["o"])) + ".000"
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        actual = analyze(list(reversed(rows)))
    assert actual == expected


def test_market_source_changes_provenance_without_turning_testnet_into_live():
    result = analyze(market_source="BINANCE_USDM_SANDBOX")
    assert result["market_source"] == "BINANCE_USDM_SANDBOX"
    assert result["input_digest"] != analyze()["input_digest"]
