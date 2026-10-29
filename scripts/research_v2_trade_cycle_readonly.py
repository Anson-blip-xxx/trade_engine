"""Bounded, retrospective SANDBOX exit-to-entry diagnosis; never order authority.

Reads the dynamically active account through V2_TRADE_CYCLE_READ_DSN, closes the
repeatable-read PostgreSQL snapshot, then reads the same-environment candle
archive. No venue/private calls, local business storage, configuration changes,
or database writes. Current strict settlement labels are EX-POST outcomes only.
Archive rows lack ingestion timestamps: historical availability is NOT proven.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

MINUTE = 60_000
MAX_PAIRS = 132
MAX_MINUTES = 720
VERSION = "trade-cycle-retrospective-v1"


def milliseconds(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("CYCLE_RESEARCH_CLOCK")
    return value


def amount(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("CYCLE_RESEARCH_AMOUNT")
    number = Decimal(value)
    if not number.is_finite() or (number and not -38 <= number.adjusted() <= 38):
        raise ValueError("CYCLE_RESEARCH_AMOUNT")
    return number


def day_ms(value):
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    )
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("CYCLE_RESEARCH_DATE")
    return int(parsed.timestamp() * 1000)


def display_time(value):
    return datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai")).isoformat()


def settled_rows(rows, *, start_ms, end_ms):
    """Validate actual settlements, with no selection by profit or loss."""
    accepted, excluded = [], Counter()
    identities = set()
    for row in rows:
        if row.get("id") in identities:
            raise ValueError("CYCLE_RESEARCH_DUPLICATE_EPISODE")
        identities.add(row.get("id"))
        if row.get("maintenance"):
            excluded["MAINTENANCE"] += 1
            continue
        if row.get("settlement_state") != "SETTLED":
            excluded["NOT_STRICTLY_SETTLED"] += 1
            continue
        try:
            opened = milliseconds(row.get("opened_ms"))
            closed = milliseconds(row.get("closed_ms"))
            decided = milliseconds(row.get("decided_ms"))
            settled = milliseconds(row.get("settled_ms"))
            amount(row.get("net_pnl"))
            if decided > opened or opened > closed or closed > settled:
                raise ValueError("CYCLE_RESEARCH_TIMING")
            if row.get("side") not in {"BUY", "SELL"}:
                raise ValueError("CYCLE_RESEARCH_SIDE")
        except (ValueError, ArithmeticError, TypeError):
            excluded["INVALID_SETTLEMENT_OR_TIMING"] += 1
            continue
        if not start_ms <= opened < end_ms:
            excluded["OUTSIDE_OPEN_WINDOW"] += 1
            continue
        accepted.append(dict(row))
    return sorted(accepted, key=lambda r: (r["opened_ms"], r["id"])), dict(excluded)


def pair_cycles(rows, *, max_pairs=MAX_PAIRS):
    """Latest factual final close before each next open, including reversals.

    Knowledge-time checks happen separately. Never skip a late-known close in
    favour of an older, more convenient episode. Ties remain UNKNOWN.
    """
    if type(max_pairs) is not int or not 1 <= max_pairs <= MAX_PAIRS:
        raise ValueError("CYCLE_RESEARCH_PAIR_LIMIT")
    pairs, missing = [], Counter()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)
    for symbol_rows in grouped.values():
        for current in symbol_rows:
            prior = [
                row
                for row in symbol_rows
                if row["id"] != current["id"]
                and row["opened_ms"] < current["opened_ms"]
                and row["closed_ms"] <= current["opened_ms"]
            ]
            if not prior:
                missing["NO_PRIOR_FLAT_IN_BOUNDED_SAMPLE"] += 1
                continue
            latest = max(row["closed_ms"] for row in prior)
            candidates = [row for row in prior if row["closed_ms"] == latest]
            overlapping = any(
                row["id"] != current["id"]
                and row["opened_ms"] < current["opened_ms"] < row["closed_ms"]
                for row in symbol_rows
            )
            pairs.append(
                {
                    "current": current,
                    "previous": candidates[0] if len(candidates) == 1 else None,
                    "pair_error": "PREVIOUS_EPISODE_STILL_OPEN"
                    if overlapping
                    else None
                    if len(candidates) == 1
                    else "PRIOR_CLOSE_AMBIGUOUS",
                }
            )
    pairs.sort(key=lambda p: (p["current"]["opened_ms"], p["current"]["id"]))
    return pairs[:max_pairs], {
        "eligible_pairs": len(pairs),
        "sampled_pairs": min(len(pairs), max_pairs),
        "pairs_truncated": len(pairs) > max_pairs,
        "selection": "EARLIEST_CHRONOLOGICAL_WITHOUT_PNL_FILTER",
        "unpaired": dict(missing),
    }


def active_scope(conn):
    """Use only the dedicated research role's existing column-level grants."""
    from v2_core.account_risk import AccountScope

    rows = conn.execute(
        """SELECT a.exchange,a.account_id,a.environment,a.product
        FROM v2_execution_routes r JOIN v2_tenant_accounts a
          ON a.registry_id=r.target_registry AND a.tenant_id=r.tenant_id
        WHERE r.environment='SANDBOX' AND a.environment='SANDBOX'
          AND r.phase='ACTIVE' LIMIT 2"""
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("CYCLE_RESEARCH_UNIQUE_ACTIVE_SANDBOX_REQUIRED")
    return AccountScope(*rows[0])


def load_trades(conn, scope, *, start_ms, as_of_ms):
    """Strict outcomes without importing optional dashboard/follow-up tables."""
    from services.v2_reentry_evaluation import outcomes

    records = conn.execute(
        """WITH candidates AS MATERIALIZED (
          SELECT i.* FROM v2_trade_intents i
          WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
            AND i.producer IN ('s6','s8')
            AND i.created_at>=to_timestamp(%s/1000.0)
            AND i.created_at<=to_timestamp(%s/1000.0)
            AND EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
              WHERE o.episode_id=i.intent_id AND o.leg='OPEN'
                AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0))
          ORDER BY i.created_at DESC,i.intent_id LIMIT 501)
        SELECT i.intent_id::text,i.payload->>'symbol',i.payload->>'side',
          (extract(epoch FROM i.created_at)*1000)::bigint,
          f.opened_ms,f.closed_ms,f.known_ms,
          EXISTS(SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id
            AND o.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE')
        FROM candidates i JOIN LATERAL (
          SELECT min(z.occurred_at_ms) FILTER(WHERE o.leg='OPEN') opened_ms,
            max(z.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
            (extract(epoch FROM max(greatest(z.created_at,o.updated_at))
              FILTER(WHERE o.leg='CLOSE'))*1000)::bigint known_ms
          FROM v2_orders o JOIN v2_fills z USING(order_id)
          WHERE o.episode_id=i.intent_id
            AND z.occurred_at_ms<=%s AND z.created_at<=to_timestamp(%s/1000.0)
        ) f ON f.opened_ms IS NOT NULL
        ORDER BY i.created_at DESC,i.intent_id""",
        (
            *asdict(scope).values(),
            max(0, start_ms - 86_400_000),
            as_of_ms,
            as_of_ms,
            as_of_ms,
            as_of_ms,
            as_of_ms,
        ),
    ).fetchall()
    columns = (
        "id",
        "symbol",
        "side",
        "decided_ms",
        "opened_ms",
        "closed_ms",
        "close_known_ms",
        "maintenance",
    )
    rows = [dict(zip(columns, record, strict=True)) for record in records[:500]]
    labels = {}
    for offset in range(0, len(rows), 100):
        labels.update(
            outcomes(
                conn,
                scope,
                [row["id"] for row in rows[offset : offset + 100]],
                now=as_of_ms,
            )
        )
    for row in rows:
        label = labels.get(row["id"], {"state": "LABEL_UNAVAILABLE"})
        row.update(
            settlement_state=label["state"],
            net_pnl=label.get("net_pnl") if label["state"] == "SETTLED" else None,
            settled_ms=label.get("settled_ms"),
        )
        if label["state"] == "SETTLED":
            row["closed_ms"] = label["closed_ms"]
    return {"trades": rows, "truncated": len(records) > 500}


def collect(conn, *, start_ms, end_ms, as_of_ms, max_pairs=MAX_PAIRS):
    """Only SELECT plus bounded transaction-local settings; no network I/O."""
    from v2_core.strategy import StrategyScope

    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on" or conn.execute(
        "SHOW transaction_isolation"
    ).fetchone()[0] not in {"repeatable read", "serializable"}:
        raise ValueError("CYCLE_RESEARCH_READ_ONLY_SNAPSHOT_REQUIRED")
    for value in (start_ms, end_ms, as_of_ms):
        milliseconds(value)
    if not start_ms < end_ms <= as_of_ms or as_of_ms - start_ms > 7 * 86_400_000:
        raise ValueError("CYCLE_RESEARCH_WINDOW_MAX_7_DAYS")
    conn.execute("SET LOCAL statement_timeout='3s'")
    conn.execute("SET LOCAL lock_timeout='100ms'")
    conn.execute("SET LOCAL jit=off")
    scope = active_scope(conn)
    review = load_trades(conn, scope, start_ms=start_ms, as_of_ms=as_of_ms)
    rows, excluded = settled_rows(review["trades"], start_ms=start_ms, end_ms=end_ms)
    pairs, coverage = pair_cycles(rows, max_pairs=max_pairs)
    ids = sorted({r["id"] for p in pairs for r in (p["current"], p["previous"]) if r})
    inputs = {}
    if ids:
        consumers = [
            StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
        ]
        records = conn.execute(
            """SELECT i.intent_id::text,
              e.snapshot->'features'->'context'->'assembled_at',
              e.snapshot->'features'->'context'->'sources'->'s3'->'observed_at',
              e.snapshot->'observed_at',
              (extract(epoch FROM s.received_at)*1000)::bigint,
              e.snapshot->>'symbol',d.side,e.snapshot->>'source',
              t.price,t.orders,t.known_ms,t.closed_ms,
              cl.request_evidence->>'reason',cl.request_evidence->>'origin'
            FROM v2_trade_intents i
            JOIN v2_strategy_decisions d ON d.decision_id=i.intent_id
              AND d.evidence_ref=i.evidence_ref AND d.signal_id=i.signal_id
              AND d.consumer=ANY(%s) AND d.action='OPEN'
            JOIN v2_decision_evidence e ON e.evidence_ref=i.evidence_ref
            LEFT JOIN v2_inbound_signals s ON s.signal_id=i.signal_id
              AND s.environment=i.environment
            LEFT JOIN LATERAL (
              SELECT max(f.occurred_at_ms) closed_ms FROM v2_orders o
              JOIN v2_fills f USING(order_id)
              WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
                AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)
            ) last_close ON true
            LEFT JOIN LATERAL (
              SELECT round(sum(f.price*f.quantity)/nullif(sum(f.quantity),0),18)::text price,
                count(DISTINCT o.order_id) orders,max(f.occurred_at_ms) closed_ms,
                (extract(epoch FROM max(greatest(f.created_at,o.updated_at)))*1000)::bigint known_ms
              FROM v2_orders o JOIN v2_fills f USING(order_id)
              WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
                AND f.occurred_at_ms=last_close.closed_ms
                AND f.created_at<=to_timestamp(%s/1000.0)
            ) t ON true
            LEFT JOIN LATERAL (
              SELECT o.request_evidence FROM v2_orders o JOIN v2_fills f USING(order_id)
              WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
                AND f.occurred_at_ms=last_close.closed_ms
                AND f.created_at<=to_timestamp(%s/1000.0)
              ORDER BY o.order_id LIMIT 1
            ) cl ON true
            WHERE i.intent_id=ANY(%s::uuid[])
              AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
              AND i.producer IN ('s6','s8')
              AND i.created_at<=to_timestamp(%s/1000.0)
              AND d.created_at<=to_timestamp(%s/1000.0)
              AND e.created_at<=to_timestamp(%s/1000.0)
            LIMIT 265""",
            (
                consumers,
                as_of_ms,
                as_of_ms,
                as_of_ms,
                as_of_ms,
                ids,
                *asdict(scope).values(),
                as_of_ms,
                as_of_ms,
                as_of_ms,
            ),
        ).fetchall()
        columns = (
            "id",
            "as_of_ms",
            "s3_observed_ms",
            "signal_observed_ms",
            "signal_known_ms",
            "symbol",
            "side",
            "source",
            "exit_price",
            "exit_orders",
            "exit_known_ms",
            "exit_ms",
            "exit_reason",
            "exit_origin",
        )
        for record in records:
            item = dict(zip(columns, record, strict=True))
            if item["id"] in inputs:
                raise ValueError("CYCLE_RESEARCH_EVIDENCE_DUPLICATE")
            inputs[item["id"]] = item
    coverage.update(
        loaded_trade_rows=len(review["trades"]),
        strict_settled_rows=len(rows),
        trade_rows_truncated=review["truncated"],
        exclusions=excluded,
        settled_wins=sum(amount(r["net_pnl"]) > 0 for r in rows),
        settled_losses=sum(amount(r["net_pnl"]) < 0 for r in rows),
    )
    return {
        "scope": scope,
        "pairs": pairs,
        "inputs": inputs,
        "coverage": coverage,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "snapshot_ms": as_of_ms,
    }


