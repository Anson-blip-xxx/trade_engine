"""Bounded read-only coverage research, separate from historical attribution.

The shared catalog is an overwrite-in-place *current* snapshot, not a listing
history. Absence in it cannot prove that a past signal was never generated, nor
invalidate an observed fill. Nothing here admits trades or changes policy.
"""

import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation

from v2_core.symbols import is_supported_symbol

VERSION = "mover-coverage-review-v1"
SNAPSHOT_VERSION = "mover-execution-catalog-snapshot-v1"
MAX_SYMBOLS = 20
_MAX_CLOCK = 2**53 - 1
_BASIS = "CURRENT_EXECUTION_CATALOG_NOT_HISTORICAL"
_ENVIRONMENTS = {"SANDBOX", "LIVE"}
_MARKETS = {"BINANCE_USDM_LIVE", "BINANCE_USDM_SANDBOX"}
_STATUSES = {
    "TRADING",
    "PENDING_TRADING",
    "PRE_DELIVERING",
    "DELIVERING",
    "DELIVERED",
    "PRE_SETTLE",
    "SETTLING",
    "CLOSE",
}
_REASONS = {
    "CATALOG_READ_UNAVAILABLE",
    "CATALOG_READ_PERMISSION_MISSING",
    "CATALOG_SCHEMA_UNAVAILABLE",
    "CATALOG_QUERY_TIMEOUT",
    "CATALOG_NOT_OBSERVED",
    "CATALOG_READ_ONLY_REQUIRED",
    "CATALOG_INPUT_INVALID",
    "CATALOG_DATA_INVALID",
    "CATALOG_STALE",
    "CATALOG_FROM_FUTURE",
    "CATALOG_ENVIRONMENT_MISMATCH",
    "CATALOG_SCOPE_MISMATCH",
    "REPORT_INPUT_INVALID",
}
_REJECTION_STAGES = {
    "JEV_VETO",
    "SIGNAL_PREFILTER_REJECTED",
    "MARKET_FILTER_REJECTED",
    "SIZING_RISK_REJECTED",
    "EXECUTION_MARKET_RISK_REJECTED",
    "OTHER_RECORDED_REJECTION",
}
_HISTORICAL_CATEGORIES = (
    "MATCHED_DIRECTION_FILLED",
    "RECORDED_REJECTION",
    "ONLY_OTHER_DIRECTION_OR_NON_ENTRY",
    "NO_RETAINED_SIGNAL_UNKNOWN",
    "ENTRY_CANDIDATE_NOT_FILLED",
    "UNKNOWN",
)

# Project only requested keys from the environment row. Never fetch contracts,
# filters, account scope, credentials, policy, or arbitrary nested JSON to Python.
_CATALOG_SQL = """
SELECT c.environment,c.observed_at_ms,jsonb_typeof(c.contracts),r.symbol,
       c.contracts ? r.symbol,
       left(c.contracts->r.symbol->>'symbol',80),
       left(c.contracts->r.symbol->>'status',40),
       CASE WHEN jsonb_typeof(c.contracts->r.symbol->'supported')='boolean'
            THEN (c.contracts->r.symbol->>'supported')::boolean ELSE NULL END,
       jsonb_typeof(c.contracts->r.symbol)
FROM (SELECT environment,observed_at_ms,contracts FROM v2_market_catalog
      WHERE environment=%s LIMIT 1) c
CROSS JOIN unnest(%s::text[]) WITH ORDINALITY r(symbol,ordinal)
ORDER BY r.ordinal
LIMIT 20
"""


def _clock(value):
    return type(value) is int and 0 <= value <= _MAX_CLOCK


def _oneof(value, choices):
    return isinstance(value, str) and value in choices


def _symbols(values):
    if (
        not isinstance(values, (list, tuple))
        or len(values) > MAX_SYMBOLS
        or any(not is_supported_symbol(value) for value in values)
        or len(set(values)) != len(values)
    ):
        return None
    return list(values)


def _parameters(environment, symbols, as_of_ms, max_age_ms):
    return (
        _oneof(environment, _ENVIRONMENTS)
        and _symbols(symbols) is not None
        and _clock(as_of_ms)
        and type(max_age_ms) is int
        and 0 <= max_age_ms <= 86_400_000
    )


