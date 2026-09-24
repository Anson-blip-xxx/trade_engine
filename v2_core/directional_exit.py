"""Pure S6/S8 exit policy over explicit, replayable evidence.

The policy has no clock, network, database or exchange access.  Percent values
are unleveraged price returns; cash economics use actual remaining quantity.
Hard-stop execution remains the registered native STOP_MARKET parent's job.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from v2_core.directional import number
from v2_core.ledger import amount
from v2_core.runtime_policy import resolve

_PARAMETERS = {
    "S6A": {
        "time_min": 120,
        "partial_pct": "5",
        "partial_ratio": "0.5",
        "peak": "3",
        "drawdown": "2",
        "be": "2.5",
    },
    "S6B": {
        "time_min": 480,
        "partial_pct": "8",
        "partial_ratio": "0.3",
        "peak": "5",
        "drawdown": "3",
        "be": "5",
    },
    "S8": {
        "time_min": 240,
        "partial_pct": "5",
        "partial_ratio": "0.3",
        "peak": "3",
        "drawdown": "2",
        "be": "2",
    },
}


@dataclass(frozen=True)
class ExitFacts:
    system_tag: str
    side: str
    entry_price: str
    mark_price: str
    stop_price: str
    remaining_quantity: str
    planned_loss: str
    held_ms: int
    funding_rate: str
    ema9_1h: str
    ema20_1h: str
    momentum_closes_15m: tuple[str, str, str, str]
    peak_return_pct: str
    partial_done: bool
    exit_fee_rate: str


@dataclass(frozen=True)
class ExitDecision:
    action: str
    reason: str
    quantity_fraction: str
    return_pct: str
    peak_return_pct: str
    gross_pnl: str
    estimated_net_pnl: str
    risk_multiple: str

    def evidence(self):
        return asdict(self)


def _exact(value):
    with localcontext() as ctx:
        ctx.prec = 100
        bounded = (
            value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
            if value.as_tuple().exponent < -18
            else value
        )
    encoded = format(bounded.normalize(), "f")
    amount(encoded)
    return encoded


def evaluate_directional_exit(facts, *, policy=None):
    settings = resolve(policy)
    if not isinstance(facts, ExitFacts) or facts.system_tag not in _PARAMETERS:
        raise ValueError("supported directional exit facts required")
    if facts.side not in {"BUY", "SELL"} or type(facts.partial_done) is not bool:
        raise ValueError("invalid directional exit identity")
    if type(facts.held_ms) is not int or not 0 <= facts.held_ms <= 10 * 365 * 86400000:
        raise ValueError("invalid holding duration")
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
    funding = number(facts.funding_rate, minimum=-1, maximum=1)
    ema9, ema20 = (
        amount(facts.ema9_1h, positive=True),
        amount(facts.ema20_1h, positive=True),
    )
    fee = number(facts.exit_fee_rate, minimum=0, maximum=Decimal(".01"))
    closes = tuple(amount(value, positive=True) for value in facts.momentum_closes_15m)
    if len(closes) != 4:
        raise ValueError("four closed 15m observations required")
    if (facts.side == "BUY" and stop >= entry) or (
        facts.side == "SELL" and stop <= entry
    ):
        raise ValueError("stop must remain on the loss side")
    with localcontext() as ctx:
        ctx.prec = 100
        direction = Decimal(1) if facts.side == "BUY" else Decimal(-1)
        change = (mark - entry) * direction
        return_pct = change / entry * 100
        peak = max(number(facts.peak_return_pct, minimum=0), return_pct, Decimal(0))
        gross = change * quantity
        estimated_net = gross - mark * quantity * fee
        risk_multiple = estimated_net / planned
        average = sum(closes[:3], Decimal(0)) / 3
        momentum_weak = (
            closes[-1] <= closes[-2] and closes[-1] < average
            if facts.side == "BUY"
            else closes[-1] >= closes[-2] and closes[-1] > average
        )
        reversed_1h = (
            ema9 < ema20 * (1 - Decimal(settings["exit.ema_reversal_fraction"]))
            if facts.side == "BUY"
            else ema9 > ema20 * (1 + Decimal(settings["exit.ema_reversal_fraction"]))
        )
        params = {
            name: settings[f"exit.{facts.system_tag}.{name}"]
            for name in _PARAMETERS[facts.system_tag]
        }

        def result(action, reason, fraction="0"):
            return ExitDecision(
                action,
                reason,
                fraction,
                _exact(return_pct),
                _exact(peak),
                _exact(gross),
                _exact(estimated_net),
                _exact(risk_multiple),
            )

        # Extreme adverse funding is evaluated first, matching the legacy PM.
        if (
            facts.side == "BUY" and funding > Decimal(settings["exit.adverse_funding"])
        ) or (
            facts.side == "SELL"
            and funding < -Decimal(settings["exit.adverse_funding"])
        ):
            return result("CLOSE", "ADVERSE_FUNDING")
        # Native stop owns this price boundary; do not race it with a market POST.
        if (facts.side == "BUY" and mark <= stop) or (
            facts.side == "SELL" and mark >= stop
        ):
            return result("WAIT_NATIVE_STOP", "NATIVE_STOP_BOUNDARY")
        if return_pct < -Decimal(settings["exit.emergency_loss_pct"]):
            return result("CLOSE", "EMERGENCY_LOSS")
        if (
            facts.held_ms >= settings["exit.early_loss_minutes"] * 60000
            and return_pct <= -Decimal(settings["exit.early_loss_pct"])
            and momentum_weak
        ):
            return result("CLOSE", "EARLY_LOSS_MOMENTUM_WEAK")
        # Normalize stagnation to the episode's planned loss and estimated exit
        # fee.  This replaces the legacy asymmetric absolute 1-USDT threshold.
        if (
            facts.held_ms >= settings["exit.stagnation_minutes"] * 60000
            and gross > 0
            and risk_multiple < Decimal(settings["exit.stagnation_r"])
        ):
            return result("CLOSE", "LOW_YIELD_STAGNATION")
        if not facts.partial_done and return_pct >= Decimal(params["partial_pct"]):
            return result("PARTIAL", "PARTIAL_TAKE_PROFIT", params["partial_ratio"])
        if peak >= Decimal(params["peak"]) and peak - return_pct >= Decimal(
            params["drawdown"]
        ):
            return result("CLOSE", "PEAK_DRAWDOWN")
        if (
            facts.held_ms >= settings["exit.reversal_minutes"] * 60000
            and return_pct >= 0
            and return_pct < Decimal(settings["exit.reversal_max_return_pct"])
            and reversed_1h
        ):
            return result("CLOSE", "ONE_HOUR_REVERSAL")
        if facts.held_ms > int(params["time_min"]) * 60000 and return_pct < Decimal(
            params["be"]
        ):
            return result("CLOSE", "TIME_STOP")
        return result("WAIT", "NO_EXIT_CONDITION")
