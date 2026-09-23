from copy import deepcopy

import pytest

from services.v2_s0_regime import VERSION, build_s0_state
from services.v2_s3_candles import VERSION as S3_VERSION


def context(*, closed_at=86400000, close="110", ema20="100"):
    window = {
        "chg": "1",
        "high": close,
        "low": close,
        "close": close,
        "ema20": ema20,
        "ema60": "95",
        "volatility": "1",
    }
    return {
        "15m": {**window, "high": "111", "low": "109"},
        "1h": dict(window),
        "4h": {**window, "ema20": "105", "ema60": "100"},
        "24h": dict(window),
        "input_evidence": {
            "source": "BINANCE_FUTURES",
            "environment": "SANDBOX",
            "window_version": S3_VERSION,
            "interval": "1m",
            "closed_at": closed_at,
            "bars_digest": "a" * 64,
            "bar_count": 1440,
        },
    }


def test_complete_bullish_universe_produces_auditable_regime():
    state = build_s0_state(
        {"BTCUSDT": context(), "ETHUSDT": context()},
        universe=["ETHUSDT", "BTCUSDT"],
        s3_frame_id="s3-closed-1m-v1:86400000",
        s3_input_digest="b" * 64,
    )
    assert state["regime"] == "bull_trend"
    assert state["breadth_ratio"] == 1.0
    assert state["risk_off"] is False
    assert state["supplemental_inputs"] == "NOT_CONFIGURED"
    assert "fng" not in state and "alts_sync" not in state
    assert state["evidence"] == {
        "algorithm": VERSION,
        "s3_frame_id": "s3-closed-1m-v1:86400000",
        "s3_input_digest": "b" * 64,
        "closed_at": 86400000,
        "universe": ["BTCUSDT", "ETHUSDT"],
    }


def test_btc_amplitude_preserves_strict_risk_off_threshold():
    btc = context(close="100")
    btc["1h"]["ema20"] = "90"
    btc["4h"].update(ema20="90", ema60="80")
    btc["15m"].update(high="103", low="99")
    at_threshold = build_s0_state(
        {"BTCUSDT": btc},
        universe=["BTCUSDT"],
        s3_frame_id="frame",
        s3_input_digest="digest",
    )
    assert at_threshold["btc_amp"] == 0.04
    assert at_threshold["risk_off"] is False
    btc["15m"]["high"] = "103.00000001"
    above = build_s0_state(
        {"BTCUSDT": btc},
        universe=["BTCUSDT"],
        s3_frame_id="frame",
        s3_input_digest="digest",
    )
    assert above["risk_off"] is True
    assert above["regime"] == "risk-off"


@pytest.mark.parametrize("failure", ["missing", "extra", "mixed_time", "bad_number"])
def test_incomplete_or_inconsistent_s3_facts_never_default_to_neutral(failure):
    windows = {"BTCUSDT": context(), "ETHUSDT": context()}
    if failure == "missing":
        del windows["ETHUSDT"]
    elif failure == "extra":
        windows["SOLUSDT"] = context()
    elif failure == "mixed_time":
        windows["ETHUSDT"]["input_evidence"]["closed_at"] += 60000
    else:
        windows["BTCUSDT"]["4h"]["ema60"] = "NaN"
    with pytest.raises(ValueError):
        build_s0_state(
            windows,
            universe=["BTCUSDT", "ETHUSDT"],
            s3_frame_id="frame",
            s3_input_digest="digest",
        )


def test_computation_does_not_mutate_archived_s3_frame():
    windows = {"BTCUSDT": context(), "ETHUSDT": context()}
    original = deepcopy(windows)
    build_s0_state(
        windows,
        universe=["BTCUSDT", "ETHUSDT"],
        s3_frame_id="frame",
        s3_input_digest="digest",
    )
    assert windows == original
