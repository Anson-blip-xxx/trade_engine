from copy import deepcopy
from decimal import Decimal

import pytest

from v2_core.quant_indicators import (
    Bar,
    aggregate_minutes,
    choppiness,
    smoothed,
    standard_series,
)
from v2_core.quant_registry import (
    PLUGINS,
    evaluate,
    feature_frame,
    parameters,
    trailing_stop,
    warmup,
)


def minutes(count=1440, *, scale="1", breakout=False):
    scale = Decimal(scale)
    rows = []
    for i in range(count):
        price = (Decimal(100) + Decimal(i % 4) / 10) * scale
        if breakout and i >= count - 5:
            price = Decimal(102) * scale
        rows.append(
            {
                "t": i * 60000,
                "o": str(price),
                "h": str(price + scale / 10),
                "l": str(price - scale / 10),
                "c": str(price),
                "v": "30" if breakout and i >= count - 5 else "10",
            }
        )
    return rows


def frame(rows=None, *, timeframe=5, params=None):
    rows = minutes(breakout=True) if rows is None else rows
    bars = aggregate_minutes(
        rows, timeframe_minutes=timeframe, closed_at_ms=rows[-1]["t"] + 60000
    )
    return feature_frame(bars, timeframe_minutes=timeframe, params=params)


def test_real_bars_alignment_ohlcv_and_partial_edges():
    rows = minutes(19)[2:]
    result = aggregate_minutes(rows, timeframe_minutes=5, closed_at_ms=19 * 60000)
    assert [b.t for b in result] == [5 * 60000, 10 * 60000]
    for bar, group in zip(result, (rows[3:8], rows[8:13]), strict=True):
        assert bar.o == Decimal(group[0]["o"])
        assert bar.c == Decimal(group[-1]["c"])
        assert bar.h == max(Decimal(b["h"]) for b in group)
        assert bar.l == min(Decimal(b["l"]) for b in group)
        assert bar.v == 50


@pytest.mark.parametrize(
    "kind",
    [
        "gap",
        "duplicate",
        "reversed",
        "future",
        "stale",
        "bad_price",
        "nan",
        "negative_volume",
        "float",
        "bool_time",
    ],
)
def test_fail_closed_input(kind):
    rows = minutes(20)
    if kind == "gap":
        rows.pop(3)
    elif kind == "duplicate":
        rows.insert(3, rows[3])
    elif kind == "reversed":
        rows.reverse()
    elif kind == "future":
        rows[-1]["t"] += 60000
    elif kind == "stale":
        rows.pop()
    elif kind == "bad_price":
        rows[1]["h"] = "1"
    elif kind == "nan":
        rows[1]["c"] = "NaN"
    elif kind == "negative_volume":
        rows[1]["v"] = "-1"
    elif kind == "float":
        rows[1]["c"] = 100.1
    else:
        rows[1]["t"] = True
    with pytest.raises(ValueError):
        aggregate_minutes(rows, timeframe_minutes=5, closed_at_ms=20 * 60000)


def test_wilder_and_ema_known_vectors_and_warmup():
    values = [Decimal(i) for i in (1, 2, 3, 7, 2)]
    ema = smoothed(values, 3)
    assert ema == list(map(Decimal, ("1", "1.5", "2.25", "4.625", "3.3125")))
    rma = smoothed(values, 3, wilder=True)
    assert rma[:3] == [None, None, Decimal(2)]
    assert abs(rma[3] - Decimal(11) / 3) < Decimal("1e-27")
    bars = [
        Bar(i * 60000, str(i + 10), str(i + 11), str(i + 9), str(i + 10), "1")
        for i in range(50)
    ]
    series = standard_series(bars, 14)
    assert all(x is None for x in series["atr"][:13])
    assert series["atr"][13:] == [Decimal(2)] * 37
    assert series["rsi"][14:] == [Decimal(100)] * 36
    assert all(x is None for x in series["adx"][:26])
    assert series["adx"][-1] == 100


def test_flat_and_zero_volume_are_not_fake_momentum():
    bars = [Bar(i * 60000, "1", "1", "1", "1", "0") for i in range(100)]
    series = standard_series(bars)
    assert series["atr"][-1] == series["adx"][-1] == 0
    assert series["rsi"][-1] == 50
    assert choppiness(bars, series["tr"], 14) == 100
    current = feature_frame(bars, timeframe_minutes=1)
    result = evaluate(current, enabled=PLUGINS)
    assert result["winner"] is None
    assert {r["reason"] for r in result["evaluations"]} == {"ZERO_VOLATILITY"}


