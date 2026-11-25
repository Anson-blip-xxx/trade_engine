"""Exact venue cash facts, independent of a guessed venue rounding algorithm.

This pure analyzer neither verifies income/wallet data nor authorizes settlement.
It retains theoretical economics and every reported cash difference separately.
An explicit anomaly policy classifies discrepancies; it never rewrites facts or
turns agreement between two venue endpoints into independent economic proof.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from v2_core.precision import amount
from v2_core.strategy_lifecycle import require_cash_subject

MODEL = "venue-cash-facts-v1"
_FINAL = {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}


def _fraction_text(value):
    """Return an exact finite decimal when possible, without Decimal arithmetic."""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return None
    places = max(twos, fives)
    integer = abs(numerator) * 2 ** (places - twos) * 5 ** (places - fives)
    digits = str(integer).zfill(places + 1)
    if places:
        digits = (digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".")
    return ("-" if numerator < 0 else "") + digits


def _rational(value):
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
        "decimal": _fraction_text(value),
    }


def _derived(value):
    """Derived economics may exceed NUMERIC(38,18); never round them to fit."""
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("INVALID_DERIVED_CASH_AMOUNT")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_DERIVED_CASH_AMOUNT") from exc
    if not result.is_finite() or not -100 <= result.as_tuple().exponent <= 100:
        raise ValueError("INVALID_DERIVED_CASH_AMOUNT")
    return Fraction(result)


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and value == value.strip()


@dataclass(frozen=True)
class CashDifferencePolicy:
    """Explicit review limits, not tolerances that modify or erase cash values.

    Both limits are mandatory. The sum of absolute per-fill differences prevents
    opposite errors from cancelling into an apparently safe net difference.
    No numerical defaults or trading-environment policy is selected here.
    """

    version: str
    max_abs_per_fill: str
    max_sum_abs_episode: str

    def __post_init__(self):
        if not _identity(self.version):
            raise ValueError("INVALID_CASH_DIFFERENCE_POLICY")
        if amount(self.max_abs_per_fill) < 0 or amount(self.max_sum_abs_episode) < 0:
            raise ValueError("INVALID_CASH_DIFFERENCE_POLICY")


def analyze_venue_cash(trace, report, *, policy=None):
    """Analyze a closed, single-opening linear Testnet episode exactly.

    Inputs are the bound immutable journal trace and its current ledger report.
    Income identity/completeness, funding ownership, account coverage, wallet
    equality and a final atomic ledger revision fence remain caller obligations.
    A review-required result must not be interpreted as settlement permission.
    """
    if policy is not None and type(policy) is not CashDifferencePolicy:
        raise ValueError("EXPLICIT_CASH_DIFFERENCE_POLICY_REQUIRED")
    scope, request = trace["scope"], trace["request"]
    try:
        require_cash_subject(trace)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("UNSUPPORTED_VENUE_CASH_SCOPE") from None
    if (
        (scope.get("exchange"), scope.get("environment"), scope.get("product"))
        != ("BINANCE", "SANDBOX", "FUTURES")
        or not _identity(scope.get("account_id"))
        or not _identity(request.get("symbol"))
        or not request["symbol"].endswith("USDT")
        or request.get("side") not in {"BUY", "SELL"}
    ):
        raise ValueError("UNSUPPORTED_VENUE_CASH_SCOPE")
    if (
        not _identity(trace.get("intent_id"))
        or report.get("intent_id") != trace["intent_id"]
        or report.get("settlement_currency") != "USDT"
        or report.get("missing_valuations") != []
    ):
        raise ValueError("VENUE_CASH_REPORT_MISMATCH")
    orders = {}
    for order in trace["orders"]:
        oid = order["order_id"]
        if (
            not _identity(oid)
            or oid in orders
            or order.get("leg") not in {"OPEN", "CLOSE"}
            or order.get("status") not in _FINAL
        ):
            raise ValueError("INVALID_VENUE_CASH_ORDER")
        orders[oid] = order
    if sum(order["leg"] == "OPEN" for order in orders.values()) != 1:
        raise ValueError("UNSUPPORTED_VENUE_CASH_TIMELINE")

    opening, closing, seen, totals = [], [], set(), {}
    for fill in trace["fills"]:
        oid, proof, timestamp = (
            fill["order_id"],
            fill["evidence"],
            fill["occurred_at_ms"],
        )
        tid = proof.get("trade_id")
        if (
            oid not in orders
            or proof.get("source") != "binance-userTrades"
            or not _identity(tid)
            or tid in seen
            or type(timestamp) is not int
            or timestamp < 0
            or fill.get("fee_currency") != "USDT"
        ):
            raise ValueError("INVALID_VENUE_CASH_FILL")
        seen.add(tid)
        quantity = Fraction(amount(fill["quantity"], positive=True))
        price = Fraction(amount(fill["price"], positive=True))
        actual = Fraction(amount(proof["venue_realized_pnl"]))
        amount(fill["fee"])
        totals[oid] = totals.get(oid, Fraction(0)) + quantity
        item = (timestamp, tid, oid, quantity, price, actual)
        (opening if orders[oid]["leg"] == "OPEN" else closing).append(item)
    if (
        not opening
        or not closing
        or max(f[0] for f in opening) >= min(f[0] for f in closing)
    ):
        raise ValueError("UNSUPPORTED_VENUE_CASH_TIMELINE")
    for oid, order in orders.items():
        filled = totals.get(oid, Fraction(0))
        requested = Fraction(amount(order["quantity"], positive=True))
        if (
            filled > requested
            or (order["status"] == "FILLED" and filled != requested)
            or (order["status"] == "REJECTED" and filled)
        ):
            raise ValueError("VENUE_CASH_FILL_QUANTITY_MISMATCH")
    if any(fill[5] for fill in opening):
        raise ValueError("OPEN_FILL_REALIZED_PNL_NONZERO")
    quantity = sum((fill[3] for fill in opening), Fraction(0))
    if (
        quantity != sum((fill[3] for fill in closing), Fraction(0))
        or quantity != Fraction(amount(report["opened_quantity"], positive=True))
        or quantity != Fraction(amount(report["closed_quantity"], positive=True))
    ):
        raise ValueError("EXPOSURE_NOT_CLOSED")
    entry = sum((fill[3] * fill[4] for fill in opening), Fraction(0)) / quantity
    sign = 1 if request["side"] == "BUY" else -1
    theoretical = actual = sum_abs = max_abs = Fraction(0)
    proofs = []
    for timestamp, tid, oid, qty, price, observed in sorted(closing):
        expected = qty * (price - entry) * sign
        delta = observed - expected
        theoretical += expected
        actual += observed
        sum_abs += abs(delta)
        max_abs = max(max_abs, abs(delta))
        proofs.append(
            {
                "order_id": oid,
                "trade_id": tid,
                "occurred_at_ms": timestamp,
                "theoretical_pnl": _rational(expected),
                "actual_pnl": _fraction_text(observed),
                "cash_difference": _rational(delta),
                "classification": "EXACT" if delta == 0 else "REPORTED_CASH_DIFFERENCE",
            }
        )
    ledger_gross = report.get("theoretical_gross_pnl", report["gross_pnl"])
    if theoretical != _derived(ledger_gross):
        raise ValueError("VENUE_LEDGER_ECONOMICS_MISMATCH")
    reasons = []
    if sum_abs and policy is None:
        reasons.append("CASH_DIFFERENCE_POLICY_REQUIRED")
    elif policy is not None:
        if max_abs > Fraction(amount(policy.max_abs_per_fill)):
            reasons.append("PER_FILL_CASH_DIFFERENCE_LIMIT")
        if sum_abs > Fraction(amount(policy.max_sum_abs_episode)):
            reasons.append("EPISODE_ABSOLUTE_CASH_DIFFERENCE_LIMIT")
    return {
        "model": MODEL,
        "intent_id": trace["intent_id"],
        "actual_gross_pnl": _fraction_text(actual),
        "theoretical_gross_pnl": _fraction_text(theoretical),
        "cash_difference": _rational(actual - theoretical),
        "opening_average_price": _rational(entry),
        "fills": proofs,
        "difference_summary": {
            "absolute_sum": _rational(sum_abs),
            "absolute_max": _rational(max_abs),
            "mismatch_count": sum(p["classification"] != "EXACT" for p in proofs),
        },
        "anomaly": {
            "status": "REVIEW_REQUIRED"
            if reasons
            else "EXACT"
            if not sum_abs
            else "WITHIN_POLICY",
            "reasons": reasons,
            "policy": None if policy is None else asdict(policy),
        },
        "algorithm_verified": False,
        "income_verified": False,
        "wallet_verified": False,
        "requires_income_match": True,
        "requires_final_wallet_match": True,
        "settlement_authorized": False,
    }
