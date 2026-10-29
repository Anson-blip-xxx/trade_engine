"""Bounded offline profitability audit, not an entry/exit permission.

Load PG facts first, close that snapshot, then call enrich_audit. Market archive
failures are isolated per case. There are no venue, model, or write ports here.
"""

import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from services.v2_profit_capture_review import (
    build_profit_capture_review,
    strict_trade_review,
)
from services.v2_s3_candles import PRICE_VERSION, ROLLING_VERSION, build_frame
from services.v2_trade_path_review import enrich_path_inputs, load_path_inputs
from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar
from v2_core.timeframe_indicators import aggregate_closed_bars
from v2_core.trade_path import PathFill, analyze_path, fill_window

MINUTE = 60000
HOUR = 60 * MINUTE
VERSION = "profitability-audit-v1"


@dataclass(frozen=True)
class AuditPolicy:
    max_trades: int = 6
    trend_cases: int = 3
    trend_history_hours: int = 60
    trend_forward_hours: int = 24
    max_archive_minutes: int = 30240
    max_archive_seconds: int = 60

    def __post_init__(self):
        for name, lower, upper in (
            ("max_trades", 1, 12),
            ("trend_cases", 0, 6),
            ("trend_history_hours", 24, 72),
            ("trend_forward_hours", 1, 48),
            ("max_archive_minutes", 1440, 60480),
            ("max_archive_seconds", 1, 90),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("PROFIT_AUDIT_BOUNDS")
        if self.trend_cases > self.max_trades:
            raise ValueError("PROFIT_AUDIT_TREND_BOUND")


class BudgetArchive:
    def __init__(self, read, policy, *, monotonic=time.monotonic):
        self.read, self.policy, self.monotonic = read, policy, monotonic
        self.deadline = monotonic() + policy.max_archive_seconds
        self.requested_minutes = self.calls = 0

    def __call__(self, target, start, end):
        if (
            any(type(t) is not int or t < 0 or t % MINUTE for t in (start, end))
            or end < start
        ):
            raise ValueError("PROFIT_AUDIT_ARCHIVE_WINDOW")
        minutes = (end - start) // MINUTE
        if self.requested_minutes + minutes > self.policy.max_archive_minutes:
            raise ValueError("PROFIT_AUDIT_ARCHIVE_BUDGET")
        self.requested_minutes += minutes  # failed reads consume budget too
        rows = []
        for offset in range(start, end, 720 * MINUTE):
            if self.monotonic() >= self.deadline:
                raise ValueError("PROFIT_AUDIT_DEADLINE")
            stop = min(end, offset + 720 * MINUTE)
            self.calls += 1
            page = self.read(target, offset, stop)
            if not isinstance(page, list) or len(page) > (stop - offset) // MINUTE:
                raise ValueError("PROFIT_AUDIT_ARCHIVE_PAGE")
            if any(
                not isinstance(b, dict)
                or type(b.get("t")) is not int
                or not offset <= b["t"] < stop
                for b in page
            ):
                raise ValueError("PROFIT_AUDIT_ARCHIVE_SCOPE")
            if len({b["t"] for b in page}) != len(page):
                raise ValueError("PROFIT_AUDIT_ARCHIVE_DUPLICATE")
            rows.extend(page)
        return sorted(rows, key=lambda b: b["t"])


def load_audit_inputs(conn, scope, *, now_ms, holdout_start_ms, days=14, policy=None):
    policy = policy or AuditPolicy()
    review = strict_trade_review(conn, scope, days=days, now_ms=now_ms, limit=500)
    cohorts = build_profit_capture_review(review, holdout_start_ms=holdout_start_ms)
    paths = load_path_inputs(
        conn, scope, now_ms=now_ms, days=days, max_trades=policy.max_trades
    )
    return {
        "version": VERSION,
        "cohorts": cohorts,
        "paths": paths,
        "policy": asdict(policy),
    }


def feature_comparison(case, read):
    row = case["trade"]
    end = row["opened_ms"] // MINUTE * MINUTE
    bars = read(row["symbol"], end - 1440 * MINUTE, end)
    batch = {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": end,
        "candles": {row["symbol"]: list(reversed(bars))},
    }
    frames = [
        build_frame(batch, environment="SANDBOX", feature_version=v)
        for v in (PRICE_VERSION, ROLLING_VERSION)
    ]
    fields = ("atr", "atr_pct", "ema20", "ema60")
    return {
        "status": "COMPARED",
        "closed_at_ms": end,
        "input_digest": digest(canonical(batch)),
        "availability_at_entry": "NOT_PROVEN",
        "versions": [PRICE_VERSION, ROLLING_VERSION],
        "windows": {
            name: {
                v: {k: f["windows"][row["symbol"]][name][k] for k in fields}
                for v, f in zip((PRICE_VERSION, ROLLING_VERSION), frames, strict=True)
            }
            for name in ("15m", "1h", "4h", "24h")
        },
        "interpretation": "ROLLING_MINUTE_FEATURES_NOT_HOURLY_EMA_OR_CAUSAL_ADMISSION_BACKTEST",
    }


def validate_case(case, *, as_of_ms):
    """All analysis branches must agree with the same actual fill ledger."""
    row = case["trade"]
    fills = [PathFill(**f) for f in case["fills"]]
    ordered, _, _ = fill_window(fills, as_of_ms=as_of_ms)
    if ordered[0].t != row["opened_ms"] or ordered[-1].t != row["closed_ms"]:
        raise ValueError("PROFIT_AUDIT_FILL_WINDOW_MISMATCH")
    for leg, prefix in (("OPEN", "opening"), ("CLOSE", "closing")):
        group = [f for f in fills if f.leg == leg]
        quantity = sum((Fraction(f.quantity) for f in group), Fraction(0))
        notional = sum(
            (Fraction(f.quantity) * Fraction(f.price) for f in group), Fraction(0)
        )
        if quantity != Fraction(row[prefix + "_quantity"]) or notional != Fraction(
            row[prefix + "_notional"]
        ):
            raise ValueError("PROFIT_AUDIT_FILL_TOTAL_MISMATCH")
    # Empty bars still validate chronological inventory and final flat. No
    # imaginary path extremes are produced by this consistency check.
    analyze_path(fills, [], side=row["side"], as_of_ms=as_of_ms)


def runner_input(case, read, *, as_of_ms, policy):
    """Fork only an actually held residual at the first full hour after entry.

    Selection is descriptive and closed-trade-biased, NOT portfolio backtesting.
    Frozen sizing stop is a plan; no venue protection confirmation is invented.
    """
    validate_case(case, as_of_ms=as_of_ms)
    row, fills = case["trade"], case["fills"]
    opens = [f for f in fills if f["leg"] == "OPEN"]
    if not opens:
        raise ValueError("PROFIT_AUDIT_OPEN_FILLS_MISSING")
    activation = (max(f["t"] for f in opens) // HOUR + 1) * HOUR
    reductions = [f for f in fills if f["leg"] == "CLOSE" and f["t"] <= activation]
    with localcontext() as ctx:
        ctx.prec = 80
        remaining = sum((Decimal(f["quantity"]) for f in opens), Decimal(0)) - sum(
            (Decimal(f["quantity"]) for f in reductions), Decimal(0)
        )
    if remaining <= 0 or activation > as_of_ms:
        raise ValueError("PROFIT_AUDIT_NOT_HELD_AT_HOUR_BOUNDARY")
    stop = (row.get("sizing") or {}).get("stop_price")
    if not isinstance(stop, str):
        raise TypeError("PROFIT_AUDIT_ORIGINAL_STOP_MISSING")
    end = min(as_of_ms // HOUR * HOUR, activation + policy.trend_forward_hours * HOUR)
    raw = read(row["symbol"], activation - policy.trend_history_hours * HOUR, end)
    hourly = aggregate_closed_bars(raw, timeframe_minutes=60, as_of_ms=end)["bars"]

    def fill(f):
        return {"at_ms": f["t"], "quantity": f["quantity"], "price": f["price"]}

    return {
        "evidence_mode": "RETROSPECTIVE",
        "side": row["side"],
        "activation_at_ms": activation,
        "as_of_ms": end,
        "entry_fills": [fill(f) for f in opens],
        "reduction_fills": [fill(f) for f in reductions],
        "native_stop": {
            "price": stop,
            "confirmed_at_ms": None,
            "available_at_ms": None,
        },
        "entry_evidence": None,
        "history": [
            {**b, "available_at_ms": None} for b in hourly if b["t"] < activation
        ],
        "forward": [
            {**b, "available_at_ms": None} for b in hourly if b["t"] >= activation
        ],
        "funding": [],
        "funding_complete": False,
    }


def public_report(value):
    """Do not export account identities, trade IDs or raw credentials to reports."""
    if isinstance(value, dict):
        return {
            k: public_report(v)
            for k, v in value.items()
            if k
            not in {
                "account_scope",
                "account_id",
                "registry_id",
                "tenant_id",
                "trade_id",
                "id",
                "scope",
            }
        }
    if isinstance(value, (list, tuple)):
        return [public_report(v) for v in value]
    return value


def unavailable(exc):
    code = str(exc)
    return {
        "status": "UNAVAILABLE",
        "reason": code
        if re.fullmatch(
            r"(?:PROFIT_AUDIT|TIMEFRAME|TREND|QUANT|MARKET)_[A-Z0-9_]{1,80}", code
        )
        else type(exc).__name__,
    }


def post_flat_comparison(case, read, now):
    from services.v2_trade_path_review import post_flat_path

    row = case["trade"]
    start = (row["closed_ms"] + MINUTE - 1) // MINUTE * MINUTE
    end = (row["closed_ms"] + 60 * MINUTE + MINUTE - 1) // MINUTE * MINUTE
    rows = read(row["symbol"], start, end) if end <= now else []
    bars = [Bar(**{k: b[k] for k in ("t", "o", "h", "l", "c", "v")}) for b in rows]
    return post_flat_path(
        bars,
        side=row["side"],
        closed_ms=row["closed_ms"],
        as_of_ms=now,
        horizon_minutes=60,
    )


def enrich_audit(inputs, *, read_bars, monotonic=time.monotonic):
    from v2_core.trend_runner import TrendRunnerPolicy, run_shadow

    if inputs.get("version") != VERSION:
        raise ValueError("PROFIT_AUDIT_VERSION")
    policy = AuditPolicy(**inputs["policy"])
    paths = inputs["paths"]
    if (
        paths.get("account_scope", {}).get("environment") != "SANDBOX"
        or len(paths["cases"]) > policy.max_trades
    ):
        raise ValueError("PROFIT_AUDIT_SCOPE")
    read = BudgetArchive(read_bars, policy, monotonic=monotonic)
    path_report = enrich_path_inputs(
        paths, read_bars=read, max_total_minutes=min(30240, policy.max_archive_minutes)
    )
    cases, trend_count = [], 0
    for case in paths["cases"]:
        row = case["trade"]
        item = {
            "symbol": row["symbol"],
            "side": row["side"],
            "opened_ms": row["opened_ms"],
            "closed_ms": row["closed_ms"],
            "entry_mode": row.get("entry_mode"),
        }
        try:
            validate_case(case, as_of_ms=paths["as_of_ms"])
        except (ValueError, TypeError, KeyError, ArithmeticError):
            item.update(
                {
                    name: {
                        "status": "UNAVAILABLE",
                        "reason": "PROFIT_AUDIT_INVALID_FILL_EVIDENCE",
                    }
                    for name in ("indicators", "post_flat", "trend")
                }
            )
            cases.append(item)
            continue
        for name, operation in (
            ("indicators", lambda case=case: feature_comparison(case, read)),
            (
                "post_flat",
                lambda case=case: post_flat_comparison(case, read, paths["as_of_ms"]),
            ),
        ):
            try:
                item[name] = operation()
            except Exception as exc:  # noqa: BLE001 - isolate cases; no raw transport messages
                item[name] = unavailable(exc)
        if trend_count < policy.trend_cases:
            try:
                payload = runner_input(
                    case, read, as_of_ms=paths["as_of_ms"], policy=policy
                )
                trend_count += 1
                item["trend"] = run_shadow(
                    payload, policy=TrendRunnerPolicy(mode="SHADOW")
                )
                item["trend"].update(
                    audit_as_of_ms=paths["as_of_ms"],
                    simulation_as_of_ms=payload["as_of_ms"],
                    observation_window_truncated=payload["as_of_ms"]
                    < paths["as_of_ms"] // HOUR * HOUR,
                )
            except Exception as exc:  # noqa: BLE001 - research cannot block remaining cases
                item["trend"] = unavailable(exc)
        else:
            item["trend"] = {"status": "BATCH_LIMIT"}
        cases.append(item)
    return public_report(
        {
            "version": VERSION,
            "as_of_ms": paths["as_of_ms"],
            "timezone": "Asia/Shanghai",
            "input_digest": digest(canonical(inputs)),
            "cohorts": inputs["cohorts"],
            "trade_paths": path_report,
            "cases": cases,
            "policy": asdict(policy),
            "requested_archive_minutes": read.requested_minutes,
            "archive_calls": read.calls,
            "trend_case_statuses": dict(Counter(c["trend"]["status"] for c in cases)),
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "profitability_valid": False,
            "limitations": [
                "Actual settlement is separate from all hypothetical residual comparisons.",
                "Archive arrival times and historical native stop confirmation are not proven.",
                "Trend cases require actual residual inventory at a UTC hourly boundary; selection is not representative of all entries.",
                "Missing funding keeps total-net comparison unknown; no fabricated zero funding.",
                "Archive timeout is per request; overall deadline is checked before each bounded request.",
            ],
        }
    )
