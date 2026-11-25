"""Versioned deterministic management of an immutable strategy plan.

This is a management contract, not an entry strategy or a profitability claim.
The reference handler uses frozen R thresholds, never current account strategy
settings. Native protection and emergency reduction remain separate services.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from v2_core.directional_exit import ExitDecision
from v2_core.ledger import amount
from v2_core.precision import decimal_text, derived_amount, money_context
from v2_core.strategy_plan import TradePlan

EXIT_HANDLER = "risk-trail"
EXIT_VERSION = "v1"
EXIT_SCHEMA = "strategy-risk-trail-exit-v1"


@dataclass(frozen=True)
class StrategyExitFacts:
    side: str
    entry_price: str
    mark_price: str
    stop_price: str
    remaining_quantity: str
    planned_loss: str
    held_ms: int
    peak_return_pct: str
    partial_done: bool
    exit_fee_rate: str

    def evidence(self):
        return asdict(self)


def exit_settings(plan):
    if type(plan) is not TradePlan or (plan.exit_handler, plan.exit_version) != (
        EXIT_HANDLER,
        EXIT_VERSION,
    ):
        raise ValueError("STRATEGY_EXIT_VERSION_UNSUPPORTED")
    settings = plan.exit_config
    if (
        type(settings) is not dict
        or set(settings)
        != {
            "schema",
            "partial_take_profit_r",
            "partial_fraction",
            "trail_activation_r",
            "trail_giveback_r",
            "max_holding_ms",
        }
        or settings["schema"] != EXIT_SCHEMA
    ):
        raise ValueError("STRATEGY_EXIT_CONFIG_INVALID")
    try:
        partial, fraction, activation, giveback = (
            amount(settings[key], positive=True)
            for key in (
                "partial_take_profit_r",
                "partial_fraction",
                "trail_activation_r",
                "trail_giveback_r",
            )
        )
        if not (
            partial <= 100
            and fraction < 1
            and activation <= 100
            and giveback < activation
            and type(settings["max_holding_ms"]) is int
            and 0 <= settings["max_holding_ms"] <= 365 * 86400000
        ):
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        raise ValueError("STRATEGY_EXIT_CONFIG_INVALID") from None
    return settings


def evaluate_strategy_exit(plan, facts):
    settings = exit_settings(plan)
    if (
        type(facts) is not StrategyExitFacts
        or facts.side != plan.side
        or type(facts.partial_done) is not bool
        or type(facts.held_ms) is not int
        or not 0 <= facts.held_ms <= 10 * 365 * 86400000
    ):
        raise ValueError("STRATEGY_EXIT_FACTS_INVALID")
    entry, mark, stop, quantity, planned = (
        amount(value, positive=True)
        for value in (
            facts.entry_price,
            facts.mark_price,
            facts.stop_price,
            facts.remaining_quantity,
            facts.planned_loss,
        )
    )
    fee, previous_peak = amount(facts.exit_fee_rate), amount(facts.peak_return_pct)
    if (
        stop != amount(plan.stop_price)
        or quantity > amount(plan.quantity)
        or not 0 <= fee <= Decimal(".01")
        or previous_peak < 0
    ):
        raise ValueError("STRATEGY_EXIT_FACTS_INVALID")
    with money_context():
        # Partial fills/partial reductions allocate the original full-order
        # budget proportionally. Never silently replace the original stop or
        # apply the entire initial budget to a smaller residual.
        expected_risk = derived_amount(
            amount(plan.planned_loss) * quantity / amount(plan.quantity),
            rounding=ROUND_CEILING,
            positive=True,
        )
        if planned != expected_risk:
            raise ValueError("STRATEGY_EXIT_RISK_BINDING_INVALID")
        direction = Decimal(1) if plan.side == "BUY" else Decimal(-1)
        change = direction * (mark - entry)
        ret = change / entry * 100
        peak = max(previous_peak, ret, Decimal(0))
        gross = change * quantity
        estimated_net = gross - mark * quantity * fee
        current_r = estimated_net / planned
        # Sampled peak is a lower bound. It cannot be a future candle extreme.
        peak_r = peak * entry * quantity / 100 / planned
        gross_r = gross / planned

        def exact(value):
            return decimal_text(derived_amount(value, rounding=ROUND_HALF_EVEN))

        def result(action, reason, fraction="0"):
            return ExitDecision(
                action,
                reason,
                fraction,
                exact(ret),
                exact(peak),
                exact(gross),
                exact(estimated_net),
                exact(current_r),
            )

        if (plan.side == "BUY" and mark <= stop) or (
            plan.side == "SELL" and mark >= stop
        ):
            return result("WAIT_NATIVE_STOP", "NATIVE_STOP_BOUNDARY")
        # A valid native stop is already required by the caller. The time cap
        # is optional (0 disables it); it is not a new default maximum hold.
        if settings["max_holding_ms"] and facts.held_ms >= settings["max_holding_ms"]:
            return result("CLOSE", "PLAN_MAX_HOLDING")
        if peak_r >= amount(
            settings["trail_activation_r"]
        ) and peak_r - gross_r >= amount(settings["trail_giveback_r"]):
            return result("CLOSE", "PLAN_R_TRAIL")
        if not facts.partial_done and current_r >= amount(
            settings["partial_take_profit_r"]
        ):
            step = amount(plan.quantity_step)
            partial = (
                quantity * amount(settings["partial_fraction"]) / step
            ).to_integral_value(rounding=ROUND_FLOOR) * step
            if partial < step or partial >= quantity:
                return result("WAIT", "PARTIAL_BELOW_QUANTITY_STEP")
            return result(
                "PARTIAL", "PARTIAL_TAKE_PROFIT", settings["partial_fraction"]
            )
        return result("WAIT", "NO_EXIT_CONDITION")
