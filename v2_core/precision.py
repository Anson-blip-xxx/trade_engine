"""Financial numeric boundaries, independent of the caller's Decimal context.

Facts are never rounded. Derived decision/analysis values require an explicit
rounding mode; cash attribution uses exact rational arithmetic separately.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

STORAGE_QUANTUM = Decimal("1e-18")


def money_context():
    return localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN))


def decimal_text(value):
    """Format without normalize(), which silently uses ambient precision."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("FINITE_DECIMAL_REQUIRED")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def amount(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("amount requires a bounded decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if (
        not result.is_finite()
        or result.copy_abs() >= Decimal("1e20")
        or result.as_tuple().exponent < -18
        or (positive and result <= 0)
    ):
        raise ValueError("amount outside exact NUMERIC(38,18) domain")
    return result


def derived_amount(value, *, rounding, positive=False, preserve_nonzero=False):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("FINITE_DECIMAL_REQUIRED")
    if value.copy_abs() >= Decimal("1e20"):
        raise ValueError("DERIVED_AMOUNT_OUT_OF_RANGE")
    with money_context():
        bounded = value.quantize(STORAGE_QUANTUM, rounding=rounding)
    if preserve_nonzero and value != 0 and bounded == 0:
        raise ValueError("DERIVED_AMOUNT_BELOW_PRECISION")
    return amount(decimal_text(bounded), positive=positive)