def unavailable_snapshot(
    *,
    environment,
    symbols,
    as_of_ms,
    reason="CATALOG_READ_UNAVAILABLE",
    max_age_ms=900000,
):
    """Fixed-schema safe downgrade; no exception messages or raw inputs escape."""
    return {
        "version": SNAPSHOT_VERSION,
        "status": "UNKNOWN",
        "reason": reason if _oneof(reason, _REASONS) else "CATALOG_READ_UNAVAILABLE",
        "environment": environment if _oneof(environment, _ENVIRONMENTS) else "UNKNOWN",
        "as_of_ms": as_of_ms if _clock(as_of_ms) else None,
        "observed_at_ms": None,
        "max_age_ms": max_age_ms
        if type(max_age_ms) is int and 0 <= max_age_ms <= 86_400_000
        else None,
        "basis": _BASIS,
        "rows": [
            {"symbol": symbol, "present": None, "status": "UNKNOWN", "supported": None}
            for symbol in (_symbols(symbols) or [])
        ],
    }


def load_execution_catalog_snapshot(
    conn, *, environment, symbols, as_of_ms, max_age_ms=900000
):
    """Read <=20 exact symbols in the caller's read-only transaction.

    A nested transaction isolates missing grants/schema/timeouts. No commit,
    network I/O, mutation, environment fallback, or full-catalog read occurs.
    The caller supplies knowledge time; no system clock is consulted.
    """
    arguments = {
        "environment": environment,
        "symbols": symbols,
        "as_of_ms": as_of_ms,
        "max_age_ms": max_age_ms,
    }
    if not _parameters(**arguments):
        return unavailable_snapshot(**arguments, reason="CATALOG_INPUT_INVALID")
    if not symbols:
        return unavailable_snapshot(**arguments, reason="CATALOG_NOT_OBSERVED")
    try:
        with conn.transaction():
            if conn.execute("SHOW transaction_read_only").fetchone() != ("on",):
                return unavailable_snapshot(
                    **arguments, reason="CATALOG_READ_ONLY_REQUIRED"
                )
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            raw = conn.execute(_CATALOG_SQL, (environment, list(symbols))).fetchall()
            if not raw:
                return unavailable_snapshot(**arguments, reason="CATALOG_NOT_OBSERVED")
            if len(raw) != len(symbols):
                return unavailable_snapshot(**arguments, reason="CATALOG_DATA_INVALID")
            observed = raw[0][1]
            if not _clock(observed):
                reason = "CATALOG_DATA_INVALID"
            elif observed > as_of_ms:
                reason = "CATALOG_FROM_FUTURE"
            elif as_of_ms - observed > max_age_ms:
                reason = "CATALOG_STALE"
            else:
                reason = None
            if reason:
                return unavailable_snapshot(**arguments, reason=reason)
            rows = []
            for symbol, record in zip(symbols, raw, strict=True):
                env, stamp, root_type, name, present, exact, status, supported, kind = (
                    record
                )
                if env != environment:
                    return unavailable_snapshot(
                        **arguments, reason="CATALOG_ENVIRONMENT_MISMATCH"
                    )
                if (
                    stamp != observed
                    or type(stamp) is not int
                    or root_type != "object"
                    or name != symbol
                    or type(present) is not bool
                    or (
                        present
                        and (
                            kind != "object"
                            or exact != symbol
                            or not _oneof(status, _STATUSES)
                            or type(supported) is not bool
                        )
                    )
                    or (
                        not present
                        and any(v is not None for v in (exact, status, supported, kind))
                    )
                ):
                    return unavailable_snapshot(
                        **arguments, reason="CATALOG_DATA_INVALID"
                    )
                rows.append(
                    {
                        "symbol": symbol,
                        "present": present,
                        "status": status if present else "ABSENT_FROM_CURRENT_CATALOG",
                        "supported": supported if present else None,
                    }
                )
            return {
                **unavailable_snapshot(**arguments),
                "status": "OBSERVED",
                "reason": "CATALOG_OBSERVED",
                "observed_at_ms": observed,
                "rows": rows,
            }
    except Exception as exc:  # noqa: BLE001 - optional research must never block trading
        reason = {
            "42501": "CATALOG_READ_PERMISSION_MISSING",
            "42P01": "CATALOG_SCHEMA_UNAVAILABLE",
            "57014": "CATALOG_QUERY_TIMEOUT",
        }.get(getattr(exc, "sqlstate", None), "CATALOG_READ_UNAVAILABLE")
        return unavailable_snapshot(**arguments, reason=reason)


def _fill(trade, side):
    if not isinstance(trade, dict) or trade.get("side") != side:
        return False
    quantity = trade.get("opening_quantity")
    if type(quantity) not in (str, int, Decimal) or len(str(quantity)) > 100:
        return False
    try:
        quantity = Decimal(quantity)
        return quantity.is_finite() and quantity > 0
    except (InvalidOperation, ValueError):
        return False


