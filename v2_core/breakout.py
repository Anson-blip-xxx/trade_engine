"""Pure failed-breakout transitions, decimal prices and source observation time."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from v2_core.ingress import milliseconds


def price(value):
    if isinstance(value, bool):
        raise TypeError("positive finite price required")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid price") from exc
    if not result.is_finite() or result <= 0 or result > Decimal("1e30"):
        raise ValueError("positive bounded finite price required")
    return result


def advance_breakout(previous, *, symbol, raw_4h, raw_15m, observed_at):
    milliseconds(observed_at)
    state, events = deepcopy(previous or {"state": "IDLE"}), []
    if len(raw_4h) < 2 or len(raw_15m) < 3:
        return state, events
    for bar in [*raw_4h, *raw_15m]:
        high, low, close = (price(bar[k]) for k in ("h", "l", "c"))
        if not low <= close <= high:
            raise ValueError("inconsistent OHLC bounds")
        if "t" in bar and milliseconds(bar["t"]) > observed_at:
            raise ValueError("future candle")
    prior_high = max(price(bar["h"]) for bar in raw_4h[1:])
    prior_low = min(price(bar["l"]) for bar in raw_4h[1:])
    high = max(price(bar["h"]) for bar in raw_15m)
    low = min(price(bar["l"]) for bar in raw_15m)
    close = price(raw_15m[0]["c"])
    current = state["state"]
    if current not in ("IDLE", "BREAKING_HIGH", "BREAKING_LOW"):
        raise ValueError("unknown breakout state")
    if current != "IDLE":
        started = milliseconds(state["started_at"])
        if observed_at < started:
            raise ValueError("breakout observation regressed")
        if observed_at - started > 27000000:
            return {"state": "IDLE"}, events
    if current == "IDLE":
        direction = (
            "HIGH"
            if high > prior_high * Decimal("1.008")
            else "LOW"
            if low < prior_low * Decimal("0.992")
            else None
        )
        if direction:
            state = {
                "state": "BREAKING_" + direction,
                "breakout_high": str(high),
                "breakout_low": str(low),
                "started_at": observed_at,
            }
    else:
        upward = current == "BREAKING_HIGH"
        extreme = price(state["breakout_high" if upward else "breakout_low"])
        rejected = (
            close < extreme * Decimal("0.998")
            if upward
            else close > extreme * Decimal("1.002")
        )
        continued = (
            high > extreme * Decimal("1.002")
            if upward
            else low < extreme * Decimal("0.998")
        )
        if rejected:
            reference = prior_high if upward else prior_low
            distance = (extreme - reference) if upward else (reference - extreme)
            events.append(
                {
                    "symbol": symbol,
                    "type": "FAILED_BREAKOUT",
                    "direction": "HIGH" if upward else "LOW",
                    "strength": max(0, min(99, int(distance / reference * 1000 + 20))),
                    "breakout_high" if upward else "breakdown_low": str(reference),
                    "rejected_from": str(extreme),
                }
            )
        if rejected or continued:
            state = {"state": "IDLE"}
    return state, events
