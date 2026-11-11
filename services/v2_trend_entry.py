"""Causal trend entry profile over verified closed 15m structure.

No I/O, order port, model call or wall clock. ATR targets are planning scenarios,
NOT estimated future returns. This profile replaces legacy momentum/reversal
classification only when explicitly activated; account risk remains downstream.
"""

from dataclasses import asdict, replace
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from decision.core import long_signal_allows_open
from v2_core.directional import DirectionalPlan, supports_event
from v2_core.ledger import amount
from v2_core.precision import decimal_text, derived_amount
from v2_core.runtime_policy import resolve

VERSION = "causal-trend-entry-v1"
REJECTIONS = frozenset(
    {
        "TREND_STRUCTURE_UNCONFIRMED",
        "TREND_STRUCTURE_STALE",
        "TREND_WARMUP",
        "TREND_NOT_ALIGNED",
        "TREND_COUNTERMOVE",
        "TREND_NO_FRESH_SETUP",
        "TREND_FLOW_UNCONFIRMED",
        "TREND_VOLUME_UNCONFIRMED",
        "TREND_ENTRY_MOVED",
        "TREND_BREAKOUT_BODY_INVALIDATED",
        "TREND_BREAKOUT_PRICE_UNCONFIRMED",
        "TREND_STOP_TOO_WIDE",
        "TREND_TARGET_EXHAUSTED",
        "TREND_ATR_EXCEEDED",
        "TREND_PLANNED_REWARD_RISK_TOO_LOW",
    }
)


def encoded(value):
    return decimal_text(
        derived_amount(value, rounding=ROUND_HALF_EVEN, preserve_nonzero=True)
    )


