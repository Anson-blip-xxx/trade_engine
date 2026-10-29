"""Verified registered candidates -> shared directional risk/lifecycle contract.

S6/S8 are long/short lifecycle profiles, not the candidate plugin identity.
"""

from decimal import Decimal, localcontext

from v2_core.directional import DirectionalPlan
from v2_core.quant_indicators import VERSION, encode
from v2_core.quant_registry import REGISTRY_VERSION, evaluate
from v2_core.runtime_policy import resolve


def quant_market_plan(signal, context, config):
    settings = resolve(config.get("policy"))
    strategy = config["strategy"]
    side = "LONG" if strategy == "S6" else "SHORT"
    proof = {
        "version": REGISTRY_VERSION,
        "source": "quant",
        "profile": "registered-closed-bar",
        "raw_strength": signal["features"]["strength"],
    }

    def reject(reason):
        return DirectionalPlan(reason, strategy, signal["signal"], side), proof

    if not settings["quant.admission_enabled"]:
        return reject("QUANT_ACCOUNT_DISABLED")
    record = context.get("quant")
    if (
        not isinstance(record, dict)
        or record["environment"] != config["account_scope"]["environment"]
        or record["symbol"] != signal["symbol"]
    ):
        raise ValueError("QUANT_VERIFIED_CONTEXT_REQUIRED")
    frame, result = record["frame"], record["result"]
    if frame["version"] != VERSION or result != evaluate(
        frame, enabled=record["configuration"]["plugins"]
    ):
        raise ValueError("QUANT_EVALUATION_MISMATCH")
    winner = result["winner"]
    if winner is None or winner["side"] != ("BUY" if strategy == "S6" else "SELL"):
        return reject("QUANT_DIRECTION_CONFLICT")
    if (
        not frame["closed_at_ms"]
        == signal["observed_at"]
        <= context["assembled_at"]
        < signal["expires_at_ms"]
    ):
        raise ValueError("QUANT_DECISION_STALE")
    with localcontext() as ctx:
        ctx.prec = 50
        price = Decimal(context["directional"]["price"])
        reference = Decimal(winner["reference_price"])
        atr = Decimal(frame["current"]["atr"])
        if price <= 0 or atr <= 0:
            return reject("zero_volatility")
        if abs(price - reference) > Decimal(settings["quant.max_chase_atr"]) * atr:
            return reject("QUANT_ENTRY_MOVED")
        anchor = reference * (
            1 - Decimal(winner["stop_fraction"])
            if side == "LONG"
            else 1 + Decimal(winner["stop_fraction"])
        )
        distance = (price - anchor if side == "LONG" else anchor - price) / price
        if not 0 < distance <= Decimal(frame["parameters"]["max_stop_fraction"]):
            return reject("QUANT_STOP_NO_LONGER_VALID")
        if winner["score"] < settings["entry.min_score"]:
            return reject("score_below_minimum")
        proof.update(
            strategy_id=winner["strategy_id"],
            strategy_version=winner["strategy_version"],
            family=winner["family"],
            timeframe_minutes=frame["timeframe_minutes"],
            observation_id=record["observation_id"],
            config_digest=record["config_digest"],
            feature_version=frame["version"],
            atr_pct=encode(Decimal(frame["current"]["atr_fraction"]) * 100),
            expected_move_model="ATR_TARGET_NOT_FORECAST",
            min_score=settings["entry.min_score"],
        )
        return DirectionalPlan(
            "CANDIDATE",
            strategy,
            signal["signal"],
            side,
            winner["score"],
            "QUANT_" + winner["strategy_id"].upper(),
            "S6A" if strategy == "S6" else "S8",
            settings["quant.base_leverage"],
            "ISOLATED",
            encode(distance),
        ), proof
