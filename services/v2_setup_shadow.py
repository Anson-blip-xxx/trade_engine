"""Frozen setup-specific volume research; pure, bounded and never admission.

The two separate preregistered alternatives retain the complete frozen baseline
except for min_volume_ratio=0.75 on their named setup. The existing mover shadow
performs both market evaluations and all frozen replay checks. A setup label
only describes the validated structure; earlier regime/flow/alignment vetoes
remain the actual market result. Hash authenticity and historical decision
matching belong to the caller, before any result enters a research cohort.
"""

import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from services.v2_mover_gate_shadow import evaluate_mover_gate_shadow
from services.v2_trend_entry import REJECTIONS
from v2_core.precision import amount
from v2_core.trend_structure import validate_trend_structure

VERSION = "setup-volume-shadow-v1"
_ALTERNATIVES = {
    "BREAKOUT_VOLUME075": "BREAKOUT",
    "PULLBACK_VOLUME075": "PULLBACK_RESUME",
}
_MARKET_REASONS = REJECTIONS | {
    "CANDIDATE",
    "regime_conflict",
    "zero_volatility",
    "score_below_minimum",
    "TV_DIRECTION_CONFLICT",
}
_UNKNOWN_REASONS = frozenset(
    {
        "MISSING_FROZEN_CONFIG",
        "MISSING_FROZEN_VOLUME_THRESHOLD",
        "BASELINE_OUTSIDE_FROZEN_STUDY_PLAN",
        "MISSING_MARKET_ENVIRONMENT",
        "MISSING_FROZEN_SIGNAL_OR_CONTEXT",
        "INCOMPLETE_FROZEN_POLICY",
        "MISSING_SUPPORTED_SOURCE_OR_STRATEGY",
        "FROZEN_TREND_PROFILE_DISABLED",
        "MISSING_FROZEN_DIRECTIONAL_CONTEXT",
        "INCOMPLETE_FROZEN_SIGNAL_OR_CONTEXT",
        "INCOMPLETE_FROZEN_DIRECTIONAL_CONTEXT",
        "UNKNOWN_OBSERVED_REGIME",
        "MISSING_FROZEN_SIGNAL_STRENGTH",
        "TREND_STRUCTURE_UNCONFIRMED",
        "TREND_STRUCTURE_STALE",
        "TREND_WARMUP",
        "MISSING_VALIDATED_STRUCTURE",
        "INVALID_FROZEN_INPUT",
        "MISSING_VOLUME_RATIO",
    }
)


def _unknown(reason):
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "regime": None,
        "setup": None,
        "side": None,
        "bar_close_ms": None,
        "observed_ms": None,
        "decided_ms": None,
        "volume_ratio": None,
        "taker_buy_ratio": None,
        "atr_pct": None,
        "baseline_market_reason": None,
        "global_volume075_market_reason": None,
        "alternatives": {
            name: {
                "status": "UNKNOWN",
                "reason": "FROZEN_SETUP_EVIDENCE_UNAVAILABLE",
                "min_volume_ratio": None,
                "market_reason": None,
                "market_reason_changed": None,
            }
            for name in _ALTERNATIVES
        },
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "not_trade_prediction": True,
        "policy_compatibility": None,
    }


def _setup(values, *, side):
    """Same precedence and resumed predicate as evaluate_trend_market.

    This descriptive predicate intentionally runs even if an earlier market
    gate rejects. It never replaces the evaluator's rejection with CANDIDATE.
    """
    suffix = "long" if side == "BUY" else "short"
    sign = Decimal(1 if side == "BUY" else -1)
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        breakout = values["breakout_" + suffix]
        resumed = (
            values["prior3_touched_ema20_" + suffix]
            and sign
            * (amount(values["previous_close"]) - amount(values["previous_open"]))
            <= 0
            and sign
            * (
                amount(values["close"])
                - amount(values["previous_high" if side == "BUY" else "previous_low"])
            )
            > 0
        )
    return (
        "BREAKOUT" if breakout else "PULLBACK_RESUME" if resumed else "NO_FRESH_SETUP"
    )


