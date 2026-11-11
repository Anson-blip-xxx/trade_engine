"""Pure, bounded factual execution review; no trading imports, I/O or tuning.

The public trade projection has no episode identity. Raw scoped account evidence
provides identity and settlement provenance; the projection must agree with it.
Missing identity is unknown, never an opportunity to guess unique trade counts.
"""

import hashlib
import json
import re
from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from statistics import median

from v2_core.symbols import is_supported_symbol

VERSION = "daily-mover-execution-review-v1"
MAX_TRADES = 500
_MINUTE = 60_000
_MINUTE_QUANTUM = Decimal("0.000001")
_DAY = 1440 * _MINUTE
_EXIT_REASONS = frozenset(
    {
        "ADVERSE_FUNDING",
        "EARLY_LOSS_MOMENTUM_WEAK",
        "EMERGENCY_LOSS",
        "LOW_YIELD_STAGNATION",
        "PARTIAL_TAKE_PROFIT",
        "PEAK_DRAWDOWN",
        "ONE_HOUR_REVERSAL",
        "TIME_STOP",
        "NATIVE_STOP_BOUNDARY",
        "VENUE_PROTECTION_CHILD",
        "RECONCILED_EXTERNAL_CLOSE",
        "EXCHANGE_LIQUIDATION",
        "UNCONFIRMED_PROTECTION",
        "STOP_LOSS",
        "TAKE_PROFIT",
        "TREND_STRUCTURE_BROKEN",
        "TREND_RUNNER_EXIT",
    }
)
_SOURCES = frozenset({"s2", "s3", "tv_bridge", "quant"})
_LIMITATIONS = [
    "TOP_MOVER_ASSOCIATED_CLOSED_EPISODES_NOT_FULL_ACCOUNT_DAY_PNL",
    "WHOLE_EPISODE_SETTLEMENT_NOT_DAY_MARK_TO_MARKET",
    "SETTLED_NET_ALREADY_INCLUDES_COSTS_NO_DOUBLE_SUBTRACTION",
    "FUNDING_UNKNOWN_NOT_RECONSTRUCTED_FROM_NET_GROSS_AND_FEES",
    "PRIMARY_SOURCE_NOT_TV_OR_JEV_INCREMENTAL_CONTRIBUTION",
    "EXIT_REASON_GROUPS_ARE_DESCRIPTIVE_NOT_POLICY_BACKTESTS",
    "RECONCILED_EXTERNAL_CLOSE_PROVES_CLOSE_NOT_MANUAL_OR_STOP_TRIGGER",
    "POST_FLAT_FIRST_COMPLETE_MINUTE_CLOSE_IS_NOT_ACTUAL_EXIT_PRICE",
    "POST_FLAT_MARKET_PATH_NOT_COUNTERFACTUAL_PNL_OR_EXECUTABLE_PROFIT",
    "POST_FLAT_OBSERVATIONS_WERE_NOT_AVAILABLE_AT_EXIT",
    "COMPLETE_PATH_PROVENANCE_IS_UPSTREAM_NOT_INDEPENDENT_ARCHIVE_VERIFICATION",
]


def _digest(value, *, ensure_ascii=False):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _stamp(value):
    return type(value) is int and 0 <= value < 2**53


def _number(value):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 100:
        raise ValueError("INVALID_NUMBER")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_NUMBER") from None
    if (
        not parsed.is_finite()
        or abs(parsed.as_tuple().exponent) > 40
        or len(parsed.as_tuple().digits) > 60
        or parsed.copy_abs() > Decimal("1e18")
    ):
        raise ValueError("INVALID_NUMBER")
    return parsed


def _optional_number(value):
    try:
        return _number(value)
    except ValueError:
        return None


def _text(value):
    if value is None:
        return None
    return format(value, "f") if value else "0"


def _symbol(value):
    return is_supported_symbol(value, quote_assets=("USDT",))


