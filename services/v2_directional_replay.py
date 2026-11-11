"""Durable, account-bound S6/S8 replay. No switch to enable order creation.

Separate replay consumers cannot consume the eventual live strategy receipt.
The provider must supply a scoped, fresh evaluation snapshot. This module does
not read legacy state or pretend its injected account data is venue-verified.
"""

from dataclasses import asdict, replace

from services.v2_trend_entry import REJECTIONS as TREND_REJECTIONS
from v2_core.directional import (
    analysis_adjustment,
    evaluate_market,
    execution_market_gate,
    size_candidate,
    supports_event,
)
from v2_core.evidence import canonical
from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker

# These rejects cannot become candidates by adding account sentiment/history.
# In particular score_below_minimum is NOT safe: real sentiment can add points.
MARKET_ONLY_REJECTIONS = frozenset(
    {
        "regime_conflict",
        "strength_below_minimum",
        "pump_down_uptrend",
        "entry_mode_unconfirmed",
        "takeover_not_ready",
        "indicator_evidence_unconfirmed",
        "takeover_flow_unconfirmed",
        "overextended",
        "atr_exceeded",
        "zero_volatility",
        "score_unreachable",
        "TV_DIRECTION_CONFLICT",
    }
    | TREND_REJECTIONS
)


def source_policy(signal, config):
    from v2_core.runtime_policy import resolve

    policy = resolve(config.get("policy"))
    structural = (
        policy["entry.trend_structure_enabled"] and config.get("source") != "quant"
    )
    selected = (
        config.get("source") == "tv_bridge"
        and signal["signal"] in {"TREND_UP", "TREND_DOWN"}
        and policy["tv.trend_policy_enabled"]
        and not structural
    )
    proof = {
        "version": "source-thresholds-v1",
        "source": config.get("source", "s3"),
        "profile": "causal-trend-entry-v1"
        if structural
        else "tv-trend-v1"
        if selected
        else "directional-default-v1",
        "raw_strength": signal["features"]["strength"],
    }
    if selected:
        policy = {
            **policy,
            "entry.short_min_strength": policy["tv.trend_min_strength"],
            "entry.min_score": policy["tv.trend_min_score"],
        }
    if structural:
        from decimal import Decimal

        policy = {
            **policy,
            "sizing.cost_buffer_fraction": str(
                max(
                    Decimal(policy["sizing.cost_buffer_fraction"]),
                    Decimal(policy["trend.roundtrip_cost_fraction"]),
                )
            ),
        }
    proof.update(
        min_score=policy["entry.min_score"],
        short_min_strength=policy["entry.short_min_strength"],
    )
    return policy, proof


def bound_indicator_evidence(context):
    """Bind warmup metadata to the same scoped, closed market observation.

    An invalid contract is a terminal candidate rejection, not a reason to keep
    retrying this signal. Outer context freshness is checked by its provider.
    """
    market = context["directional"]["market"]
    evidence = market.get("input_evidence")
    source = context.get("sources", {}).get("s3")
    now = context.get("assembled_at")
    if not isinstance(evidence, dict) or not isinstance(source, dict):
        return None
    closed = evidence.get("closed_at")
    if (
        evidence.get("source") != "BINANCE_FUTURES"
        or evidence.get("interval") != "1m"
        or evidence.get("environment") != context.get("environment")
        or context.get("environment") not in {"SANDBOX", "LIVE"}
        or type(now) is not int
        or type(closed) is not int
        or closed < 1440 * 60000
        or closed % 60000
        or closed > now
        or source.get("source") != "s3"
        or source.get("environment") != context["environment"]
        or source.get("symbol") != context.get("symbol")
        or type(source.get("observed_at")) is not int
        or source["observed_at"] != closed
        or source.get("features") != market
    ):
        return None
    return evidence


