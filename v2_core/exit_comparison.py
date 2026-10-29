"""Causal same-entry exit experiments. Pure research; no order transport.

The cohort is frozen by the baseline replay, including its sizing decisions.
Counterfactual episodes may overlap: totals are NOT a deployable portfolio.
Exhaustion is an independent wick/volume/profit hypothesis, not a Pine port.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import decimal, encode
from v2_core.quant_registry import feature_frame, parameters, trailing_stop, warmup
from v2_core.quant_replay import ReplayPolicy, replay

VERSION = "same-entry-exhaustion-v1"
VARIANTS = ("baseline", "tighten", "partial")


@dataclass(frozen=True)
class ExitPolicy:
    volume_bars: int = 20
    min_relative_volume: str = "1.5"
    min_rejection_wick: str = "0.45"
    max_directional_close: str = "0.45"
    min_profit_r: str = "0.5"
    tight_atr: str = "1"
    partial_fraction: str = "0.5"

    def __post_init__(self):
        if type(self.volume_bars) is not int or not 2 <= self.volume_bars <= 200:
            raise ValueError("EXIT_VOLUME_WINDOW")
        for key, value in asdict(self).items():
            if key != "volume_bars" and (type(value) is not str or decimal(value) <= 0):
                raise ValueError("EXIT_PARAMETER")
        if not (
            decimal(self.min_relative_volume) <= 20
            and decimal(self.min_profit_r) <= 10
            and decimal(self.tight_atr) <= 10
            and decimal(self.min_rejection_wick) < 1
            and decimal(self.max_directional_close) < 1
            and decimal(self.partial_fraction) < 1
        ):
            raise ValueError("EXIT_PARAMETER_BOUND")


def exhaustion(bars, entry, policy):
    """Uses the current CLOSED bar and preceding volume only."""
    if len(bars) < policy.volume_bars + 1:
        return False
    bar = bars[-1]
    prior = bars[-policy.volume_bars - 1 : -1]
    mean = sum((b.v for b in prior), Decimal(0)) / len(prior)
    span = bar.h - bar.l
    if mean <= 0 or span <= 0:
        return False
    direction = 1 if entry["side"] == "BUY" else -1
    price, stop = decimal(entry["entry_price"]), decimal(entry["initial_stop"])
    wick = bar.h - max(bar.o, bar.c) if direction > 0 else min(bar.o, bar.c) - bar.l
    close = (bar.c - bar.l) / span if direction > 0 else (bar.h - bar.c) / span
    return (
        bar.v / mean >= decimal(policy.min_relative_volume)
        and wick / span >= decimal(policy.min_rejection_wick)
        and close <= decimal(policy.max_directional_close)
        and (bar.c - price) * direction
        >= abs(price - stop) * decimal(policy.min_profit_r)
    )


def _episode(
    bars, frames, entry, *, variant, policy, exits, params, width, opening_fee=None
):
    direction = Decimal(1 if entry["side"] == "BUY" else -1)
    price, quantity, stop, target = map(
        decimal,
        (
            entry["entry_price"],
            entry["quantity"],
            entry["initial_stop"],
            entry["target"],
        ),
    )
    tick, fee, slip, step = map(
        decimal,
        (
            policy.price_tick,
            policy.fee_rate,
            policy.slippage_fraction,
            policy.quantity_step,
        ),
    )
    remaining, gross, fees = (
        quantity,
        Decimal(0),
        (quantity * price * fee if opening_fee is None else decimal(opening_fee)),
    )
    high, low, age = price, price, 0
    time_pending = partial_pending = partial_attempted = False
    fills, observations, curve = [], [], []
    peak_net, drawdown = Decimal(0), Decimal(0)
    mfe = mae = Decimal(0)
    last = None

    def executable_price(reference):
        fill_price = (reference * (1 - direction * slip) / tick).to_integral_value(
            rounding=ROUND_FLOOR if direction > 0 else ROUND_CEILING
        ) * tick
        if fill_price <= 0:
            raise ValueError("EXIT_PRICE_BELOW_TICK")
        return fill_price

    def close(amount, reference, reason, at_ms):
        nonlocal remaining, gross, fees
        fill_price = executable_price(reference)
        pnl = direction * amount * (fill_price - price)
        cost = amount * fill_price * fee
        gross += pnl
        fees += cost
        remaining -= amount
        fills.append(
            {
                "bar_at_ms": at_ms,
                "quantity": encode(amount),
                "price": encode(fill_price),
                "gross_pnl": encode(pnl),
                "fee": encode(cost),
                "reason": reason,
            }
        )

    for i, bar in enumerate(bars):
        if bar.t < entry["opened_at_ms"]:
            continue
        last = bar
        gap_stop = bar.o <= stop if direction > 0 else bar.o >= stop
        stop_hit = bar.l <= stop if direction > 0 else bar.h >= stop
        target_hit = bar.h >= target if direction > 0 else bar.l <= target
        if time_pending:
            close(remaining, bar.o, "TIME_EXIT", bar.t)
        elif gap_stop:
            close(
                remaining,
                bar.o,
                "STOP_FIRST_AMBIGUOUS_BAR" if target_hit else "STOP",
                bar.t,
            )
        else:
            if partial_pending:
                amount = (
                    quantity * decimal(exits.partial_fraction) / step
                ).to_integral_value(rounding=ROUND_FLOOR) * step
                minimum = decimal(policy.minimum_quantity)
                # Conservative assumed filters; venue historical rules unknown.
                executable = executable_price(bar.o)
                if min(amount, remaining - amount) >= minimum and min(
                    amount, remaining - amount
                ) * executable >= decimal(policy.minimum_notional):
                    close(amount, bar.o, "EXHAUSTION_PARTIAL_NEXT_OPEN", bar.t)
                else:
                    observations.append(
                        {"at_ms": bar.t, "event": "PARTIAL_BELOW_FILTERS"}
                    )
                partial_pending = False
            if stop_hit:
                close(
                    remaining,
                    stop,
                    "STOP_FIRST_AMBIGUOUS_BAR" if target_hit else "STOP",
                    bar.t,
                )
            elif target_hit:
                close(remaining, target, "TARGET", bar.t)
        marked = gross - fees + direction * remaining * (bar.c - price)
        peak_net = max(peak_net, marked)
        drawdown = max(drawdown, peak_net - marked)
        curve.append({"closed_at_ms": bar.t + width, "net_marked": encode(marked)})
        if remaining == 0:
            break
        # Close-only excursions exclude unknown post-exit intrabar highs/lows.
        excursion = direction * (bar.c - price) * quantity
        mfe, mae = max(mfe, excursion), min(mae, excursion)
        age += 1
        high, low = max(high, bar.h), min(low, bar.l)
        time_pending = age >= policy.max_hold_bars
        frame = frames[i]
        if frame and decimal(frame["current"]["atr"]) > 0:
            atr = decimal(frame["current"]["atr"])
            stop = decimal(
                trailing_stop(
                    side=entry["side"],
                    prior_stop=stop,
                    high_water=high,
                    low_water=low,
                    atr=atr,
                    multiplier=params["trail_atr"],
                )
            )
            if variant != "baseline" and exhaustion(
                bars[max(0, i - exits.volume_bars) : i + 1], entry, exits
            ):
                observations.append(
                    {"at_ms": bar.t + width, "event": "EXHAUSTION_CLOSED_BAR"}
                )
                if variant == "tighten":
                    proposed = bar.c - direction * atr * decimal(exits.tight_atr)
                    if proposed > 0:
                        stop = (
                            max(stop, proposed)
                            if direction > 0
                            else min(stop, proposed)
                        )
                elif not partial_attempted:
                    partial_pending = partial_attempted = True
    if last is None:
        raise ValueError("EXIT_ENTRY_OUTSIDE_HISTORY")
    marked = decimal(curve[-1]["net_marked"])
    return {
        "entry": entry,
        "variant": variant,
        "status": "CLOSED" if remaining == 0 else "CENSORED_OPEN",
        "remaining_quantity": encode(remaining),
        "gross_realized": encode(gross),
        "fees": encode(fees),
        "net_realized": encode(gross - fees),
        "terminal_net_marked": encode(marked),
        "closed_bar_at_ms": last.t if remaining == 0 else None,
        "holding_minutes_lower_bound": encode(
            Decimal(last.t - entry["opened_at_ms"]) / 60000
        ),
        "close_sampled_mfe_gross": encode(mfe),
        "close_sampled_mae_gross": encode(mae),
        "close_sampled_max_drawdown_usdt": encode(drawdown),
        "close_sampled_profit_giveback_usdt": encode(
            max(Decimal(0), peak_net - marked)
        ),
        "fills": fills,
        "observations": observations,
    }


def compare_exits(
    bars,
    *,
    timeframe_minutes=5,
    entry_start_ms=None,
    entry_end_ms=None,
    policy=None,
    exits=None,
    params=None,
):
    policy, exits, params = (
        policy or ReplayPolicy(),
        exits or ExitPolicy(),
        parameters(params),
    )
    if not isinstance(exits, ExitPolicy) or len(bars) > 3000:
        raise ValueError("EXIT_STUDY_BOUND")
    baseline = replay(
        bars,
        timeframe_minutes=timeframe_minutes,
        policy=policy,
        params=params,
        entry_start_ms=entry_start_ms,
        entry_end_ms=entry_end_ms,
    )
    frames = [
        feature_frame(
            bars[max(0, i + 1 - warmup(params)) : i + 1],
            timeframe_minutes=timeframe_minutes,
            params=params,
        )
        if i + 1 >= warmup(params)
        else None
        for i in range(len(bars))
    ]
    with localcontext() as ctx:
        ctx.prec = 50
        pairs = []
        for entry in baseline["entries"]:
            results = {
                v: _episode(
                    bars,
                    frames,
                    entry,
                    variant=v,
                    policy=policy,
                    exits=exits,
                    params=params,
                    width=timeframe_minutes * 60000,
                )
                for v in VARIANTS
            }
            base = results["baseline"]
            for v, result in results.items():
                complete = base["status"] == result["status"] == "CLOSED"
                result["paired_closed_net_difference"] = (
                    encode(
                        decimal(result["net_realized"]) - decimal(base["net_realized"])
                    )
                    if complete
                    else None
                )
                result["early_exit_missed_profit"] = (
                    complete
                    and v != "baseline"
                    and (
                        result["closed_bar_at_ms"] < base["closed_bar_at_ms"]
                        and decimal(result["net_realized"])
                        < decimal(base["net_realized"])
                    )
                )
            pairs.append(results)
    return {
        "version": VERSION,
        "execution_authorized": False,
        "publication": "SHADOW_ONLY",
        "input_digest": digest(canonical({"bars": [b.payload() for b in bars]})),
        "cohort_digest": digest(canonical({"entries": baseline["entries"]})),
        "entry_start_ms": entry_start_ms,
        "entry_end_ms": entry_end_ms,
        "policy": asdict(policy),
        "exit_policy": asdict(exits),
        "parameters": params,
        "pairs": pairs,
        "entry_attempts": baseline["entry_attempts"],
        "summary": {
            v: {
                "episodes": len(pairs),
                "closed": sum(p[v]["status"] == "CLOSED" for p in pairs),
                "exhaustion_observations": sum(
                    len(p[v]["observations"]) for p in pairs
                ),
                "paired_closed_net_difference": encode(
                    sum(
                        (
                            decimal(p[v]["paired_closed_net_difference"])
                            for p in pairs
                            if p[v]["paired_closed_net_difference"] is not None
                        ),
                        Decimal(0),
                    )
                ),
                "paired_closed_count": sum(
                    p[v]["paired_closed_net_difference"] is not None for p in pairs
                ),
                "early_exit_missed_profit_count": sum(
                    p[v]["early_exit_missed_profit"] for p in pairs
                ),
            }
            for v in VARIANTS
        },
        "limitations": [
            "FROZEN_BASELINE_ENTRIES_AND_SIZING_NOT_LIVE_TRADES",
            "OVERLAPPING_COUNTERFACTUAL_EPISODES_NOT_A_PORTFOLIO_BACKTEST",
            "FUNDING_NOT_SUPPLIED_COSTS_AND_VENUE_FILTERS_ASSUMED",
            "CLOSE_SAMPLED_EXCURSIONS_NOT_INTRABAR_MFE_MAE",
            "CENSORED_OPEN_EPISODES_EXCLUDED_FROM_PAIRED_CLOSED_RESULTS",
            "BASELINE_IS_RESEARCH_ATR_EXIT_NOT_PRODUCTION_EXIT_POLICY",
        ],
    }