def _empty(reason=None):
    return {
        "version": VERSION,
        "status": "UNKNOWN" if reason else "AVAILABLE",
        "reason": reason,
        "research_only": True,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "profitability_proven": False,
        "funding_status": "NOT_SEPARATELY_LOADED",
        "funding_usdt": None,
        "totals": None,
        "by_side": {},
        "by_exit_reason": {},
        "primary_sources": {},
        "trades": [],
        "coverage": {},
        "post_flat_evidence": {
            "status": "UNKNOWN",
            "reason": "PATHS_UNAVAILABLE",
            "observed_count": 0,
            "favorable_end_count": 0,
            "adverse_end_count": 0,
            "unchanged_end_count": 0,
            "cases": [],
            "unknown_reasons": {},
            "market_return_not_trade_pnl": True,
            "counterfactual_execution_proven": False,
            "costs_included": False,
            "anchor_is_actual_exit_price": False,
        },
        "limitations": list(_LIMITATIONS),
    }


def _seal(result):
    result["content_digest"] = _digest(result)
    return result


def _envelope(report):
    if not isinstance(report, dict):
        raise ValueError("REPORT_INVALID")  # noqa: TRY004 - stable report boundary
    cutoff, window = report.get("as_of_ms"), report.get("window")
    if not _stamp(cutoff) or not isinstance(window, dict):
        raise ValueError("REVIEW_WINDOW_INVALID")
    start, end = window.get("start_ms"), window.get("end_ms")
    if (
        not _stamp(start)
        or not _stamp(end)
        or not start < end <= cutoff
        or end - start != _DAY
        or (start + 480 * _MINUTE) % _DAY
        or window.get("timezone") != "Asia/Shanghai"
    ):
        raise ValueError("REVIEW_WINDOW_INVALID")
    evidence = report.get("account_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("IDENTIFIED_ACCOUNT_EVIDENCE_UNAVAILABLE")  # noqa: TRY004
    scope = evidence.get("account_scope")
    environment = report.get("account_environment")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"exchange", "account_id", "environment", "product"}
        or scope.get("exchange") != "BINANCE"
        or scope.get("product") != "FUTURES"
        or environment not in {"SANDBOX", "LIVE"}
        or scope.get("environment") != environment
        or not isinstance(scope.get("account_id"), str)
        or not 1 <= len(scope["account_id"]) <= 128
        or report.get("scope_key") != _digest(scope, ensure_ascii=True)
        or evidence.get("window_start_ms") != start
        or evidence.get("window_end_ms") != end
        or not _stamp(evidence.get("as_of_ms"))
        or not end <= evidence["as_of_ms"] <= cutoff
    ):
        raise ValueError("ACCOUNT_SCOPE_OR_CUTOFF_MISMATCH")
    rows, symbols = report.get("rows"), evidence.get("symbols")
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError("ROW_BOUND")
    if not isinstance(symbols, dict) or len(symbols) > 40:
        raise ValueError("ACCOUNT_SYMBOL_BOUND")
    return start, end, evidence["as_of_ms"], scope, rows, symbols


def _projection_key(row):
    return (
        row.get("side"),
        row.get("opened_ms"),
        row.get("closed_ms"),
        row.get("settled_ms"),
        _number(row.get("opening_quantity")),
        _number(row.get("closing_quantity")),
        _number(row.get("actual_settled_net_usdt")),
        row.get("settlement_state"),
        row.get("status"),
    )


def _projected(rows):
    projections, count = {}, 0
    for row in rows:
        if not isinstance(row, dict) or not _symbol(row.get("symbol")):
            raise ValueError("ROW_SYMBOL_INVALID")
        evidence = row.get("evidence")
        trades = evidence.get("trades") if isinstance(evidence, dict) else None
        if not isinstance(trades, list):
            raise ValueError("PROJECTED_TRADES_INVALID")  # noqa: TRY004
        count += len(trades)
        if count > MAX_TRADES:
            raise ValueError("TRADE_BOUND")
        candidates = projections.setdefault(row["symbol"], set())
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            try:
                key = _projection_key(trade)
                hash(key)
                candidates.add(key)
            except (ValueError, TypeError):
                continue
    return projections, count


