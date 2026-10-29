"""Bounded same-environment fill paths for daily mover research, never orders.

Load inside a short read-only PG snapshot, close it, then enrich using the local
SANDBOX archive. LIVE winner-selection candles must never be passed as this port.
"""

import time
from collections import Counter
from dataclasses import asdict
from fractions import Fraction

from services.v2_trade_path_review import post_flat_path
from v2_core.account_risk import AccountScope
from v2_core.quant_indicators import MINUTE, Bar, decimal
from v2_core.trade_path import PathFill, analyze_path, fill_window

VERSION = "daily-mover-sandbox-fill-path-v1"
SOURCE = "BINANCE_USDM_SANDBOX"
_TRADE_FIELDS = (
    "id",
    "symbol",
    "side",
    "opened_ms",
    "closed_ms",
    "opening_quantity",
    "closing_quantity",
    "opening_notional",
    "closing_notional",
    "net_pnl",
    "settlement_state",
    "settled_ms",
    "settlement_revision",
    "status",
)
_FILLS_SQL = """SELECT f.fill_key,f.occurred_at_ms,o.leg,f.quantity::text,f.price::text,
  EXISTS(SELECT 1 FROM v2_orders m WHERE m.episode_id=i.intent_id
    AND m.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE')
FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
JOIN v2_fills f USING(order_id)
WHERE i.intent_id=%s
  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.payload->>'symbol'=%s AND i.payload->>'side'=%s
  AND i.created_at<=to_timestamp(%s/1000.0)
  AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)
ORDER BY f.occurred_at_ms,f.fill_key LIMIT 1001"""


def _timestamp(value):
    return type(value) is int and 0 <= value < 2**53


def _valid_trade(row, *, start, end, as_of_ms):
    if row.get("maintenance") or row.get("exit_origin") in {
        "APPROVED_TESTNET_MAINTENANCE",
        "USER_APPROVED_MAINTENANCE",
    }:
        return "MAINTENANCE_EXCLUDED"
    if (
        row.get("status") != "FILLED_SETTLED"
        or row.get("settlement_state") != "SETTLED"
    ):
        return "UNSETTLED_OR_UNFILLED"
    if not _timestamp(row.get("closed_ms")) or not start <= row["closed_ms"] < end:
        return "NOT_CLOSED_IN_REVIEW_DAY"
    if (
        not _timestamp(row.get("settled_ms"))
        or not row["closed_ms"] <= row["settled_ms"] <= as_of_ms
    ):
        return "SETTLEMENT_TIMING_UNKNOWN"
    if not _timestamp(row.get("opened_ms")) or row["opened_ms"] > row["closed_ms"]:
        return "FILL_TIMING_UNKNOWN"
    try:
        decimal(row.get("net_pnl"))
    except ValueError:
        return "SETTLED_NET_UNKNOWN"
    return None


