from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest
from test_v2_indicator_contract_v3 import minutes, varied_batch
from test_v2_indicator_contract_v4 import breakout_batch

from s3.detector import detect_candidate_events
from services.v2_price_features import price_features_v5
from services.v2_s3_candles import (
    BREAKOUT_VERSION,
    CORRECTED_VERSION,
    PRICE_VERSION,
    ROLLING_VERSION,
    VERSION,
    build_frame,
)
from services.v2_s3_runtime import evaluate, numeric_features
from v2_core.evidence import canonical, digest


def batch_from(rows):
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": rows[-1]["t"] + 60000,
        "candles": {"BTCUSDT": list(reversed(rows))},
    }


def frame_from(rows):
    return build_frame(
        batch_from(rows), environment="SANDBOX", feature_version=CORRECTED_VERSION
    )


def expansion_frame(current, reference):
    rows = minutes(1440, spread="0")
    for block, width in ((rows[-28:-14], reference), (rows[-14:], current)):
        half = Decimal(width) / 2
        for row in block:
            row.update(h=str(100 + half), l=str(100 - half))
    return frame_from(rows)


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
        (
            BREAKOUT_VERSION,
            "66e7f0ecf0522ac106ad077f8bceef7afcf623f103a765435942d59aafca1fc4",
        ),
    ],
)
def test_pre_v5_frame_and_runtime_event_golden_snapshots(version, expected):
    # Captured before the v5 edit; include full metadata, raw bars and events.
    frame = build_frame(varied_batch(), environment="SANDBOX", feature_version=version)
    assert digest(canonical(frame)) == expected
    events, _ = evaluate(
        frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
    )
    assert digest(canonical({"events": events})) == (
        "e0988e4e9c201ed22021b1edfb090df28cf670b8fae00d17895a682a0caf3111"
    )


def test_trending_15m_ema_uses_preceding_closed_minutes_not_last_close():
    rows = minutes(1440, spread="0")
    for index, row in enumerate(rows):
        price = str(100 + index)
        row.update(o=price, h=price, l=price, c=price)
    frame = frame_from(rows)
    windows = frame["windows"]["BTCUSDT"]
    for name in ("15m", "1h", "4h", "24h"):
        # For a linear ramp, SMA-seeded EMA lags by (period-1)/2 steps.
        assert windows[name]["ema20"] == "1529.5"
        assert windows[name]["ema60"] == "1509.5"
        assert windows[name]["close"] == "1539"
    assert windows["15m"]["low"] == "1525"
    assert windows["15m"]["close_pos"] == "100"
    contract = windows["input_evidence"]["indicator_contract"]
    assert contract["ema_input_bars"] == {"15m": 120, "1h": 120, "4h": 240, "24h": 1440}
    assert contract["ema_warmup_min_bars"] == 120
    assert contract["ema_seed"] == "FIRST_PERIOD_SMA"
    assert contract["ema_insufficient_history"] == "REJECT"
    assert contract["ema_period_unit"] == contract["atr_period_unit"] == "1m_bar"
    assert contract["window_semantics"] == "ROLLING_1M_LOOKBACK"
    assert contract["lookback_bars"] == {"15m": 15, "1h": 60, "4h": 240, "24h": 1440}
    assert contract["atr_expansion_contract"] == "LATEST_14_VS_PRECEDING_14_TR_1M"
    assert contract["atr_reference_zero"] == "NO_EXPANSION"
    assert contract["atr_pct_denominator"] == "LATEST_CLOSE_FOR_CURRENT_AND_REFERENCE"


def test_warmup_changes_ema_without_extending_window_range_or_returns():
    rows = minutes(1440)
    baseline = frame_from(rows)
    for row in rows[-120:-15]:
        row.update(o="90", h="91", l="89", c="90")
    changed = frame_from(rows)
    first = baseline["windows"]["BTCUSDT"]["15m"]
    second = changed["windows"]["BTCUSDT"]["15m"]
    assert Decimal(second["ema20"]) < Decimal(first["ema20"])
    assert Decimal(second["ema60"]) < Decimal(first["ema60"])
    for field in ("high", "low", "close", "close_pos", "chg", "volume", "vol_ratio"):
        assert second[field] == first[field]
    assert (
        changed["raw_windows"]["BTCUSDT"]["15m"]
        == baseline["raw_windows"]["BTCUSDT"]["15m"]
    )
    # Older-than-warmup values cannot change the deterministic 15m indicator.
    rows[-121].update(o="500", h="501", l="499", c="500")
    assert frame_from(rows)["windows"]["BTCUSDT"]["15m"] == second