def _normalize(row, *, start, end, cutoff, scope):
    if (
        row.get("environment", scope["environment"]) != scope["environment"]
        or row.get("account_id", scope["account_id"]) != scope["account_id"]
    ):
        raise ValueError("TRADE_SCOPE_MISMATCH")
    if row.get("maintenance") or row.get("exit_origin") in {
        "APPROVED_TESTNET_MAINTENANCE",
        "USER_APPROVED_MAINTENANCE",
    }:
        raise ValueError("MAINTENANCE_EXCLUDED")
    if (
        row.get("settlement_state") != "SETTLED"
        or row.get("status") != "FILLED_SETTLED"
        or row.get("net_pnl_available") is not True
        or row.get("fills_valid") is not True
    ):
        raise ValueError("COMPLETE_SETTLEMENT_UNPROVEN")
    opened, closed, settled = (
        row.get(k) for k in ("opened_ms", "closed_ms", "settled_ms")
    )
    if not all(_stamp(t) for t in (opened, closed, settled)) or not (
        opened <= closed <= settled <= cutoff
    ):
        raise ValueError("SETTLEMENT_TIMING_INVALID")
    if not start <= closed < end:
        raise ValueError("NOT_CLOSED_IN_REVIEW_DAY")
    decided = row.get("decided_ms")
    if decided is not None and (not _stamp(decided) or decided > opened):
        raise ValueError("DECISION_TIMING_INVALID")
    side = row.get("side")
    if side not in {"BUY", "SELL"} or not _symbol(row.get("symbol")):
        raise ValueError("TRADE_DIRECTION_OR_SYMBOL_INVALID")
    opening, closing, net = (
        _number(row.get(k)) for k in ("opening_quantity", "closing_quantity", "net_pnl")
    )
    if opening <= 0 or closing != opening:
        raise ValueError("UNBALANCED_FILL_QUANTITIES")
    if (
        row.get("remaining_quantity") is not None
        and _number(row["remaining_quantity"]) != 0
    ):
        raise ValueError("UNBALANCED_FILL_QUANTITIES")
    gross = _optional_number(row.get("gross_pnl_usdt"))
    fees = _optional_number(row.get("fees_usdt"))
    # Gross is independently checked against fill notionals, never net + fees.
    if (
        row.get("opening_notional") is not None
        or row.get("closing_notional") is not None
    ):
        before = _optional_number(row.get("opening_notional"))
        after = _optional_number(row.get("closing_notional"))
        if before is None or after is None or before <= 0 or after <= 0:
            gross = None
        else:
            calculated = (after - before) * (1 if side == "BUY" else -1)
            gross = calculated if gross == calculated else None
    return {
        "symbol": row["symbol"],
        "side": side,
        "opened_ms": opened,
        "closed_ms": closed,
        "settled_ms": settled,
        # Millisecond durations can produce repeating decimals. Keep derived
        # minutes bounded for downstream validators without rounding PnL.
        "held_minutes": _text(
            (Decimal(closed - opened) / _MINUTE).quantize(
                _MINUTE_QUANTUM, rounding=ROUND_HALF_EVEN
            )
        ),
        "net_usdt": _text(net),
        "gross_usdt": _text(gross),
        "recorded_fees_usdt": _text(fees),
        "exit_reason": row.get("exit_reason")
        if row.get("exit_reason") in _EXIT_REASONS
        else "UNKNOWN_RECORDED_REASON",
        "primary_source": row.get("source")
        if row.get("source") in _SOURCES
        else "UNKNOWN",
        "opening_quantity": _text(opening),
        "closing_quantity": _text(closing),
        "settlement_state": "SETTLED",
        "status": "FILLED_SETTLED",
        "actual_settled_net_usdt": _text(net),
    }


