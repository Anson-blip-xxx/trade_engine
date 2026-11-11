"""Opt-in entry confirmation semantics; no venue, database or policy writes."""

from copy import deepcopy

import pytest
from test_v2_directional import inputs

from services.v2_directional_replay import source_policy
from v2_core.directional import (
    below_minimum_strength,
    directional_events,
    evaluate_market,
)
from v2_core.runtime_policy import resolve


def warm_evidence():
    return {
        "window_version": "s3-closed-1m-v5",
        "bar_count": 1440,
        "indicator_contract": {
            "input_interval": "1m",
            "window_semantics": "ROLLING_1M_LOOKBACK",
            "ema_period_unit": "1m_bar",
            "ema_periods": [20, 60],
            "ema_seed": "FIRST_PERIOD_SMA",
            "ema_insufficient_history": "REJECT",
            "ema_warmup_min_bars": 120,
            "ema_input_bars": {"15m": 120, "1h": 120, "4h": 240, "24h": 1440},
        },
    }


def confirmed_inputs(strategy="S6", kind=None):
    return {
        **inputs(strategy, kind),
        "policy": {"entry.confirmation_version": 2},
        "indicator_evidence": warm_evidence(),
    }


def test_confirmation_version_is_explicit_and_off_by_default():
    assert resolve()["entry.confirmation_version"] == 1
    assert resolve({"entry.confirmation_version": 2})["entry.confirmation_version"] == 2


@pytest.mark.parametrize("version", [0, 3, True, "2", 2.0, None])
def test_invalid_confirmation_version(version):
    with pytest.raises(ValueError, match="INVALID_POLICY"):
        resolve({"entry.confirmation_version": version})


@pytest.mark.parametrize("kind", directional_events("S8"))
def test_all_short_kinds_share_configured_strength_boundary(kind):
    data = confirmed_inputs("S8", kind)
    data["policy"]["entry.short_min_strength"] = "63"
    data["market"]["15m"]["taker_buy_ratio"] = "0.4"
    for strength, rejected in [("62.999", True), ("63", False), ("63.001", False)]:
        data["event"]["strength"] = strength
        assert (
            below_minimum_strength("S8", kind, strength, policy=data["policy"])
            is rejected
        )
        assert evaluate_market(**data).reason == (
            "strength_below_minimum" if rejected else "CANDIDATE"
        )


@pytest.mark.parametrize("kind", directional_events("S6"))
@pytest.mark.parametrize("regime", ["weak_bear", "WEAK_BEAR", "risk-off", "RISK_OFF"])
def test_all_long_kinds_share_existing_bear_veto(kind, regime):
    data = confirmed_inputs(kind=kind)
    data["regime"] = regime
    assert evaluate_market(**data).reason == "regime_conflict"


@pytest.mark.parametrize("regime", ["neutral", "bull", "BEAR"])
def test_confirmation_does_not_invent_new_regime_labels(regime):
    data = confirmed_inputs()
    data["regime"] = regime
    assert evaluate_market(**data).reason == "CANDIDATE"


@pytest.mark.parametrize(
    "strategy,price,rsi,flow,confirmed",
    [
        ("S6", "98", "35", None, False),
        ("S6", "98", "35", "0", False),
        ("S6", "98", "35", "0.519", False),
        ("S6", "98", "35", "0.52", True),
        ("S8", "102", "65", None, False),
        ("S8", "102", "65", "0", True),
        ("S8", "102", "65", "0.48", True),
        ("S8", "102", "65", "0.481", False),
    ],
)
def test_reversal_requires_observed_directional_flow(
    strategy, price, rsi, flow, confirmed
):
    data = confirmed_inputs(strategy)
    data["price"] = price
    data["event"]["taker_buy_ratio"] = flow
    data["market"]["15m"].update(rsi=rsi, taker_buy_ratio=None)
    plan = evaluate_market(**data)
    assert plan.reason == ("CANDIDATE" if confirmed else "entry_mode_unconfirmed")
    if confirmed:
        assert plan.entry_mode == "LEFT_REVERSAL"