def evaluate_setup_shadow(
    signal, context, config, *, environment, evidence_ref, config_digest
):
    """Compare two fixed setup alternatives using at most two market calls.

    The caller supplies the exact frozen, complete policy and bound closed-bar
    context. No current defaults, account identifiers, raw context or exception
    text are emitted. ``decided_ms`` remains unset for the caller to attach the
    original decision time. READY means the frozen market comparison succeeded;
    it does not establish a matching historical baseline, entry, fill, return,
    source authenticity, parameter recommendation or permission to trade.
    """
    result = _unknown("INVALID_FROZEN_INPUT")
    try:
        if environment not in {"LIVE", "SANDBOX"}:
            return _unknown("EXPLICIT_SHADOW_ENVIRONMENT_REQUIRED")
        if evidence_ref is None or config_digest is None:
            return _unknown("MISSING_FROZEN_EVIDENCE_REFERENCE_OR_CONFIG_DIGEST")
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (evidence_ref, config_digest)
        ):
            return result
        # This is the sole evaluator call: full baseline policy completeness,
        # digest, environment, source binding, READY/staleness and values are
        # checked before the 1.0 / 0.75 frozen comparisons are returned.
        replay = evaluate_mover_gate_shadow(
            [
                {
                    "signal": signal,
                    "context": context,
                    "config": config,
                    "environment": environment,
                    "evidence_ref": evidence_ref,
                    "config_digest": config_digest,
                }
            ],
            environment=environment,
        )["samples"][0]
        volume = replay["volume"]
        result["policy_compatibility"] = replay["policy_compatibility"]
        if replay["error"] or replay["mode"] != "FULL_FROZEN_REPLAY":
            reason = volume["reason"]
            return _unknown(
                reason if reason in _UNKNOWN_REASONS else "INVALID_FROZEN_INPUT"
            )

        market = context["directional"]["market"]
        structure = validate_trend_structure(
            market["trend_structure"],
            symbol=context["symbol"],
            environment=environment,
            closed_at_ms=market["input_evidence"]["closed_at"],
        )
        if structure["status"] != "READY":
            return _unknown("MISSING_VALIDATED_STRUCTURE")
        baseline = volume["baseline_market_reason"]
        global075 = volume["candidate_market_reason"]
        if baseline not in _MARKET_REASONS or global075 not in _MARKET_REASONS:
            return result
        values = structure["values"]
        side = "BUY" if config["strategy"] == "S6" else "SELL"
        setup = _setup(values, side=side)
        result.update(
            regime=replay["regime"]["observed_regime"],
            setup=setup,
            side=side,
            bar_close_ms=structure["last_closed_at_ms"],
            observed_ms=signal["observed_at"],
            volume_ratio=values["volume_ratio"],
            taker_buy_ratio=values["taker_buy_ratio"],
            atr_pct=values["atr_pct"],
            baseline_market_reason=baseline,
            global_volume075_market_reason=global075,
        )
        if volume["status"] != "COMPARED" or values["volume_ratio"] is None:
            result["reason"] = "MISSING_VOLUME_RATIO"
            return result
        if values["taker_buy_ratio"] is None:
            result["reason"] = "MISSING_FROZEN_FLOW"
            return result
        result.update(status="READY", reason="FROZEN_SETUP_MARKET_COMPARISON_ONLY")
        for name, target_setup in _ALTERNATIVES.items():
            selected = setup == target_setup
            market_reason = global075 if selected else baseline
            result["alternatives"][name] = {
                "status": "READY",
                "reason": "SETUP_SPECIFIC_VOLUME_COMPARISON"
                if selected
                else "SETUP_NOT_SELECTED_BASELINE_RETAINED",
                "min_volume_ratio": "0.75" if selected else "1",
                "market_reason": market_reason,
                "market_reason_changed": market_reason != baseline,
            }
    except (
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        ArithmeticError,
        RecursionError,
    ):
        # No raw exception or partially validated content escapes.
        return _unknown("INVALID_FROZEN_INPUT")
    return result
