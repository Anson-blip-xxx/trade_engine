"""Pure S6/S8 market rules and exact, bounded sizing. NOT an execution permit.

No legacy executor imports, clocks, files, credentials or network access. The
caller supplies an immutable evidence snapshot; account admission, freshness,
cooldowns, historical analysis and protection readiness remain separate gates.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction

from decision.core import (
    classify_entry_mode,
    contract_score,
    long_signal_allows_open,
    long_trend_takeover_ready,
    price_is_overextended,
    pump_down_uptrend_guard,
    short_signal_allows_open,
)
from risk.core import bounded_stop_pct, leverage_for_score
from v2_core.ledger import amount

_LONG = frozenset(("PULSE_UP", "TREND_UP", "VIOLENT_BULLISH", "PUMP_UP"))
_SHORT = frozenset(
    ("PULSE_DOWN", "TREND_DOWN", "VIOLENT_BEARISH", "PUMP_DOWN", "PANIC_SELL")
)


def supports_event(strategy, event_type):
    if strategy not in {"S6", "S8"}:
        raise ValueError("unsupported directional strategy")
    return isinstance(event_type, str) and event_type in (
        _LONG if strategy == "S6" else _SHORT
    )


def number(value, *, minimum=None, maximum=None):
    result = amount(value)
    if (minimum is not None and result < minimum) or (
        maximum is not None and result > maximum
    ):
        raise ValueError("strategy number outside domain")
    return result


@dataclass(frozen=True)
class DirectionalPlan:
    """A candidate, never an executable order; stops/leverage are desired only."""

    reason: str
    strategy: str
    event_type: str
    side: str
    score: int | None = None
    entry_mode: str | None = None
    system_tag: str | None = None
    leverage: int | None = None
    margin_mode: str | None = None
    stop_fraction: str | None = None


def evaluate_market(strategy, event, market, *, price, regime, short_ratio, age_ms):
    """Evaluate validated decimal-string inputs using frozen legacy pure rules.

    Missing indicators are errors, not zero/neutral defaults. Optional flow and
    short ratio must be explicitly None if unavailable. Time is event age, not
    wall time; freshness must be enforced against the original source deadline.
    """
    if strategy not in {"S6", "S8"}:
        raise ValueError("unsupported directional strategy")
    if not isinstance(regime, str) or not regime or regime != regime.strip():
        raise ValueError("explicit market regime required")
    if type(age_ms) is not int or not 0 <= age_ms <= 86400000:
        raise ValueError("invalid original event age")
    kind = event["type"]
    if not supports_event(strategy, kind):
        raise ValueError("event does not belong to strategy")
    if type(event["breakout_confirmed"]) is not bool:
        raise ValueError("explicit breakout boolean required")
    strength = float(number(event["strength"], minimum=0, maximum=100))
    px = float(amount(price, positive=True))
    windows = {}
    for window, fields in {
        "15m": ("ema20", "atr", "rsi", "taker_buy_ratio"),
        "1h": ("ema20", "atr", "atr_pct"),
        "4h": ("ema20", "chg"),
        "24h": ("ema20", "ema60", "chg"),
    }.items():
        windows[window] = {}
        for field in fields:
            value = market[window][field]
            if field == "taker_buy_ratio" and value is None:
                windows[window][field] = None
                continue
            if field.startswith("ema") or field == "atr":
                parsed = amount(value, positive=True)
            else:
                parsed = number(
                    value,
                    minimum=None if field == "chg" else 0,
                    maximum=1
                    if field == "taker_buy_ratio"
                    else (100 if field == "rsi" else None),
                )
            windows[window][field] = float(parsed)
    # Explicit event flow is authoritative, including zero (all aggressive sell).
    raw_flow = event["taker_buy_ratio"]
    flow = (
        windows["15m"]["taker_buy_ratio"]
        if raw_flow is None
        else float(number(raw_flow, minimum=0, maximum=1))
    )
    ratio = (
        None
        if short_ratio is None
        else float(number(short_ratio, minimum=0, maximum=1))
    )
    side = "LONG" if strategy == "S6" else "SHORT"

    def reject(reason):
        return DirectionalPlan(reason, strategy, kind, side)

    if strategy == "S6" and not long_signal_allows_open(kind, {"regime": regime}):
        return reject("regime_conflict")
    if strategy == "S8" and not short_signal_allows_open(kind, strength):
        return reject("strength_below_minimum")
    if kind == "PUMP_DOWN" and pump_down_uptrend_guard(
        px, windows["4h"], windows["24h"]
    ):
        return reject("pump_down_uptrend")
    ema, atr, atr_pct = (windows["1h"][key] for key in ("ema20", "atr", "atr_pct"))
    mode = classify_entry_mode(px, ema, windows["15m"]["rsi"], flow, side)
    takeover = strategy == "S6" and (
        kind == "PUMP_UP"
        or (
            kind == "VIOLENT_BULLISH"
            and windows["4h"]["chg"] > 3
            and windows["24h"]["chg"] > 10
        )
        or event["breakout_confirmed"]
    )
    if takeover:
        if not long_trend_takeover_ready(px, windows):
            return reject("takeover_not_ready")
        mode = "S6B_TREND_TAKEOVER"
    elif mode == "UNCONFIRMED":
        return reject("entry_mode_unconfirmed")
    extension_limit = 1.25 if kind.startswith("VIOLENT_") else 2.0
    if (
        not takeover
        and mode == "RIGHT_MOMENTUM"
        and price_is_overextended(px, ema, atr, side, extension_limit)
    ):
        return reject("overextended")
    if atr_pct > (12 if takeover else 6):
        return reject("atr_exceeded")
    extension = (px - ema if strategy == "S6" else ema - px) / atr
    score = contract_score(
        strength, kind, atr_pct, extension, flow, age_ms / 1000, side, ratio
    )
    if score < 30:
        return reject("score_below_minimum")
    base_stop = (
        0.10
        if takeover
        else (
            0.035
            if kind == "PANIC_SELL"
            else 0.04
            if kind.startswith("PULSE_")
            else 0.08
        )
    )
    stop = bounded_stop_pct(base_stop, atr_pct, 0.12 if takeover else 0.08)
    return DirectionalPlan(
        "CANDIDATE",
        strategy,
        kind,
        side,
        score,
        mode,
        "S6B" if takeover else "S6A" if strategy == "S6" else "S8",
        2 if takeover else leverage_for_score(kind, score, atr_pct),
        "ISOLATED"
        if kind.startswith(("PULSE_", "PUMP_")) or kind == "PANIC_SELL"
        else "CROSSED",
        str(stop),
    )


@dataclass(frozen=True)
class SizeResult:
    reason: str
    quantity: str = "0"
    stop_price: str | None = None
    notional: str = "0"
    planned_loss: str = "0"


def size_candidate(
    plan,
    *,
    price,
    balance,
    available_margin,
    used_pool_margin,
    atr_pct,
    quantity_step,
    price_tick,
    min_quantity,
    max_quantity,
    min_notional,
    max_notional,
    drawdown_factor,
    analysis_factor,
):
    """Decimal sizing: never increase size to satisfy venue minimums.

    Legacy 80% pool, 3..15% allocation and 1% stop-risk ceiling; no $10
    minimum-margin uplift. Explicit factors may only reduce. Estimated stop
    loss excludes fees/slippage/gaps and is NOT a guaranteed maximum loss.
    """
    if not isinstance(plan, DirectionalPlan) or plan.reason != "CANDIDATE":
        raise ValueError("market candidate required")
    if type(plan.score) is not int or not 30 <= plan.score <= 100:
        raise ValueError("invalid candidate score")
    if type(plan.leverage) is not int or not 1 <= plan.leverage <= 5:
        raise ValueError("invalid candidate leverage")
    if plan.side not in {"LONG", "SHORT"}:
        raise ValueError("invalid candidate side")
    px, step, tick, qmin, qmax, nmin, nmax = (
        amount(x, positive=True)
        for x in (
            price,
            quantity_step,
            price_tick,
            min_quantity,
            max_quantity,
            min_notional,
            max_notional,
        )
    )
    bal, available, used, atr = (
        number(x, minimum=0)
        for x in (balance, available_margin, used_pool_margin, atr_pct)
    )
    dd, analysis = (
        number(x, minimum=0, maximum=1) for x in (drawdown_factor, analysis_factor)
    )
    stop = number(
        plan.stop_fraction,
        minimum=Decimal("0.000000000000000001"),
        maximum=Decimal("0.12"),
    )
    if qmin > qmax or nmin > nmax:
        raise ValueError("inverted instrument limits")
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_FLOOR
        # Round stop toward entry so tick alignment never enlarges stop risk.
        target = px * (1 - stop if plan.side == "LONG" else 1 + stop)
        stop_px = (target / tick).to_integral_value(
            rounding=ROUND_CEILING if plan.side == "LONG" else ROUND_FLOOR
        ) * tick
        if stop_px <= 0 or (stop_px >= px if plan.side == "LONG" else stop_px <= px):
            return SizeResult("INVALID_STOP_TICK")
        remaining = max(Decimal(0), bal * Decimal(".8") - used)
        fraction = max(Decimal(".03"), Decimal(plan.score) / 100 * Decimal(".15"))
        # Keep division rational until venue-step flooring, including repeating
        # decimal boundaries such as margin / 3 followed by leverage * 3.
        margin = Fraction(remaining * fraction)
        if atr > 4:
            margin *= max(Fraction(1, 5), 4 / Fraction(atr))
        margin = min(
            margin,
            Fraction(available),
            Fraction(bal) / (100 * plan.leverage * Fraction(stop)),
        )
        margin *= Fraction(dd) * Fraction(analysis)
        raw = min(
            margin * plan.leverage / Fraction(px),
            Fraction(qmax),
            Fraction(nmax) / Fraction(px),
        )
        qty = (raw // Fraction(step)) * step
        notional = qty * px
        if qty <= 0 or qty < qmin or notional < nmin:
            return SizeResult("INSUFFICIENT_BUDGET")
        loss = qty * abs(px - stop_px)

        # Protect downstream NUMERIC(38,18) storage; never silently round evidence.
        def exact(value):
            encoded = format(value.normalize(), "f")
            amount(encoded)
            return encoded

        return SizeResult(
            "SIZED", exact(qty), exact(stop_px), exact(notional), exact(loss)
        )


@dataclass(frozen=True)
class AnalysisAdjustment:
    reason: str
    factor: str


def analysis_adjustment(stats, *, mode):
    """Legacy rolling-history filter, without fail-open DB/cache fallbacks.

    An explicitly empty, successfully loaded history is distinct from missing
    statistics. Scope, 14-day range and freshness are the provider's obligation.
    """
    if mode not in {"hard", "soft"}:
        raise ValueError("explicit hard/soft analysis mode required")
    trades = stats["trades"]
    if type(trades) is not int or not 0 <= trades <= 1_000_000_000:
        raise ValueError("invalid historical sample count")
    win_rate = number(stats["win_rate"], minimum=0, maximum=100)
    quality = number(stats["avg_quality_score"], minimum=0, maximum=100)
    follow = number(stats["t60_avg_post_close_return_pct"])
    average = number(stats["avg_pct"])
    reason = (
        "INSUFFICIENT_HISTORY"
        if trades < 6
        else "LOW_QUALITY"
        if win_rate < 35 and quality < 40
        else "BAD_FOLLOW"
        if follow < Decimal("-.8") and average <= 0
        else "ACCEPTABLE_HISTORY"
    )
    return AnalysisAdjustment(
        reason,
        ("0.5" if mode == "soft" else "0")
        if reason in {"LOW_QUALITY", "BAD_FOLLOW"}
        else "1",
    )


def execution_market_gate(plan, sized, *, price, expected_move_pct, funding_rate):
    """Remaining pure executor market gates; not account/protection approval.

    Zero expected move retains the legacy 'no estimate' behavior; missing or
    malformed data cannot silently turn into zero. Funding is signed fraction.
    """
    if not isinstance(plan, DirectionalPlan) or plan.reason != "CANDIDATE":
        raise ValueError("candidate required")
    if not isinstance(sized, SizeResult) or sized.reason != "SIZED":
        raise ValueError("sized candidate required")
    if plan.side not in {"LONG", "SHORT"}:
        raise ValueError("invalid direction")
    px, stop = amount(price, positive=True), amount(sized.stop_price, positive=True)
    move = number(expected_move_pct, minimum=0)
    funding = number(funding_rate, minimum=-1, maximum=1)
    if stop >= px if plan.side == "LONG" else stop <= px:
        raise ValueError("stop is not on loss side")
    with localcontext() as ctx:
        ctx.prec = 100
        # Cross multiplication avoids division/rounding at the R:R=1 boundary.
        if move > 0 and move * px < abs(px - stop) * 100:
            return "REWARD_RISK_TOO_LOW"
    if (plan.side == "LONG" and funding > Decimal(".001")) or (
        plan.side == "SHORT" and funding < Decimal("-.001")
    ):
        return "ADVERSE_FUNDING"
    return "MARKET_GATES_PASSED"
