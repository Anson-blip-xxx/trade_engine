"""Deterministic single-symbol, next-bar research simulator, NEVER an executor.

Costs and funding are explicit inputs. Intrabar stop/target ambiguity is resolved
against the position. Trailing changes take effect on the following bar. An open
position at data exhaustion stays OPEN; mark-to-market is not a realized result.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal, encode
from v2_core.quant_registry import (
    evaluate,
    feature_frame,
    parameters,
    trailing_stop,
    warmup,
)


@dataclass(frozen=True)
class ReplayPolicy:
    initial_equity: str = "1000"
    risk_fraction: str = "0.005"
    pool_fraction: str = "0.70"
    max_margin_fraction: str = "0.05"
    leverage: int = 2
    fee_rate: str = "0.0005"
    slippage_fraction: str = "0.0005"
    quantity_step: str = "0.001"
    price_tick: str = "0.00000001"
    minimum_quantity: str = "0.001"
    maximum_quantity: str = "1000000000"
    minimum_notional: str = "5"
    maximum_notional: str = "1000000000"
    max_hold_bars: int = 24
    max_chase_atr: str = "1"

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key in {"leverage", "max_hold_bars"}:
                continue
            if type(value) is not str or decimal(value) < 0:
                raise ValueError("QUANT_REPLAY_POLICY")
        if (
            not 0
            < decimal(self.risk_fraction)
            <= decimal(self.max_margin_fraction)
            <= decimal(self.pool_fraction)
            < 1
        ):
            raise ValueError("QUANT_REPLAY_RESERVE_REQUIRED")
        if decimal(self.initial_equity) <= 0 or decimal(self.quantity_step) <= 0:
            raise ValueError("QUANT_REPLAY_CAPITAL")
        if (
            decimal(self.price_tick) <= 0
            or not 0 < decimal(self.minimum_quantity) <= decimal(self.maximum_quantity)
            or not 0 < decimal(self.minimum_notional) <= decimal(self.maximum_notional)
        ):
            raise ValueError("QUANT_REPLAY_VENUE_RULES")
        if type(self.leverage) is not int or self.leverage not in (1, 2, 3, 5, 8):
            raise ValueError("QUANT_REPLAY_LEVERAGE")
        if type(self.max_hold_bars) is not int or not 1 <= self.max_hold_bars <= 10000:
            raise ValueError("QUANT_REPLAY_HOLD_BOUND")
        if (
            decimal(self.fee_rate) > Decimal("0.01")
            or decimal(self.slippage_fraction) > Decimal("0.1")
            or not 0 < decimal(self.max_chase_atr) <= 5
        ):
            raise ValueError("QUANT_REPLAY_COST_BOUND")


def replay(
    bars,
    *,
    timeframe_minutes,
    plugin="donchian",
    params=None,
    policy=None,
    funding=(),
    entry_gate=None,
    entry_start_ms=None,
    entry_end_ms=None,
):
    """Funding records: {at_ms, rate, mark_price}; supplied actual signed rates.

    Single-symbol results cannot be summed as if each had an independent copy
    of the same portfolio's cash. This simulator does not validate live liquidity.
    """
    p, policy = parameters(params), policy or ReplayPolicy()
    if (
        not isinstance(policy, ReplayPolicy)
        or not isinstance(bars, (list, tuple))
        or not 1 <= len(bars) <= 100000
        or not isinstance(funding, (list, tuple))
        or len(funding) > 10000
        or (entry_gate is not None and not callable(entry_gate))
        or (
            entry_start_ms is not None
            and (type(entry_start_ms) is not int or entry_start_ms < 0)
        )
        or (
            entry_end_ms is not None
            and (
                type(entry_end_ms) is not int
                or entry_end_ms < 0
                or (entry_start_ms is not None and entry_end_ms <= entry_start_ms)
            )
        )
    ):
        raise ValueError("QUANT_REPLAY_INPUT")
    if type(timeframe_minutes) is not int or timeframe_minutes not in (
        1,
        5,
        15,
        30,
        60,
        240,
        1440,
    ):
        raise ValueError("QUANT_TIMEFRAME_UNSUPPORTED")
    width = timeframe_minutes * 60000
    if any(not isinstance(b, Bar) or b.t % width for b in bars) or any(
        bars[i].t != bars[i - 1].t + width for i in range(1, len(bars))
    ):
        raise ValueError("QUANT_REPLAY_GAP")
    events = sorted(funding, key=lambda event: event["at_ms"])
    if len({event["at_ms"] for event in events}) != len(events):
        raise ValueError("QUANT_FUNDING_DUPLICATE")
    for event in events:
        if (
            set(event) != {"at_ms", "rate", "mark_price"}
            or type(event["at_ms"]) is not int
            or not bars[0].t <= event["at_ms"] < bars[-1].t + width
            or abs(decimal(event["rate"])) > 1
            or decimal(event["mark_price"]) <= 0
        ):
            raise ValueError("QUANT_FUNDING_INVALID")
        if event["at_ms"] % width:
            raise ValueError("QUANT_FUNDING_REQUIRES_FINER_BARS")
    by_time = {e["at_ms"]: e for e in events}
    # Validate the plugin even when data never reaches warmup.
    evaluate({"version": "quant-closed-bars-v1", "status": "WARMUP"}, enabled=(plugin,))
    with localcontext() as ctx:
        ctx.prec = 50
        cash = decimal(policy.initial_equity)
        fee, slip, step = map(
            decimal, (policy.fee_rate, policy.slippage_fraction, policy.quantity_step)
        )
        position, pending, close_pending = None, None, False
        trades, decisions, curve, entry_attempts = [], [], [], []
        entries = []
        high_equity, max_drawdown = cash, Decimal(0)
        fees_total, funding_total = Decimal(0), Decimal(0)
        tick = decimal(policy.price_tick)

        def executable(price, direction):
            result = (price / tick).to_integral_value(
                rounding=ROUND_CEILING if direction > 0 else ROUND_FLOOR
            ) * tick
            if result <= 0:
                raise ValueError("QUANT_REPLAY_PRICE_BELOW_TICK")
            return result

        for i, bar in enumerate(bars):
            # Explicit boundary convention: funding belongs to inventory carried
            # into this boundary, before any simulated executions at that open.
            event = by_time.get(bar.t)
            if position and event:
                charge = (
                    -position["direction"]
                    * position["quantity"]
                    * decimal(event["mark_price"])
                    * decimal(event["rate"])
                )
                position["funding"] += charge
                cash += charge
                funding_total += charge
            if position is None and pending is not None:
                win, signal_frame = pending
                direction = Decimal(1) if win["side"] == "BUY" else Decimal(-1)
                entry = executable(bar.o * (1 + direction * slip), direction)
                reference = decimal(win["reference_price"])
                stop = reference * (1 - direction * decimal(win["stop_fraction"]))
                stop = executable(stop, -direction)
                distance = (entry - stop) * direction
                risk = cash * decimal(policy.risk_fraction)
                margin = cash * min(
                    decimal(policy.pool_fraction), decimal(policy.max_margin_fraction)
                )
                entry_failure = (
                    "STOP_INVALID_AT_NEXT_OPEN"
                    if distance <= 0
                    else "NEXT_OPEN_CHASE_LIMIT"
                )
                if distance > 0 and abs(entry - reference) <= decimal(
                    policy.max_chase_atr
                ) * decimal(signal_frame["current"]["atr"]):
                    unit_risk = distance + entry * (fee * 2 + slip * 2)
                    raw = min(
                        risk / unit_risk,
                        margin * policy.leverage / entry,
                        decimal(policy.maximum_quantity),
                        decimal(policy.maximum_notional) / entry,
                    )
                    qty = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
                    opening_fee = qty * entry * fee
                    entry_failure = (
                        "QUANTITY_BELOW_MINIMUM"
                        if qty < decimal(policy.minimum_quantity)
                        else "NOTIONAL_BELOW_MINIMUM"
                        if qty * entry < decimal(policy.minimum_notional)
                        else "MARGIN_RESERVE_REQUIRED"
                    )
                    if (
                        qty >= decimal(policy.minimum_quantity)
                        and qty * entry >= decimal(policy.minimum_notional)
                        and qty * entry / policy.leverage + opening_fee
                        <= cash * decimal(policy.pool_fraction)
                    ):
                        cash -= opening_fee
                        fees_total += opening_fee
                        position = {
                            "strategy_id": plugin,
                            "side": win["side"],
                            "direction": direction,
                            "quantity": qty,
                            "entry": entry,
                            "stop": stop,
                            "target": entry
                            + direction * distance * decimal(p["target_r_multiple"]),
                            "opened_at_ms": bar.t,
                            "signal_at_ms": signal_frame["closed_at_ms"],
                            "signal_digest": digest(canonical(signal_frame)),
                            "fees": opening_fee,
                            "funding": Decimal(0),
                            "high_water": entry,
                            "low_water": entry,
                            "bars": 0,
                            "trailing_stop": stop,
                        }
                        entries.append(
                            {
                                "side": position["side"],
                                "opened_at_ms": bar.t,
                                "signal_at_ms": position["signal_at_ms"],
                                "signal_digest": position["signal_digest"],
                                "quantity": encode(qty),
                                "entry_price": encode(entry),
                                "initial_stop": encode(stop),
                                "target": encode(position["target"]),
                            }
                        )
                entry_attempts.append(
                    {
                        "signal_at_ms": signal_frame["closed_at_ms"],
                        "attempted_at_ms": bar.t,
                        "status": "SIMULATED_FILLED"
                        if position is not None
                        else "NOT_FILLED",
                        "reason": None if position is not None else entry_failure,
                    }
                )
                pending = None
            if position:
                pos = position
                direction = pos["direction"]
                stop_hit = (
                    bar.l <= pos["trailing_stop"]
                    if direction > 0
                    else bar.h >= pos["trailing_stop"]
                )
                target_hit = (
                    bar.h >= pos["target"] if direction > 0 else bar.l <= pos["target"]
                )
                reason, exit_price = None, None
                if close_pending:
                    reason, exit_price = "TIME_EXIT", bar.o
                elif stop_hit:
                    reason = "STOP_FIRST_AMBIGUOUS_BAR" if target_hit else "STOP"
                    exit_price = (
                        min(bar.o, pos["trailing_stop"])
                        if direction > 0
                        else max(bar.o, pos["trailing_stop"])
                    )
                elif target_hit:
                    reason, exit_price = "TARGET", pos["target"]
                if reason:
                    exit_price = executable(
                        exit_price * (1 - direction * slip), -direction
                    )
                    closing_fee = pos["quantity"] * exit_price * fee
                    gross = direction * pos["quantity"] * (exit_price - pos["entry"])
                    cash += gross - closing_fee
                    fees_total += closing_fee
                    trades.append(
                        {
                            "strategy_id": plugin,
                            "side": pos["side"],
                            "signal_at_ms": pos["signal_at_ms"],
                            "opened_at_ms": pos["opened_at_ms"],
                            "closed_bar_at_ms": bar.t,
                            "signal_digest": pos["signal_digest"],
                            "quantity": encode(pos["quantity"]),
                            "entry_price": encode(pos["entry"]),
                            "exit_price": encode(exit_price),
                            "gross_pnl": encode(gross),
                            "fees": encode(pos["fees"] + closing_fee),
                            "funding": encode(pos["funding"]),
                            "net_pnl": encode(
                                gross - pos["fees"] - closing_fee + pos["funding"]
                            ),
                            "reason": reason,
                            "intrabar_execution_time_unknown": reason != "TIME_EXIT",
                        }
                    )
                    position, close_pending = None, False
                else:
                    pos["bars"] += 1
                    pos["high_water"] = max(pos["high_water"], bar.h)
                    pos["low_water"] = min(pos["low_water"], bar.l)
                    close_pending = pos["bars"] >= policy.max_hold_bars
            if i + 1 >= warmup(p):
                frame = feature_frame(
                    bars[max(0, i + 1 - warmup(p)) : i + 1],
                    timeframe_minutes=timeframe_minutes,
                    params=p,
                )
                candidates = evaluate(frame, enabled=(plugin,))
                decisions.append(
                    {
                        "closed_at_ms": frame["closed_at_ms"],
                        "status": candidates["status"],
                        "evaluations": candidates["evaluations"],
                        "bars_digest": frame["bars_digest"],
                    }
                )
                gate_allowed = True
                after_start = (
                    entry_start_ms is None or frame["closed_at_ms"] >= entry_start_ms
                ) and (entry_end_ms is None or frame["closed_at_ms"] < entry_end_ms)
                if candidates["winner"] and entry_gate is not None and after_start:
                    verdict = entry_gate(
                        tuple(bars[max(0, i - 511) : i + 1]),
                        candidates["winner"]["side"],
                    )
                    if (
                        not isinstance(verdict, dict)
                        or type(verdict.get("allow")) is not bool
                    ):
                        raise ValueError("QUANT_RESEARCH_GATE_VERDICT")
                    canonical(verdict)
                    decisions[-1]["research_gate"] = verdict
                    decisions[-1]["flat_at_decision"] = position is None
                    gate_allowed = verdict["allow"]
                if (
                    position is None
                    and candidates["winner"]
                    and gate_allowed
                    and after_start
                ):
                    pending = candidates["winner"], frame
                elif position and decimal(frame["current"]["atr"]) > 0:
                    position["trailing_stop"] = decimal(
                        trailing_stop(
                            side=position["side"],
                            prior_stop=position["trailing_stop"],
                            high_water=position["high_water"],
                            low_water=position["low_water"],
                            atr=frame["current"]["atr"],
                            multiplier=p["trail_atr"],
                        )
                    )
            unrealized = (
                position["direction"]
                * position["quantity"]
                * (bar.c - position["entry"])
                if position
                else Decimal(0)
            )
            equity = cash + unrealized
            high_equity = max(high_equity, equity)
            max_drawdown = max(max_drawdown, (high_equity - equity) / high_equity)
            curve.append(
                {
                    "closed_at_ms": bar.t + width,
                    "cash": encode(cash),
                    "equity": encode(equity),
                    "unrealized": encode(unrealized),
                }
            )
        return {
            "version": "quant-next-bar-replay-v1",
            "scope": "SINGLE_SYMBOL_SIMULATION_NOT_VENUE_ACCEPTANCE",
            "assumptions": {
                "signal": "CLOSED_BAR_NEXT_OPEN",
                "funding": "SUPPLIED_BOUNDARIES_BEFORE_EXECUTIONS_COMPLETENESS_NOT_ASSERTED",
                "costs": "CONFIGURED_NOT_OBSERVED",
                "exits": "RESEARCH_ATR_TARGET_TIMEOUT_NOT_FULL_PRODUCTION_EXIT_POLICY",
                "intrabar": "STOP_FIRST_IF_AMBIGUOUS",
                "historical_contract_eligibility": "CALLER_MUST_VALIDATE",
            },
            "strategy_id": plugin,
            "policy": asdict(policy),
            "parameters": p,
            "trades": trades,
            "decisions": decisions,
            "entry_attempts": entry_attempts,
            "entries": entries,
            "curve": curve,
            "open_position": None
            if position is None
            else {
                k: encode(v) if isinstance(v, Decimal) else v
                for k, v in position.items()
            },
            "pending_signal": pending is not None,
            "summary": {
                "closed_trades": len(trades),
                "wins": sum(decimal(t["net_pnl"]) > 0 for t in trades),
                "cash": encode(cash),
                "equity": curve[-1]["equity"],
                "realized_net_pnl": encode(
                    sum((decimal(t["net_pnl"]) for t in trades), Decimal(0))
                ),
                "fees": encode(fees_total),
                "funding": encode(funding_total),
                "max_drawdown_fraction": encode(max_drawdown),
                "entry_rejections": dict(
                    Counter(a["reason"] for a in entry_attempts if a["reason"])
                ),
            },
        }
