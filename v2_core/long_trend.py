"""Isolated, long-only trend research; never imported by a trading executor.

Hourly input is aggregated into completed UTC days. Signals use completed days,
fills use the next hourly open, and daily stop ratchets apply to the next hour.
This is a single-symbol paper sleeve, not a portfolio or liquidation simulator.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal, encode

HOUR = 3_600_000
DAY = 24 * HOUR
VERSION = "long-trend-research-v1"


@dataclass(frozen=True)
class LongTrendPolicy:
    """Untuned research defaults, not authorization to change live risk limits."""

    initial_equity: str = "1000"
    breakout_days: int = 20
    atr_days: int = 14
    atr_multiple: str = "3"
    structure_days: int = 10  # Zero disables the daily-low structure stop.
    risk_fraction: str = "0.005"
    max_margin_fraction: str = "0.10"
    pool_fraction: str = "0.70"
    leverage: int = 1
    fee_rate: str = "0.0005"
    slippage_fraction: str = "0.0005"
    max_chase_atr: str = "1"
    quantity_step: str = "0.001"
    price_tick: str = "0.00000001"
    minimum_quantity: str = "0.001"
    maximum_quantity: str = "1000000000"
    minimum_notional: str = "5"
    maximum_notional: str = "1000000000"

    def __post_init__(self):
        integer_fields = {"breakout_days", "atr_days", "structure_days", "leverage"}
        for key, value in asdict(self).items():
            if key in integer_fields:
                if type(value) is not int:
                    raise ValueError("LONG_TREND_POLICY_INTEGER")
            elif type(value) is not str or decimal(value) < 0:
                raise ValueError("LONG_TREND_POLICY_DECIMAL")
        if not (
            2 <= self.breakout_days <= 365
            and 2 <= self.atr_days <= 365
            and 0 <= self.structure_days <= 365
            and self.leverage == 1
        ):
            raise ValueError("LONG_TREND_PERIOD_OR_LEVERAGE")
        if not (
            0
            < decimal(self.risk_fraction)
            <= decimal(self.max_margin_fraction)
            <= decimal(self.pool_fraction)
            <= Decimal("0.70")
        ):
            raise ValueError("LONG_TREND_RESERVE_REQUIRED")
        if not (
            decimal(self.initial_equity) > 0
            and decimal(self.quantity_step) > 0
            and decimal(self.price_tick) > 0
            and 0 < decimal(self.minimum_quantity) <= decimal(self.maximum_quantity)
            and 0 < decimal(self.minimum_notional) <= decimal(self.maximum_notional)
        ):
            raise ValueError("LONG_TREND_CAPITAL_OR_VENUE_RULES")
        if not (
            0 < decimal(self.atr_multiple) <= 20
            and 0 < decimal(self.max_chase_atr) <= 5
            and decimal(self.fee_rate) <= Decimal("0.01")
            and decimal(self.slippage_fraction) <= Decimal("0.1")
        ):
            raise ValueError("LONG_TREND_COST_OR_ATR_BOUND")


def _json(value):
    if isinstance(value, Decimal):
        return encode(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def replay(
    bars,
    *,
    policy=None,
    funding=(),
    funding_complete=False,
    entry_start_ms=None,
    closed_at_ms=None,
):
    """Replay contiguous closed 1h ``Bar`` records, oldest first, without I/O.

    ``closed_at_ms`` is the caller's as-of boundary, not the wall clock. If
    omitted, the caller asserts the last bar is closed. Funding records have
    exactly ``at_ms``, ``rate`` and ``mark_price``; exact event times are retained.
    Boundary funding charges carried inventory before new fills. Intrahour
    funding is charged before a possible intrahour stop; ambiguous credits are
    omitted. A stop already breached at the open precedes later funding.

    Funding completeness is caller evidence, never independently inferred from
    an empty array. Even complete funding cannot make this a profitability or
    venue acceptance result. Exhausted input does not force-close inventory.
    """
    policy = LongTrendPolicy() if policy is None else policy
    if (
        not isinstance(policy, LongTrendPolicy)
        or not isinstance(bars, (list, tuple))
        or not 1 <= len(bars) <= 100_000
        or not isinstance(funding, (list, tuple))
        or len(funding) > 100_000
        or type(funding_complete) is not bool
        or (
            entry_start_ms is not None
            and (
                type(entry_start_ms) is not int or not 0 <= entry_start_ms <= 2**53 - 1
            )
        )
    ):
        raise ValueError("LONG_TREND_INPUT")
    if any(not isinstance(b, Bar) or b.t % HOUR for b in bars) or any(
        bars[i].t != bars[i - 1].t + HOUR for i in range(1, len(bars))
    ):
        raise ValueError("LONG_TREND_HISTORY_GAP")
    boundary = bars[-1].t + HOUR if closed_at_ms is None else closed_at_ms
    if (
        type(boundary) is not int
        or not 0 <= boundary <= 2**53 - 1
        or boundary % HOUR
        or bars[-1].t + HOUR > boundary
    ):
        raise ValueError("LONG_TREND_UNCLOSED_OR_FUTURE_BAR")
    if bars[-1].t + HOUR != boundary:
        raise ValueError("LONG_TREND_STALE_HISTORY")
    by_hour, funding_times = {}, set()
    for event in funding:
        if (
            not isinstance(event, dict)
            or set(event) != {"at_ms", "rate", "mark_price"}
            or type(event["at_ms"]) is not int
            or not bars[0].t <= event["at_ms"] < boundary
            or abs(decimal(event["rate"])) > 1
            or decimal(event["mark_price"]) <= 0
        ):
            raise ValueError("LONG_TREND_FUNDING_INVALID")
        if event["at_ms"] in funding_times:
            raise ValueError("LONG_TREND_FUNDING_DUPLICATE")
        funding_times.add(event["at_ms"])
        by_hour.setdefault(event["at_ms"] // HOUR * HOUR, []).append(event)
    for events in by_hour.values():
        events.sort(key=lambda event: event["at_ms"])
    with localcontext() as ctx:
        ctx.prec = 50
        return _replay(
            bars,
            policy,
            by_hour,
            funding_complete,
            entry_start_ms,
            boundary,
            closed_at_ms is not None,
        )


def _replay(
    bars, policy, by_hour, funding_complete, entry_start_ms, boundary, explicit_boundary
):
    cash = decimal(policy.initial_equity)
    initial = cash
    fee, slip, tick, step, atr_multiple = map(
        decimal,
        (
            policy.fee_rate,
            policy.slippage_fraction,
            policy.price_tick,
            policy.quantity_step,
            policy.atr_multiple,
        ),
    )
    position, pending, atr = None, None, None
    days, day_hours, true_ranges = [], [], []
    decisions, trades, entries, attempts, curve = [], [], [], [], []
    fees_total, funding_total = Decimal(0), Decimal(0)
    high_equity, max_drawdown = cash, Decimal(0)
    insolvent_at_ms = None
    skipped_ambiguous_credits = 0

    def price(value, *, buy=False):
        rounded = (value / tick).to_integral_value(
            rounding=ROUND_CEILING if buy else ROUND_FLOOR
        ) * tick
        if rounded <= 0:
            raise ValueError("LONG_TREND_PRICE_BELOW_TICK")
        return rounded

    def apply_funding(event, *, ambiguous_stop=False):
        nonlocal cash, funding_total, skipped_ambiguous_credits
        if position is None:
            return
        charge = (
            -position["quantity"]
            * decimal(event["mark_price"])
            * decimal(event["rate"])
        )
        if ambiguous_stop and charge > 0:
            skipped_ambiguous_credits += 1
            return
        position["funding"] += charge
        cash += charge
        funding_total += charge

    def close(bar, reason, reference):
        nonlocal cash, fees_total, position
        exit_price = price(reference * (1 - slip))
        closing_fee = position["quantity"] * exit_price * fee
        gross = position["quantity"] * (exit_price - position["entry"])
        total_fees = position["fees"] + closing_fee
        cash += gross - closing_fee
        fees_total += closing_fee
        trades.append(
            _json(
                {
                    "strategy_id": VERSION,
                    "side": "BUY",
                    "signal_at_ms": position["signal_at_ms"],
                    "opened_at_ms": position["opened_at_ms"],
                    "closed_bar_at_ms": bar.t,
                    "observed_at_ms": bar.t + HOUR,
                    "quantity": position["quantity"],
                    "entry_price": position["entry"],
                    "exit_price": exit_price,
                    "initial_stop": position["initial_stop"],
                    "exit_stop": position["stop"],
                    "gross_pnl": gross,
                    "fees": total_fees,
                    "funding": position["funding"],
                    "net_pnl": gross - total_fees + position["funding"],
                    "reason": reason,
                    "held_hours": (bar.t - position["opened_at_ms"]) // HOUR,
                    "intrabar_execution_time_unknown": reason != "GAP_STOP",
                }
            )
        )
        position = None

    for bar in bars:
        events = by_hour.get(bar.t, ())
        for event in events:
            if event["at_ms"] == bar.t:
                apply_funding(event)
        if pending is not None:
            reason = (
                "INSOLVENT_PAPER_ACCOUNT"
                if insolvent_at_ms is not None or cash <= 0
                else None
            )
            entry, stop = price(bar.o * (1 + slip), buy=True), pending["stop"]
            if reason is None and bar.o <= stop:
                reason = "STOP_INVALID_AT_NEXT_OPEN"
            if (
                reason is None
                and abs(entry - pending["reference"])
                > decimal(policy.max_chase_atr) * pending["atr"]
            ):
                reason = "NEXT_OPEN_CHASE_LIMIT"
            if reason is None:
                stop_fill = price(stop * (1 - slip))
                unit_risk = entry - stop_fill + (entry + stop_fill) * fee
                risk_budget = cash * decimal(policy.risk_fraction)
                margin_budget = cash * min(
                    decimal(policy.pool_fraction), decimal(policy.max_margin_fraction)
                )
                raw = min(
                    risk_budget / unit_risk,
                    margin_budget / (entry * (1 + fee)),
                    decimal(policy.maximum_quantity),
                    decimal(policy.maximum_notional) / entry,
                )
                qty = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
                if qty < decimal(policy.minimum_quantity):
                    reason = "QUANTITY_BELOW_MINIMUM"
                elif qty * entry < decimal(policy.minimum_notional):
                    reason = "NOTIONAL_BELOW_MINIMUM"
                else:
                    opening_fee = qty * entry * fee
                    position = {
                        "strategy_id": VERSION,
                        "side": "BUY",
                        "quantity": qty,
                        "entry": entry,
                        "stop": stop,
                        "initial_stop": stop,
                        "opened_at_ms": bar.t,
                        "signal_at_ms": pending["signal_at_ms"],
                        "fees": opening_fee,
                        "funding": Decimal(0),
                        "daily_high_water": entry,
                        "entry_cash": cash,
                        "initial_margin": qty * entry,
                        "initial_stop_risk_including_costs": qty * unit_risk,
                    }
                    entries.append(_json(position))
                    cash -= opening_fee
                    fees_total += opening_fee
            attempts.append(
                {
                    "signal_at_ms": pending["signal_at_ms"],
                    "attempted_at_ms": bar.t,
                    "status": "SIMULATED_FILLED" if reason is None else "NOT_FILLED",
                    "reason": reason,
                }
            )
            pending = None
        # Known open gaps exit before later intrahour funding. A bar's favorable
        # high can never move its own stop before that bar's adverse low is tested.
        if position is not None and bar.o <= position["stop"]:
            close(bar, "GAP_STOP", bar.o)
        stop_hit = position is not None and bar.l <= position["stop"]
        for event in events:
            if event["at_ms"] > bar.t:
                apply_funding(event, ambiguous_stop=stop_hit)
        if stop_hit:
            close(bar, "PROTECTIVE_STOP", position["stop"])

        # Drop only partial leading UTC days. Never fill gaps with synthetic bars.
        if bar.t % DAY == 0:
            day_hours = []
        day_hours.append(bar)
        if (bar.t + HOUR) % DAY == 0 and len(day_hours) == 24:
            day = Bar(
                day_hours[0].t,
                day_hours[0].o,
                max(b.h for b in day_hours),
                min(b.l for b in day_hours),
                bar.c,
                sum((b.v for b in day_hours), Decimal(0)),
            )
            tr = (
                max(day.h - day.l, abs(day.h - days[-1].c), abs(day.l - days[-1].c))
                if days
                else day.h - day.l
            )
            true_ranges.append(tr)
            if atr is not None:
                atr += (tr - atr) / policy.atr_days
            elif len(true_ranges) >= policy.atr_days:
                atr = sum(true_ranges[-policy.atr_days :]) / policy.atr_days
            prior_high = (
                max(d.h for d in days[-policy.breakout_days :])
                if len(days) >= policy.breakout_days
                else None
            )
            days.append(day)
            ready = (
                atr is not None
                and atr > 0
                and prior_high is not None
                and len(days) >= policy.structure_days
            )
            decision = {
                "closed_at_ms": bar.t + HOUR,
                "status": "WARMUP",
                "close": encode(day.c),
                "atr": encode(atr),
                "prior_breakout_high": encode(prior_high),
                "bars_digest": digest(
                    canonical(
                        {
                            "days": [
                                d.payload()
                                for d in days[
                                    -max(
                                        policy.breakout_days + 1,
                                        policy.atr_days,
                                        policy.structure_days,
                                    ) :
                                ]
                            ]
                        }
                    )
                ),
            }
            structure = (
                min(d.l for d in days[-policy.structure_days :])
                if policy.structure_days and len(days) >= policy.structure_days
                else Decimal(0)
            )
            if ready and position is not None:
                position["daily_high_water"] = max(position["daily_high_water"], day.h)
                candidate = max(
                    position["daily_high_water"] - atr_multiple * atr, structure
                )
                old_stop = position["stop"]
                if candidate > old_stop:
                    position["stop"] = max(old_stop, price(candidate))
                decision.update(
                    status="HOLD_TREND",
                    prior_stop=encode(old_stop),
                    next_bar_stop=encode(position["stop"]),
                )
            elif ready:
                stop_reference = max(day.c - atr_multiple * atr, structure)
                if insolvent_at_ms is not None or cash <= 0:
                    decision["status"] = "INSOLVENT_PAPER_ACCOUNT"
                elif entry_start_ms is not None and bar.t + HOUR < entry_start_ms:
                    decision["status"] = "BEFORE_ENTRY_WINDOW"
                elif day.c <= prior_high:
                    decision["status"] = "NO_BREAKOUT"
                elif stop_reference < tick or stop_reference >= day.c:
                    decision["status"] = "INVALID_INITIAL_STOP"
                else:
                    pending = {
                        "signal_at_ms": bar.t + HOUR,
                        "reference": day.c,
                        "atr": atr,
                        "stop": price(stop_reference),
                    }
                    decision.update(
                        status="ENTRY_SIGNAL_NEXT_OPEN",
                        next_bar_stop=encode(pending["stop"]),
                    )
            decisions.append(decision)

        unrealized = (
            position["quantity"] * (bar.c - position["entry"])
            if position
            else Decimal(0)
        )
        equity = cash + unrealized
        if equity <= 0 and insolvent_at_ms is None:
            insolvent_at_ms = bar.t + HOUR
        high_equity = max(high_equity, equity)
        max_drawdown = max(max_drawdown, (high_equity - equity) / high_equity)
        curve.append(
            _json(
                {
                    "closed_at_ms": bar.t + HOUR,
                    "cash": cash,
                    "equity": equity,
                    "unrealized": unrealized,
                }
            )
        )

    return {
        "version": VERSION,
        "execution_authorized": False,
        "profitability_valid": False,
        "scope": "ISOLATED_SINGLE_SYMBOL_PAPER_SLEEVE_NOT_PORTFOLIO",
        "policy": asdict(policy),
        "funding_complete": funding_complete,
        "cost_complete": funding_complete,
        "costs_observed": False,
        "assumptions": {
            "signal": "COMPLETED_UTC_DAILY_BREAKOUT_NEXT_HOUR_OPEN",
            "exit": "DAILY_ATR_AND_OPTIONAL_STRUCTURE_RATCHET_EFFECTIVE_NEXT_HOUR_NO_TIME_EXIT",
            "funding": "BOUNDARY_CARRIED_INVENTORY_FIRST_INTRAHOUR_DEBITS_BEFORE_STOP_AMBIGUOUS_CREDITS_OMITTED",
            "fees_and_slippage": "CONFIGURED_NOT_OBSERVED",
            "cost_completeness_scope": "CONFIGURED_FEES_SLIPPAGE_AND_CALLER_ASSERTED_FUNDING_ONLY",
            "margin_reserve": "ENTRY_NOTIONAL_PLUS_FEE_CAPPED_AT_MOST_70_PERCENT_NO_PYRAMIDING",
            "as_of": "CALLER_SUPPLIED_CLOSED_BOUNDARY"
            if explicit_boundary
            else "CALLER_ASSERTED_CLOSED_BARS",
            "liquidation": "NOT_MODELED_1X_ONLY_INSOLVENCY_FLAGGED_NO_NEW_ENTRIES",
            "eligibility_liquidity_survivorship": "NOT_VALIDATED_REQUIRES_CROSS_SECTIONAL_OUT_OF_SAMPLE_RESEARCH",
            "funding_completeness": "CALLER_ASSERTED"
            if funding_complete
            else "MISSING_OR_UNVERIFIED_NOT_PROFITABILITY_EVIDENCE",
        },
        "summary": {
            "closed_at_ms": boundary,
            "hourly_bars": len(bars),
            "completed_days": len(days),
            "closed_trades": len(trades),
            "entries": len(entries),
            "wins": sum(decimal(t["net_pnl"]) > 0 for t in trades),
            "cash": encode(cash),
            "equity": curve[-1]["equity"],
            "net_pnl_marked": encode(decimal(curve[-1]["equity"]) - initial),
            "realized_net_pnl": encode(
                sum((decimal(t["net_pnl"]) for t in trades), Decimal(0))
            ),
            "fees": encode(fees_total),
            "funding": encode(funding_total),
            "max_drawdown_fraction": encode(max_drawdown),
            "insolvent_at_ms": insolvent_at_ms,
            "ambiguous_funding_credits_omitted": skipped_ambiguous_credits,
            "entry_rejections": dict(
                Counter(a["reason"] for a in attempts if a["reason"])
            ),
        },
        "decisions": decisions,
        "entries": entries,
        "entry_attempts": attempts,
        "trades": trades,
        "curve": curve,
        "open_position": _json(position),
        "pending_signal": _json(pending),
    }