@pytest.mark.parametrize(
    "current,reference,expands",
    [
        ("8", "2", True),
        ("2", "8", False),
        ("0", "0", False),
        ("8", "0", False),
        ("4", "2", False),
        ("0.5", "0.1", False),
        ("0.6", "0.1", True),
    ],
)
def test_adjacent_atr_blocks_cover_expansion_contraction_flat_and_boundaries(
    current, reference, expands
):
    frame = expansion_frame(current, reference)
    windows = frame["windows"]["BTCUSDT"]
    for name in ("15m", "1h", "4h", "24h"):
        assert windows[name]["atr"] == windows[name]["atr_pct"] == current
        assert (
            windows[name]["atr_reference"]
            == windows[name]["atr_reference_pct"]
            == reference
        )
    events, _ = evaluate(
        frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
    )
    expanded = [event for event in events if event["type"] == "ATR_EXPAND"]
    assert bool(expanded) is expands
    if expanded:
        assert (
            expanded[0]["atr_expansion_contract"] == "LATEST_14_VS_PRECEDING_14_TR_1M"
        )
        assert Decimal(expanded[0]["atr_current_pct"]) == Decimal(current)
        assert Decimal(expanded[0]["atr_reference_pct"]) == Decimal(reference)
        assert "atr_1h" not in expanded[0]


def test_reference_tr_includes_its_own_predecessor_and_shared_pct_denominator():
    rows = minutes(120, spread="0")
    for row in rows[-29:-14]:
        row.update(o="110", h="110", l="110", c="110")
    result = price_features_v5(rows, window_bars=15, closed_at=120 * 60000)
    # First reference TR needs row[-29]. The jump into row[-29] is outside it;
    # the jump back at row[-14] belongs only to the current block.
    assert result["atr_reference"] == "0"
    assert result["atr"] == "0.714285714285714286"
    assert result["atr_pct"] == result["atr"]
    rows[-29].update(o="100", h="100", l="100", c="100")
    result = price_features_v5(rows, window_bars=15, closed_at=120 * 60000)
    assert result["atr_reference"] == result["atr"]
    assert result["atr_reference_pct"] == result["atr_pct"]


@pytest.mark.parametrize("count", [0, 15, 59, 60, 119, 121, 1440])
def test_direct_indicator_rejects_missing_or_ambiguous_ema_history(count):
    with pytest.raises(ValueError, match="EMA_WARMUP_REQUIRED"):
        price_features_v5(minutes(count), window_bars=15, closed_at=120 * 60000)


@pytest.mark.parametrize("defect", ["future", "stale", "gap", "duplicate", "bad_close"])
def test_v5_rejects_unclosed_or_invalid_history_without_lookahead(defect):
    rows = minutes(120)
    if defect == "future":
        rows[-1]["t"] += 60000
    elif defect == "stale":
        for row in rows:
            row["t"] += 60000
    elif defect == "gap":
        rows[12]["t"] += 60000
    elif defect == "duplicate":
        rows[12] = dict(rows[11])
    else:
        rows[-1]["c"] = "NaN"
    with pytest.raises(ValueError):
        price_features_v5(rows, window_bars=15, closed_at=120 * 60000)


def test_frame_rejects_future_candle_and_retains_default_and_source_immutability():
    source = varied_batch()
    before = deepcopy(source)
    new = build_frame(source, environment="SANDBOX", feature_version=CORRECTED_VERSION)
    assert source == before
    assert new["frame_id"] == "s3-closed-1m-v5:86400000"
    assert build_frame(source, environment="SANDBOX")["frame_id"].startswith(VERSION)
    source["candles"]["BTCUSDT"][0]["t"] = source["closed_at"]
    with pytest.raises(ValueError, match="contiguous closed minutes"):
        build_frame(source, environment="SANDBOX", feature_version=CORRECTED_VERSION)


@pytest.mark.parametrize("field", ["atr_pct", "atr_reference_pct"])
@pytest.mark.parametrize(
    "bad", [None, False, -1, "NaN", "Infinity", "garbage", [], "1" * 81]
)
def test_v5_expansion_inputs_never_default_missing_or_malformed_values(field, bad):
    frame = expansion_frame("8", "2")
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["15m"][field] = bad
    with pytest.raises(ValueError, match="S3_ATR_EXPANSION_INPUT"):
        detect_candidate_events("BTCUSDT", windows, {})
    del windows["15m"][field]
    with pytest.raises(ValueError, match="S3_ATR_EXPANSION_INPUT"):
        detect_candidate_events("BTCUSDT", windows, {})