def _history(row):
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return "UNKNOWN"
    primary = evidence.get("primary_reason")
    trades = evidence.get("trades", [])
    if (
        isinstance(trades, list)
        and len(trades) <= 500
        and any(_fill(trade, row["direction"]) for trade in trades)
    ) or primary == "MATCHED_DIRECTION_FILLED":
        return "MATCHED_DIRECTION_FILLED"
    if _oneof(primary, {"OTHER_DIRECTION_ONLY", "NO_DIRECTIONAL_ENTRY_SIGNAL"}):
        return "ONLY_OTHER_DIRECTION_OR_NON_ENTRY"
    if primary == "NO_RETAINED_SIGNAL_UNKNOWN":
        return "NO_RETAINED_SIGNAL_UNKNOWN"
    if primary == "CANDIDATE_NOT_FILLED":
        return "ENTRY_CANDIDATE_NOT_FILLED"
    if _oneof(primary, _REJECTION_STAGES):
        return "RECORDED_REJECTION"
    if primary == "MIXED_RECORDED_REASONS":
        stages = evidence.get("stage_counts")
        if isinstance(stages, dict) and any(
            type(stages.get(key)) is int and 0 < stages[key] <= 1_000_000
            for key in _REJECTION_STAGES
        ):
            return "RECORDED_REJECTION"
    # Zero signals, expiry, no receipt and unavailable JEV are not a model veto.
    return "UNKNOWN"


def _snapshot_for_report(snapshot, *, environment, symbols, as_of_ms):
    arguments = {"environment": environment, "symbols": symbols, "as_of_ms": as_of_ms}
    if not isinstance(snapshot, dict):
        return unavailable_snapshot(**arguments)
    maximum = snapshot.get("max_age_ms")
    if snapshot.get("version") != SNAPSHOT_VERSION or not _parameters(
        **arguments, max_age_ms=maximum
    ):
        return unavailable_snapshot(**arguments, reason="CATALOG_DATA_INVALID")
    arguments["max_age_ms"] = maximum
    if snapshot.get("environment") != environment:
        return unavailable_snapshot(**arguments, reason="CATALOG_ENVIRONMENT_MISMATCH")
    knowledge = snapshot.get("as_of_ms")
    if not _clock(knowledge):
        return unavailable_snapshot(**arguments, reason="CATALOG_DATA_INVALID")
    if knowledge > as_of_ms:
        return unavailable_snapshot(**arguments, reason="CATALOG_FROM_FUTURE")
    if snapshot.get("status") != "OBSERVED":
        return unavailable_snapshot(**arguments, reason=snapshot.get("reason"))
    observed = snapshot.get("observed_at_ms")
    if not _clock(observed):
        return unavailable_snapshot(**arguments, reason="CATALOG_DATA_INVALID")
    if observed > knowledge:
        return unavailable_snapshot(**arguments, reason="CATALOG_FROM_FUTURE")
    if as_of_ms - observed > maximum:
        return unavailable_snapshot(**arguments, reason="CATALOG_STALE")
    rows = snapshot.get("rows")
    if not isinstance(rows, list) or len(rows) != len(symbols):
        return unavailable_snapshot(**arguments, reason="CATALOG_SCOPE_MISMATCH")
    clean = []
    for symbol, row in zip(symbols, rows, strict=True):
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            return unavailable_snapshot(**arguments, reason="CATALOG_SCOPE_MISMATCH")
        present, status, supported = (
            row.get("present"),
            row.get("status"),
            row.get("supported"),
        )
        if (
            type(present) is not bool
            or (
                present
                and (not _oneof(status, _STATUSES) or type(supported) is not bool)
            )
            or (
                not present
                and (status != "ABSENT_FROM_CURRENT_CATALOG" or supported is not None)
            )
        ):
            return unavailable_snapshot(**arguments, reason="CATALOG_DATA_INVALID")
        clean.append(
            {
                "symbol": symbol,
                "present": present,
                "status": status,
                "supported": supported,
            }
        )
    return {
        **unavailable_snapshot(**arguments),
        "status": "OBSERVED",
        "reason": "CATALOG_OBSERVED",
        "observed_at_ms": observed,
        "rows": clean,
    }


def _seal(result):
    content = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {**result, "content_digest": hashlib.sha256(content.encode()).hexdigest()}


