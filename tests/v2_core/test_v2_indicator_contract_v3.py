from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest

from services.v2_price_features import price_features, price_features_v3
from services.v2_s3_candles import PRICE_VERSION, ROLLING_VERSION, VERSION, build_frame
from v2_core.evidence import canonical, digest
from v2_core.timeframe_indicators import aggregate_closed_bars, closed_bar_indicators

MINUTE = 60000


def minutes(count=120, *, start=0, price="100", spread="1"):
    px, delta = Decimal(price), Decimal(spread)
    return [
        {
            "t": i * MINUTE,
            "o": price,
            "h": str(px + delta),
            "l": str(px - delta),
            "c": price,
            "v": "0.1",
            "tbv": "0.06",
        }
        for i in range(start, start + count)
    ]


def varied_batch():
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": 86400000,
        "candles": {
            "BTCUSDT": [
                {
                    "t": 86400000 - (i + 1) * MINUTE,
                    "o": str(100 + i % 7),
                    "h": str(110 + i % 5),
                    "l": str(90 - i % 3),
                    "c": str(101 + i % 7),
                    "v": str(10 + i % 3),
                    "tbv": "6",
                }
                for i in range(1440)
            ]
        },
    }


@pytest.mark.parametrize(
    "version,expected",
    [
        (VERSION, "2da0916056e32d2f0464dc62c4fa289c35c56ee1c95b33362efd1734062ce523"),
        (
            PRICE_VERSION,
            "47e4b663b2486e42ea186d6f3e8fbab9b4cb770f536436573d290573249f6fb3",
        ),
    ],
)
def test_legacy_whole_frame_is_byte_identical_to_pre_v3_snapshot(version, expected):
    # Snapshots captured from the existing implementation before the v3 edit.
    result = build_frame(varied_batch(), environment="SANDBOX", feature_version=version)
    assert digest(canonical(result)) == expected


def test_latest_atr_changes_only_explicit_v3_fields_not_ema_contract():
    rows = minutes()
    for row in rows[-14:]:
        row.update(h="110", l="90")
    before = deepcopy(rows)
    old, new = price_features(rows), price_features_v3(rows)
    assert old["atr"] == "2"
    assert new["atr"] == "20"
    assert new["atr_pct"] == "20"
    assert {k: v for k, v in old.items() if k not in {"atr", "atr_pct"}} == {
        k: v for k, v in new.items() if k not in {"atr", "atr_pct"}
    }
    assert rows == before


def test_latest_true_range_includes_previous_close_and_can_cool_after_old_spike():
    rows = minutes()
    rows[4].update(h="150", l="50")
    rows[-1].update(o="110", h="111", l="109", c="110")
    new = price_features_v3(rows)
    assert new["atr"] == "2.642857142857142857"  # 13 * 2 + 11, divided by 14.
    assert Decimal(price_features(rows)["atr"]) > Decimal(new["atr"])


def test_rolling_contract_never_claims_hourly_ema_and_default_stays_v1():
    source = varied_batch()
    before = deepcopy(source)
    frame = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    proof = frame["windows"]["BTCUSDT"]["input_evidence"]
    contract = proof["indicator_contract"]
    assert contract["input_interval"] == "1m"
    assert contract["lookback_bars"] == {"15m": 15, "1h": 60, "4h": 240, "24h": 1440}
    assert contract["ema_period_unit"] == "1m_bar"
    assert contract["atr_method"] == "LATEST_14_TRUE_RANGES_ARITHMETIC_MEAN"
    assert frame["frame_id"].startswith(ROLLING_VERSION)
    assert build_frame(source, environment="SANDBOX")["frame_id"].startswith(VERSION)
    assert source == before