def market_plan(signal, context, config):
    if config.get("source") == "quant":
        from services.v2_quant_admission import quant_market_plan

        return quant_market_plan(signal, context, config)
    if signal["signal"].startswith("QUANT_"):
        raise ValueError("QUANT_SOURCE_REQUIRED")
    from v2_core.runtime_policy import resolve

    if resolve(config.get("policy"))["entry.trend_structure_enabled"]:
        from services.v2_trend_entry import evaluate_trend_market

        return evaluate_trend_market(signal, context, config)
    event, snapshot = signal["features"], context["directional"]
    policy, proof = source_policy(signal, config)
    inputs = {
        "strategy": config["strategy"],
        "event": {
            "type": signal["signal"],
            "strength": decimal_features(event["strength"]),
            "breakout_confirmed": event.get("breakout_confirmed", False),
            "taker_buy_ratio": decimal_features(event.get("taker_buy_ratio")),
        },
        "market": decimal_features(snapshot["market"]),
        "price": snapshot["price"],
        "regime": snapshot["regime"],
        "short_ratio": snapshot["short_ratio"],
        "age_ms": context["assembled_at"] - signal["observed_at"],
        "policy": policy,
    }
    if policy["entry.confirmation_version"] == 2:
        # Metadata is not a numeric indicator. Keep its integer/bool types and
        # freeze the producer's actual contract, never a reconstructed label.
        evidence = snapshot["market"].get("input_evidence")
        inputs["indicator_evidence"] = bound_indicator_evidence(context)
        proof["confirmation_version"] = 2
        proof["indicator_evidence"] = evidence
        proof["indicator_evidence_bound"] = inputs["indicator_evidence"] is not None
        # An observed move is not a calibrated remaining-return forecast. Keep
        # the inherited gate for comparison, but never label its proxy as edge.
        proof["reward_estimate_kind"] = "REALIZED_MOVE_PROXY_NOT_FORECAST"
    from v2_core.signal_fusion import validate_fusion

    fusion = validate_fusion(
        signal,
        source=config.get("source", "s3"),
        proof=context.get("signal_fusion"),
        now_ms=context["assembled_at"],
    )
    baseline = evaluate_market(**inputs)
    bonus = fusion["score_bonus"] if fusion else 0
    plan = evaluate_market(**inputs, score_bonus=bonus) if bonus else baseline
    if (
        fusion
        and fusion["veto"]
        and plan.reason in {"CANDIDATE", "score_below_minimum"}
    ):
        plan = replace(plan, reason="TV_DIRECTION_CONFLICT")
    if (
        context.get("market_only") is True
        and snapshot["short_ratio"] is None
        and plan.reason == "score_below_minimum"
    ):
        # This is an upper-bound test, NOT an observed sentiment value. Even
        # the largest possible crowding bonus cannot qualify this candidate.
        # A potentially qualifying score must still collect the real account
        # context, ratio, funding and history before it can be admitted.
        best = evaluate_market(
            **{
                **inputs,
                "score_bonus": bonus,
                "short_ratio": "1" if config["strategy"] == "S6" else "0",
            }
        )
        if best.reason == "score_below_minimum":
            plan = replace(plan, reason="score_unreachable")
            proof["score_bound"] = "MAXIMUM_CROWDING_BONUS_BELOW_THRESHOLD"
    if fusion:
        proof["signal_fusion"] = {
            **fusion,
            "baseline_plan": asdict(baseline),
            "fused_plan": asdict(plan),
            "allocation_score": baseline.score,
        }
    return plan, proof


def decimal_features(value):
    """S3 keeps exact integer strengths/indicators; do not stringify bool/float."""
    if isinstance(value, dict):
        return {key: decimal_features(item) for key, item in value.items()}
    return str(value) if type(value) is int else value