def load_case_fills(conn, scope, evidence, *, as_of_ms, max_cases=6):
    """Select latest closed-day settled episodes; <=6 reads, <=1000 fills each."""
    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise ValueError("MOVER_PATH_SANDBOX_REQUIRED")
    if (
        type(max_cases) is not int
        or not 1 <= max_cases <= 6
        or not _timestamp(as_of_ms)
    ):
        raise ValueError("MOVER_PATH_LOAD_BOUND")
    if not isinstance(evidence, dict) or evidence.get("account_scope") != asdict(scope):
        raise ValueError("MOVER_PATH_SCOPE_MISMATCH")
    start, end = evidence.get("window_start_ms"), evidence.get("window_end_ms")
    if (
        not _timestamp(start)
        or not _timestamp(end)
        or end - start != 1440 * MINUTE
        or (start + 480 * MINUTE) % (1440 * MINUTE)
        or end > as_of_ms
    ):
        raise ValueError("MOVER_PATH_DAY_BOUND")
    symbols = evidence.get("symbols")
    if not isinstance(symbols, dict) or not 0 <= len(symbols) <= 20:
        raise ValueError("MOVER_PATH_SYMBOL_BOUND")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("MOVER_PATH_READ_ONLY_REQUIRED")
    eligible, exclusions, seen = [], [], set()
    for symbol, item in sorted(symbols.items()):
        if not isinstance(item, dict):
            raise ValueError("MOVER_PATH_SYMBOL_EVIDENCE_INVALID")  # noqa: TRY004
        trades = item.get("trades", [])
        if not isinstance(trades, list) or len(trades) > 500:
            raise ValueError("MOVER_PATH_TRADES_BOUND")
        for row in trades:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("MOVER_PATH_TRADE_IDENTITY")  # noqa: TRY004
            identity = row["id"]
            reason = _valid_trade(row, start=start, end=end, as_of_ms=as_of_ms)
            if row.get("symbol") != symbol:
                reason = "SYMBOL_EVIDENCE_MISMATCH"
            elif identity in seen:
                reason = "DUPLICATE_EPISODE_EVIDENCE"
            seen.add(identity)
            if reason:
                exclusions.append(
                    {"episode_ref": identity, "symbol": symbol, "reason": reason}
                )
            else:
                eligible.append({key: row.get(key) for key in _TRADE_FIELDS})
    # Any repeated episode is unusable, including its earlier apparently-valid copy.
    duplicated = {
        r["episode_ref"]
        for r in exclusions
        if r["reason"] == "DUPLICATE_EPISODE_EVIDENCE"
    }
    eligible = [row for row in eligible if row["id"] not in duplicated]
    selected = sorted(eligible, key=lambda r: (r["closed_ms"], r["id"]), reverse=True)[
        :max_cases
    ]
    cases = []
    for row in selected:
        case = {"trade": row, "fills": [], "source": SOURCE, "load_error": None}
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='3000ms'")
                fills = conn.execute(
                    _FILLS_SQL,
                    (
                        row["id"],
                        *asdict(scope).values(),
                        row["symbol"],
                        row["side"],
                        as_of_ms,
                        as_of_ms,
                        as_of_ms,
                    ),
                ).fetchall()
            if len(fills) > 1000:
                case["load_error"] = "FILL_COUNT_BOUND"
            elif not fills:
                case["load_error"] = "SCOPED_FILLS_UNAVAILABLE"
            elif any(fill[5] for fill in fills):
                case["load_error"] = "MAINTENANCE_EXCLUDED"
            else:
                case["fills"] = [
                    dict(
                        zip(
                            ("identity", "t", "leg", "quantity", "price"),
                            fill[:5],
                            strict=True,
                        )
                    )
                    for fill in fills
                ]
        except Exception:  # noqa: BLE001 - isolate one bounded read, redact DB errors
            case["load_error"] = "SCOPED_FILL_READ_UNAVAILABLE"
        cases.append(case)
    return {
        "version": VERSION,
        "source": SOURCE,
        "as_of_ms": as_of_ms,
        "selection": "LATEST_CLOSED_IN_DAY_SETTLED_NON_MAINTENANCE_TOP_MOVERS",
        "eligible_count": len(eligible),
        "selection_truncated": len(eligible) > len(cases),
        "cases": cases,
        "exclusions": exclusions,
    }


class _Archive:
    def __init__(self, read, *, max_seconds, max_minutes, monotonic):
        self.read, self.clock = read, monotonic
        self.deadline = monotonic() + max_seconds
        self.max_minutes, self.minutes, self.calls = max_minutes, 0, 0

    def __call__(self, symbol, start, end):
        if any(not _timestamp(t) or t % MINUTE for t in (start, end)) or start > end:
            raise ValueError("ARCHIVE_WINDOW_INVALID")
        requested = (end - start) // MINUTE
        if self.minutes + requested > self.max_minutes:
            raise ValueError("ARCHIVE_MINUTE_BUDGET")
        self.minutes += requested  # failed reads also consume budget
        rows = []
        for begin in range(start, end, 720 * MINUTE):
            if self.clock() >= self.deadline:
                raise ValueError("ARCHIVE_DEADLINE")
            stop = min(end, begin + 720 * MINUTE)
            self.calls += 1
            batch = self.read(symbol, begin, stop)
            if not isinstance(batch, list) or len(batch) > (stop - begin) // MINUTE:
                raise ValueError("ARCHIVE_PAGE_INVALID")
            if any(
                not isinstance(row, dict)
                or not _timestamp(row.get("t"))
                or not begin <= row["t"] < stop
                for row in batch
            ):
                raise ValueError("ARCHIVE_PAGE_SCOPE")
            if len({row["t"] for row in batch}) != len(batch):
                raise ValueError("ARCHIVE_DUPLICATE_MINUTE")
            rows.extend(
                Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
                for row in batch
            )
        return sorted(rows, key=lambda row: row.t)