def _summary(trades):
    nets = [Decimal(row["net_usdt"]) for row in trades]
    grosses = [
        Decimal(row["gross_usdt"]) for row in trades if row["gross_usdt"] is not None
    ]
    fees = [
        Decimal(row["recorded_fees_usdt"])
        for row in trades
        if row["recorded_fees_usdt"] is not None
    ]
    return {
        "settled_count": len(trades),
        "net_usdt": _text(sum(nets, Decimal(0))),
        "gross_known_count": len(grosses),
        "gross_usdt": _text(sum(grosses, Decimal(0)))
        if len(grosses) == len(trades)
        else None,
        "gross_known_subtotal_usdt": _text(sum(grosses, Decimal(0))),
        "recorded_fees_known_count": len(fees),
        "recorded_fees_usdt": _text(sum(fees, Decimal(0)))
        if len(fees) == len(trades)
        else None,
        "recorded_fees_known_subtotal_usdt": _text(sum(fees, Decimal(0))),
        "wins": sum(value > 0 for value in nets),
        "losses": sum(value < 0 for value in nets),
        "breakeven": sum(value == 0 for value in nets),
        "held_minutes_median": _text(
            (
                median(Decimal(row["closed_ms"] - row["opened_ms"]) for row in trades)
                / _MINUTE
            ).quantize(_MINUTE_QUANTUM, rounding=ROUND_HALF_EVEN)
        )
        if trades
        else None,
    }


