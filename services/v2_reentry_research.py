"""Bounded read-only reconstruction of actual same-side re-entry decisions.

No alternative fills/portfolio returns, venue calls, policy changes or writes.
Current archived bars are not proof of their historical arrival time.
"""

import time
from collections import Counter

from services.v2_trade_review import trade_review
from v2_core.quant_indicators import aggregate_minutes
from v2_core.reentry import ReentryPolicy, assess_reentry


def review_reentries(
    conn,
    scope,
    *,
    read_bars,
    days=7,
    now_ms=None,
    max_pairs=20,
    policy=None,
    max_seconds=10,
):
    if type(max_pairs) is not int or not 1 <= max_pairs <= 30:
        raise ValueError("REENTRY_RESEARCH_BOUND")
    if type(max_seconds) is not int or not 1 <= max_seconds <= 30:
        raise ValueError("REENTRY_RESEARCH_TIME_BOUND")
    deadline = time.monotonic() + max_seconds
    policy = policy or ReentryPolicy()
    now = time.time_ns() // 1000000 if now_ms is None else now_ms
    review = trade_review(conn, scope, days=days, now_ms=now)
    trades = review["trades"]
    ids = [r["id"] for r in trades]
    # Select timestamps and lifecycle fields only; never dump snapshots/secrets.
    signals = {
        r[0]: r[1:]
        for r in conn.execute(
            """SELECT i.intent_id::text,(e.snapshot->>'observed_at')::bigint,
        e.snapshot->'features'->'signal'->>'episode_id',last_close.price::text
        FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
        LEFT JOIN LATERAL (SELECT f.price FROM v2_orders o JOIN v2_fills f USING(order_id)
          WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
          AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)
          ORDER BY f.occurred_at_ms DESC,f.fill_key DESC LIMIT 1) last_close ON true
        WHERE i.intent_id=ANY(%s::uuid[]) AND
        (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
            (
                now,
                now,
                ids,
                scope.exchange,
                scope.account_id,
                scope.environment,
                scope.product,
            ),
        ).fetchall()
    }
    by_id = {r["id"]: r for r in trades}
    pairs = []
    for row in trades:
        prev = by_id.get(row["prior_episode_id"])
        if (
            prev
            and prev["side"] == row["side"]
            and not prev["maintenance"]
            and not row["maintenance"]
        ):
            pairs.append((prev, row))
    results = []
    width = policy.timeframe_minutes * 60000
    warmup = max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
    for prev, row in pairs[-max_pairs:]:
        known = max(prev["closed_ms"], prev["close_known_ms"])
        observed, signal_episode, _ = signals.get(row["id"], (None, None, None))
        end = row["decided_ms"] // width * width
        start = max(0, (known // width - warmup) * width)
        evidence = {
            "id": row["id"],
            "previous_episode": prev["id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "entry_mode": row["entry_mode"],
            "exit_reason": prev["exit_reason"],
            "gap_ms": row["decided_ms"] - prev["closed_ms"],
            "same_producer_episode": bool(
                signal_episode
                and signal_episode == signals.get(prev["id"], (None, None))[1]
            ),
        }
        if time.monotonic() >= deadline:
            assessment = {"status": "UNKNOWN", "reason": "RESEARCH_BUDGET_EXHAUSTED"}
        elif observed is None:
            assessment = {"status": "UNKNOWN", "reason": "SIGNAL_TIME_MISSING"}
        elif end <= start or end - start > 1440 * 60000:
            assessment = {"status": "UNKNOWN", "reason": "REPLAY_WINDOW_EXCEEDS_BUDGET"}
        else:
            try:
                rows = read_bars(row["symbol"], start, end)
                if len(rows) != (end - start) // 60000:
                    raise ValueError("ARCHIVE_HISTORY_INCOMPLETE")
                bars = aggregate_minutes(
                    list(reversed(rows)),
                    timeframe_minutes=policy.timeframe_minutes,
                    closed_at_ms=end,
                )
                assessment = assess_reentry(
                    bars=bars,
                    side=row["side"],
                    previous_episode=prev["id"],
                    exit_reason=prev["exit_reason"],
                    exit_known_ms=known,
                    signal_observed_ms=observed,
                    as_of_ms=row["decided_ms"],
                    exit_price=signals.get(prev["id"], (None, None, None))[2],
                    policy=policy,
                )
            except ValueError as exc:
                assessment = {"status": "UNKNOWN", "reason": str(exc)}
        # Outcomes are appended AFTER assessment, never supplied to the evaluator.
        results.append(
            {
                **evidence,
                "assessment": assessment,
                "actual_net_pnl": row["net_pnl"],
                "previous_actual_net_pnl": prev["net_pnl"],
            }
        )
    return {
        "kind": "STRUCTURAL_REENTRY_ARCHIVE_RECONSTRUCTION",
        "as_of": review["as_of"],
        "actual_reentry_pairs": len(pairs),
        "evaluated_pairs": len(results),
        "truncated": review["truncated"] or len(pairs) > max_pairs,
        "counts": dict(Counter(r["assessment"]["status"] for r in results)),
        "rows": results,
        "execution_authorized": False,
        "simulated_pnl": None,
        "max_seconds": max_seconds,
        "limitations": [
            "当前归档不证明历史到达时间；这里只做结构回放，不是实时影子验收。",
            "实际净收益只是原有交易标签，不是阻止再入场后的收益或避免的亏损。",
            "只复核实际发生的同向再入场；未重建未成交候选、组合资金复用或替代退出。",
            "阈值尚未校准；UNKNOWN 不是允许开仓。没有启用在线阻断，也没有改止损。",
        ],
    }
