from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest
from test_v2_indicator_contract_v3 import minutes, varied_batch

from s3.detector import detect_candidate_events
from services.v2_price_features import price_features_v3, price_features_v4
from services.v2_s3_candles import (
    BREAKOUT_VERSION,
    PRICE_VERSION,
    ROLLING_VERSION,
    VERSION,
    build_frame,
)
from services.v2_s3_runtime import evaluate, numeric_features
from v2_core.evidence import canonical, digest


@pytest.mark.parametrize(
    "version,expected",
    [
        (VERSION, "2da0916056e32d2f0464dc62c4fa289c35c56ee1c95b33362efd1734062ce523"),
        (
            PRICE_VERSION,
            "47e4b663b2486e42ea186d6f3e8fbab9b4cb770f536436573d290573249f6fb3",
        ),
        (
            ROLLING_VERSION,
            "45ff4da4323c1611a3ad1901e667ea522957fe277a078798010439ff0aa6ae5b",
        ),
    ],
)
def test_pre_v4_whole_frame_snapshots_stay_identical(version, expected):
    frame = build_frame(varied_batch(), environment="SANDBOX", feature_version=version)
    assert digest(canonical(frame)) == expected
    assert "close_pos" not in frame["windows"]["BTCUSDT"]["15m"]


@pytest.mark.parametrize(
    "last_close,expected", [("99", "0"), ("100", "50"), ("101", "100")]
)
def test_close_position_covers_both_endpoints(last_close, expected):
    rows = minutes()
    rows[-1]["c"] = last_close
    before = deepcopy(rows)
    result = price_features_v4(rows)
    assert result["close_pos"] == expected
    assert {k: v for k, v in result.items() if k != "close_pos"} == price_features_v3(
        rows
    )
    assert rows == before


def test_close_position_uses_whole_window_and_original_decimal_prices():
    rows = minutes(price="0.000000000001", spread="0.0000000000000001")
    rows[0].update(h="0.0000000000010003", l="0.0000000000009999")
    expected = price_features_v4(rows)
    assert expected["close_pos"] == "25"
    with localcontext() as ctx:
        ctx.prec = 5
        ctx.traps[Inexact] = True
        assert price_features_v4(rows) == expected


def test_flat_window_has_neutral_position_and_zero_atr():
    result = price_features_v4(minutes(spread="0"))
    assert result["close_pos"] == "50"
    assert result["atr"] == result["atr_pct"] == "0"


def test_v4_uses_latest_fourteen_true_ranges_with_full_predecessor():
    rows = minutes()
    for row in rows[-14:]:
        row.update(h="110", l="90")
    assert price_features_v4(rows)["atr"] == "20"
    with pytest.raises(ValueError, match="HISTORY_BOUND"):
        price_features_v4(rows[-14:])


def test_v4_frame_declares_rolling_minutes_and_only_adds_position_to_v3_values():
    source = varied_batch()
    old = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    new = build_frame(source, environment="SANDBOX", feature_version=BREAKOUT_VERSION)
    windows = new["windows"]["BTCUSDT"]
    contract = windows["input_evidence"]["indicator_contract"]
    assert contract["window_semantics"] == "ROLLING_1M_LOOKBACK"
    assert contract["ema_period_unit"] == "1m_bar"
    assert contract["atr_method"] == "LATEST_14_TRUE_RANGES_ARITHMETIC_MEAN"
    assert (
        contract["close_pos_method"]
        == "LATEST_CLOSE_WITHIN_ROLLING_WINDOW_HIGH_LOW_PERCENT"
    )
    assert contract["close_pos_range"] == [0, 100]
    assert contract["close_pos_flat_window"] == 50
    for name in ("15m", "1h", "4h", "24h"):
        window = windows[name]
        assert Decimal(0) <= Decimal(window["close_pos"]) <= Decimal(100)
        assert {k: v for k, v in window.items() if k != "close_pos"} == old["windows"][
            "BTCUSDT"
        ][name]
    assert new["raw_windows"] == old["raw_windows"]
    assert build_frame(source, environment="SANDBOX")["frame_id"].startswith(VERSION)