@pytest.mark.parametrize(
    "defect",
    [
        "empty",
        "one",
        "reversed",
        "gap",
        "duplicate",
        "float",
        "bool",
        "nan",
        "zero",
        "negative",
        "ohlc",
        "fields",
        "time_bool",
        "time_fraction",
    ],
)
def test_v3_rejects_invalid_source(defect):
    rows = minutes()
    if defect == "empty":
        rows = []
    elif defect == "one":
        rows = rows[:1]
    elif defect == "reversed":
        rows.reverse()
    elif defect == "gap":
        rows.pop(5)
    elif defect == "duplicate":
        rows[5] = deepcopy(rows[4])
    elif defect == "fields":
        del rows[0]["c"]
    elif defect in {"time_bool", "time_fraction"}:
        rows[0]["t"] = True if defect == "time_bool" else 1
    else:
        rows[0]["c"] = {
            "float": 100.0,
            "bool": True,
            "nan": "NaN",
            "zero": "0",
            "negative": "-1",
            "ohlc": "200",
        }[defect]
    with pytest.raises(ValueError):
        price_features_v3(rows)


def test_v3_preserves_low_price_precision_and_isolates_decimal_context():
    rows = minutes(price="0.000000000001", spread="0.0000000000000001")
    expected = price_features_v3(rows)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        assert price_features_v3(rows) == expected
    assert expected["atr"] == "0.0000000000000002"


def test_latest_atr_does_not_evaluate_an_unrepresentable_old_atr():
    rows = minutes(price="1", spread="0")
    for row in rows[:15]:
        row.update(h="1.0000000000000000001")
    for row in rows[-14:]:
        row.update(h="1.1")
    with pytest.raises(ValueError, match="BELOW_PRECISION"):
        price_features(rows)
    assert price_features_v3(rows)["atr"] == "0.1"


def test_utc_aggregation_drops_partial_edges_and_sums_exactly():
    source = minutes(130, start=5)
    before = deepcopy(source)
    result = aggregate_closed_bars(source, timeframe_minutes=60, as_of_ms=135 * MINUTE)
    assert result["bars"] == [
        {"t": 60 * MINUTE, "o": "100", "h": "101", "l": "99", "c": "100", "v": "6.0"}
    ]
    assert result["metadata"]["last_closed_at"] == 120 * MINUTE
    assert result["metadata"]["excluded_partial_minutes"] == 70
    assert source == before


def test_aggregation_ohlc_is_first_max_min_last_not_average():
    source = minutes(60)
    source[0].update(o="99.5")
    source[5].update(h="120")
    source[6].update(l="80")
    source[-1].update(c="100.5")
    row = aggregate_closed_bars(source, timeframe_minutes=60, as_of_ms=60 * MINUTE)[
        "bars"
    ][0]
    assert (row["o"], row["h"], row["l"], row["c"]) == ("99.5", "120", "80", "100.5")


def test_future_and_incomplete_bars_cannot_change_historical_values():
    source = minutes(120)
    expected = aggregate_closed_bars(
        source[:60], timeframe_minutes=60, as_of_ms=60 * MINUTE
    )
    for row in source[60:]:
        row.update(c="NaN", h="secret invalid future")
    actual = aggregate_closed_bars(source, timeframe_minutes=60, as_of_ms=60 * MINUTE)
    assert actual["bars"] == expected["bars"]
    assert actual["metadata"]["ignored_unclosed_minutes"] == 60
    assert (
        aggregate_closed_bars(
            minutes(60), timeframe_minutes=60, as_of_ms=60 * MINUTE - 1
        )["bars"]
        == []
    )


@pytest.mark.parametrize(
    "defect",
    [
        "gap",
        "duplicate",
        "reversed",
        "ohlc",
        "stale",
        "time_bool",
        "unaligned",
        "missing_volume",
    ],
)
def test_aggregation_fails_closed_on_bad_closed_history(defect):
    rows = minutes()
    if defect == "gap":
        rows.pop(60)
    elif defect == "duplicate":
        rows[5] = deepcopy(rows[4])
    elif defect == "reversed":
        rows.reverse()
    elif defect == "ohlc":
        rows[0]["c"] = "200"
    elif defect == "stale":
        rows.pop()
    elif defect == "time_bool":
        rows[0]["t"] = True
    elif defect == "unaligned":
        rows[0]["t"] = 1
    else:
        del rows[0]["v"]
    with pytest.raises(ValueError):
        aggregate_closed_bars(rows, timeframe_minutes=60, as_of_ms=120 * MINUTE)