def _validated(case, *, as_of_ms):
    if case.get("source") != SOURCE:
        raise ValueError("ARCHIVE_ENVIRONMENT_MISMATCH")
    if case.get("load_error"):
        raise ValueError(case["load_error"])
    row = case["trade"]
    if (
        row.get("status") != "FILLED_SETTLED"
        or row.get("settlement_state") != "SETTLED"
    ):
        raise ValueError("SETTLED_LABEL_REQUIRED")
    if (
        not _timestamp(row.get("settled_ms"))
        or not row["closed_ms"] <= row["settled_ms"] <= as_of_ms
    ):
        raise ValueError("SETTLEMENT_TIMING_UNKNOWN")
    decimal(row["net_pnl"])
    raw_fills = case.get("fills")
    if not isinstance(raw_fills, list) or not 2 <= len(raw_fills) <= 1000:
        raise ValueError("FILL_COUNT_BOUND")
    fills = [PathFill(**fill) for fill in raw_fills]
    ordered, start, end = fill_window(fills, as_of_ms=as_of_ms)
    if ordered[0].t != row["opened_ms"] or ordered[-1].t != row["closed_ms"]:
        raise ValueError("FILL_WINDOW_MISMATCH")
    for leg, prefix in (("OPEN", "opening"), ("CLOSE", "closing")):
        group = [fill for fill in fills if fill.leg == leg]
        quantity = sum((Fraction(fill.quantity) for fill in group), Fraction())
        notional = sum(
            (Fraction(fill.quantity) * Fraction(fill.price) for fill in group),
            Fraction(),
        )
        if quantity != Fraction(
            decimal(row[prefix + "_quantity"])
        ) or notional != Fraction(decimal(row[prefix + "_notional"])):
            raise ValueError("FILL_TOTAL_MISMATCH")
    analyze_path(fills, [], side=row["side"], as_of_ms=as_of_ms)
    if end - start > 10080 * MINUTE:
        raise ValueError("PATH_HISTORY_BOUND")
    return row, fills, start, end


def _unknown(reason):
    # Only stable allowlisted reasons can leave this boundary; never SQL/URL details.
    allowed = {
        "FILL_COUNT_BOUND",
        "SCOPED_FILLS_UNAVAILABLE",
        "MAINTENANCE_EXCLUDED",
        "SCOPED_FILL_READ_UNAVAILABLE",
        "ARCHIVE_ENVIRONMENT_MISMATCH",
        "SETTLED_LABEL_REQUIRED",
        "SETTLEMENT_TIMING_UNKNOWN",
        "FILL_WINDOW_MISMATCH",
        "FILL_TOTAL_MISMATCH",
        "ARCHIVE_MINUTE_BUDGET",
        "ARCHIVE_DEADLINE",
        "ARCHIVE_PAGE_INVALID",
        "ARCHIVE_PAGE_SCOPE",
        "ARCHIVE_DUPLICATE_MINUTE",
        "PATH_HISTORY_BOUND",
    }
    return {
        "status": "UNKNOWN",
        "reason": reason
        if reason in allowed
        else "SOURCE_OR_FILL_VALIDATION_UNAVAILABLE",
    }