def summarize(cases):
    result = {"count": len(cases)}
    values = [amount(case["actual_net_pnl_usdt"]) for case in cases]
    with localcontext() as ctx:
        ctx.prec = 80
        result.update(
            wins=sum(value > 0 for value in values),
            losses=sum(value < 0 for value in values),
            breakeven=sum(value == 0 for value in values),
            actual_net_pnl_usdt=str(sum(values, Decimal(0))) if values else None,
            outcome_basis="ACTUAL_ORIGINAL_TRADE_NOT_SIMULATED_OR_AVOIDED_LOSS",
        )
    return result


def subgroup_report(snapshot, cases):
    """Predeclared factual cohorts, not thresholds selected from PnL results."""
    groups = {"same_side_prior_win_within_5m": [], "opposite_side_within_60m": []}
    for pair, case in zip(snapshot["pairs"], cases, strict=True):
        previous, current = pair["previous"], pair["current"]
        if previous is None:
            continue
        gap = current["opened_ms"] - previous["closed_ms"]
        if (
            0 <= gap <= 5 * MINUTE
            and previous["side"] == current["side"]
            and amount(previous["net_pnl"]) > 0
        ):
            groups["same_side_prior_win_within_5m"].append(case)
        if 0 <= gap <= 60 * MINUTE and previous["side"] != current["side"]:
            groups["opposite_side_within_60m"].append(case)
    return {
        name: {
            "overall": summarize(values),
            "states": {
                state: summarize([case for case in values if case["status"] == state])
                for state in ("WAIT", "READY", "UNKNOWN")
            },
            "selection_basis": "ACTUAL_FILLED_TIMES_AND_EX_POST_STRICT_SETTLEMENT",
        }
        for name, values in groups.items()
    }


