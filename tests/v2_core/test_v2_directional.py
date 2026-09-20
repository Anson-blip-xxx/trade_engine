"""Market-rule migration and sizing invariants, without legacy executor imports."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from v2_core.directional import (
    analysis_adjustment,
    evaluate_market,
    execution_market_gate,
    size_candidate,
)


def inputs(strategy="S6", kind=None):
    return {
        "strategy": strategy,
        "event": {
            "type": kind or ("TREND_UP" if strategy == "S6" else "TREND_DOWN"),
            "strength": "70",
            "breakout_confirmed": False,
            "taker_buy_ratio": None,
        },
        "market": {
            "15m": {"ema20": "100", "atr": "1", "rsi": "50", "taker_buy_ratio": "0.5"},
            "1h": {"ema20": "100", "atr": "1", "atr_pct": "1"},
            "4h": {"ema20": "95", "chg": "1"},
            "24h": {"ema20": "95", "ema60": "90", "chg": "5"},
        },
        "price": "100",
        "regime": "neutral",
        "short_ratio": "0.5",
        "age_ms": 0,
    }


def sizing(**overrides):
    params = {
        "price": "100",
        "balance": "1000",
        "available_margin": "800",
        "used_pool_margin": "0",
        "atr_pct": "1",
        "quantity_step": "0.001",
        "price_tick": "0.01",
        "min_quantity": "0.001",
        "max_quantity": "100",
        "min_notional": "5",
        "max_notional": "10000",
        "drawdown_factor": "1",
        "analysis_factor": "1",
    }
    return dict(params, **overrides)


@pytest.mark.parametrize(
    "strategy,kind,margin,stop",
    [
        ("S6", "PULSE_UP", "ISOLATED", "0.04"),
        ("S6", "TREND_UP", "CROSSED", "0.08"),
        ("S6", "VIOLENT_BULLISH", "CROSSED", "0.08"),
        ("S6", "PUMP_UP", "ISOLATED", "0.1"),
        ("S8", "PULSE_DOWN", "ISOLATED", "0.04"),
        ("S8", "TREND_DOWN", "CROSSED", "0.08"),
        ("S8", "VIOLENT_BEARISH", "CROSSED", "0.08"),
        ("S8", "PUMP_DOWN", "ISOLATED", "0.08"),
        ("S8", "PANIC_SELL", "ISOLATED", "0.035"),
    ],
)
def test_signal_rules(strategy, kind, margin, stop):
    data = inputs(strategy, kind)
    data["market"]["15m"]["taker_buy_ratio"] = "0.6" if strategy == "S6" else "0.4"
    before = deepcopy(data)
    plan = evaluate_market(**data)
    assert data == before
    assert plan.reason == "CANDIDATE"
    assert plan.score == 75
    assert plan.margin_mode == margin
    assert plan.stop_fraction == stop
    assert plan.leverage == (2 if kind.startswith("PUMP_") else 3)
    assert plan.system_tag == (
        "S6B" if kind == "PUMP_UP" else "S6A" if strategy == "S6" else "S8"
    )


@pytest.mark.parametrize(
    "strategy,price,flow,rsi",
    [
        ("S6", "98", "0.52", "35"),
        ("S8", "102", "0.48", "65"),
    ],
)
def test_left_reversal(strategy, price, flow, rsi):
    data = inputs(strategy)
    data["price"] = price
    data["market"]["15m"].update(taker_buy_ratio=flow, rsi=rsi)
    assert evaluate_market(**data).entry_mode == "LEFT_REVERSAL"
    data["market"]["15m"]["rsi"] = "50"
    assert evaluate_market(**data).reason == "entry_mode_unconfirmed"


@pytest.mark.parametrize("kind", ["TREND_DOWN", "PULSE_DOWN", "VIOLENT_BEARISH"])
def test_short_strength_boundary(kind):
    data = inputs("S8", kind)
    data["event"]["strength"] = "59.999"
    assert evaluate_market(**data).reason == "strength_below_minimum"
    data["event"]["strength"] = "60"
    assert evaluate_market(**data).reason == "CANDIDATE"


@pytest.mark.parametrize("regime", ["weak_bear", "risk-off", "risk_off", "WEAK_BEAR"])
def test_violent_long_regime(regime):
    data = inputs(kind="VIOLENT_BULLISH")
    data["regime"] = regime
    assert evaluate_market(**data).reason == "regime_conflict"


@pytest.mark.parametrize(
    "strategy,kind,price,limit",
    [
        ("S6", "TREND_UP", "102", "102.001"),
        ("S8", "TREND_DOWN", "98", "97.999"),
        ("S6", "VIOLENT_BULLISH", "101.25", "101.251"),
        ("S8", "VIOLENT_BEARISH", "98.75", "98.749"),
    ],
)
def test_extension_boundary(strategy, kind, price, limit):
    data = inputs(strategy, kind)
    data["price"] = price
    assert evaluate_market(**data).reason == "CANDIDATE"
    data["price"] = limit
    assert evaluate_market(**data).reason == "overextended"


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_atr_boundary(strategy):
    data = inputs(strategy)
    data["market"]["1h"]["atr_pct"] = "6"
    assert evaluate_market(**data).reason == "CANDIDATE"
    data["market"]["1h"]["atr_pct"] = "6.001"
    assert evaluate_market(**data).reason == "atr_exceeded"


def test_takeover_and_pump_guard():
    data = inputs(kind="PUMP_UP")
    assert evaluate_market(**data).reason == "takeover_not_ready"
    data["market"]["15m"]["taker_buy_ratio"] = "0.6"
    data["market"]["1h"]["atr_pct"] = "12"
    plan = evaluate_market(**data)
    assert (plan.entry_mode, plan.stop_fraction, plan.leverage) == (
        "S6B_TREND_TAKEOVER",
        "0.12",
        2,
    )
    data["market"]["1h"]["atr_pct"] = "12.001"
    assert evaluate_market(**data).reason == "atr_exceeded"
    data = inputs("S8", "PUMP_DOWN")
    data["market"]["24h"]["chg"] = "15"
    assert evaluate_market(**data).reason == "pump_down_uptrend"


def test_breakout_and_violent_takeover():
    data = inputs()
    data["event"]["breakout_confirmed"] = True
    data["market"]["15m"]["taker_buy_ratio"] = "0.6"
    assert evaluate_market(**data).system_tag == "S6B"
    data["event"].update(type="VIOLENT_BULLISH", breakout_confirmed=False)
    data["market"]["4h"]["chg"] = "3.1"
    data["market"]["24h"]["chg"] = "10.1"
    assert evaluate_market(**data).system_tag == "S6B"


def test_zero_event_flow_is_not_neutralized_by_fallback():
    data = inputs()
    data["market"]["15m"]["taker_buy_ratio"] = "0.6"
    assert evaluate_market(**data).score == 75
    data["event"]["taker_buy_ratio"] = "0"
    assert evaluate_market(**data).score == 65


def test_score_age_and_floor():
    data = inputs()
    assert evaluate_market(**data).score == 65
    data["age_ms"] = 300000
    assert evaluate_market(**data).score == 55
    data["event"]["strength"] = "30"
    assert evaluate_market(**data).reason == "score_below_minimum"


@pytest.mark.parametrize(
    "bad", [None, True, 1.5, "NaN", "Infinity", "-1", "1e30", "1e-19"]
)
def test_invalid_price_rejected(bad):
    data = inputs()
    data["price"] = bad
    with pytest.raises(ValueError):
        evaluate_market(**data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("strength", "101"),
        ("strength", True),
        ("breakout_confirmed", "false"),
        ("taker_buy_ratio", "1.1"),
        ("type", "TREND_DOWN"),
    ],
)
def test_invalid_event_rejected(field, value):
    data = inputs()
    data["event"][field] = value
    with pytest.raises(ValueError):
        evaluate_market(**data)


def test_missing_indicator_does_not_become_zero():
    data = inputs()
    del data["market"]["1h"]["ema20"]
    with pytest.raises(KeyError):
        evaluate_market(**data)


@pytest.mark.parametrize("strategy,stop", [("S6", "92"), ("S8", "108")])
def test_exact_risk_cap(strategy, stop):
    plan = evaluate_market(**inputs(strategy))
    result = size_candidate(plan, **sizing())
    assert (result.reason, result.quantity, result.stop_price) == (
        "SIZED",
        "1.25",
        stop,
    )
    assert result.notional == "125"
    assert result.planned_loss == "10"


@pytest.mark.parametrize(
    "overrides",
    [
        {"balance": "0"},
        {"used_pool_margin": "800"},
        {"used_pool_margin": "900"},
        {"available_margin": "0"},
        {"available_margin": "0.01"},
        {"drawdown_factor": "0"},
        {"analysis_factor": "0"},
        {"min_notional": "126"},
        {"min_quantity": "2"},
        {"quantity_step": "10"},
    ],
)
def test_no_minimum_uplift(overrides):
    result = size_candidate(evaluate_market(**inputs()), **sizing(**overrides))
    assert result.reason == "INSUFFICIENT_BUDGET"
    assert result.quantity == "0"


@pytest.mark.parametrize("factor", ["0.25", "0.5", "1"])
@pytest.mark.parametrize("step", ["0.001", "0.03", "0.5"])
def test_rounding_never_enlarges_budget(factor, step):
    result = size_candidate(
        evaluate_market(**inputs()),
        **sizing(drawdown_factor=factor, quantity_step=step),
    )
    qty = Decimal(result.quantity)
    assert qty % Decimal(step) == 0
    assert Decimal(result.planned_loss) <= Decimal(10) * Decimal(factor)
    assert qty * 100 <= Decimal(125) * Decimal(factor)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_stop_tick_toward_entry(strategy):
    result = size_candidate(
        evaluate_market(**inputs(strategy)), **sizing(price_tick="3")
    )
    assert result.stop_price == ("93" if strategy == "S6" else "108")
    assert Decimal(result.planned_loss) <= 10
    result = size_candidate(
        evaluate_market(**inputs(strategy)), **sizing(price_tick="100")
    )
    assert result.reason == "INVALID_STOP_TICK"


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity_step", "0"),
        ("price_tick", "0"),
        ("analysis_factor", "1.01"),
        ("drawdown_factor", "-1"),
        ("balance", "NaN"),
        ("available_margin", "-1"),
        ("min_quantity", "101"),
        ("min_notional", "10001"),
    ],
)
def test_invalid_sizing_inputs(field, value):
    with pytest.raises(ValueError):
        size_candidate(evaluate_market(**inputs()), **sizing(**{field: value}))


def test_caller_decimal_precision_does_not_change_result():
    plan = evaluate_market(**inputs())
    expected = size_candidate(plan, **sizing(price="101.12345678"))
    with localcontext() as ctx:
        ctx.prec = 5
        assert size_candidate(plan, **sizing(price="101.12345678")) == expected


def test_venue_caps_and_available_margin():
    plan = evaluate_market(**inputs())
    for limits in ({"max_quantity": "0.5"}, {"max_notional": "50"}):
        assert size_candidate(plan, **sizing(**limits)).quantity == "0.5"
    result = size_candidate(plan, **sizing(available_margin="10"))
    assert result.quantity == "0.3"


def test_rejected_or_forged_plan_not_sized():
    plan = evaluate_market(**inputs())
    for altered in (
        replace(plan, reason="overextended"),
        replace(plan, leverage=20),
        replace(plan, score=True),
        replace(plan, side="BUY"),
    ):
        with pytest.raises(ValueError):
            size_candidate(altered, **sizing())


def history(**changes):
    return dict(
        {
            "trades": 6,
            "win_rate": "35",
            "avg_quality_score": "40",
            "t60_avg_post_close_return_pct": "-0.8",
            "avg_pct": "0",
        },
        **changes,
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({}, "ACCEPTABLE_HISTORY"),
        (
            {"trades": 5, "win_rate": "0", "avg_quality_score": "0"},
            "INSUFFICIENT_HISTORY",
        ),
        ({"win_rate": "34.99", "avg_quality_score": "39.99"}, "LOW_QUALITY"),
        ({"win_rate": "34.99"}, "ACCEPTABLE_HISTORY"),
        ({"avg_quality_score": "39.99"}, "ACCEPTABLE_HISTORY"),
        ({"t60_avg_post_close_return_pct": "-0.801"}, "BAD_FOLLOW"),
        (
            {"t60_avg_post_close_return_pct": "-0.801", "avg_pct": "0.01"},
            "ACCEPTABLE_HISTORY",
        ),
    ],
)
@pytest.mark.parametrize("mode", ["hard", "soft"])
def test_analysis_thresholds(changes, reason, mode):
    result = analysis_adjustment(history(**changes), mode=mode)
    assert result.reason == reason
    assert result.factor == (
        ("0" if mode == "hard" else "0.5")
        if reason in {"LOW_QUALITY", "BAD_FOLLOW"}
        else "1"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"trades": True},
        {"trades": -1},
        {"win_rate": "101"},
        {"avg_quality_score": None},
        {"avg_pct": "NaN"},
    ],
)
def test_malformed_history_is_not_insufficient_history(changes):
    with pytest.raises(ValueError):
        analysis_adjustment(history(**changes), mode="hard")


def test_missing_history_and_implicit_disable_rejected():
    with pytest.raises(KeyError):
        analysis_adjustment({}, mode="hard")
    with pytest.raises(ValueError):
        analysis_adjustment(history(), mode="off")


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize(
    "move,reason",
    [
        ("0", "MARKET_GATES_PASSED"),
        ("8", "MARKET_GATES_PASSED"),
        ("7.999999999999999999", "REWARD_RISK_TOO_LOW"),
    ],
)
def test_reward_risk_exact_boundary(strategy, move, reason):
    plan = evaluate_market(**inputs(strategy))
    sized = size_candidate(plan, **sizing())
    assert (
        execution_market_gate(
            plan, sized, price="100", expected_move_pct=move, funding_rate="0"
        )
        == reason
    )


@pytest.mark.parametrize(
    "strategy,rate,reason",
    [
        ("S6", "0.001", "MARKET_GATES_PASSED"),
        ("S6", "0.001000000000000001", "ADVERSE_FUNDING"),
        ("S8", "-0.001", "MARKET_GATES_PASSED"),
        ("S8", "-0.001000000000000001", "ADVERSE_FUNDING"),
        ("S6", "-0.01", "MARKET_GATES_PASSED"),
        ("S8", "0.01", "MARKET_GATES_PASSED"),
    ],
)
def test_funding_exact_boundary(strategy, rate, reason):
    plan = evaluate_market(**inputs(strategy))
    sized = size_candidate(plan, **sizing())
    assert (
        execution_market_gate(
            plan, sized, price="100", expected_move_pct="8", funding_rate=rate
        )
        == reason
    )


def test_soft_history_and_drawdown_both_reduce_quantity():
    plan = evaluate_market(**inputs())
    adjustment = analysis_adjustment(
        history(win_rate="20", avg_quality_score="20"), mode="soft"
    )
    sized = size_candidate(
        plan, **sizing(analysis_factor=adjustment.factor, drawdown_factor="0.25")
    )
    assert sized.quantity == "0.156"
    assert Decimal(sized.planned_loss) <= Decimal("1.25")


@pytest.mark.parametrize("bad", [None, "NaN", "-1"])
def test_missing_or_bad_estimate_cannot_skip_gate(bad):
    plan = evaluate_market(**inputs())
    sized = size_candidate(plan, **sizing())
    with pytest.raises(ValueError):
        execution_market_gate(
            plan, sized, price="100", expected_move_pct=bad, funding_rate="0"
        )
