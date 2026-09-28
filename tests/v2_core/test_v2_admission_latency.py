"""No real venue calls: freshness, source policy and bounded scheduling regressions."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_directional import inputs
from test_v2_directional_replay import SCOPE, scenario
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal, strategy_worker

from services.v2_directional_context import DirectionalContext
from services.v2_directional_replay import (
    MARKET_ONLY_REJECTIONS,
    market_plan,
    replay_decision,
)
from v2_core.scheduling import StrategyScheduler
from v2_core.signals import Signals
from v2_core.strategy import StrategyDecision

database = database_fixture


def test_priority_bypasses_unrelated_fresh_events_and_keeps_expiry(database):
    signals = Signals(database)
    for n in range(30):
        signals.admit(
            source="tv_bridge",
            environment="SANDBOX",
            request_key=f"noise-{n}",
            snapshot={
                "observed_at": 1,
                "expires_at_ms": 9,
                "symbol": "BTCUSDT",
                "signal": "HIGH_VOL",
                "features": {},
            },
        )
    target = scheduler_signal(database, "actionable")
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: {}, priority_events=("TREND_UP",)
    )
    assert scheduler.run_once(1) == {target: "PREPARED"}
    assert worker.decision(target)["snapshot"]["expires_at_ms"] <= 10
    assert scheduler.progress()["undiscovered"] == 30


def test_prepared_yields_without_consuming_remaining_candidates(database):
    first = scheduler_signal(database, "first", expires=10)
    second = scheduler_signal(database, "second", expires=11)
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once(20, stop_on_prepared=True) == {first: "PREPARED"}
    assert worker.decision(second) is None
    with database() as c:
        assert c.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE lease_token IS NOT NULL"
        ).fetchone() == (0,)


def test_time_budget_yields_after_inflight_work_without_leasing_next(database):
    first = scheduler_signal(database, "first", expires=10)
    second = scheduler_signal(database, "second", expires=11)
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    elapsed = [0.0]

    def context(_):
        elapsed[0] += 2
        return {}

    scheduler = StrategyScheduler(
        worker, context_provider=context, monotonic=lambda: elapsed[0]
    )
    assert scheduler.run_once(20, budget_ms=1000) == {first: "IGNORED"}
    assert worker.decision(second) is None
    # Next cycle resumes, not loss/expiry manipulation.
    assert scheduler.run_once(20, budget_ms=1000) == {second: "IGNORED"}


@pytest.mark.parametrize("value", [0, -1, 60001, True, 1.5])
def test_invalid_time_budget_rejected(database, value):
    worker, _ = strategy_worker(database)
    with pytest.raises(ValueError, match="time budget"):
        StrategyScheduler(worker, context_provider=lambda _: {}).run_once(
            budget_ms=value
        )


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_tv_trend_thresholds_are_source_specific_with_unmodified_strength(
    database, strategy
):
    replay, _, sid, context = scenario(database, strategy)
    with database() as c:
        signal = replay._read(c, sid)[0]
    signal["features"]["strength"] = 20
    context["directional"]["market"]["15m"]["taker_buy_ratio"] = (
        "0.6" if strategy == "S6" else "0.4"
    )
    config = {
        **json.loads(replay.config_json),
        "source": "tv_bridge",
        "policy": {"tv.trend_policy_enabled": True},
    }
    before = deepcopy(signal)
    decision = replay_decision(signal, context, config)
    assert decision.rationale == "DIRECTIONAL_REPLAY_ONLY"
    proof = json.loads(decision.features_json)
    assert proof["source_scoring"]["raw_strength"] == 20
    assert proof["source_scoring"]["profile"] == "tv-trend-v1"
    assert proof["market_plan"]["score"] == 25
    assert proof["market_plan"]["leverage"] == 2
    assert signal == before
    assert replay_decision(signal, context, {**config, "source": "s3"}).rationale in {
        "strength_below_minimum",
        "score_below_minimum",
    }
    assert replay_decision(signal, context, {**config, "policy": {}}).rationale in {
        "strength_below_minimum",
        "score_below_minimum",
    }
    context["directional"]["price"] = "104" if strategy == "S6" else "96"
    assert replay_decision(signal, context, config).rationale == "overextended"


def test_tv_profile_does_not_change_pulse_or_profitability_gate(database):
    replay, _, sid, context = scenario(database, "S8")
    with database() as c:
        signal = replay._read(c, sid)[0]
    config = {
        **json.loads(replay.config_json),
        "source": "tv_bridge",
        "policy": {"tv.trend_policy_enabled": True},
    }
    signal["signal"] = signal["features"]["type"] = "PULSE_DOWN"
    signal["features"]["strength"] = 20
    assert (
        replay_decision(signal, context, config).rationale == "strength_below_minimum"
    )
    signal["signal"] = signal["features"]["type"] = "TREND_DOWN"
    context["directional"]["market"]["15m"]["taker_buy_ratio"] = "0.4"
    context["directional"]["expected_move_pct"] = "0.1"
    assert replay_decision(signal, context, config).rationale == "REWARD_RISK_TOO_LOW"


def test_market_only_context_cannot_open_even_if_screen_policy_changes(database):
    replay, _, sid, context = scenario(database, "S8")
    with database() as c:
        signal = replay._read(c, sid)[0]
    signal["features"]["strength"] = 20
    context["market_only"] = True
    config = json.loads(replay.config_json)
    assert (
        replay_decision(signal, context, config).rationale == "strength_below_minimum"
    )
    with pytest.raises(ValueError, match="FULL_ACCOUNT_CONTEXT_REQUIRED"):
        replay_decision(
            signal,
            context,
            {
                **config,
                "source": "tv_bridge",
                "policy": {"tv.trend_policy_enabled": True},
            },
        )


def test_market_screen_avoids_account_calls_only_for_structural_rejection():
    facts = inputs("S8")
    facts["market"]["15m"]["close"] = "100"
    market = {
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "assembled_at": 2,
        "valid_until_ms": 9,
        "sources": {
            "s3": {"features": facts["market"]},
            "s0": {"features": {"regime": "neutral"}},
        },
    }
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": 1,
        "expires_at_ms": 10,
        "signal": "TREND_DOWN",
        "features": {"strength": 20},
    }
    calls = []
    account = {
        "account_scope": asdict(SCOPE),
        "symbol": "BTCUSDT",
        "evidence": {"QA": True},
        "observed_at_ms": 2,
        "valid_until_ms": 9,
        "short_ratio": "0.5",
        "history": {},
        "sizing": {},
        "expected_move_pct": "1",
        "funding_rate": "0",
    }
    provider = DirectionalContext(
        lambda _: market,
        lambda _: calls.append("account") or account,
        scope=SCOPE,
        clock_ms=lambda: 3,
        market_screen=lambda s, c: (
            market_plan(s, c, {"strategy": "S8"})[0].reason in MARKET_ONLY_REJECTIONS
        ),
    )
    result = provider(signal)
    assert result["market_only"] is True and not calls
    assert "account_evidence" not in result and "sizing" not in result["directional"]
    signal["features"]["strength"] = 70
    assert "market_only" not in provider(signal)
    assert calls == ["account"]
    # Market staleness is checked before potentially expensive account reads.
    market["valid_until_ms"] = 3
    with pytest.raises(ValueError, match="market context"):
        provider(signal)
    assert calls == ["account"]


@pytest.mark.parametrize("strength,account_required", [(10, False), (28, True)])
def test_score_bound_filters_only_when_real_ratio_cannot_rescue(
    strength, account_required
):
    facts = inputs("S6")
    facts["market"]["15m"]["close"] = "100"
    market = {
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "assembled_at": 2,
        "valid_until_ms": 9,
        "sources": {
            "s3": {"features": facts["market"]},
            "s0": {"features": {"regime": "neutral"}},
        },
    }
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": 1,
        "expires_at_ms": 10,
        "signal": "TREND_UP",
        "features": {"strength": strength},
    }
    calls = []
    account = {
        "account_scope": asdict(SCOPE),
        "symbol": "BTCUSDT",
        "evidence": {"QA": True},
        "observed_at_ms": 2,
        "valid_until_ms": 9,
        "short_ratio": "1",
        "history": {},
        "sizing": {},
        "expected_move_pct": "1",
        "funding_rate": "0",
    }
    provider = DirectionalContext(
        lambda _: market,
        lambda _: calls.append("account") or account,
        scope=SCOPE,
        clock_ms=lambda: 3,
        market_screen=lambda s, c: (
            market_plan(s, c, {"strategy": "S6"})[0].reason in MARKET_ONLY_REJECTIONS
        ),
    )
    result = provider(signal)
    assert bool(calls) is account_required
    assert bool(result.get("market_only")) is not account_required
    if not account_required:
        assert result["directional"]["short_ratio"] is None
        assert (
            market_plan(signal, result, {"strategy": "S6"})[0].reason
            == "score_unreachable"
        )
