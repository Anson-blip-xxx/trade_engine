import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, Inexact, localcontext
from zoneinfo import ZoneInfo

import pytest

from v2_core.daily_movers import (
    DAY,
    MINUTE,
    analyze_intraday_path,
    previous_day_window,
    rank_daily_movers,
    summarize_day,
)

START = int(datetime(2026, 10, 25, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()) * 1000
END = START + DAY
WINDOW = {"start_ms": START, "end_ms": END, "as_of_ms": END}


def bars(*, interval=1, opening="100", closing=None):
    closing = closing or opening
    low, high = (
        min(Decimal(opening), Decimal(closing)),
        max(Decimal(opening), Decimal(closing)),
    )
    rows = [
        {
            "t": START + i * MINUTE * interval,
            "o": opening,
            "h": str(high),
            "l": str(low),
            "c": opening,
            "v": "1",
        }
        for i in range(1440 // interval)
    ]
    rows[-1]["c"] = closing
    return rows


def summary(symbol="BTCUSDT", opening="100", closing="120"):
    return summarize_day(
        bars(interval=60, opening=opening, closing=closing),
        symbol=symbol,
        interval_minutes=60,
        **WINDOW,
    )


def universe(*symbols):
    return [
        {
            "symbol": symbol,
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": START - DAY,
            "deliveryDate": END + DAY * 10,
        }
        for symbol in symbols
    ]


def rank(items, summaries, **kwargs):
    return rank_daily_movers(items, summaries, **{**WINDOW, **kwargs})


def path(rows, **kwargs):
    return analyze_intraday_path(rows, **{**WINDOW, **kwargs})


@pytest.mark.parametrize("hour,minute", [(0, 0), (0, 1), (12, 23), (23, 59)])
def test_previous_complete_utc8_day_never_rolling(hour, minute):
    now = END + (hour * 60 + minute) * MINUTE
    assert previous_day_window(now) == {
        "date": "2026-10-25",
        "timezone": "Asia/Shanghai",
        "start_ms": START,
        "end_ms": END,
    }


@pytest.mark.parametrize("interval", [1, 5, 15, 30, 60])
def test_whole_day_summary_validates_each_supported_closed_interval(interval):
    rows = bars(interval=interval, closing="125")
    result = summarize_day(rows, symbol="BTCUSDT", interval_minutes=interval, **WINDOW)
    assert result["complete"] is True
    assert result["source_count"] == 1440 // interval
    assert result["o"] == "100" and result["c"] == "125"
    assert result["v"] == str(1440 // interval)


def test_utc_day_is_not_the_requested_civil_day():
    with pytest.raises(ValueError, match="NOT_UTC8_CIVIL_DAY"):
        summarize_day(
            bars(),
            symbol="BTCUSDT",
            start_ms=START + 8 * 60 * MINUTE,
            end_ms=END + 8 * 60 * MINUTE,
            as_of_ms=END + DAY,
        )


def test_current_day_is_never_ranked_or_given_path_features():
    result = rank(universe("BTCUSDT"), [summary()], as_of_ms=END - 1)
    assert result["status"] == "UNKNOWN" and result["winners"] == []
    assert path(bars(), as_of_ms=END - 1)["features"] is None


def test_deterministic_exact_top_bottom_with_zero_exclusion_and_short_lists():
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "ZEROUSDT")
    summaries = [
        summary(s, closing=c)
        for s, c in zip(symbols, ("120", "120", "80", "80", "100"), strict=True)
    ]
    result = rank(universe(*reversed(symbols)), list(reversed(summaries)))
    assert [r["symbol"] for r in result["winners"]] == ["AAAUSDT", "BBBUSDT"]
    assert [r["symbol"] for r in result["losers"]] == ["CCCUSDT", "DDDUSDT"]
    assert result["coverage"]["zero_return"] == 1
    assert result["winners"][0]["return_pct"] == "20"
    assert result["losers"][0]["return_pct"] == "-20"
    assert result["universe_basis"] == "CURRENT_DISCOVERY_SURVIVORSHIP_LIMITED"
    assert result["status"] == "PARTIAL_COVERAGE"


def test_top_ten_both_directions_and_exact_tiny_price_ranking():
    symbols = [f"COIN{i:02}USDT" for i in range(30)]
    summaries = [
        summary(
            s,
            opening="0.00000000000000001",
            closing=str(Decimal("0.00000000000000001") * Decimal(85 + i) / 100),
        )
        for i, s in enumerate(symbols)
    ]
    result = rank(universe(*symbols), summaries)
    assert len(result["winners"]) == len(result["losers"]) == 10
    assert result["winners"][0]["symbol"] == "COIN29USDT"
    assert result["losers"][0]["symbol"] == "COIN00USDT"


def test_historical_snapshot_is_distinguished_and_future_snapshot_rejected():
    result = rank(universe("BTCUSDT"), [summary()], universe_observed_at_ms=START)
    assert result["status"] == "COMPLETE"
    assert result["universe_basis"] == "HISTORICAL_SNAPSHOT"
    with pytest.raises(ValueError, match="FUTURE_UNIVERSE"):
        rank(universe("BTCUSDT"), [summary()], universe_observed_at_ms=END + 1)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("onboardDate", START + 1, "NEW_LISTING_PARTIAL_DAY"),
        ("deliveryDate", END - 1, "DELISTING_PARTIAL_DAY"),
        ("quoteAsset", "USDC", "OUTSIDE_USDT_PERPETUAL_UNIVERSE"),
        ("contractType", "CURRENT_QUARTER", "OUTSIDE_USDT_PERPETUAL_UNIVERSE"),
    ],
)
def test_universe_exclusions_are_reported_not_silently_dropped(field, value, reason):
    items = universe("BTCUSDT")
    items[0][field] = value
    result = rank(items, [summary()])
    assert result["coverage"] == {
        "discovered": 1,
        "eligible": 0,
        "excluded": 1,
        "zero_return": 0,
    }
    assert result["exclusions"] == [{"symbol": "BTCUSDT", "reason": reason}]


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("missing", "MISSING_DAILY_SUMMARY"),
        ("duplicate", "DUPLICATE_SUMMARY"),
        ("partial", "SUMMARY_COMPLETENESS_UNPROVEN"),
        ("wrong_window", "SUMMARY_WINDOW_MISMATCH"),
        ("no_count", "SUMMARY_COMPLETENESS_UNPROVEN"),
        ("unknown", "SUMMARY_COMPLETENESS_UNPROVEN"),
    ],
)
def test_bad_summary_cannot_enter_rank(defect, reason):
    row = summary()
    rows = [row]
    if defect == "missing":
        rows = []
    elif defect == "duplicate":
        rows.append(deepcopy(row))
    elif defect == "partial":
        row["source_count"] -= 1
    elif defect == "wrong_window":
        row["start_ms"] -= 1
    elif defect == "no_count":
        del row["source_count"]
    else:
        row["complete"] = False
    assert rank(universe("BTCUSDT"), rows)["exclusions"][0]["reason"] == reason