@pytest.mark.parametrize("kind", ["PUMP_UP", "VIOLENT_BULLISH", "TREND_UP"])
@pytest.mark.parametrize(
    "event_flow,market_flow,reason",
    [
        (None, None, "takeover_flow_unconfirmed"),
        (None, "0.6", "CANDIDATE"),
        ("0", "0.6", "takeover_not_ready"),
        ("0.519", "0.6", "takeover_not_ready"),
        ("0.52", "0", "CANDIDATE"),
        ("0.6", None, "CANDIDATE"),
    ],
)
def test_takeover_uses_same_authoritative_flow_as_entry(
    kind, event_flow, market_flow, reason
):
    data = confirmed_inputs(kind=kind)
    data["event"]["taker_buy_ratio"] = event_flow
    data["market"]["15m"]["taker_buy_ratio"] = market_flow
    if kind == "VIOLENT_BULLISH":
        data["market"]["4h"]["chg"] = "3.1"
        data["market"]["24h"]["chg"] = "10.1"
    elif kind == "TREND_UP":
        data["event"]["breakout_confirmed"] = True
    before = deepcopy(data)
    assert evaluate_market(**data).reason == reason
    assert data == before


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("evidence", [None, {}, {"window_version": "s3-closed-1m-v4"}])
def test_every_v2_entry_requires_indicator_contract(strategy, evidence):
    data = confirmed_inputs(strategy)
    data["indicator_evidence"] = evidence
    assert evaluate_market(**data).reason == "indicator_evidence_unconfirmed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_interval", "15m"),
        ("window_semantics", "RESAMPLED_CANDLES"),
        ("ema_period_unit", "window_bar"),
        ("ema_periods", [20]),
        ("ema_seed", "LATEST_PRICE"),
        ("ema_insufficient_history", "LATEST_PRICE_FALLBACK"),
        ("ema_warmup_min_bars", True),
        ("ema_warmup_min_bars", "120"),
        ("ema_warmup_min_bars", 15),
        ("ema_input_bars", None),
    ],
)
def test_fallback_or_ambiguous_indicator_contract_cannot_confirm(field, value):
    data = confirmed_inputs(kind="PUMP_UP")
    data["market"]["15m"]["taker_buy_ratio"] = "0.6"
    data["indicator_evidence"]["indicator_contract"][field] = value
    assert evaluate_market(**data).reason == "indicator_evidence_unconfirmed"


@pytest.mark.parametrize("window", ["15m", "1h", "4h", "24h"])
@pytest.mark.parametrize("count", [True, "120", 0, 15, 119, 2000])
def test_declared_warmup_does_not_replace_valid_input_counts(window, count):
    data = confirmed_inputs()
    data["indicator_evidence"]["indicator_contract"]["ema_input_bars"][window] = count
    assert evaluate_market(**data).reason == "indicator_evidence_unconfirmed"


@pytest.mark.parametrize("count", [True, "1440", 120, 1439, 1441])
def test_indicator_input_count_must_match_frozen_contract(count):
    data = confirmed_inputs()
    data["indicator_evidence"]["bar_count"] = count
    assert evaluate_market(**data).reason == "indicator_evidence_unconfirmed"


@pytest.mark.parametrize("kind", ["TREND_DOWN", "PUMP_DOWN", "PANIC_SELL"])
def test_tv_override_remains_explicit_and_trend_only(kind):
    config = {
        "source": "tv_bridge",
        "policy": {"entry.confirmation_version": 2, "tv.trend_policy_enabled": True},
    }
    signal = {"signal": kind, "features": {"strength": "20"}}
    policy, proof = source_policy(signal, config)
    assert policy["entry.confirmation_version"] == 2
    data = confirmed_inputs("S8", kind)
    data["policy"] = policy
    data["event"]["strength"] = "20"
    data["market"]["15m"]["taker_buy_ratio"] = "0.4"
    if kind == "TREND_DOWN":
        assert proof["short_min_strength"] == "20"
        assert evaluate_market(**data).reason == "CANDIDATE"
        data["event"]["strength"] = "19.999"
    else:
        assert proof["short_min_strength"] == "60"
    assert evaluate_market(**data).reason == "strength_below_minimum"


@pytest.mark.parametrize(
    "case", ["reversal", "takeover_missing", "takeover_zero", "panic", "pump", "bear"]
)
def test_version_one_retains_known_legacy_behavior(case):
    data = inputs()
    if case == "reversal":
        data["price"] = "98"
        data["market"]["15m"].update(rsi="35", taker_buy_ratio=None)
    elif case.startswith("takeover"):
        data["event"]["type"] = "PUMP_UP"
        data["market"]["15m"]["taker_buy_ratio"] = (
            None if case == "takeover_missing" else "0.6"
        )
        if case == "takeover_zero":
            data["event"]["taker_buy_ratio"] = "0"
    elif case in {"panic", "pump"}:
        data = inputs("S8", "PANIC_SELL" if case == "panic" else "PUMP_DOWN")
        data["event"]["strength"] = "59"
    else:
        data["regime"] = "WEAK_BEAR"
    old = evaluate_market(**data)
    assert old.reason == "CANDIDATE"
    explicit = evaluate_market(
        **data,
        policy={"entry.confirmation_version": 1},
        indicator_evidence={"bad": True},
    )
    assert explicit == old
