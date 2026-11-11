"""Bounded causal breakout observations, without I/O or execution authority.

The caller validates/binds the complete server-produced structure first. The
only actionable condition is a fully erased confirming candle body. Partial
retracement, large prior returns and EMA extension are observations for a
prospective study, never an exhaustion forecast or a reason to change sizing.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from v2_core.precision import amount, decimal_text, derived_amount

VERSION = "trend-entry-quality-v1"


def _encoded(value):
    return decimal_text(
        derived_amount(value, rounding=ROUND_HALF_EVEN, preserve_nonzero=True)
    )


def observe_entry_quality(
    *,
    structure,
    side,
    price,
    bound_price,
    price_observed_at_ms,
    evaluated_at_ms,
    market,
    enforce,
):
    """Observe fixed-size fields from an already bound, closed market snapshot.

    Price must be the latest closed-minute close from the same source envelope,
    not an unbound signal price. Future or missing inputs return UNKNOWN; the
    caller decides whether that is an opening rejection or shadow-only result.
    This checks a single candidate, never holds a queue or suppresses exits.
    """
    proof = {
        "version": VERSION,
        "mode": "ENFORCE_BODY_INVALIDATION" if enforce else "SHADOW",
        "status": "UNKNOWN",
        "reason": "ENTRY_QUALITY_INPUT_UNCONFIRMED",
        "execution_authorized": False,
        "side": side,
        "interpretation": "OBSERVATIONS_NOT_EXHAUSTION_OR_WIN_PROBABILITY",
        "partial_retracement_action": "OBSERVE_ONLY",
        "prior_return_action": "OBSERVE_ONLY",
    }
    try:
        closed = structure["last_closed_at_ms"]
        if (
            side not in {"LONG", "SHORT"}
            or any(
                type(t) is not int
                for t in (closed, price_observed_at_ms, evaluated_at_ms)
            )
            or not 0 <= closed <= price_observed_at_ms <= evaluated_at_ms
            or price_observed_at_ms % 60000
        ):
            return proof
        px = amount(price, positive=True)
        if px != amount(bound_price, positive=True):
            return proof
        values = structure["values"]
        sign = Decimal(1 if side == "LONG" else -1)
        suffix = "long" if side == "LONG" else "short"
        with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
            opened, close, atr = (
                amount(values[key], positive=True) for key in ("open", "close", "atr")
            )
            if price_observed_at_ms == closed and px != close:
                # At the exact 15m boundary these are the very same closing
                # observation. Different prices cannot be later information.
                return proof
            body = sign * (close - opened)
            anchor = amount(
                values["prior20_high" if side == "LONG" else "prior20_low"],
                positive=True,
            )
            body_erased = (
                values["breakout_" + suffix] is True
                and body > 0
                and sign * (px - opened) <= 0
            )
            high, low = (amount(values[key], positive=True) for key in ("high", "low"))
            adverse = max(Decimal(0), sign * (close - px))
            observations = {
                "confirming_open": _encoded(opened),
                "confirming_close": _encoded(close),
                "observed_price": _encoded(px),
                "breakout_anchor": _encoded(anchor),
                "body_atr": _encoded(body / atr),
                "retained_body_atr": _encoded(sign * (px - opened) / atr),
                "breakout_acceptance_atr": _encoded(sign * (px - anchor) / atr),
                "adverse_retrace_atr": _encoded(adverse / atr),
                "body_retrace_fraction": _encoded(adverse / body) if body > 0 else None,
                "opposing_wick_fraction": _encoded(
                    (high - close if side == "LONG" else close - low) / (high - low)
                )
                if high > low
                else None,
                "ema20_extension_atr": _encoded(
                    sign * (px - amount(values["ema20"])) / atr
                ),
                "ema60_extension_atr": _encoded(
                    sign * (px - amount(values["ema60"])) / atr
                ),
            }
            # Optional diagnostics cannot invalidate a structural observation.
            # Existing rolling-window chg is close-to-first-close percentage,
            # not a forward return. Preserve missing/invalid data as unknown.
            for window in ("1h", "4h", "24h"):
                try:
                    value = amount(market[window]["chg"])
                    observations["directional_change_" + window + "_pct"] = _encoded(
                        sign * value
                    )
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    observations["directional_change_" + window + "_pct"] = None
        proof.update(
            status="READY",
            reason="CONFIRMING_BODY_ERASED" if body_erased else "BODY_NOT_ERASED",
            structure_closed_at_ms=closed,
            price_observed_at_ms=price_observed_at_ms,
            evaluated_at_ms=evaluated_at_ms,
            confirming_body_erased=body_erased,
            observations=observations,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        # Deterministic unknown is local to this candidate. No external retries
        # and no fabricated neutral metrics for missing data.
        pass
    return proof