def replay(
    snapshot,
    *,
    read_bars,
    assess,
    policy,
    cycle_policy,
    max_read_seconds=180,
    clock=time.monotonic,
):
    """Pure orchestration through injected read/assess ports; all cases retained."""
    from v2_core.quant_indicators import Bar

    if type(max_read_seconds) is not int or not 1 <= max_read_seconds <= 400:
        raise ValueError("CYCLE_RESEARCH_READ_BUDGET")
    if len(snapshot["pairs"]) > MAX_PAIRS:
        raise ValueError("CYCLE_RESEARCH_PAIR_LIMIT")
    if policy.timeframe_minutes != 1:
        raise ValueError("CYCLE_RESEARCH_1M_REQUIRED")
    deadline = clock() + max_read_seconds
    cases = []
    for pair in snapshot["pairs"]:
        current, previous = pair["current"], pair["previous"]
        c = snapshot["inputs"].get(current["id"], {})
        p = snapshot["inputs"].get(previous["id"], {}) if previous else {}
        case = {
            "symbol": current["symbol"],
            "side": current["side"],
            "opened_ms": current["opened_ms"],
            "opened_at_utc8": display_time(current["opened_ms"]),
            "closed_ms": current["closed_ms"],
            "previous_side": previous["side"] if previous else None,
            "previous_closed_ms": previous["closed_ms"] if previous else None,
            "actual_net_pnl_usdt": current["net_pnl"],
            "status": "UNKNOWN",
            "reason": "EVIDENCE_UNAVAILABLE",
            "archive_arrival_at_decision_proven": False,
            "orders_authorized": False,
        }
        cases.append(case)
        if pair.get("pair_error"):
            case["reason"] = pair["pair_error"]
            continue
        if not c or not p:
            continue
        try:
            observed, known, as_of, s3 = (
                milliseconds(c[key])
                for key in (
                    "signal_observed_ms",
                    "signal_known_ms",
                    "as_of_ms",
                    "s3_observed_ms",
                )
            )
            closed, exit_known = (
                milliseconds(p[key]) for key in ("exit_ms", "exit_known_ms")
            )
            full_exit_known = milliseconds(previous.get("close_known_ms"))
            case.update(
                signal_observed_ms=observed,
                signal_known_ms=known,
                assessment_as_of_ms=as_of,
                bar_cutoff_ms=s3 // MINUTE * MINUTE,
                prior_exit_known_ms=max(closed, exit_known, full_exit_known),
            )
            if c["symbol"] != current["symbol"] or c["side"] != current["side"]:
                case["reason"] = "DECISION_IDENTITY_MISMATCH"
                continue
            if p["symbol"] != current["symbol"] or p["side"] != previous["side"]:
                case["reason"] = "PRIOR_IDENTITY_MISMATCH"
                continue
            if max(observed, known, s3) > as_of or as_of > current["opened_ms"]:
                case["reason"] = "INPUT_NOT_KNOWN_AT_ORIGINAL_CONTEXT"
                continue
            if (
                closed != previous["closed_ms"]
                or max(closed, exit_known, full_exit_known) > as_of
            ):
                case["reason"] = "PRIOR_CLOSE_NOT_KNOWN_AT_ORIGINAL_CONTEXT"
                continue
            if p["exit_orders"] != 1 or amount(p["exit_price"]) <= 0:
                case["reason"] = "PRIOR_TERMINAL_PRICE_AMBIGUOUS"
                continue
            end = s3 // MINUTE * MINUTE
            warmup = (
                max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
            )
            start = max(
                0,
                end - MAX_MINUTES * MINUTE,
                (max(closed, exit_known, full_exit_known) // MINUTE - warmup) * MINUTE,
            )
            if start >= end:
                case["reason"] = "NO_CLOSED_BAR_WINDOW_AFTER_EXIT"
                continue
            if clock() >= deadline:
                case["reason"] = "ARCHIVE_TOTAL_BUDGET_EXHAUSTED"
                continue
            try:
                raw = read_bars(current["symbol"], start, end)
                if not isinstance(raw, list) or len(raw) != (end - start) // MINUTE:
                    raise ValueError("INCOMPLETE_ARCHIVE")
                bars = [
                    Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
                    for row in sorted(raw, key=lambda row: row["t"])
                ]
                if any(
                    bar.t != start + index * MINUTE for index, bar in enumerate(bars)
                ):
                    raise ValueError("INCOMPLETE_ARCHIVE")
            except Exception:  # noqa: BLE001 - never leak backend/connection details
                case["reason"] = "ARCHIVE_INCOMPLETE_OR_UNAVAILABLE"
                continue
            previous_input = {
                "previous_episode": previous["id"],
                "side": previous["side"],
                "exit_reason": "VENUE_PROTECTION_CHILD"
                if p.get("exit_origin") == "BINANCE_ALGO_CHILD"
                else p.get("exit_reason"),
                "exit_known_ms": max(closed, exit_known, full_exit_known),
                "exit_price": p["exit_price"],
                "exit_net_pnl": previous["net_pnl"]
                if previous["settled_ms"] <= as_of
                else None,
            }
            try:
                assessment = assess(
                    bars=bars,
                    previous=previous_input,
                    side=current["side"],
                    signal_observed_ms=observed,
                    as_of_ms=as_of,
                    bar_cutoff_ms=end,
                    policy=policy,
                    cycle_policy=cycle_policy,
                )
            except Exception:  # noqa: BLE001 - one malformed input cannot hide other cases
                case["reason"] = "ASSESSMENT_UNAVAILABLE_OR_INVALID"
                continue
            if not isinstance(assessment, dict):
                case["reason"] = "ASSESSMENT_CONTRACT_INVALID"
                continue
            status, reason = assessment.get("status"), assessment.get("reason")
            if (
                status not in {"WAIT", "READY", "UNKNOWN"}
                or not isinstance(reason, str)
                or not reason.isascii()
                or not reason.replace("_", "").isalnum()
                or len(reason) > 100
            ):
                case["reason"] = "ASSESSMENT_CONTRACT_INVALID"
                continue
            case.update(
                status=status,
                reason=reason,
                bars=len(bars),
                anchor_truncated=start
                > (previous_input["exit_known_ms"] // MINUTE - warmup) * MINUTE,
            )
        except (ValueError, TypeError, KeyError, ArithmeticError):
            case["reason"] = "INVALID_OR_MISSING_FROZEN_INPUT"
    groups = {
        status: summarize([c for c in cases if c["status"] == status])
        for status in ("WAIT", "READY", "UNKNOWN")
    }
    reasons = Counter((case["status"], case["reason"]) for case in cases)
    return {
        "version": VERSION,
        "environment": "SANDBOX",
        "timezone": "UTC+8",
        "start_ms": snapshot["start_ms"],
        "end_ms": snapshot["end_ms"],
        "snapshot_ms": snapshot["snapshot_ms"],
        "coverage": snapshot["coverage"],
        "policy": asdict(policy),
        "cycle_policy": asdict(cycle_policy),
        "groups": groups,
        "subgroups": subgroup_report(snapshot, cases),
        "sample_digest": hashlib.sha256(
            json.dumps(
                [
                    (
                        pair["previous"]["id"] if pair["previous"] else None,
                        pair["current"]["id"],
                        pair["previous"]["net_pnl"] if pair["previous"] else None,
                        pair["current"]["net_pnl"],
                    )
                    for pair in snapshot["pairs"]
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "reason_counts": [
            {"status": key[0], "reason": key[1], "count": value}
            for key, value in sorted(reasons.items())
        ],
        "cases": cases,
        "net_already_includes_settlement_costs": True,
        "archive_arrival_at_decision_proven": False,
        "limitations": [
            "RETROSPECTIVE_DIAGNOSTIC_NOT_HISTORICAL_LIVE_AVAILABILITY_PROOF",
            "CURRENT_STRICT_SETTLEMENT_LABELS_USED_ONLY_FOR_EX_POST_OUTCOMES",
            "NO_SIMULATED_RETURNS_OR_AVOIDED_LOSS_OR_PARAMETER_PROMOTION",
            "BOUNDED_OPEN_WINDOW_MAY_OMIT_PRIOR_EPISODES_OUTSIDE_WINDOW",
            "CURRENT_ORDER_UPDATE_AFTER_HISTORICAL_DECISION_MEANS_UNKNOWN",
            "ONE_GATE_SAMPLE_DOES_NOT_RECREATE_PORTFOLIO_OR_ALTERNATIVE_ENTRIES",
        ],
        "orders_authorized": False,
        "database_writes": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive UTC+8 YYYY-MM-DD")
    parser.add_argument(
        "--end",
        required=True,
        help="Inclusive UTC+8 YYYY-MM-DD; clipped to DB snapshot",
    )
    parser.add_argument(
        "--max-pairs", type=int, choices=range(1, MAX_PAIRS + 1), default=MAX_PAIRS
    )
    parser.add_argument("--max-read-seconds", type=int, default=180)
    parser.add_argument("--policy-json", default="{}")
    parser.add_argument(
        "--cycle-policy-json",
        default='{"confirmation_bars":2,"max_trigger_age_bars":0}',
    )
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import psycopg

    from services.v2_daily_mover_entry import archive_client
    from services.v2_reentry_archive import ReentryArchive
    from v2_core.reentry import ReentryPolicy
    from v2_core.trade_cycle import CyclePolicy, assess_trade_cycle

    dsn = os.environ.get("V2_TRADE_CYCLE_READ_DSN")
    if not dsn:
        raise ValueError("CYCLE_RESEARCH_READ_DSN_REQUIRED")
    policy = ReentryPolicy(**json.loads(args.policy_json))
    cycle_policy = CyclePolicy(**json.loads(args.cycle_policy_json))
    with psycopg.connect(
        dsn, connect_timeout=3, options="-c default_transaction_read_only=on"
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
        now = conn.execute(
            "SELECT (extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        snapshot = collect(
            conn,
            start_ms=day_ms(args.start),
            end_ms=min(now, day_ms(args.end) + 86_400_000),
            as_of_ms=now,
            max_pairs=args.max_pairs,
        )
    archive = ReentryArchive(factory=archive_client)
    try:
        report = replay(
            snapshot,
            read_bars=archive.read,
            assess=assess_trade_cycle,
            policy=policy,
            cycle_policy=cycle_policy,
            max_read_seconds=args.max_read_seconds,
        )
        # Account switches do not authorize reusing another account's inputs.
        # This is a fresh short read, after all archive I/O has completed.
        with psycopg.connect(
            dsn,
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=3000",
        ) as check:
            check.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
            if active_scope(check) != snapshot["scope"]:
                raise ValueError("CYCLE_RESEARCH_ACTIVE_ACCOUNT_CHANGED")
        report["active_route_unchanged_at_completion"] = True
        root = Path(__file__).resolve().parents[1]
        report["source_hashes"] = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "scripts/research_v2_trade_cycle_readonly.py",
                "v2_core/trade_cycle.py",
                "v2_core/reentry.py",
            )
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        archive.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - CLI must not expose account/DSN/server details
        print(
            json.dumps(
                {"error": "CYCLE_RESEARCH_UNAVAILABLE", "orders_authorized": False}
            )
        )
        raise SystemExit(1) from None
