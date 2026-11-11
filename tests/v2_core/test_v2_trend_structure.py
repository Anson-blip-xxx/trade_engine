from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import aggregate_minutes, smoothed, standard_series
from v2_core.trend_structure import (
    REQUIRED_BARS,
    VERSION,
    build_trend_structure,
    validate_trend_structure,
)

MINUTE = 60_000


def minutes(count=1440, *, first=0, price="100", volume="10", flow="0.4"):
    rows = []
    for minute in range(first, first + count):
        px = Decimal(price)
        rows.append(
            {
                "t": minute * MINUTE,
                "o": str(px),
                "h": str(px + 1),
                "l": str(px - 1),
                "c": str(px),
                "v": volume,
                "tbv": str(Decimal(volume) * Decimal(flow)),
            }
        )
    return rows


def frame(rows=None, **kwargs):
    rows = minutes() if rows is None else rows
    return build_trend_structure(
        rows,
        symbol=kwargs.get("symbol", "BTCUSDT"),
        environment=kwargs.get("environment", "SANDBOX"),
        closed_at_ms=kwargs.get("closed_at_ms", rows[-1]["t"] + MINUTE),
    )


def validate(value, **kwargs):
    return validate_trend_structure(
        value,
        symbol=kwargs.get("symbol", "BTCUSDT"),
        environment=kwargs.get("environment", "SANDBOX"),
        closed_at_ms=kwargs.get("closed_at_ms", value["closed_at_ms"]),
    )


def reprice(row, px, spread=Decimal(1)):
    row.update(o=str(px), h=str(px + spread), l=str(px - spread), c=str(px))