def test_duplicate_universe_identity_excluded_even_if_summary_good():
    result = rank(universe("BTCUSDT", "BTCUSDT"), [summary()])
    assert result["exclusions"][0]["reason"] == "DUPLICATE_UNIVERSE_SYMBOL"


def test_unicode_public_contract_must_rank_even_when_engine_symbol_unsupported():
    symbols = ("币安人生USDT", "BTCUSDT")
    result = rank(
        universe(*symbols), [summary(symbols[0], closing="200"), summary(symbols[1])]
    )
    assert result["winners"][0]["symbol"] == "币安人生USDT"
    assert result["winners"][0]["engine_symbol_supported"] is False
    assert result["winners"][1]["engine_symbol_supported"] is True
    assert len(result["eligible_summaries"]) == 2


@pytest.mark.parametrize(
    "symbol",
    [
        "/BTCUSDT",
        "BTC USDT",
        "BTC\nUSDT",
        "BTC?USDT",
        "BTC&USDT",
        "USDT",
        "币" * 41 + "USDT",
    ],
)
def test_public_symbol_validation_rejects_path_query_control_or_empty_prefix(symbol):
    with pytest.raises(ValueError, match="INVALID_USDT_SYMBOL"):
        summary(symbol)


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("gap", "MISSING_OR_PARTIAL_DAY"),
        ("duplicate", "DUPLICATE_BAR"),
        ("partial", "MISSING_OR_PARTIAL_DAY"),
        ("misaligned", "MISALIGNED_BAR"),
        ("bad_ohlc", "INVALID_OHLC"),
        ("missing_field", "MISSING_BAR_FIELD"),
    ],
)
def test_path_unknown_for_gap_duplicate_partial_invalid_source(defect, reason):
    rows = bars()
    if defect == "gap":
        del rows[700]
    elif defect == "duplicate":
        rows[700] = deepcopy(rows[699])
    elif defect == "partial":
        rows.pop()
    elif defect == "misaligned":
        rows[700]["t"] += 1
    elif defect == "bad_ohlc":
        rows[700]["h"] = "1"
    else:
        del rows[700]["v"]
    result = path(rows)
    assert result["status"] == "UNKNOWN" and result["reason"] == reason
    assert result["features"] is None and result["post_entry"] is None