def _post_flat(case, trade, *, cutoff, source):
    if (
        case.get("source") != source
        or case.get("symbol") != trade["symbol"]
        or case.get("side") != trade["side"]
    ):
        raise ValueError("PATH_SCOPE_MISMATCH")
    if case.get(
        "actual_net_basis"
    ) != "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED" or _number(
        case.get("actual_net_pnl")
    ) != Decimal(trade["net_usdt"]):
        raise ValueError("PATH_SETTLEMENT_MISMATCH")
    path = case.get("post_flat")
    if not isinstance(path, dict) or path.get("status") != "OBSERVED":
        raise ValueError("PATH_NOT_MATURE_AND_COMPLETE")
    if (
        path.get("basis") != "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE"
        or path.get("flat_ms") != trade["closed_ms"]
        or path.get("market_return_not_trade_pnl") is not True
        or path.get("counterfactual_execution_proven") is not False
        or path.get("costs_included") is not False
    ):
        raise ValueError("PATH_BASIS_UNPROVEN")
    horizon = path.get("requested_horizon_minutes")
    if type(horizon) is not int or not 1 <= horizon <= 1440:
        raise ValueError("PATH_HORIZON_INVALID")
    flat = trade["closed_ms"]
    first = ((flat + _MINUTE - 1) // _MINUTE) * _MINUTE
    anchor, end = (
        first + _MINUTE,
        ((flat + horizon * _MINUTE + _MINUTE - 1) // _MINUTE) * _MINUTE,
    )
    if (
        not anchor < end <= cutoff
        or path.get("anchor_ms") != anchor
        or path.get("anchor_delay_ms") != anchor - flat
        or path.get("observed_end_ms") != end
        or path.get("observed_duration_ms") != end - anchor
        or path.get("horizon_delay_ms") != end - (flat + horizon * _MINUTE)
        or not isinstance(path.get("path_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", path["path_digest"]) is None
    ):
        raise ValueError("PATH_TIMING_OR_PROOF_INVALID")
    ret, favorable, adverse, price, last = (
        _number(path.get(k))
        for k in (
            "return_pct",
            "favorable_excursion_pct",
            "adverse_excursion_pct",
            "anchor_price",
            "end_price",
        )
    )
    if (
        price <= 0
        or last <= 0
        or favorable < max(ret, Decimal(0))
        or adverse > min(ret, Decimal(0))
    ):
        raise ValueError("PATH_VALUES_INVALID")
    proof = path.get("anchor_provenance")
    bar = proof.get("bar") if isinstance(proof, dict) else None
    if (
        not isinstance(proof, dict)
        or proof.get("source") != "CALLER_CLOSED_1M_ARCHIVE"
        or not isinstance(bar, dict)
        or not _stamp(bar.get("t"))
        or bar["t"] != first
    ):
        raise ValueError("PATH_ANCHOR_PROVENANCE_INVALID")
    opened, high, low, close, volume = (
        _number(bar.get(key)) for key in ("o", "h", "l", "c", "v")
    )
    if (
        not (0 < low <= min(opened, close) <= max(opened, close) <= high)
        or volume < 0
        or close != price
    ):
        raise ValueError("PATH_ANCHOR_PROVENANCE_INVALID")
    calculated = (last / price - 1) * 100 * (1 if trade["side"] == "BUY" else -1)
    if abs(calculated - ret) > Decimal("1e-10"):
        raise ValueError("PATH_VALUES_INVALID")
    return {
        "trade_ref": trade["trade_ref"],
        "symbol": trade["symbol"],
        "side": trade["side"],
        "horizon_minutes": horizon,
        "directional_market_return_pct": _text(ret),
        "favorable_excursion_pct": _text(favorable),
        "adverse_excursion_pct": _text(adverse),
        "end_state": "FAVORABLE" if ret > 0 else "ADVERSE" if ret < 0 else "UNCHANGED",
        "anchor_ms": anchor,
        "observed_end_ms": end,
        "anchor_basis": "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE_NOT_EXIT_PRICE",
    }


def _paths(report, trades, result):
    raw = report.get("actual_same_environment_paths")
    if not isinstance(raw, dict):
        return
    source = "BINANCE_USDM_" + report["account_environment"]
    if (
        raw.get("source") != source
        or not _stamp(raw.get("as_of_ms"))
        or raw["as_of_ms"] > report["as_of_ms"]
    ):
        result["reason"] = "PATH_SOURCE_OR_CUTOFF_MISMATCH"
        return
    cases = raw.get("cases")
    if not isinstance(cases, list) or len(cases) > 6:
        result["reason"] = "PATH_CASE_BOUND"
        return
    identities = Counter(
        case.get("episode_ref")
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("episode_ref"), str)
    )
    unknown, observed = Counter(), []
    for case in cases:
        identity = case.get("episode_ref") if isinstance(case, dict) else None
        if (
            not isinstance(identity, str)
            or identities[identity] != 1
            or identity not in trades
        ):
            unknown["PATH_IDENTITY_UNPROVEN"] += 1
            continue
        try:
            if raw["as_of_ms"] < trades[identity]["settled_ms"]:
                raise ValueError("PATH_CUTOFF_BEFORE_SETTLEMENT")
            observed.append(
                _post_flat(
                    case,
                    trades[identity],
                    cutoff=min(raw["as_of_ms"], report["account_evidence"]["as_of_ms"]),
                    source=source,
                )
            )
        except (ValueError, TypeError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "PATH_VALUE_INVALID"
            unknown[
                code
                if code
                in {
                    "INVALID_NUMBER",
                    "PATH_SCOPE_MISMATCH",
                    "PATH_SETTLEMENT_MISMATCH",
                    "PATH_NOT_MATURE_AND_COMPLETE",
                    "PATH_BASIS_UNPROVEN",
                    "PATH_HORIZON_INVALID",
                    "PATH_TIMING_OR_PROOF_INVALID",
                    "PATH_VALUES_INVALID",
                    "PATH_ANCHOR_PROVENANCE_INVALID",
                    "PATH_CUTOFF_BEFORE_SETTLEMENT",
                }
                else "PATH_VALUE_INVALID"
            ] += 1
    observed.sort(key=lambda row: row["trade_ref"])
    result.update(
        status="AVAILABLE"
        if observed and not unknown
        else "PARTIAL"
        if observed
        else "UNKNOWN",
        reason=None if observed else "NO_VALID_MATURE_PATHS",
        observed_count=len(observed),
        favorable_end_count=sum(row["end_state"] == "FAVORABLE" for row in observed),
        adverse_end_count=sum(row["end_state"] == "ADVERSE" for row in observed),
        unchanged_end_count=sum(row["end_state"] == "UNCHANGED" for row in observed),
        cases=observed,
        unknown_reasons=dict(sorted(unknown.items())),
    )


def build_execution_review(report):
    """Return detached JSON research facts; malformed input fails to unknown.

    At most 500 raw and 500 projected trade references, 20 ranking rows and six
    path cases are examined. Alias changes and input order do not change digest.
    No absent costs, outcomes, opportunity counts or funding are synthesized.
    """
    try:
        start, end, cutoff, scope, rows, symbols = _envelope(report)
        projections, projected_count = _projected(rows)
    except (ValueError, TypeError) as exc:
        return _seal(
            _empty(str(exc) if isinstance(exc, ValueError) else "REPORT_INVALID")
        )
    result, unknown, candidates, conflicts = _empty(), Counter(), {}, set()
    result.update(
        account_environment=scope["environment"],
        scope_key=report["scope_key"],
        as_of_ms=report["as_of_ms"],
        evidence_as_of_ms=cutoff,
    )
    matched_projections = set()
    raw_count, duplicates = 0, 0
    with localcontext() as context:
        context.prec = 100
        for evidence in symbols.values():
            raw = evidence.get("trades") if isinstance(evidence, dict) else None
            if not isinstance(raw, list):
                return _seal(_empty("RAW_TRADES_INVALID"))
            raw_count += len(raw)
            if raw_count > MAX_TRADES:
                return _seal(_empty("TRADE_BOUND"))
            for row in raw:
                identity = row.get("id") if isinstance(row, dict) else None
                if not isinstance(identity, str) or not 1 <= len(identity) <= 160:
                    unknown["TRADE_IDENTITY_UNAVAILABLE"] += 1
                    continue
                try:
                    normalized = _normalize(
                        row, start=start, end=end, cutoff=cutoff, scope=scope
                    )
                    if normalized["symbol"] not in projections:
                        raise ValueError("TRADE_NOT_IN_SELECTED_ROWS")
                    if (
                        _projection_key(normalized)
                        not in projections[normalized["symbol"]]
                    ):
                        raise ValueError("TRADE_PROJECTION_MISMATCH")
                    matched_projections.add(
                        (normalized["symbol"], _projection_key(normalized))
                    )
                    error = None
                except ValueError as exc:
                    error = str(exc)  # private validators use fixed error codes
                    normalized = None
                except TypeError:
                    error = "TRADE_VALUE_INVALID"
                    normalized = None
                # Revision and recorded provenance changes are conflicts even if
                # net amounts happen to match or a reason is not in our labels.
                provenance = tuple(
                    row.get(key)
                    for key in (
                        "settlement_revision",
                        "decided_ms",
                        "source",
                        "exit_reason",
                        "exit_origin",
                    )
                )
                current = (normalized, error, provenance)
                if identity in candidates:
                    duplicates += 1
                    if current != candidates[identity]:
                        conflicts.add(identity)
                else:
                    candidates[identity] = current
        for symbol, keys in projections.items():
            for key in keys:
                if (symbol, key) not in matched_projections:
                    unknown["PROJECTED_TRADE_WITHOUT_VERIFIED_IDENTITY"] += 1
        trades = {}
        for identity, (row, error, _provenance) in candidates.items():
            if identity in conflicts:
                unknown["CONFLICTING_EPISODE_EVIDENCE"] += 1
            elif error:
                unknown[error] += 1
            else:
                row["trade_ref"] = _digest(
                    {"scope_key": report["scope_key"], "episode": identity}
                )
                trades[identity] = row
        selected = sorted(
            trades.values(), key=lambda row: (row["closed_ms"], row["trade_ref"])
        )
        result["status"] = (
            "PARTIAL" if selected and unknown else "UNKNOWN" if unknown else "AVAILABLE"
        )
        result["reason"] = (
            "NO_VALID_COMPLETE_SETTLEMENTS" if not selected and unknown else None
        )
        result["totals"] = _summary(selected) if selected or not unknown else None
        for field, key in (("side", "by_side"), ("exit_reason", "by_exit_reason")):
            result[key] = {
                name: _summary([row for row in selected if row[field] == name])
                for name in sorted({row[field] for row in selected})
            }
        result["primary_sources"] = dict(
            sorted(Counter(row["primary_source"] for row in selected).items())
        )
        result["coverage"] = {
            "raw_trade_references": raw_count,
            "projected_trade_references": projected_count,
            "unique_identified_episodes": len(candidates),
            "duplicate_copies_removed": duplicates,
            "conflicting_episodes": len(conflicts),
            "unknown_reasons": dict(sorted(unknown.items())),
            "max_trade_references": MAX_TRADES,
            "full_account_day_coverage_proven": False,
        }
        _paths(report, trades, result["post_flat_evidence"])
        private = {
            "opening_quantity",
            "closing_quantity",
            "settlement_state",
            "status",
            "actual_settled_net_usdt",
        }
        result["trades"] = [
            {key: value for key, value in row.items() if key not in private}
            for row in selected
        ]
    return _seal(result)