def replay_decision(signal, context, config):
    if config["mode"] != "REPLAY_ONLY":
        raise ValueError("replay cannot authorize execution")
    event = signal["features"]
    if event.get("type", signal["signal"]) != signal["signal"]:
        raise ValueError("conflicting event type")
    if not supports_event(config["strategy"], signal["signal"]):
        return StrategyDecision(
            "IGNORED",
            "UNSUPPORTED_DIRECTIONAL_EVENT",
            features_json='{"execution_authorized":false}',
        )
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
    plan, scoring = market_plan(signal, context, config)
    if context.get("market_only") and plan.reason not in MARKET_ONLY_REJECTIONS:
        raise ValueError("FULL_ACCOUNT_CONTEXT_REQUIRED")
    features = {
        "market_plan": asdict(plan),
        "source_scoring": scoring,
        "execution_authorized": False,
    }
    fusion = scoring.pop("signal_fusion", None)
    if fusion:
        features["signal_fusion"] = fusion

    def result(reason):
        return StrategyDecision("IGNORED", reason, features_json=canonical(features))

    if plan.reason != "CANDIDATE":
        return result(plan.reason)
    controls = None
    if "entry_controls" in context:
        from v2_core.entry_controls import from_snapshot

        controls = from_snapshot(context["entry_controls"], config["account_scope"])
        features["entry_controls"] = context["entry_controls"]
        if controls.values["reentry.mode"] != "OFF":
            from v2_core.entry_opportunities import validate

            proof = context.get("entry_structure")
            rejection = validate(
                proof,
                scope=config["account_scope"],
                target=signal["symbol"],
                side={"LONG": "BUY", "SHORT": "SELL"}[plan.side],
                observed=signal["observed_at"],
                now=now,
                expected_control_digest=controls.digest,
                expected_controls=controls.snapshot(),
            )
            features["entry_structure"] = proof
            if rejection:
                return result(rejection)
    # Sizing validates the candidate against the same source-specific minimum.
    # All other risk/capital settings remain identical to the frozen policy.
    settings, _ = source_policy(signal, config)
    structural = scoring.get("profile") == "causal-trend-entry-v1"
    if settings["leverage.adaptive_enabled"]:
        from decimal import Decimal

        from v2_core.adaptive_leverage import choose

        # Use the full single-trade risk cap as the conservative depth/tier
        # estimate. Final sizing may be smaller but cannot be larger.
        cap = (
            Decimal(snapshot["sizing"]["balance"])
            * Decimal(settings["sizing.risk_fraction"])
            / (
                Decimal(plan.stop_fraction)
                + Decimal(settings["sizing.cost_buffer_fraction"])
            )
        )
        selected, proof = choose(
            replace(plan, score=fusion["allocation_score"])
            if fusion and fusion["allocation_score"] is not None
            else plan,
            {**snapshot, "quant_atr_pct": scoring["atr_pct"]}
            if config.get("source") == "quant" or structural
            else snapshot,
            source=config.get("source"),
            age_ms=now - signal["observed_at"],
            settings=settings,
            notional=cap,
        )
        features["leverage_decision"] = proof
        if selected is None:
            return result("NO_VERIFIED_SAFE_LEVERAGE")
        if structural:
            # A profile ceiling is not a new venue-approved tier. For example,
            # cap=4 over configured 2/3/5 must choose 3, never invent 4. Every
            # lower tier retains the same conservative stop and venue proof.
            tiers = {
                settings["leverage.low"],
                settings["leverage.medium"],
                settings["leverage.adaptive_high"],
            }
            if settings["leverage.boost_enabled"]:
                tiers.add(settings["leverage.boost"])
            selected = max(
                (
                    tier
                    for tier in tiers
                    if tier <= min(selected, settings["trend.max_leverage"])
                ),
                default=None,
            )
            proof["profile_leverage_cap"] = settings["trend.max_leverage"]
            proof["selected"] = selected
            if selected is None:
                proof["reason"] = "NO_VERIFIED_SAFE_LEVERAGE"
                return result("NO_VERIFIED_SAFE_LEVERAGE")
        plan = replace(plan, leverage=selected)
        features["market_plan"] = asdict(plan)
    adjustment = analysis_adjustment(
        snapshot["history"], mode=config["analysis_mode"], policy=settings
    )
    features["analysis"] = asdict(adjustment)
    if adjustment.factor == "0":
        return result(adjustment.reason)
    analysis_factor = adjustment.factor
    if controls and controls.values["history.mode"] != "OFF":
        from decimal import Decimal

        from services.v2_layered_history import validate

        proof = context.get("layered_history")
        factor = validate(
            proof,
            {**signal, "source": config.get("source", "s3")},
            context,
            plan,
            controls,
        )
        features["layered_history"] = proof
        analysis_factor = str(min(Decimal(analysis_factor), Decimal(factor)))
    # No alternative price/ATR/history factor can be smuggled through sizing.
    explanation = {}
    sized = size_candidate(
        plan,
        **snapshot["sizing"],
        price=snapshot["price"],
        atr_pct=scoring["atr_pct"]
        if config.get("source") == "quant" or structural
        else decimal_features(snapshot["market"]["1h"]["atr_pct"]),
        analysis_factor=analysis_factor,
        policy=settings,
        explanation=explanation,
        allocation_score=fusion["allocation_score"] if fusion else None,
    )
    features["sizing"] = asdict(sized)
    features["sizing_explanation"] = explanation
    if explanation.get("version") == "remaining-risk-sizing-v1":
        features["sizing_budget"] = explanation
    if sized.reason != "SIZED":
        return result(sized.reason)
    gate = execution_market_gate(
        plan,
        sized,
        price=snapshot["price"],
        expected_move_pct=scoring["target_move_pct"]
        if structural
        else snapshot["expected_move_pct"],
        funding_rate=snapshot["funding_rate"],
        policy=settings,
    )
    if structural and gate == "MARKET_GATES_PASSED":
        from services.v2_trend_entry import trend_reward_gate

        gate = trend_reward_gate(
            plan, sized, price=snapshot["price"], proof=scoring, policy=settings
        )
    features["execution_market_gate"] = gate
    if gate == "MARKET_GATES_PASSED" and "liquidity_controls" in context:
        from v2_core.liquidity import evaluate_execution_quote, evaluate_liquidity
        from v2_core.liquidity_controls import from_snapshot

        liquidity_controls = from_snapshot(
            context["liquidity_controls"], config["account_scope"]
        )
        liquidity_now = context.get("liquidity_as_of_ms", now)
        if type(liquidity_now) is not int or not now <= liquidity_now < min(
            context["valid_until_ms"], signal["expires_at_ms"]
        ):
            raise ValueError("LIQUIDITY_AS_OF_INVALID")
        liquidity = evaluate_liquidity(
            evidence=context.get("liquidity_evidence"),
            controls=liquidity_controls.values,
            symbol=signal["symbol"],
            side="BUY" if plan.side == "LONG" else "SELL",
            quantity=str(sized.quantity),
            reference_price=str(snapshot["price"]),
            now_ms=liquidity_now,
        )
        features["liquidity_gate"] = liquidity
        if liquidity["blocked"]:
            return result(liquidity["reason"])
        execution_quote = evaluate_execution_quote(
            evidence=snapshot.get("leverage_venue"),
            controls=liquidity_controls.values,
            symbol=signal["symbol"],
            now_ms=liquidity_now,
        )
        features["liquidity_execution_quote"] = execution_quote
        if execution_quote["blocked"]:
            return result(execution_quote["reason"])
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
