from pathlib import Path

from v2_core.webhook import _SIGNALS

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