@pytest.mark.parametrize("contract", [None, {}, {"atr_expansion_contract": "UNKNOWN"}])
def test_v5_unknown_or_missing_expansion_contract_is_rejected(contract):
    frame = expansion_frame("8", "2")
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["input_evidence"]["indicator_contract"] = contract
    with pytest.raises(ValueError, match="S3_ATR_EXPANSION_CONTRACT"):
        detect_candidate_events("BTCUSDT", windows, {})


@pytest.mark.parametrize(
    "field",
    ["atr_method", "atr_reference_method", "atr_period_unit", "atr_pct_denominator"],
)
def test_v5_expansion_rejects_mismatched_methods_units_and_denominators(field):
    frame = expansion_frame("8", "2")
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["input_evidence"]["indicator_contract"][field] = "UNKNOWN"
    with pytest.raises(ValueError, match="S3_ATR_EXPANSION_CONTRACT"):
        detect_candidate_events("BTCUSDT", windows, {})


@pytest.mark.parametrize("side,kind", [("LONG", "PULSE_UP"), ("SHORT", "PULSE_DOWN")])
def test_v5_real_position_reaches_detector_including_zero_at_window_low(side, kind):
    frame = build_frame(
        breakout_batch(side), environment="SANDBOX", feature_version=CORRECTED_VERSION
    )
    events, _ = evaluate(
        frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
    )
    assert any(
        event["type"] == kind and event.get("breakout_confirmed") for event in events
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("missing", ["none", "absent"])
def test_v5_breakout_confirmation_requires_observed_flow(side, missing):
    frame = build_frame(
        breakout_batch(side), environment="SANDBOX", feature_version=CORRECTED_VERSION
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    if missing == "none":
        windows["15m"]["taker_buy_ratio"] = None
    else:
        del windows["15m"]["taker_buy_ratio"]
    events = detect_candidate_events("BTCUSDT", windows, {})
    assert not any(event.get("breakout_confirmed") for event in events)


@pytest.mark.parametrize("side,confirmed", [("LONG", False), ("SHORT", True)])
def test_v5_zero_flow_is_observed_selling_not_missing_data(side, confirmed):
    frame = build_frame(
        breakout_batch(side), environment="SANDBOX", feature_version=CORRECTED_VERSION
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["15m"]["taker_buy_ratio"] = 0.0
    events = detect_candidate_events("BTCUSDT", windows, {})
    assert any(event.get("breakout_confirmed") for event in events) is confirmed


@pytest.mark.parametrize(
    "version", [VERSION, PRICE_VERSION, ROLLING_VERSION, BREAKOUT_VERSION]
)
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_pre_v5_detector_missing_flow_confirmation_is_frozen(version, side):
    frame = build_frame(
        breakout_batch(side), environment="SANDBOX", feature_version=version
    )
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    # Explicit nonzero position exercises the legacy consumer independently of
    # v1-v3 producers' absent-position behavior, which remains golden-tested.
    windows["15m"]["close_pos"] = 100.0 if side == "LONG" else 1.0
    windows["15m"]["taker_buy_ratio"] = None
    events = detect_candidate_events("BTCUSDT", windows, {})
    assert any(event.get("breakout_confirmed") for event in events)


def test_unknown_indicator_version_cannot_silently_reuse_legacy_expansion():
    frame = expansion_frame("8", "2")
    windows = numeric_features(frame["windows"]["BTCUSDT"])
    windows["input_evidence"]["window_version"] = "s3-closed-1m-v99"
    with pytest.raises(ValueError, match="S3_INDICATOR_VERSION_UNSUPPORTED"):
        detect_candidate_events("BTCUSDT", windows, {})


@pytest.mark.parametrize(
    "last_close,position", [("99", "0"), ("100", "50"), ("101", "100")]
)
def test_v5_window_close_position_preserves_endpoints(last_close, position):
    rows = minutes(120)
    rows[-1]["c"] = last_close
    assert (
        price_features_v5(rows, window_bars=15, closed_at=120 * 60000)["close_pos"]
        == position
    )


def test_v5_flat_window_and_ambient_decimal_context_are_deterministic():
    rows = minutes(120, price="0.000000000001", spread="0")
    expected = price_features_v5(rows, window_bars=15, closed_at=120 * 60000)
    assert expected["close_pos"] == "50"
    assert expected["atr"] == expected["atr_reference"] == "0"
    with localcontext() as context:
        context.prec = 5
        context.traps[Inexact] = True
        assert (
            price_features_v5(rows, window_bars=15, closed_at=120 * 60000) == expected
        )