@pytest.mark.parametrize("scale", ["1", "0.00000001", "10000"])
def test_breakout_is_price_scale_invariant_and_uses_previous_channel(scale):
    current = frame(minutes(scale=scale, breakout=True))
    result = evaluate(current)
    winner = result["winner"]
    assert winner["strategy_id"] == "donchian"
    assert winner["side"] == "BUY"
    assert Decimal(current["current"]["close"]) > Decimal(
        current["current"]["donchian_upper"]
    )
    assert Decimal(current["current"]["atr"]) > 0
    reference = evaluate(frame())["winner"]
    assert winner["score"] == reference["score"]
    assert abs(
        Decimal(winner["stop_fraction"]) - Decimal(reference["stop_fraction"])
    ) < Decimal("1e-15")


def test_finite_seed_is_independent_of_extra_old_history():
    rows = minutes(2000, breakout=True)
    full = frame(rows)
    short = frame(rows[-1000:])
    assert full == short
    assert full["bar_count"] == warmup()


def test_prevent_warmup_fallback_and_bad_registry_configuration():
    current = frame(minutes(60))
    assert evaluate(current)["status"] == "WARMUP"
    for params in (
        {"period": True},
        {"unknown": 1},
        {"stop_atr": "0"},
        {"ema_fast": 100},
        {"max_stop_fraction": "1"},
    ):
        with pytest.raises(ValueError):
            parameters(params)
    with pytest.raises(ValueError):
        evaluate(frame(), enabled=("import-anything",))


def test_candidates_are_deterministic_without_mutation():
    current = frame()
    before = deepcopy(current)
    assert evaluate(current, enabled=PLUGINS) == evaluate(current, enabled=PLUGINS)
    assert current == before
    assert len(evaluate(current, enabled=PLUGINS)["evaluations"]) == 5


@pytest.mark.parametrize(
    "side,prior,high,low,expected",
    [
        ("BUY", "90", "110", "100", "104"),
        ("BUY", "105", "110", "100", "105"),
        ("SELL", "115", "110", "100", "106"),
        ("SELL", "104", "110", "100", "104"),
    ],
)
def test_trailing_stop_never_widens(side, prior, high, low, expected):
    assert (
        trailing_stop(
            side=side,
            prior_stop=prior,
            high_water=high,
            low_water=low,
            atr="2",
            multiplier="3",
        )
        == expected
    )


def test_both_directions_conflict_does_not_double_order():
    current = frame()
    current["previous"]["supertrend"] = 1
    current["current"]["supertrend"] = -1
    result = evaluate(current, enabled=("donchian", "supertrend"))
    assert result["status"] == "CONFLICT" and result["winner"] is None


@pytest.mark.parametrize("plugin", PLUGINS)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_all_registered_plugin_direction_transitions(plugin, side):
    current = frame()
    before, after = current["previous"], current["current"]
    long = side == "BUY"
    if plugin in {"donchian", "atr_channel"}:
        prefix = "donchian" if plugin == "donchian" else "channel"
        before.update(
            close="100", **{prefix + "_upper": "101", prefix + "_lower": "99"}
        )
        after.update(
            close="102" if long else "98",
            **{prefix + "_upper": "101", prefix + "_lower": "99"},
        )
    elif plugin == "squeeze":
        before["compressed"] = True
        after.update(compressed=False, momentum=".01" if long else "-.01")
    elif plugin == "supertrend":
        before["supertrend"] = -1 if long else 1
        after["supertrend"] = 1 if long else -1
    else:
        before["vwap_z"] = "-3" if long else "3"
        after.update(vwap_z="-1" if long else "1", regime="RANGE")
    assert evaluate(current, enabled=(plugin,))["winner"]["side"] == side
    if plugin == "vwap_reversion":
        after["regime"] = "TREND"
        assert (
            evaluate(current, enabled=(plugin,))["evaluations"][0]["reason"]
            == "MEAN_REVERSION_REGIME_MISMATCH"
        )


def test_regime_and_relative_volume_are_independent_configured_filters():
    current = frame(params={"trend_regime_required": True})
    current["current"]["regime"] = "RANGE"
    assert (
        evaluate(current, enabled=("donchian",))["evaluations"][0]["reason"]
        == "TREND_REGIME_MISMATCH"
    )
    current["parameters"]["trend_regime_required"] = False
    assert evaluate(current, enabled=("donchian",))["winner"]
    current["current"]["rvol"] = ".99"
    assert (
        evaluate(current, enabled=("donchian",))["evaluations"][0]["reason"]
        == "INSUFFICIENT_RELATIVE_VOLUME"
    )