def build_coverage_review(report, snapshot):
    """Project public facts only; current listing never replaces past evidence."""
    from v2_core.research_market_sources import market_comparison_contract

    base = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": "REPORT_INPUT_INVALID",
        "account_environment": "UNKNOWN",
        "market_source": "UNKNOWN",
        "rows": [],
        "counts": {},
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "limitations": [
            "CURRENT_CATALOG_CANNOT_PROVE_DAY_OR_SIGNAL_TIME_LISTING",
            "CATALOG_ABSENCE_IS_NOT_NO_SIGNAL_GENERATED_OR_STRATEGY_REJECTION",
            "RETAINED_EVIDENCE_IS_BOUNDED_NOT_COMPLETE_DAY_ATTRIBUTION",
            "CURRENT_LISTING_DOES_NOT_PROVE_ACCOUNT_READINESS_OR_TRADABILITY",
            "HISTORICAL_FILL_IS_NOT_INVALIDATED_BY_CURRENT_CATALOG_ABSENCE",
            "CROSS_ENVIRONMENT_PRICE_EXTREMES_ARE_NOT_EXECUTABLE_PNL",
        ],
    }
    if not isinstance(report, dict):
        return _seal(base)
    environment, as_of = report.get("account_environment"), report.get("as_of_ms")
    rows, window = report.get("rows"), report.get("window")
    if (
        not _oneof(environment, _ENVIRONMENTS)
        or not _clock(as_of)
        or not isinstance(rows, list)
        or len(rows) > MAX_SYMBOLS
        or not isinstance(window, dict)
        or not _clock(window.get("start_ms"))
        or not _clock(window.get("end_ms"))
        or not window["start_ms"] < window["end_ms"] <= as_of
        or any(
            not isinstance(row, dict)
            or not _oneof(row.get("direction"), {"BUY", "SELL"})
            for row in rows
        )
    ):
        return _seal(base)
    symbols = _symbols([row.get("symbol") for row in rows])
    if symbols is None:
        return _seal(base)
    checked = _snapshot_for_report(
        snapshot, environment=environment, symbols=symbols, as_of_ms=as_of
    )
    counts, intersections, result_rows = Counter(), Counter(), []
    for row, catalog in zip(rows, checked["rows"], strict=True):
        history = _history(row)
        current = (
            "UNKNOWN"
            if catalog["present"] is None
            else "ABSENT_FROM_CURRENT_CATALOG"
            if not catalog["present"]
            else "PRESENT_ENGINE_UNSUPPORTED"
            if catalog["supported"] is False
            else "PRESENT_TRADING"
            if catalog["status"] == "TRADING"
            else "PRESENT_NOT_TRADING"
        )
        counts[history] += 1
        intersections[f"{history}:{current}"] += 1
        result_rows.append(
            {
                "symbol": row["symbol"],
                "direction": row["direction"],
                "current_listing": current,
                "catalog_status": catalog["status"],
                "engine_symbol_supported": catalog["supported"],
                "historical_evidence": history,
                "historical_listing": "NOT_PROVEN_BY_CURRENT_CATALOG",
                "evidence_conflict": "HISTORICAL_FILL_WITH_CURRENT_ABSENCE_NOT_A_CONTRADICTION"
                if history == "MATCHED_DIRECTION_FILLED"
                and current == "ABSENT_FROM_CURRENT_CATALOG"
                else "NONE_OBSERVED",
            }
        )
    observed = checked["observed_at_ms"]
    relation = (
        "UNKNOWN"
        if observed is None
        else "BEFORE_DAY"
        if observed < window["start_ms"]
        else "DURING_DAY"
        if observed < window["end_ms"]
        else "AT_OR_AFTER_DAY_END"
    )
    market = report.get("market_source")
    return _seal(
        {
            **base,
            "status": "OBSERVED_BOUNDED"
            if checked["status"] == "OBSERVED"
            else "PARTIAL_UNKNOWN",
            "reason": checked["reason"],
            "account_environment": environment,
            "market_source": market if _oneof(market, _MARKETS) else "UNKNOWN",
            "market_comparison": market_comparison_contract(
                research_source=market, execution_environment=environment
            ),
            "market_execution_environment_match": (
                market
                == {"SANDBOX": "BINANCE_USDM_SANDBOX", "LIVE": "BINANCE_USDM_LIVE"}[
                    environment
                ]
                if _oneof(market, _MARKETS)
                else None
            ),
            "as_of_ms": as_of,
            "snapshot": {key: value for key, value in checked.items() if key != "rows"},
            "snapshot_relation_to_review_day": relation,
            "rows": result_rows,
            "counts": {key: counts[key] for key in _HISTORICAL_CATEGORIES},
            "current_listing_counts": dict(
                sorted(Counter(row["current_listing"] for row in result_rows).items())
            ),
            "historical_current_intersections": dict(sorted(intersections.items())),
        }
    )
