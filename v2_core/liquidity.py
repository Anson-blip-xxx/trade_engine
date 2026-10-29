"""Pure opening execution-quality checks against a bounded LIVE depth snapshot.

This is not an alpha signal or a probability of execution. Decisions use exact
rationals; only displayed ratios are rounded, independently of Decimal context.
The caller remains responsible for all other opening checks and order dispatch.
"""

import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction

from v2_core.evidence import canonical, digest
from v2_core.precision import amount

VERSION = "liquidity-gate-v1"
EVIDENCE_VERSION = "binance-depth-v1"
_NUMERIC = re.compile(r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_SYMBOL = re.compile(r"[A-Z0-9]{1,32}USDT")
_LIMITATIONS = (
    "Snapshot depth is not a fill guarantee; displayed orders can change before execution.",
    "No order flow, cancellation rate, historical baseline, alpha or probability is inferred.",
    "LIVE depth does not establish executable TESTNET liquidity.",
)
_DEFAULTS = {
    "max_age_ms": 5000,
    "max_spread_bps": "20",
    "max_impact_bps": "15",
    "max_price_basis_bps": "100",
}
_SNAPSHOT_FIELDS = (
    "version",
    "symbol",
    "source_environment",
    "source_host",
    "observed_at_ms",
    "received_at_ms",
    "last_update_id",
    "bids",
    "asks",
)


def _number(value, *, positive=True):
    if not isinstance(value, str) or not _NUMERIC.fullmatch(value):
        raise ValueError("decimal string required")
    result = Fraction(amount(value, positive=positive))
    if result < 0:
        raise ValueError("nonnegative decimal required")
    return result


def _text(value):
    """Decimal strings for audit output; comparisons never use this rounding."""
    value = Fraction(value)
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        rendered = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _levels(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("depth bound")
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("depth row")
        parsed.append((_number(row[0]), _number(row[1])))
    return parsed


def evaluate_liquidity(
    *, evidence, controls, symbol, side, quantity, reference_price, now_ms
):
    """Return a JSON-safe decision, without I/O, clock reads or input mutation.

    ``authorized`` grants only this individual gate, and only in ENFORCE mode.
    SHADOW always has ``authorized == blocked == False``; ``would_block`` records
    its counterfactual. OFF does not inspect evidence or execution parameters.
    Missing or malformed evidence can never pass an active gate.
    """
    mode = controls.get("mode", "OFF") if isinstance(controls, dict) else "INVALID"
    if not isinstance(mode, str):
        mode = "INVALID"
    report = {
        "version": VERSION,
        "mode": mode,
        "reason": None,
        "passed": False,
        "authorized": False,
        "would_block": False,
        "blocked": False,
        "observed": {},
        "evidence_digest": None,
        "missing": [],
        "limitations": list(_LIMITATIONS),
    }

    def result(reason, *, missing=()):
        passed = reason == "LIQUIDITY_PASSED"
        active_failure = reason != "LIQUIDITY_DISABLED" and not passed
        report.update(
            reason=reason,
            passed=passed,
            authorized=mode == "ENFORCE" and passed,
            would_block=active_failure,
            blocked=active_failure and mode != "SHADOW",
            missing=list(missing),
        )
        return report

    if mode == "OFF":
        return result("LIQUIDITY_DISABLED")
    if mode not in {"SHADOW", "ENFORCE"}:
        return result("LIQUIDITY_CONTROLS_INVALID")
    values = {**_DEFAULTS, **controls}
    max_age = values["max_age_ms"]
    if type(max_age) is not int or not 0 <= max_age <= 2**53 - 1:
        return result("LIQUIDITY_CONTROLS_INVALID")
    try:
        max_spread, max_impact, max_basis = (
            _number(values[field], positive=False)
            for field in (
                "max_spread_bps",
                "max_impact_bps",
                "max_price_basis_bps",
            )
        )
    except (ValueError, TypeError):
        return result("LIQUIDITY_CONTROLS_INVALID")
    if (
        not isinstance(symbol, str)
        or not _SYMBOL.fullmatch(symbol)
        or side not in ("BUY", "SELL")
        or type(now_ms) is not int
        or not 0 <= now_ms <= 2**53 - 1
    ):
        return result("LIQUIDITY_REQUEST_INVALID")
    try:
        qty, reference = _number(quantity), _number(reference_price)
    except (ValueError, TypeError):
        return result("LIQUIDITY_REQUEST_INVALID")
    observed = report["observed"]
    observed.update(
        symbol=symbol,
        side=side,
        quantity=_text(qty),
        reference_price=_text(reference),
        evaluated_at_ms=str(now_ms),
    )
    if evidence is None:
        return result("LIQUIDITY_EVIDENCE_MISSING", missing=("order_book",))
    if not isinstance(evidence, dict):
        return result("LIQUIDITY_EVIDENCE_INVALID", missing=("order_book",))
    if evidence.get("version") != EVIDENCE_VERSION:
        return result("LIQUIDITY_EVIDENCE_VERSION_INVALID")
    if evidence.get("status") == "UNAVAILABLE":
        reason = evidence.get("reason")
        safe_reason = (
            reason
            if isinstance(reason, str) and _CODE.fullmatch(reason)
            else "UNSPECIFIED"
        )
        observed["unavailable_reason"] = safe_reason
        report["evidence_digest"] = digest(
            canonical(
                {
                    "version": EVIDENCE_VERSION,
                    "status": "UNAVAILABLE",
                    "reason": safe_reason,
                }
            )
        )
        return result("LIQUIDITY_UNAVAILABLE", missing=("order_book",))
    missing = [field for field in _SNAPSHOT_FIELDS if field not in evidence]
    if missing:
        return result("LIQUIDITY_EVIDENCE_MISSING", missing=missing)
    if evidence.get("status") not in (None, "AVAILABLE"):
        return result("LIQUIDITY_EVIDENCE_INVALID")
    if evidence["symbol"] != symbol:
        return result("LIQUIDITY_SYMBOL_MISMATCH")
    if evidence["source_environment"] != "LIVE":
        return result("LIQUIDITY_SOURCE_ENVIRONMENT_INVALID")
    if evidence["source_host"] != "fapi.binance.com":
        return result("LIQUIDITY_SOURCE_HOST_INVALID")
    observed_ms, received_ms = (
        evidence["observed_at_ms"],
        evidence["received_at_ms"],
    )
    if any(
        type(t) is not int or not 0 <= t <= 2**53 - 1
        for t in (observed_ms, received_ms)
    ):
        return result("LIQUIDITY_TIMESTAMP_INVALID")
    if observed_ms > now_ms or received_ms > now_ms:
        return result("LIQUIDITY_FUTURE_EVIDENCE")
    if observed_ms > received_ms:
        return result("LIQUIDITY_TIMESTAMP_ORDER_INVALID")
    update_id = evidence["last_update_id"]
    if type(update_id) is not int or not 1 <= update_id <= 2**63 - 1:
        return result("LIQUIDITY_UPDATE_ID_INVALID")
    observed.update(
        source_environment="LIVE",
        source_host="fapi.binance.com",
        observed_at_ms=str(observed_ms),
        received_at_ms=str(received_ms),
        last_update_id=str(update_id),
        age_ms=str(now_ms - observed_ms),
    )
    try:
        bids, asks = _levels(evidence["bids"]), _levels(evidence["asks"])
    except (ValueError, TypeError):
        return result("LIQUIDITY_DEPTH_INVALID")
    if any(bids[i - 1][0] <= bids[i][0] for i in range(1, len(bids))) or any(
        asks[i - 1][0] >= asks[i][0] for i in range(1, len(asks))
    ):
        return result("LIQUIDITY_DEPTH_UNSORTED")
    report["evidence_digest"] = digest(
        canonical({field: evidence[field] for field in _SNAPSHOT_FIELDS})
    )
    observed.update(
        bid_levels=str(len(bids)),
        ask_levels=str(len(asks)),
        best_bid=_text(bids[0][0]),
        best_ask=_text(asks[0][0]),
    )
    if bids[0][0] >= asks[0][0]:
        return result("LIQUIDITY_BOOK_CROSSED")
    if now_ms - observed_ms >= max_age:
        return result("LIQUIDITY_STALE", missing=("fresh_order_book",))
    mid = (bids[0][0] + asks[0][0]) / 2
    spread = (asks[0][0] - bids[0][0]) * 10000 / mid
    basis = abs(reference - mid) * 10000 / mid
    levels = asks if side == "BUY" else bids
    best = levels[0][0]
    available = sum((size for _, size in levels), Fraction(0))
    observed.update(
        mid_price=_text(mid),
        spread_bps=_text(spread),
        price_basis_bps=_text(basis),
        side_best_price=_text(best),
        available_quantity=_text(available),
    )
    if available < qty:
        observed["quantity_shortfall"] = _text(qty - available)
        return result("LIQUIDITY_INSUFFICIENT_DEPTH", missing=("executable_depth",))
    remaining, notional = qty, Fraction(0)
    worst, consumed = best, 0
    for price, size in levels:
        taken = min(remaining, size)
        notional += price * taken
        remaining -= taken
        worst = price
        consumed += 1
        if remaining == 0:
            break
    vwap = notional / qty
    sign = 1 if side == "BUY" else -1
    vwap_impact = sign * (vwap - best) * 10000 / best
    worst_impact = sign * (worst - best) * 10000 / best
    observed.update(
        consumed_levels=str(consumed),
        executable_notional=_text(notional),
        vwap_price=_text(vwap),
        worst_price=_text(worst),
        vwap_impact_bps=_text(vwap_impact),
        worst_impact_bps=_text(worst_impact),
    )
    if spread > max_spread:
        return result("LIQUIDITY_SPREAD_EXCEEDED")
    if worst_impact > max_impact:
        return result("LIQUIDITY_IMPACT_EXCEEDED")
    if basis > max_basis:
        return result("LIQUIDITY_PRICE_BASIS_EXCEEDED")
    return result("LIQUIDITY_PASSED")


def evaluate_execution_quote(*, evidence, controls, symbol, now_ms):
    """Check the existing SANDBOX BBO independently of LIVE depth.

    ``spread_fraction`` is (ask - bid) / bid; compare its mid-price equivalent
    with the common spread limit. A BBO summary contains no multi-level depth
    or side/quantity execution proof. ``top_notional`` and ``tiers`` are unused.
    """
    mode = controls.get("mode", "OFF") if isinstance(controls, dict) else "INVALID"
    if not isinstance(mode, str):
        mode = "INVALID"
    report = {
        "version": VERSION,
        "mode": mode,
        "reason": None,
        "passed": False,
        "authorized": False,
        "would_block": False,
        "blocked": False,
        "observed": {},
        "evidence_digest": None,
        "missing": [],
        "limitations": [
            "SANDBOX BBO is not a fill guarantee; quotes can change before execution.",
            "A BBO summary does not prove multi-level depth or side/quantity execution capacity.",
            "LIVE depth does not establish executable TESTNET liquidity.",
        ],
    }

    def result(reason, *, missing=()):
        passed = reason == "LIQUIDITY_EXECUTION_PASSED"
        active_failure = reason != "LIQUIDITY_EXECUTION_DISABLED" and not passed
        report.update(
            reason=reason,
            passed=passed,
            authorized=mode == "ENFORCE" and passed,
            would_block=active_failure,
            blocked=active_failure and mode != "SHADOW",
            missing=list(missing),
        )
        return report

    if mode == "OFF":
        return result("LIQUIDITY_EXECUTION_DISABLED")
    if mode not in {"SHADOW", "ENFORCE"}:
        return result("LIQUIDITY_EXECUTION_CONTROLS_INVALID")
    max_age = controls.get("max_execution_quote_age_ms", 30000)
    if type(max_age) is not int or not 0 <= max_age <= 2**53 - 1:
        return result("LIQUIDITY_EXECUTION_CONTROLS_INVALID")
    try:
        max_spread = _number(controls.get("max_spread_bps", "20"), positive=False)
    except (ValueError, TypeError):
        return result("LIQUIDITY_EXECUTION_CONTROLS_INVALID")
    if (
        not isinstance(symbol, str)
        or not _SYMBOL.fullmatch(symbol)
        or type(now_ms) is not int
        or not 0 <= now_ms <= 2**53 - 1
    ):
        return result("LIQUIDITY_EXECUTION_REQUEST_INVALID")
    observed = report["observed"]
    observed.update(symbol=symbol, evaluated_at_ms=str(now_ms))
    if evidence is None:
        return result(
            "LIQUIDITY_EXECUTION_EVIDENCE_MISSING", missing=("execution_quote",)
        )
    if not isinstance(evidence, dict):
        return result(
            "LIQUIDITY_EXECUTION_EVIDENCE_INVALID", missing=("execution_quote",)
        )
    fields = (
        "symbol",
        "environment",
        "source_host",
        "book_at_ms",
        "spread_fraction",
    )
    missing = [field for field in fields if field not in evidence]
    if missing:
        return result("LIQUIDITY_EXECUTION_EVIDENCE_MISSING", missing=missing)
    if evidence["symbol"] != symbol:
        return result("LIQUIDITY_EXECUTION_SYMBOL_MISMATCH")
    if evidence["environment"] != "SANDBOX":
        return result("LIQUIDITY_EXECUTION_SOURCE_ENVIRONMENT_INVALID")
    if evidence["source_host"] != "demo-fapi.binance.com":
        return result("LIQUIDITY_EXECUTION_SOURCE_HOST_INVALID")
    stamp = evidence["book_at_ms"]
    if type(stamp) is not int or not 0 <= stamp <= 2**53 - 1:
        return result("LIQUIDITY_EXECUTION_TIMESTAMP_INVALID")
    if stamp > now_ms:
        return result("LIQUIDITY_EXECUTION_FUTURE_EVIDENCE")
    observed.update(
        source_environment="SANDBOX",
        source_host="demo-fapi.binance.com",
        book_at_ms=str(stamp),
        age_ms=str(now_ms - stamp),
    )
    try:
        spread_fraction = _number(evidence["spread_fraction"], positive=False)
    except (ValueError, TypeError):
        return result("LIQUIDITY_EXECUTION_SPREAD_INVALID")
    report["evidence_digest"] = digest(
        canonical({field: evidence[field] for field in fields})
    )
    spread_bps = spread_fraction / (1 + spread_fraction / 2) * 10000
    observed.update(
        spread_fraction=_text(spread_fraction),
        spread_bps=_text(spread_bps),
    )
    if now_ms - stamp >= max_age:
        return result("LIQUIDITY_EXECUTION_STALE", missing=("fresh_execution_quote",))
    if spread_bps > max_spread:
        return result("LIQUIDITY_EXECUTION_SPREAD_EXCEEDED")
    return result("LIQUIDITY_EXECUTION_PASSED")