def hourly(count=60):
    rows = minutes(count)
    for row in rows:
        row["t"] *= 60
    return rows


def test_true_hour_indicators_need_sixty_hours_not_sixty_minutes():
    one_hour = aggregate_closed_bars(
        minutes(60), timeframe_minutes=60, as_of_ms=60 * MINUTE
    )
    warmup = closed_bar_indicators(
        one_hour["bars"], timeframe_minutes=60, as_of_ms=60 * MINUTE
    )
    assert warmup["status"] == "WARMUP" and warmup["values"] is None
    assert warmup["bar_count"] == 1 and warmup["required_bars"] == 60
    ready = closed_bar_indicators(
        hourly(), timeframe_minutes=60, as_of_ms=60 * 60 * MINUTE
    )
    assert ready["status"] == "READY"
    assert ready["values"] == {
        "ema20": "100",
        "ema60": "100",
        "atr": "2",
        "atr_pct": "2",
        "close": "100",
    }


def test_missing_complete_time_bars_are_explicit_warmup_not_fabricated_prices():
    aggregated = aggregate_closed_bars(
        minutes(15), timeframe_minutes=60, as_of_ms=15 * MINUTE
    )
    result = closed_bar_indicators(
        aggregated["bars"], timeframe_minutes=60, as_of_ms=15 * MINUTE
    )
    assert result["status"] == "WARMUP"
    assert result["bar_count"] == 0
    assert result["values"] is None
    assert result["last_closed_at"] is None


def test_true_bar_ema_and_wilder_atr_match_explicit_seed_equations():
    rows = hourly(3)
    for i, row in enumerate(rows):
        row.update(
            o=str(100 + 2 * i),
            c=str(100 + 2 * i),
            h=str(101 + 2 * i),
            l=str(99 + 2 * i),
        )
    result = closed_bar_indicators(
        rows,
        timeframe_minutes=60,
        as_of_ms=180 * MINUTE,
        ema_periods=(3,),
        atr_period=2,
    )
    assert result["values"]["ema3"] == "102.5"  # 100 -> 101 -> 102.5
    assert result["values"]["atr"] == "2.75"  # TR 2,3,3; seed2.5 then2.75


def test_closed_bar_values_are_causal_and_context_independent():
    rows = hourly(61)
    cutoff = 60 * 60 * MINUTE
    expected = closed_bar_indicators(rows[:60], timeframe_minutes=60, as_of_ms=cutoff)
    rows[-1]["c"] = "NaN"
    with localcontext() as ctx:
        ctx.prec = 5
        ctx.traps[Inexact] = True
        actual = closed_bar_indicators(rows, timeframe_minutes=60, as_of_ms=cutoff)
        aggregated = aggregate_closed_bars(
            minutes(), timeframe_minutes=60, as_of_ms=120 * MINUTE
        )
    assert actual["values"] == expected["values"]
    assert actual["ignored_unclosed_bars"] == 1
    assert aggregated["bars"][0]["v"] == "6.0"


@pytest.mark.parametrize(
    "arguments",
    [
        {"timeframe_minutes": True},
        {"timeframe_minutes": 7},
        {"as_of_ms": True},
        {"as_of_ms": -1},
        {"ema_periods": ()},
        {"ema_periods": (20, 20)},
        {"ema_periods": (False,)},
        {"atr_period": 0},
        {"atr_period": 2001},
    ],
)
def test_indicator_configuration_is_bounded(arguments):
    kwargs = {"timeframe_minutes": 60, "as_of_ms": 60 * 60 * MINUTE, **arguments}
    with pytest.raises(ValueError):
        closed_bar_indicators(hourly(), **kwargs)


def test_true_time_bar_history_gap_and_staleness_are_not_warmup():
    rows = hourly()
    for bad in (rows[:-1], rows[:10] + rows[11:]):
        with pytest.raises(ValueError):
            closed_bar_indicators(bad, timeframe_minutes=60, as_of_ms=60 * 60 * MINUTE)
