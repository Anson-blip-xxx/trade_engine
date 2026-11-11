import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from v2_core.webhook import _PRODUCER_SETUPS, _PRODUCER_VERSION, _SIGNALS

PINE = (
    Path(__file__).resolve().parents[2] / "docs" / "tradingview_v2_universal.pine"
)


def test_pine_template_is_confirmed_bar_candidate_only_contract():
    source = PINE.read_text()
    assert "//@version=6" in source
    assert "indicator(" in source and "strategy." not in source
    assert "barstate.isconfirmed" in source
    assert "alert.freq_once_per_bar_close" in source
    assert "timeframe.in_seconds() != 15 * 60" in source
    assert "not chart.is_standard" in source
    assert 'syminfo.prefix != "BINANCE"' in source
    assert "time_close - time_close[4] == 3600000" in source
    assert "request.security" not in source
    assert "lookahead" not in source
    assert "REPLACE_WITH_V2_WEBHOOK_SECRET" in source
    assert "BINANCE_TESTNET_API_KEY" not in source
    assert "BINANCE_API_KEY" not in source


def test_pine_template_emits_exact_v2_identity_and_evidence_fields():
    source = PINE.read_text()
    for field in (
        "secret",
        "event_id",
        "observed_at",
        "expires_at_ms",
        "symbol",
        "signal",
        "price",
        "strength",
        "chg_15m",
        "chg_1h",
        "vol_1h",
        "producer",
        "version",
        "setup",
        "timeframe",
        "bar_open_ms",
        "bar_close_ms",
    ):
        assert f'\\"{field}\\"' in source
    assert "time_close + ttlMs" in source
    assert 'eventId = "tv:" + standardTicker + ":15:"' in source
    for signal in (
        "PUMP_LONG",
        "PUMP_SHORT",
        "VIOLENT_LONG",
        "VIOLENT_SHORT",
        "PULSE_UP_LONG",
        "PULSE_DOWN_SHORT",
        "TREND_UP_LONG",
        "TREND_DOWN_SHORT",
    ):
        assert signal in _SIGNALS
        assert f'"{signal}"' in source
    assert f'producerVersion = "{_PRODUCER_VERSION}"' in source
    for setup in _PRODUCER_SETUPS:
        assert f'"{setup}"' in source


def test_signal_baselines_are_prior_bars_and_only_one_alert_can_fire():
    source = PINE.read_text()
    assert "ta.sma(volume[1], volumeLength)" in source
    assert "ta.highest(high[1], breakoutLength)" in source
    assert "ta.lowest(low[1], breakoutLength)" in source
    assert "ta.sma(volume, volumeLength)" not in source
    assert "math.abs(chg" not in source
    assert "and not violentLong" not in source
    assert source.count("alert(") == 2  # One comment and one executable call.
    block = source.split("if ready\n", 1)[1].split("// A bounded", 1)[0]
    assert block.count("else if enable") == 7
    assert 'string candidate = ""' in source
    assert "Cooldown" not in source and "lastTrendBar" not in source
    assert "jsonEscape(webhookSecret)" in source
    assert "jsonEscape(eventId)" in source
    assert "jsonEscape(standardTicker)" in source
    assert '"0.##################"' in source


def expression(name, **facts):
    """Evaluate a flat numeric/boolean source expression, not the Pine runtime.

    The fixtures exercise the actual gate expressions rather than a second copy
    of their logic. They cannot establish Pine compilation or series semantics.
    """
    match = re.search(
        rf"^(?:bool|float) {name} = (.+)$", PINE.read_text(), re.MULTILINE
    )
    assert match is not None
    source = re.sub(
        r"(\w+)\[([123])\]",
        lambda found: (
            f"previous_{found[1]}" if found[2] == "1"
            else f"previous{found[2]}_{found[1]}"
        ),
        match[1],
    )
    return eval(source, {"__builtins__": {}}, {"math": SimpleNamespace(max=max), **facts})


@pytest.mark.parametrize("name", ["pulseShort", "pumpShort", "violentShort"])
def test_large_prior_drop_does_not_emit_bearish_candidate_on_upward_rebound(name):
    facts = {
        "chg15m": 8,
        "chg1h": -20,
        "close": 100,
        "open": 95,
        "liquidMove": True,
        "volumeRatio": 10,
        "pulse15m": 5,
        "pulse1h": 8,
        "pump15m": 8,
        "pump1h": 12,
        "vol1h": 40,
        "violent1h": 15,
        "rangeMid1h": 120,
        "closePosition": 0.2,
    }
    facts["movingShort"] = expression("movingShort", **facts)
    assert not expression(name, **facts)


def test_strength_counts_only_returns_aligned_with_candidate_direction():
    short_rebound = expression(
        "directionalStrength", direction=-1, chg15m=8, chg1h=20, volumeRatio=1
    )
    aligned_down = expression(
        "directionalStrength", direction=-1, chg15m=-2, chg1h=-5, volumeRatio=1
    )
    assert short_rebound == 0
    assert aligned_down == 36


def test_established_short_trend_can_emit_on_fresh_breakout_without_new_ema_transition():
    facts = {
        "trendShort": True,
        "movingShort": True,
        "trendVolume": True,
        "priorLow": 95,
        "close": 94,
        "closePosition": 0.2,
    }
    assert expression("breakoutShort", **facts)
    assert not expression("breakoutShort", **{**facts, "close": 96})
    assert not expression("breakoutShort", **{**facts, "trendVolume": False})
    # A distinct later bar's fresh low is still eligible, with no N-bar lockout.
    assert expression("breakoutShort", **{**facts, "priorLow": 94, "close": 93})


def test_short_pullback_requires_prior_retest_and_fresh_break_below_prior_low():
    facts = {
        "trendShort": True,
        "movingShort": True,
        "trendVolume": True,
        "priorShortRetest": True,
        "previous_close": 99,
        "previous_open": 98.5,
        "previous_emaSlow": 105,
        "close": 97,
        "emaFast": 99,
        "previous_low": 98,
        "closePosition": 0.25,
    }
    assert expression("resumeShort", **facts)
    assert not expression("resumeShort", **{**facts, "priorShortRetest": False})
    assert not expression("resumeShort", **{**facts, "previous_open": 100})
    assert not expression("resumeShort", **{**facts, "close": 98.5})


def test_short_retest_uses_only_the_three_prior_bars():
    facts = {
        "previous_high": 99,
        "previous_emaFast": 100,
        "previous2_high": 101,
        "previous2_emaFast": 100,
        "previous3_high": 99,
        "previous3_emaFast": 100,
    }
    assert expression("priorShortRetest", **facts)
    assert not expression("priorShortRetest", **{**facts, "previous2_high": 99})


def test_short_trend_requires_both_ema_slopes_to_fall():
    facts = {
        "emaFast": 99,
        "emaSlow": 105,
        "previous3_emaFast": 100,
        "previous3_emaSlow": 106,
        "adx": 30,
        "minAdx": 20,
        "diMinus": 30,
        "diPlus": 10,
    }
    assert expression("trendShort", **facts)
    assert not expression("trendShort", **{**facts, "previous3_emaSlow": 104})
