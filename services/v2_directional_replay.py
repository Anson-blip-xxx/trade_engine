"""Durable, account-bound S6/S8 replay. No switch to enable order creation.

Separate replay consumers cannot consume the eventual live strategy receipt.
The provider must supply a scoped, fresh evaluation snapshot. This module does
not read legacy state or pretend its injected account data is venue-verified.
"""

from dataclasses import asdict

from v2_core.directional import (
    analysis_adjustment,
    evaluate_market,
    execution_market_gate,
    size_candidate,
    supports_event,
)
from v2_core.evidence import canonical
from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker


def decimal_features(value):
    """S3 keeps exact integer strengths/indicators; do not stringify bool/float."""
    if isinstance(value, dict):
        return {key: decimal_features(item) for key, item in value.items()}
    return str(value) if type(value) is int else value


def replay_decision(signal, context, config):
    if config["mode"] != "REPLAY_ONLY":
        raise ValueError("replay cannot authorize execution")
    if context["account_scope"] != config["account_scope"]:
        raise ValueError("replay account scope mismatch")
    if (
        context["environment"] != config["account_scope"]["environment"]
        or context["symbol"] != signal["symbol"]
    ):
        raise ValueError("replay market scope mismatch")
    now = context["assembled_at"]
    if type(now) is not int or not signal["observed_at"] <= now < min(
        signal["expires_at_ms"], context["valid_until_ms"]
    ):
        raise ValueError("replay context expired or future")
    snapshot = context["directional"]
    event = signal["features"]
    if event.get("type", signal["signal"]) != signal["signal"]:
        raise ValueError("conflicting event type")
    if not supports_event(config["strategy"], signal["signal"]):
        return StrategyDecision(
            "IGNORED",
            "UNSUPPORTED_DIRECTIONAL_EVENT",
            features_json='{"execution_authorized":false}',
        )
    plan = evaluate_market(
        config["strategy"],
        {
            "type": signal["signal"],
            "strength": decimal_features(event["strength"]),
            "breakout_confirmed": event.get("breakout_confirmed", False),
            "taker_buy_ratio": decimal_features(event.get("taker_buy_ratio")),
        },
        decimal_features(snapshot["market"]),
        price=snapshot["price"],
        regime=snapshot["regime"],
        short_ratio=snapshot["short_ratio"],
        age_ms=now - signal["observed_at"],
    )
    features = {"market_plan": asdict(plan), "execution_authorized": False}

    def result(reason):
        return StrategyDecision("IGNORED", reason, features_json=canonical(features))

    if plan.reason != "CANDIDATE":
        return result(plan.reason)
    adjustment = analysis_adjustment(snapshot["history"], mode=config["analysis_mode"])
    features["analysis"] = asdict(adjustment)
    if adjustment.factor == "0":
        return result(adjustment.reason)
    # No alternative price/ATR/history factor can be smuggled through sizing.
    sized = size_candidate(
        plan,
        **snapshot["sizing"],
        price=snapshot["price"],
        atr_pct=decimal_features(snapshot["market"]["1h"]["atr_pct"]),
        analysis_factor=adjustment.factor,
    )
    features["sizing"] = asdict(sized)
    if sized.reason != "SIZED":
        return result(sized.reason)
    gate = execution_market_gate(
        plan,
        sized,
        price=snapshot["price"],
        expected_move_pct=snapshot["expected_move_pct"],
        funding_rate=snapshot["funding_rate"],
    )
    features["execution_market_gate"] = gate
    return result("DIRECTIONAL_REPLAY_ONLY" if gate == "MARKET_GATES_PASSED" else gate)


def create_directional_replay_worker(runtime, *, strategy, analysis_mode, max_delay_ms):
    """Explicit factory; starts no loop and exposes no trading-enable flag."""
    if strategy not in {"S6", "S8"} or analysis_mode not in {"hard", "soft"}:
        raise ValueError("explicit strategy and analysis mode required")
    if runtime.scope is None:
        raise ValueError("account-bound runtime required")
    account = asdict(runtime.scope)
    scope = StrategyScope(**account, producer=strategy.lower() + "-replay")
    return StrategyWorker(
        runtime,
        scope,
        source="s3",
        strategy_version="directional-rules-v2-1",
        config={
            "strategy": strategy,
            "analysis_mode": analysis_mode,
            "mode": "REPLAY_ONLY",
            "account_scope": account,
        },
        decide=replay_decision,
        max_delay_ms=max_delay_ms,
    )