def test_true_closed_15m_aggregation_and_periods():
    rows = minutes()
    for index, row in enumerate(rows):
        reprice(row, Decimal(100 + index // 15))
    result = frame(rows)
    values = result["values"]
    assert result["version"] == VERSION
    assert result["status"] == "READY"
    assert result["complete_bar_count"] == 96
    assert result["indicator_bar_count"] == REQUIRED_BARS == 90
    assert result["last_closed_at_ms"] == 1440 * MINUTE
    assert values["open"] == values["close"] == "195"
    assert values["high"] == "196" and values["low"] == "194"
    assert values["previous_close"] == "194"
    assert values["volume"] == "150"
    assert values["prior20_mean_volume"] == "150"
    assert values["taker_buy_ratio"] == "0.4"
    assert values["volume_ratio"] == "1"
    assert values["atr"] == "2"
    # Independent EMA recurrence on 90 true 15m closes, not 1m samples.
    for period in (20, 60):
        expected = Decimal(106)
        alpha = Decimal(2) / (period + 1)
        for close in range(107, 196):
            expected = alpha * close + (1 - alpha) * expected
        assert abs(Decimal(values[f"ema{period}"]) - expected) < Decimal("1e-18")
        assert Decimal(values[f"ema{period}_slope"]) > 0
    assert Decimal(values["ema20"]) > Decimal(values["ema60"])
    assert result["contract"]["indicator_period_unit"] == "15m_bar"
    assert validate(result) is result


def test_previous_channels_and_flow_exclude_current_bar():
    rows = minutes()
    for row in rows[-15:]:
        reprice(row, Decimal(105), Decimal(7))
        row.update(v="20", tbv="0")
    result = frame(rows)["values"]
    assert result["close"] == "105"
    assert result["high"] == "112" and result["low"] == "98"
    assert (
        result["prior20_high"]
        == result["prior_swing_high"]
        == result["prior3_high"]
        == "101"
    )
    assert (
        result["prior20_low"]
        == result["prior_swing_low"]
        == result["prior3_low"]
        == "99"
    )
    assert result["previous_open"] == result["previous_close"] == "100"
    assert result["breakout_long"] is True and result["breakout_short"] is False
    assert result["prior20_mean_volume"] == "150"
    assert result["volume_ratio"] == "2"
    assert result["taker_buy_ratio"] == "0"
    assert abs(Decimal(result["atr"]) - (Decimal(2) + Decimal(12) / 14)) < Decimal(
        "1e-18"
    )


def test_taker_flow_uses_volume_weight_not_mean_minute_ratio():
    rows = minutes()
    for row in rows[-15:]:
        row.update(v="1", tbv="0")
    rows[-1].update(v="14", tbv="14")
    assert frame(rows)["values"]["taker_buy_ratio"] == "0.5"


def test_uncompleted_15m_bucket_cannot_change_indicators_or_breakouts():
    rows = minutes(first=5)
    original = frame(rows)
    # Last five source minutes are closed; their 15m bucket is not.
    for row in rows[-5:]:
        reprice(row, Decimal(10000), Decimal(50))
        row.update(v="10000", tbv="10000")
    changed = frame(rows)
    assert changed["values"] == original["values"]
    assert changed["input_digest"] != original["input_digest"]
    assert changed["last_closed_at_ms"] == 1440 * MINUTE
    assert changed["complete_bar_count"] == 95
    assert changed["excluded_partial_minutes"] == 15
    validate(changed)


def test_fixed_seed_ignores_extra_old_bars_but_records_input_digest():
    rows = minutes()
    for index, row in enumerate(rows):
        reprice(row, Decimal(100) + Decimal(index % 37) / 10)
    short, long = frame(rows[-1350:]), frame(rows)
    assert short["values"] == long["values"]
    assert short["input_digest"] != long["input_digest"]


@pytest.mark.parametrize("first", [0, 1, 5, 14])
def test_cached_minute_fast_path_matches_reference_aggregation_and_digest(first):
    rows = minutes(first=first)
    for index, row in enumerate(rows):
        reprice(
            row, Decimal(100) + Decimal((index * 17) % 103) / 100, Decimal("0.05")
        )
        row.update(
            v=str(index % 19 + 1), tbv=str(Decimal(index % 19 + 1) * Decimal(".37"))
        )
    result = frame(rows)
    complete = aggregate_minutes(
        rows, timeframe_minutes=15, closed_at_ms=(first + 1440) * MINUTE
    )
    selected = complete[-90:]
    values = result["values"]
    assert result["complete_bar_count"] == len(complete)
    assert result["input_digest"] == digest(canonical({"bars": rows}))
    for key, value in (
        ("open", selected[-1].o),
        ("high", selected[-1].h),
        ("low", selected[-1].l),
        ("close", selected[-1].c),
        ("volume", selected[-1].v),
    ):
        assert Decimal(values[key]) == value
    assert abs(
        Decimal(values["atr"]) - standard_series(selected)["atr"][-1]
    ) <= Decimal("1e-18")
    for period in (20, 60):
        reference = smoothed([bar.c for bar in selected], period)[-1]
        assert abs(Decimal(values[f"ema{period}"]) - reference) <= Decimal("1e-18")


@pytest.mark.parametrize("bar_count", [1, 60, 89, 90])
def test_warmup_is_explicit_and_never_falls_back_to_price(bar_count):
    result = frame(minutes(15 * bar_count))
    assert result["status"] == ("READY" if bar_count == 90 else "WARMUP")
    assert (result["values"] is None) is (bar_count < 90)
    validate(result)


def test_partial_only_history_and_zero_volume_have_explicit_semantics():
    result = frame(minutes(3, first=5))
    assert result["status"] == "WARMUP" and result["last_closed_at_ms"] is None
    validate(result)
    result = frame(minutes(volume="0"))
    assert result["values"]["taker_buy_ratio"] is None
    assert result["values"]["volume_ratio"] is None
    rows = minutes()
    for row in rows[-15:]:
        row.update(v="0", tbv="0")
    result = frame(rows)
    assert result["values"]["taker_buy_ratio"] is None
    assert result["values"]["volume_ratio"] == "0"


def test_flat_prices_have_zero_atr_without_false_breakout():
    rows = minutes()
    for row in rows:
        row.update(o="100", h="100", l="100", c="100")
    result = frame(rows)["values"]
    assert result["atr"] == result["atr_pct"] == "0"
    assert result["breakout_long"] is result["breakout_short"] is False
    assert result["ema20_slope"] == result["ema60_slope"] == "0"


def test_long_short_reflection_is_symmetric():
    rows = minutes()
    for index, row in enumerate(rows):
        reprice(row, Decimal(100) + Decimal(index // 15) / 10, Decimal("0.05"))
    reflected = deepcopy(rows)
    for row in reflected:
        original = deepcopy(row)
        row.update(
            o=str(300 - Decimal(original["o"])),
            c=str(300 - Decimal(original["c"])),
            h=str(300 - Decimal(original["l"])),
            l=str(300 - Decimal(original["h"])),
            tbv=str(Decimal(original["v"]) - Decimal(original["tbv"])),
        )
    up, down = frame(rows)["values"], frame(reflected)["values"]
    assert up["breakout_long"] is down["breakout_short"] is True
    assert up["breakout_short"] is down["breakout_long"] is False
    assert up["atr"] == down["atr"]
    for name in ("ema20", "ema60", "previous_ema20", "previous_ema60"):
        assert abs(Decimal(up[name]) + Decimal(down[name]) - 300) <= Decimal("1e-18")
    for name in ("ema20_slope", "ema60_slope"):
        assert abs(Decimal(up[name]) + Decimal(down[name])) <= Decimal("1e-18")
    assert Decimal(up["taker_buy_ratio"]) + Decimal(down["taker_buy_ratio"]) == 1
    assert up["prior3_touched_ema20_long"] == down["prior3_touched_ema20_short"]
    assert up["prior3_touched_ema20_short"] == down["prior3_touched_ema20_long"]


@pytest.mark.parametrize(
    "kind",
    [
        "gap",
        "duplicate",
        "reverse",
        "future",
        "stale",
        "unaligned",
        "bool_time",
        "missing_tbv",
        "missing_volume",
        "bad_ohlc",
        "float",
        "nan",
        "negative_volume",
        "excess_tbv",
        "negative_tbv",
        "extra_field",
        "too_many",
    ],
)
def test_invalid_source_fails_closed(kind):
    rows = minutes()
    if kind == "gap":
        rows.pop(5)
    elif kind == "duplicate":
        rows[5] = deepcopy(rows[4])
    elif kind == "reverse":
        rows.reverse()
    elif kind == "future":
        rows[-1]["t"] += MINUTE
    elif kind == "stale":
        rows.pop()
    elif kind == "unaligned":
        rows[5]["t"] += 1
    elif kind == "bool_time":
        rows[0]["t"] = False
    elif kind == "missing_tbv":
        rows[5].pop("tbv")
    elif kind == "missing_volume":
        rows[5].pop("v")
    elif kind == "bad_ohlc":
        rows[5]["h"] = "1"
    elif kind == "float":
        rows[5]["c"] = 100.0
    elif kind == "nan":
        rows[5]["tbv"] = "NaN"
    elif kind == "negative_volume":
        rows[5]["v"] = "-1"
    elif kind == "excess_tbv":
        rows[5]["tbv"] = "11"
    elif kind == "negative_tbv":
        rows[5]["tbv"] = "-1"
    elif kind == "extra_field":
        rows[5]["unexpected"] = "1"
    else:
        rows.extend(minutes(1, first=1440))
    with pytest.raises(ValueError):
        frame(rows, closed_at_ms=1440 * MINUTE)


@pytest.mark.parametrize(
    "kind",
    [
        "symbol",
        "environment",
        "time",
        "last_time",
        "version",
        "source",
        "digest",
        "contract",
        "bool_period",
        "warmup_count",
        "status",
        "extra_metadata",
        "missing_value",
        "nan",
        "float",
        "negative_atr",
        "bad_range",
        "bad_flow",
        "missing_flow",
        "fake_breakout",
        "bool_as_number",
        "unbounded",
        "fake_atr_pct",
        "fake_volume_ratio",
    ],
)
def test_tampered_envelope_rejected(kind):
    result = frame()
    if kind == "symbol":
        result["symbol"] = "ETHUSDT"
    elif kind == "environment":
        result["environment"] = "LIVE"
    elif kind == "time":
        result["closed_at_ms"] += MINUTE
    elif kind == "last_time":
        result["last_closed_at_ms"] -= MINUTE
    elif kind == "version":
        result["version"] = "legacy"
    elif kind == "source":
        result["source"] = "other"
    elif kind == "digest":
        result["input_digest"] = "not-a-digest"
    elif kind == "contract":
        result["contract"]["indicator_period_unit"] = "1m_bar"
    elif kind == "bool_period":
        result["contract"]["prior_windows_exclude_current"] = 1
    elif kind == "warmup_count":
        result["indicator_bar_count"] = 60
    elif kind == "status":
        result["status"] = "WARMUP"
    elif kind == "extra_metadata":
        result["unknown"] = 1
    elif kind == "missing_value":
        result["values"].pop("ema60")
    elif kind == "nan":
        result["values"]["ema60"] = "NaN"
    elif kind == "float":
        result["values"]["ema60"] = 100.0
    elif kind == "negative_atr":
        result["values"]["atr"] = "-1"
    elif kind == "bad_range":
        result["values"]["prior_swing_high"] = "200"
    elif kind == "bad_flow":
        result["values"]["taker_buy_ratio"] = "1.1"
    elif kind == "missing_flow":
        result["values"]["taker_buy_ratio"] = None
    elif kind == "fake_breakout":
        result["values"]["breakout_short"] = True
    elif kind == "bool_as_number":
        result["complete_bar_count"] = True
    elif kind == "fake_atr_pct":
        result["values"]["atr_pct"] = "1"
    elif kind == "fake_volume_ratio":
        result["values"]["volume_ratio"] = "2"
    else:
        result["values"]["volume_ratio"] = "1e10000"
    with pytest.raises(ValueError):
        validate(result, closed_at_ms=1440 * MINUTE)


def test_wrong_requested_scope_and_unclosed_boundary_rejected():
    result = frame()
    for changed in (
        {"symbol": "ETHUSDT"},
        {"environment": "LIVE"},
        {"closed_at_ms": 1439 * MINUTE},
    ):
        with pytest.raises(ValueError):
            validate(result, **changed)
    for changed in (
        {"symbol": "btc"},
        {"environment": "unknown"},
        {"closed_at_ms": 1440 * MINUTE + 1},
    ):
        with pytest.raises(ValueError):
            frame(**changed)


def test_no_input_mutation_and_independence_from_ambient_decimal_context():
    rows = minutes()
    for index, row in enumerate(rows):
        reprice(
            row,
            Decimal("0.00001") + Decimal(index // 15) / 10**8,
            Decimal("0.000000001"),
        )
    original = deepcopy(rows)
    expected = frame(rows)
    with localcontext() as context:
        context.prec = 4
        context.traps[Inexact] = True
        actual = frame(rows)
        assert validate(actual) is actual
    assert actual == expected
    assert rows == original
    assert Decimal(actual["values"]["ema60"]) > 0
