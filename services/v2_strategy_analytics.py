"""Bounded account-scoped evidence reports; no hypothetical trades as profits."""

import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from statistics import median
from zoneinfo import ZoneInfo

from psycopg.errors import QueryCanceled

from v2_core.directional_outcomes import settlement_amount
from v2_core.state import StateKey
from v2_core.strategy import StrategyScope


def sizing_impact(conn, scope, changes, *, now_ms=None):
    """Replay ONLY sizing on immutable old decisions, not a portfolio backtest.

    Stops, leverage, market/account facts and historical-analysis factors stay
    frozen. Changed quantities cannot be multiplied by realized historical PnL.
    """
    from dataclasses import asdict as to_dict

    from services.v2_directional_replay import source_policy
    from v2_core.directional import DirectionalPlan, size_candidate
    from v2_core.runtime_policy import resolve

    now = time.time_ns() // 1000000 if now_ms is None else now_ms
    consumers = [
        StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
    ]
    records = conn.execute(
        """WITH recent AS MATERIALIZED (
          SELECT d.* FROM unnest(%s::text[]) AS owner(consumer) CROSS JOIN LATERAL (
            SELECT decision_id,evidence_ref,created_at FROM v2_strategy_decisions
            WHERE consumer=owner.consumer AND created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
            ORDER BY created_at DESC,decision_id LIMIT 101
          ) d ORDER BY d.created_at DESC,d.decision_id LIMIT 101)
        SELECT d.decision_id::text,e.snapshot,e.config FROM recent d JOIN v2_decision_evidence e USING(evidence_ref)
        ORDER BY d.created_at DESC,d.decision_id""",
        (consumers, max(0, now - 14 * 86400000), now),
    ).fetchall()
    rows, skipped = [], Counter()
    for identifier, snapshot, config in records[:100]:
        try:
            features = snapshot["features"]
            evaluation, context = (
                features["evaluation"],
                features["context"]["directional"],
            )
            if evaluation.get("market_plan", {}).get("reason") != "CANDIDATE":
                skipped["NOT_SIZED_CANDIDATE"] += 1
                continue
            plan = DirectionalPlan(**evaluation["market_plan"])
            before, _ = source_policy(
                {"signal": plan.event_type, "features": features["signal"]}, config
            )
            after = resolve({**before, **changes})
            args = {
                **context["sizing"],
                "price": context["price"],
                "atr_pct": str(
                    evaluation["source_scoring"]["atr_pct"]
                    if snapshot["source"] == "quant"
                    else context["market"]["1h"]["atr_pct"]
                ),
                "analysis_factor": evaluation["analysis"]["factor"],
            }
            old = size_candidate(plan, **args, policy=before)
            if to_dict(old) != evaluation["sizing"]:
                skipped["ORIGINAL_SIZING_REPLAY_MISMATCH"] += 1
                continue
            proof = {}
            new = size_candidate(plan, **args, policy=after, explanation=proof)
            rows.append(
                {
                    "decision_id": identifier,
                    "symbol": snapshot["symbol"],
                    "source": snapshot["source"],
                    "before": to_dict(old),
                    "after": to_dict(new),
                    "explanation": proof,
                }
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            skipped["INCOMPLETE_OR_INCOMPATIBLE_EVIDENCE"] += 1
    counts = Counter(
        "LARGER"
        if Decimal(r["after"]["notional"]) > Decimal(r["before"]["notional"])
        else "SMALLER"
        if Decimal(r["after"]["notional"]) < Decimal(r["before"]["notional"])
        else "UNCHANGED"
        for r in rows
    )
    return {
        "kind": "FROZEN_EVIDENCE_SIZING_ONLY",
        "window_days": 14,
        "sample_limit": 100,
        "sampled_decisions": min(len(records), 100),
        "truncated": len(records) > 100,
        "replayed": len(rows),
        "skipped": dict(skipped),
        "changes": dict(counts),
        "rows": rows,
        "pnl_comparison": None,
        "execution_authorized": False,
        "limitations": "仅改变选定资金参数；止损、杠杆、账户与行情事实冻结。组合风险和发单预占检查未重建，相关参数预览不变不等于无影响；未模拟后续成交或退出，不推算盈利。",
    }


def statistics(trades):
    with localcontext() as ctx:
        ctx.prec = 80
        return _statistics(trades)


def _statistics(trades):
    settled = [t for t in trades if t["net_pnl"] is not None and not t["maintenance"]]
    values = [settlement_amount(t["net_pnl"]) for t in settled]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    net, gross_win, gross_loss = (
        sum(values, Decimal(0)),
        sum(wins, Decimal(0)),
        -sum(losses, Decimal(0)),
    )
    held = [
        Decimal(t["held_minutes"]) for t in settled if t["held_minutes"] is not None
    ]
    return {
        "settled_trades": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": sum(v == 0 for v in values),
        "net_pnl": str(net),
        "win_rate_pct": str(Decimal(len(wins)) / len(values) * 100) if values else None,
        "mean_net_pnl": str(net / len(values)) if values else None,
        "profit_factor": str(gross_win / gross_loss) if gross_loss else None,
        "profit_factor_status": "AVAILABLE"
        if gross_loss
        else "NO_LOSS_SAMPLE"
        if values
        else "NO_SAMPLE",
        "median_held_minutes": str(median(held)) if held else None,
        "maintenance_excluded": sum(t["maintenance"] for t in trades),
        "unsettled_or_unvalued": sum(t["net_pnl"] is None for t in trades),
    }


def account_analytics(conn, scope, *, days=14, now_ms=None, limit=500):
    from services.v2_equity_analytics import equity_report

    if type(days) is not int or days not in {1, 7, 14, 30, 90}:
        raise ValueError("ANALYSIS_WINDOW_INVALID")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("ANALYSIS_LIMIT_INVALID")
    now = time.time_ns() // 1000000 if now_ms is None else now_ms
    if type(now) is not int or now < 0:
        raise ValueError("ANALYSIS_CLOCK_INVALID")
    start = max(0, now - days * 86400000)
    rows = conn.execute(
        """SELECT i.intent_id::text,i.payload->>'symbol',i.payload->>'side',i.producer,
        e.strategy_version,e.config_digest,e.snapshot,e.config,
        s.evidence->>'net_pnl',f.opened_ms,f.closed_ms,f.fees,
        EXISTS(SELECT 1 FROM v2_orders m WHERE m.episode_id=i.intent_id
          AND m.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE')
        FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
        LEFT JOIN LATERAL (SELECT evidence FROM v2_settlements WHERE episode_id=i.intent_id
          AND currency='USDT' ORDER BY revision DESC LIMIT 1) s ON true
        LEFT JOIN LATERAL (
          SELECT min(z.occurred_at_ms) FILTER(WHERE o.leg='OPEN') AS opened_ms,
            max(z.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') AS closed_ms,
            CASE WHEN count(*)>0 AND bool_and(z.fee_currency='USDT') THEN sum(z.fee) END AS fees
          FROM v2_orders o JOIN v2_fills z USING(order_id) WHERE o.episode_id=i.intent_id
        ) f ON true
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
          AND i.producer IN ('s6','s8') AND COALESCE(f.closed_ms,(extract(epoch FROM i.created_at)*1000)::bigint) BETWEEN %s AND %s
        ORDER BY COALESCE(f.closed_ms,(extract(epoch FROM i.created_at)*1000)::bigint) DESC,i.intent_id LIMIT %s""",
        (*asdict(scope).values(), start, now, limit + 1),
    ).fetchall()
    # Compute existing deterministic state IDs instead of joining/detoasting the
    # entire shared business-state JSON table for every report request.
    exit_keys = {
        StateKey(
            **asdict(scope), namespace="directional-exit-v1", key=r[0]
        ).identity: r[0]
        for r in rows[:limit]
    }
    exit_states = {
        exit_keys[str(key)]: value
        for key, value in conn.execute(
            "SELECT state_id,payload FROM v2_business_state WHERE state_id=ANY(%s::uuid[]) AND NOT deleted",
            (list(exit_keys),),
        ).fetchall()
    }
    trades, groups = [], defaultdict(list)
    for (
        episode,
        symbol,
        side,
        producer,
        version,
        config_digest,
        snapshot,
        config,
        net,
        opened,
        closed,
        fees,
        maintenance,
    ) in rows[:limit]:
        exit_state = exit_states.get(episode)
        features = snapshot.get("features", {})
        evaluation, context = (
            features.get("evaluation", {}),
            features.get("context", {}),
        )
        explanation = evaluation.get("sizing_explanation")
        scoring = evaluation.get("source_scoring", {})
        event = evaluation.get("market_plan", {}).get("event_type", "UNKNOWN")
        key = (
            snapshot.get("source", "UNKNOWN"),
            producer,
            version,
            config_digest,
            context.get("directional", {}).get("regime", "UNKNOWN"),
            event,
            scoring.get("timeframe_minutes"),
            scoring.get("config_digest"),
            scoring.get("strategy_version"),
        )
        trade = {
            "id": episode,
            "symbol": symbol,
            "side": side,
            "source": key[0],
            "producer": producer,
            "strategy_version": version,
            "config_digest": config_digest,
            "regime": key[4],
            "event_type": event,
            "timeframe_minutes": key[6],
            "net_pnl": net,
            "fees_usdt": str(fees) if fees is not None else None,
            "held_minutes": str(Decimal(closed - opened) / 60000)
            if opened is not None and closed is not None and closed >= opened
            else None,
            "maintenance": maintenance,
            "sizing_explanation": explanation,
            "excursions": (exit_state or {}).get("excursions")
            if not (exit_state or {}).get("excursions_error")
            else None,
            "exit_reason": (exit_state or {}).get("decision", {}).get("reason"),
            "policy_version": config.get("policy_version"),
            "explanation_status": "RECORDED" if explanation else "NOT_RECORDED_LEGACY",
        }
        trades.append(trade)
        groups[key].append(trade)
    consumers = [
        StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
    ]
    receipt_rows = conn.execute(
        """WITH recent AS MATERIALIZED (
          SELECT r.* FROM unnest(%s::text[]) AS owner(consumer) CROSS JOIN LATERAL (
            SELECT signal_id,consumer,outcome,reason,completed_at FROM v2_signal_receipts
            WHERE consumer=owner.consumer AND completed_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
            ORDER BY completed_at DESC,signal_id LIMIT 5001
          ) r ORDER BY r.completed_at DESC,r.consumer,r.signal_id LIMIT 5001)
        SELECT s.source,r.consumer,r.outcome,r.reason FROM recent r JOIN v2_inbound_signals s USING(signal_id)
        WHERE s.environment=%s ORDER BY r.completed_at DESC,r.consumer,r.signal_id""",
        (consumers, start, now, scope.environment),
    ).fetchall()
    # Each (consumer,signal_id) is unique, so each grouped receipt also represents
    # one distinct signal WITHIN that consumer; not across multiple consumers.
    counts = Counter(receipt_rows[:5000])
    rejections = [
        (*k, n, n)
        for k, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:100]
    ]
    # Explain all recently evaluated candidates, including those without intents.
    explanations = conn.execute(
        """WITH recent AS MATERIALIZED (
          SELECT d.* FROM unnest(%s::text[]) AS owner(consumer) CROSS JOIN LATERAL (
            SELECT decision_id,evidence_ref,created_at FROM v2_strategy_decisions
            WHERE consumer=owner.consumer AND created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
            ORDER BY created_at DESC,decision_id LIMIT %s
          ) d ORDER BY d.created_at DESC,d.decision_id LIMIT %s)
        SELECT e.snapshot->'features'->'evaluation'->'sizing_explanation'
        FROM recent d JOIN v2_decision_evidence e USING(evidence_ref)
        ORDER BY d.created_at DESC,d.decision_id""",
        (consumers, start, now, limit + 1, limit + 1),
    ).fetchall()
    bindings = Counter()
    for (explanation,) in explanations[:limit]:
        for code in (explanation or {}).get("binding_margin_limits", []):
            bindings[code] += 1
    state = conn.execute(
        "SELECT payload FROM v2_business_state WHERE state_id=%s AND NOT deleted",
        (
            StateKey(
                **asdict(scope), namespace="capital-snapshot-v1", key="latest"
            ).identity,
        ),
    ).fetchone()
    capital = state[0] if state else {}
    try:
        # Optional historical analytics must not poison the surrounding read-only
        # transaction or hide settlement data if a cold query times out.
        with conn.transaction():
            equity = equity_report(conn, scope, start_ms=start, now_ms=now)
    except QueryCanceled:
        equity = {
            "status": "QUERY_TIMEOUT",
            "latest": None,
            "execution_authorized": False,
        }
    return {
        "as_of": datetime.fromtimestamp(
            now / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "window_days": days,
        "equity": equity,
        "window_start_ms": start,
        "timezone": "Asia/Shanghai",
        "cohort": "按最后平仓成交时间；未平仓意图按创建时间；统计只含已结算非维护交易",
        "truncated": len(rows) > limit,
        "sample_limit": limit,
        "summary": statistics(trades),
        "trades": trades,
        "groups": [
            {
                **dict(
                    zip(
                        (
                            "source",
                            "producer",
                            "strategy_version",
                            "config_digest",
                            "regime",
                            "event_type",
                            "timeframe_minutes",
                            "quant_config_digest",
                            "registry_version",
                        ),
                        k,
                        strict=True,
                    )
                ),
                **statistics(v),
            }
            for k, v in groups.items()
        ],
        "receipts": [
            dict(
                zip(
                    (
                        "source",
                        "consumer",
                        "outcome",
                        "reason",
                        "receipts",
                        "distinct_signals",
                    ),
                    r,
                    strict=True,
                )
            )
            for r in rejections
        ],
        "sizing_limits": dict(bindings),
        "receipt_sample_count": min(len(receipt_rows), 5000),
        "receipt_sample_limit": 5000,
        "receipt_sample_truncated": len(receipt_rows) > 5000,
        "receipt_groups_truncated": len(counts) > 100,
        "sizing_sample_count": min(len(explanations), limit),
        "sizing_sample_truncated": len(explanations) > limit,
        "capital": {
            "base": capital.get("capital_health", {}).get("base"),
            "risk_factor": capital.get("capital_health", {}).get("factor"),
            "observed_at_ms": capital.get("started_at_ms"),
            "fresh": capital.get("started_at_ms", now + 1)
            <= now
            < capital.get("valid_until_ms", 0),
        },
        "limitations": [
            "不是收益预测；小样本不支持自动提高仓位。",
            "累计结算损益不是含浮盈浮亏的净值，未提供完整账户最大回撤。",
            "费用明细不得再次从已结算净收益扣除；未估值费用不当作零。",
            "浮盈浮亏极值仅来自退出检查时的标记价采样，不是完整价格路径。",
            "拒绝统计按消费回执分组，跨策略相加不等于独立信号数；尚未评估被拒绝信号的假设收益。",
            "当前列表有样本与查询时间上限；截断时不得当作全量业绩。",
        ],
        "execution_authorized": False,
    }