@pytest.mark.parametrize(
    "value", [float("nan"), 1.0, True, "NaN", "Infinity", "-1", "0", "1e999", "1e-999"]
)
def test_price_validation_never_accepts_float_nonfinite_zero_or_unbounded(value):
    rows = bars()
    rows[1]["c"] = value
    assert path(rows)["status"] == "UNKNOWN"


def test_future_and_previous_prices_never_change_day_features():
    rows = bars(closing="120")
    original = path(rows)
    rows.extend([{"t": END, "c": "garbage"}, {"t": START - MINUTE, "h": "99999999"}])
    result = path(rows)
    assert result["features"] == original["features"]
    assert result["ignored_outside_day"] == 2
    assert path(list(reversed(rows)))["features"] == original["features"]


@pytest.mark.parametrize(
    "side,favorable,adverse", [("BUY", "10", "-5"), ("SELL", "5", "-10")]
)
def test_post_entry_extrema_exclude_preentry_and_entry_minute_and_sell_symmetry(
    side, favorable, adverse
):
    rows = bars()
    rows[2].update(h="1000", l="1")
    rows[100].update(h="500", l="2")
    rows[101].update(h="110", l="95")
    result = path(
        rows,
        side=side,
        entry_ms=START + 100 * MINUTE,
        entry_price="100",
        realized_net="-3.5",
        market_source="BINANCE_LIVE",
        entry_price_source="BINANCE_LIVE",
    )
    bound = result["post_entry"]
    assert bound["favorable_price_excursion_pct"] == favorable
    assert bound["adverse_price_excursion_pct"] == adverse
    assert bound["first_observed_minute_ms"] == START + 101 * MINUTE
    assert bound["actual_or_achievable_profit"] is False
    assert result["actual_realized_net"] == "-3.5"
    assert result["features"]["high"] == "1000"  # day extreme is separate
    assert "OHLC_EXTREMA_NOT_EXECUTABLE_PROFIT" in result["limitations"]


@pytest.mark.parametrize(
    "market,entry",
    [
        ("BINANCE_LIVE", "BINANCE_TESTNET"),
        ("UNKNOWN", "UNKNOWN"),
        ("BINANCE_LIVE", None),
    ],
)
def test_testnet_fill_is_never_compared_with_live_market_or_unknown_source(
    market, entry
):
    result = path(
        bars(),
        entry_ms=START,
        entry_price="100",
        market_source=market,
        entry_price_source=entry,
    )
    assert result["post_entry"]["reason"] == "PRICE_SOURCES_NOT_COMPARABLE"


def test_no_post_entry_window_is_unknown_not_zero_profit():
    result = path(
        bars(),
        entry_ms=END - 1,
        entry_price="100",
        market_source="BINANCE_LIVE",
        entry_price_source="BINANCE_LIVE",
    )
    assert result["post_entry"]["reason"] == "NO_FULL_POST_ENTRY_MINUTE"


def test_feature_volume_zero_and_constant_range_remain_unknown_ratios():
    rows = bars()
    for row in rows:
        row["v"] = "0"
    features = path(rows)["features"]
    assert features["close_location_in_range_pct"] is None
    assert features["largest_hour_volume_share_pct"] is None
    assert features["favorable_close_step_pct"] == "0"
    assert features["best_ordered_close_run_pct"] == "0"
    assert len(features["hourly"]) == 24


def test_context_isolation_json_compatible_and_no_input_mutation():
    rows = bars(closing="113")
    before = deepcopy(rows)
    expected = path(rows)
    with localcontext() as context:
        context.prec = 4
        context.traps[Inexact] = True
        assert path(rows) == expected
    assert rows == before
    json.dumps(expected, allow_nan=False)


@pytest.mark.parametrize(
    "value", [True, 1.0, "NaN", "sNaN", "Infinity", "1e999", "-1e999"]
)
def test_nonfinite_or_float_realized_labels_rejected(value):
    with pytest.raises(ValueError):
        path(bars(), realized_net=value)


def test_hindsight_ordered_run_respects_price_order_not_whole_day_range():
    rows = bars()
    for row in rows[:720]:
        row.update(o="200", h="200", l="200", c="200")
    buy, sell = path(rows), path(rows, side="SELL")
    assert buy["features"]["best_ordered_close_run_pct"] == "0"
    assert sell["features"]["best_ordered_close_run_pct"] == "50"
