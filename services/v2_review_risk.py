"""Descriptive risk units from frozen sizing and actual fills; never forecasts.

USDT linear contracts only. No account equity reconstruction, model confidence,
execution authority, risk-of-ruin formula, or parameter updates.
"""

from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext


def _number(value, *, positive=False, derived=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TypeError("INVALID_NUMBER")
    # Bound malformed evidence before doing high precision arithmetic.
    if len(str(value)) > (128 if derived else 100):
        raise ValueError("INVALID_NUMBER")
    number = Decimal(value)
    bound = 80 if derived else 36
    if not number.is_finite() or (number and not -bound <= number.adjusted() <= bound):
        raise ValueError("INVALID_NUMBER")
    if positive and number <= 0:
        raise ValueError("INVALID_NUMBER")
    return number


def trade_risk(row):
    """R denominator uses actual entry quantity/price and the ORIGINAL stop.

    Planned risk is scaled to the filled quantity, so partial fills are not
    mistaken for improved performance. Costs are already in settled net PnL.
    Missing or contradictory evidence stays unknown; no stop is inferred from
    the eventual exit. The frozen sizing is a decision plan, not proof of the
    exchange's accepted protection price.
    """
    result = {
        "status": "MISSING_SIZING_EVIDENCE",
        "planned_risk_at_filled_quantity_usdt": None,
        "initial_stop_risk_usdt": None,
        "entry_risk_drift_pct": None,
        "realized_net_r": None,
        "net_loss_beyond_initial_risk_usdt": None,
        "planned_reward_risk": None,
    }
    sizing = row.get("sizing")
    if not isinstance(sizing, dict) or sizing.get("reason") != "SIZED":
        return result
    try:
        with localcontext() as ctx:
            ctx.prec = 100
            qty = _number(sizing.get("quantity"), positive=True)
            notional = _number(sizing.get("notional"), positive=True)
            stop = _number(sizing.get("stop_price"), positive=True)
            loss = _number(sizing.get("planned_loss"), positive=True)
            filled = _number(row.get("opening_quantity"), positive=True)
            actual_notional = _number(row.get("opening_notional"), positive=True)
            side = row.get("side")
            if side not in {"BUY", "LONG", "SELL", "SHORT"}:
                raise ValueError("SIDE_UNKNOWN")
            sign = 1 if side in {"BUY", "LONG"} else -1
            if sign * (notional - qty * stop) != loss:
                raise ValueError("INCONSISTENT_SIZING_EVIDENCE")
            risk = sign * (actual_notional - filled * stop)
            if risk <= 0:
                raise ValueError("STOP_NOT_ON_LOSS_SIDE_OF_FILL")
            planned = loss * filled / qty
            result.update(
                status="AVAILABLE",
                planned_risk_at_filled_quantity_usdt=str(planned),
                initial_stop_risk_usdt=str(risk),
                entry_risk_drift_pct=str((risk / planned - 1) * 100),
            )
            if row.get("net_pnl") is not None and not row.get("maintenance"):
                net = _number(row["net_pnl"])
                result.update(
                    realized_net_r=str(net / risk),
                    net_loss_beyond_initial_risk_usdt=str(max(Decimal(0), -net - risk)),
                )
    except (TypeError, ValueError, InvalidOperation) as exc:
        # Do not return a partially computed R if a later field is invalid.
        return {
            **dict.fromkeys(result),
            "status": str(exc)
            if isinstance(exc, (TypeError, ValueError))
            else "INVALID_NUMBER",
        }
    return result


def risk_statistics(trades):
    """Sample averages, not calibrated predictions or strategy enablement gates."""
    with localcontext() as ctx:
        ctx.prec = 100
        settled = [
            r
            for r in trades
            if r.get("net_pnl") is not None and not r.get("maintenance")
        ]
        values = [_number(r["net_pnl"]) for r in settled]
        wins, losses = [v for v in values if v > 0], [-v for v in values if v < 0]
        avg_win = sum(wins) / Decimal(len(wins)) if wins else None
        avg_loss = sum(losses) / Decimal(len(losses)) if losses else None
        risks = [r.get("risk") or trade_risk(r) for r in settled]
        multiples = [
            _number(r["realized_net_r"], derived=True)
            for r in risks
            if r["realized_net_r"] is not None
        ]
        return {
            "kind": "DESCRIPTIVE_NOT_FORECAST",
            "settled_count": len(values),
            "historical_mean_net_usdt": str(sum(values) / Decimal(len(values)))
            if values
            else None,
            "average_net_win_usdt": str(avg_win) if avg_win is not None else None,
            "average_net_loss_usdt": str(avg_loss) if avg_loss is not None else None,
            "realized_payoff_ratio": str(avg_win / avg_loss)
            if avg_win is not None and avg_loss is not None
            else None,
            "r_sample_count": len(multiples),
            "r_missing_count": len(values) - len(multiples),
            "r_missing_reasons": dict(
                Counter(r["status"] for r in risks if r["realized_net_r"] is None)
            ),
            "historical_mean_net_r": str(sum(multiples) / Decimal(len(multiples)))
            if multiples
            else None,
            "loss_beyond_initial_risk_count": sum(
                _number(r["net_loss_beyond_initial_risk_usdt"]) > 0
                for r in risks
                if r["realized_net_r"] is not None
            ),
            "forecast_expectancy": None,
            "ruin_probability": None,
        }