@pytest.mark.parametrize(
    "defect", ["future", "gap", "duplicate", "stale", "outside_ohlc"]
)
def test_v4_rejects_unclosed_or_incomplete_history(defect):
    source = varied_batch()
    rows = source["candles"]["BTCUSDT"]
    if defect == "future":
        rows[0]["t"] = source["closed_at"]
    elif defect == "gap":
        rows.pop(40)
    elif defect == "duplicate":
        rows[2] = deepcopy(rows[1])
    elif defect == "stale":
        source["closed_at"] += 60000
    else:
        rows[0]["c"] = "999"
    with pytest.raises(ValueError):
        build_frame(source, environment="SANDBOX", feature_version=BREAKOUT_VERSION)


def breakout_batch(side):
    rows = minutes(1440, spread="0")
    sign = 1 if side == "LONG" else -1
    for index, row in enumerate(rows[-14:]):
        prior = Decimal(100) + sign * Decimal("1.5") * index
        close = prior + sign * Decimal("1.5")
        row.update(
            o=str(prior),
            h=str(max(prior, close)),
            l=str(min(prior, close)),
            c=str(close),
        )
    for row in rows:
        row.update(v="10", tbv="8" if side == "LONG" else "2")
    rows[-1].update(v="100", tbv="80" if side == "LONG" else "20")
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": 86400000,
        "candles": {"BTCUSDT": list(reversed(rows))},
    }


@pytest.mark.parametrize(
    "side,event_type,position",
    [("LONG", "PULSE_UP", "100"), ("SHORT", "PULSE_DOWN", "0")],
)
def test_real_candle_to_runtime_path_confirms_both_breakout_directions(
    side, event_type, position
):
    source = breakout_batch(side)
    old = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    new = build_frame(source, environment="SANDBOX", feature_version=BREAKOUT_VERSION)
    assert new["windows"]["BTCUSDT"]["15m"]["close_pos"] == position
    outputs = []
    for frame in (old, new):
        numeric = numeric_features(frame["windows"]["BTCUSDT"])
        # This is genuinely beyond the chase guard, so close_pos must reach
        # the detector for the strong-breakout exemption to make a difference.
        deviation = (
            abs(numeric["15m"]["close"] - numeric["4h"]["ema20"])
            / numeric["4h"]["ema20"]
            * 100
        )
        assert deviation > numeric["4h"]["atr_pct"] * 3
        events, _ = evaluate(
            frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
        )
        outputs.append(events)
    assert not any(event["type"] == event_type for event in outputs[0])
    assert any(
        event["type"] == event_type and event.get("breakout_confirmed") is True
        for event in outputs[1]
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("defect", ["opposing_flow", "low_volume", "reversal"])
def test_breakout_still_requires_flow_volume_and_directional_support(side, defect):
    frame = build_frame(
        breakout_batch(side), environment="SANDBOX", feature_version=BREAKOUT_VERSION
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    if defect == "opposing_flow":
        windows["15m"]["taker_buy_ratio"] = 0.2 if side == "LONG" else 0.8
    elif defect == "low_volume":
        windows["15m"]["vol_ratio"] = 1.49
    else:
        windows["4h"]["chg"] = -1 if side == "LONG" else 1
    events = detect_candidate_events("BTCUSDT", windows, {})
    assert not any(event.get("breakout_confirmed") for event in events)
    assert not any(
        event["type"] == ("PULSE_UP" if side == "LONG" else "PULSE_DOWN")
        for event in events
    )


@pytest.mark.parametrize("bad", [None, False, -1, 101, "NaN", "Infinity"])
def test_v4_invalid_position_cannot_be_a_breakout_exemption(bad):
    frame = build_frame(
        breakout_batch("SHORT"), environment="SANDBOX", feature_version=BREAKOUT_VERSION
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["15m"]["close_pos"] = bad
    with pytest.raises(ValueError):
        detect_candidate_events("BTCUSDT", windows, {})


@pytest.mark.parametrize("version", [VERSION, PRICE_VERSION, ROLLING_VERSION])
def test_legacy_numeric_zero_consumer_semantics_are_frozen(version):
    frame = build_frame(
        breakout_batch("SHORT"), environment="SANDBOX", feature_version=ROLLING_VERSION
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["input_evidence"]["window_version"] = version
    windows["15m"]["close_pos"] = 0.0
    assert not any(
        event["type"] == "PULSE_DOWN"
        for event in detect_candidate_events("BTCUSDT", windows, {})
    )