def evaluate_trend_market(signal, context, config):
    from services.v2_directional_replay import bound_indicator_evidence, source_policy
    from v2_core.signal_fusion import validate_fusion
    from v2_core.trend_structure import validate_trend_structure

    settings, _ = source_policy(signal, config)
    strategy = config["strategy"]
    side = "LONG" if strategy == "S6" else "SHORT"
    if (
        strategy not in {"S6", "S8"}
        or not settings["entry.trend_structure_enabled"]
        or not supports_event(strategy, signal["signal"])
        or signal["signal"].startswith("QUANT_")
    ):
        raise ValueError("TREND_PROFILE_SCOPE")
    proof = {
        "version": VERSION,
        "profile": VERSION,
        "source": config.get("source", "s3"),
        "raw_strength": signal["features"]["strength"],
        "strength_semantics": "OBSERVED_AMPLITUDE_NOT_WIN_PROBABILITY",
        "reward_estimate_kind": "ATR_TARGET_NOT_FORECAST",
        "min_score": settings["entry.min_score"],
        "execution_authorized": False,
    }

    def reject(reason):
        return DirectionalPlan(reason, strategy, signal["signal"], side), proof

    evidence = bound_indicator_evidence(context)
    if evidence is None or evidence.get("window_version") != "s3-closed-1m-v6":
        return reject("TREND_STRUCTURE_UNCONFIRMED")
    try:
        structure = validate_trend_structure(
            context["directional"]["market"].get("trend_structure"),
            symbol=context["symbol"],
            environment=context["environment"],
            closed_at_ms=evidence["closed_at"],
        )
    except (ValueError, TypeError, KeyError):
        return reject("TREND_STRUCTURE_UNCONFIRMED")
    proof["structure"] = structure
    if structure["status"] != "READY":
        return reject("TREND_WARMUP")
    if (
        not 0
        <= context["assembled_at"] - structure["last_closed_at_ms"]
        <= settings["trend.max_structure_age_ms"]
    ):
        return reject("TREND_STRUCTURE_STALE")
    quality = None
    if (
        settings["trend.breakout_body_guard_enabled"]
        or settings["trend.entry_quality_shadow_enabled"]
    ):
        from v2_core.trend_entry_quality import observe_entry_quality

        market = context["directional"]["market"]
        latest_window = market.get("15m")
        quality = observe_entry_quality(
            structure=structure,
            side=side,
            price=context["directional"]["price"],
            bound_price=latest_window.get("close")
            if isinstance(latest_window, dict)
            else None,
            price_observed_at_ms=evidence["closed_at"],
            evaluated_at_ms=context["assembled_at"],
            market=market,
            enforce=settings["trend.breakout_body_guard_enabled"],
        )
        proof["entry_quality"] = quality
    if strategy == "S6" and not long_signal_allows_open(
        "VIOLENT_BULLISH", {"regime": context["directional"]["regime"]}
    ):
        return reject("regime_conflict")
    values = structure["values"]
    # All prices are decimal strings validated by the producer contract.
    d = lambda key: amount(values[key])
    p = lambda key: Decimal(settings[key])
    sign = Decimal(1 if side == "LONG" else -1)
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        close, atr = d("close"), d("atr")
        price = amount(context["directional"]["price"], positive=True)
        if atr <= 0:
            return reject("zero_volatility")
        proof["atr_pct"] = values["atr_pct"]
        if d("atr_pct") > p("trend.max_atr_pct"):
            return reject("TREND_ATR_EXCEEDED")
        if not all(
            sign * x > 0
            for x in (
                close - d("ema20"),
                d("ema20") - d("ema60"),
                d("ema20_slope"),
                d("ema60_slope"),
            )
        ):
            return reject("TREND_NOT_ALIGNED")
        if sign * (close - d("previous_close")) <= 0 or sign * (close - d("open")) <= 0:
            return reject("TREND_COUNTERMOVE")
        flow = values["taker_buy_ratio"]
        threshold = p("entry.flow_long" if side == "LONG" else "entry.flow_short")
        if flow is None or sign * (amount(flow) - threshold) < 0:
            return reject("TREND_FLOW_UNCONFIRMED")
        if values["volume_ratio"] is None or d("volume_ratio") < p(
            "trend.min_volume_ratio"
        ):
            return reject("TREND_VOLUME_UNCONFIRMED")
        suffix = "long" if side == "LONG" else "short"
        breakout = values["breakout_" + suffix]
        resumed = (
            values["prior3_touched_ema20_" + suffix]
            and sign * (d("previous_close") - d("previous_open")) <= 0
            and sign
            * (close - d("previous_high" if side == "LONG" else "previous_low"))
            > 0
        )
        if not (breakout or resumed):
            return reject("TREND_NO_FRESH_SETUP")
        setup = "BREAKOUT" if breakout else "PULLBACK_RESUME"
        anchor = (
            d("prior20_high" if side == "LONG" else "prior20_low")
            if breakout
            else d("ema20")
        )
        proof.update(
            setup=setup, reference_price=values["close"], entry_anchor=encoded(anchor)
        )
        # Breakout proximity uses the breached level, not distance from a slow
        # EMA. Current-price drift and invalidation are still checked both ways.
        if (
            abs(price - close) > p("trend.max_chase_atr") * atr
            or sign * (price - anchor) <= 0
            or sign * (price - anchor) > p("trend.max_chase_atr") * atr
        ):
            return reject("TREND_ENTRY_MOVED")
        if breakout and settings["trend.breakout_body_guard_enabled"]:
            if quality["status"] != "READY":
                return reject("TREND_BREAKOUT_PRICE_UNCONFIRMED")
            if quality["confirming_body_erased"]:
                return reject("TREND_BREAKOUT_BODY_INVALIDATED")
        structural_stop = (
            anchor - sign * p("trend.stop_buffer_atr") * atr
            if breakout
            else d("prior3_low" if side == "LONG" else "prior3_high")
            - sign * p("trend.stop_buffer_atr") * atr
        )
        risk = max(
            sign * (price - structural_stop),
            p("trend.stop_atr") * atr,
            price * p("trend.min_stop_fraction"),
        )
        stop_fraction = risk / price
        # Never tighten to the cap just to force a wide invalidation into a trade.
        if stop_fraction > min(p("trend.max_stop_fraction"), p("stop.max_default")):
            return reject("TREND_STOP_TOO_WIDE")
        target = close + sign * p("trend.target_atr") * atr
        if not breakout:
            resistance = d("prior20_high" if side == "LONG" else "prior20_low")
            if sign * (resistance - price) > 0:
                target = (
                    min(target, resistance)
                    if side == "LONG"
                    else max(target, resistance)
                )
        remaining = sign * (target - price)
        if target <= 0 or remaining <= 0:
            return reject("TREND_TARGET_EXHAUSTED")
        proof.update(
            target_price=encoded(target),
            target_move_pct=encoded(remaining / price * 100),
            structural_stop_price=encoded(structural_stop),
            cost_buffer_fraction=settings["sizing.cost_buffer_fraction"],
            target_model="ATR_TARGET_NOT_FORECAST",
        )
        cost = price * p("sizing.cost_buffer_fraction")
        proof["planned_net_reward_risk"] = encoded((remaining - cost) / (risk + cost))
        if remaining <= cost or remaining - cost < (risk + cost) * p(
            "entry.min_reward_risk"
        ):
            return reject("TREND_PLANNED_REWARD_RISK_TOO_LOW")
        score = settings["trend.base_score"]
        plan = DirectionalPlan(
            "CANDIDATE"
            if score >= settings["entry.min_score"]
            else "score_below_minimum",
            strategy,
            signal["signal"],
            side,
            score,
            "TREND_" + setup,
            "S6A" if side == "LONG" else "S8",
            settings["trend.max_leverage"],
            "ISOLATED",
            encoded(stop_fraction),
        )
    fusion = validate_fusion(
        signal,
        source=config.get("source", "s3"),
        proof=context.get("signal_fusion"),
        now_ms=context["assembled_at"],
    )
    if fusion:
        baseline = plan
        plan = replace(plan, score=min(100, score + fusion["score_bonus"]))
        if fusion["veto"]:
            plan = replace(plan, reason="TV_DIRECTION_CONFLICT")
        elif plan.score >= settings["entry.min_score"]:
            plan = replace(plan, reason="CANDIDATE")
        proof["signal_fusion"] = {
            **fusion,
            "baseline_plan": asdict(baseline),
            "fused_plan": asdict(plan),
            "allocation_score": score,
        }
    return plan, proof


def trend_reward_gate(plan, sized, *, price, proof, policy):
    """Net planning R:R after exact tick rounding, not a profitability forecast."""
    settings = resolve(policy)
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        px = amount(price, positive=True)
        target = amount(proof["target_price"], positive=True)
        stop = amount(sized.stop_price, positive=True)
        cost = px * Decimal(settings["sizing.cost_buffer_fraction"])
        reward = (target - px if plan.side == "LONG" else px - target) - cost
        risk = abs(px - stop) + cost
        return (
            "REWARD_RISK_TOO_LOW"
            if reward <= 0 or reward < risk * Decimal(settings["entry.min_reward_risk"])
            else "MARKET_GATES_PASSED"
        )