def enrich_case_paths(
    loaded,
    read_bars,
    *,
    as_of_ms,
    max_seconds=30,
    max_minutes=10080,
    monotonic=time.monotonic,
):
    """No PG access. read_bars must be the bounded local SANDBOX archive port."""
    if (
        not _timestamp(as_of_ms)
        or type(max_seconds) is not int
        or not 1 <= max_seconds <= 90
        or type(max_minutes) is not int
        or not 1 <= max_minutes <= 10080
    ):
        raise ValueError("MOVER_PATH_ARCHIVE_BOUND")
    if (
        not isinstance(loaded, dict)
        or loaded.get("source") != SOURCE
        or not isinstance(loaded.get("cases"), list)
        or len(loaded["cases"]) > 6
    ):
        raise ValueError("MOVER_PATH_CASE_BOUND")
    archive = _Archive(
        read_bars, max_seconds=max_seconds, max_minutes=max_minutes, monotonic=monotonic
    )
    results = []
    for case in loaded["cases"]:
        case = case if isinstance(case, dict) else {}
        row = case.get("trade", {})
        row = row if isinstance(row, dict) else {}
        result = {
            "episode_ref": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "source": SOURCE,
            "actual_net_pnl": None,
            "gross_path": None,
            "post_flat": None,
        }
        try:
            row, fills, start, end = _validated(case, as_of_ms=as_of_ms)
        except Exception as exc:  # noqa: BLE001 - explicit sanitized unknown per case
            result.update(_unknown(str(exc)))
            results.append(result)
            continue
        result["actual_net_pnl"] = str(decimal(row["net_pnl"]))
        result["actual_net_basis"] = (
            "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED"
        )
        try:
            result["gross_path"] = analyze_path(
                fills,
                archive(row["symbol"], start, end),
                side=row["side"],
                as_of_ms=as_of_ms,
            )
        except Exception as exc:  # noqa: BLE001 - archive must not block remaining cases
            result["gross_path"] = _unknown(str(exc))
        post_start = ((row["closed_ms"] + MINUTE - 1) // MINUTE) * MINUTE
        post_end = ((row["closed_ms"] + 60 * MINUTE + MINUTE - 1) // MINUTE) * MINUTE
        try:
            post_bars = (
                []
                if post_end > as_of_ms
                else archive(row["symbol"], post_start, post_end)
            )
            result["post_flat"] = post_flat_path(
                post_bars,
                side=row["side"],
                closed_ms=row["closed_ms"],
                as_of_ms=as_of_ms,
            )
        except Exception as exc:  # noqa: BLE001 - independent optional follow-up
            result["post_flat"] = _unknown(str(exc))
        result["status"] = (
            "OBSERVED_LOWER_BOUND"
            if result["gross_path"]["status"] == "AVAILABLE_LOWER_BOUND"
            else "UNKNOWN"
        )
        results.append(result)
    return {
        "version": VERSION,
        "source": SOURCE,
        "as_of_ms": as_of_ms,
        "cases": results,
        "status_counts": dict(Counter(row["status"] for row in results)),
        "selection_truncated": loaded.get("selection_truncated", False),
        "eligible_count": loaded.get("eligible_count", len(results)),
        "exclusions": loaded.get("exclusions", []),
        "budget": {
            "calls": archive.calls,
            "requested_minutes": archive.minutes,
            "max_minutes": max_minutes,
            "max_seconds": max_seconds,
        },
        "execution_authorized": False,
        "limitations": [
            "SAME_SANDBOX_ARCHIVE_ONLY_NOT_PUBLIC_LIVE_MOVER_PATH",
            "GROSS_EXCURSIONS_ARE_SAMPLED_LOWER_BOUNDS_NOT_EXECUTABLE_BEST_PROFIT",
            "ACTUAL_NET_IS_SEPARATE_COMPLETE_SETTLEMENT_NO_DOUBLE_COST_SUBTRACTION",
            "POST_FLAT_MARKET_RETURN_IS_NOT_HYPOTHETICAL_TRADE_PNL",
            "LATEST_SETTLED_CLOSED_DAY_SELECTION_NOT_PORTFOLIO_BACKTEST",
        ],
    }
