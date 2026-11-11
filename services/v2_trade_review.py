"""Read-only factual review and fixed-history admission-filter comparisons.

No venue client, scheduler, policy writes, order authority, or profit forecasts.
Comparison labels deliberately describe observed cohorts, not alternate fills.
"""

import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from services.v2_review_risk import risk_statistics, trade_risk
from services.v2_strategy_analytics import statistics
from v2_core.directional_outcomes import LEGACY_FOLLOWUP_BASIS


def recorded_exit_reason(reason, origin, kind):
    """Attribute only the selected filled close's frozen request evidence.

    External adoption proves a close, not its trigger. Never infer a stop,
    manual action or liquidation from missing reason or from later state.
    """
    if reason:
        return reason
    if origin == "BINANCE_ALGO_CHILD":
        return "VENUE_PROTECTION_CHILD"
    if origin == "BINANCE_EXTERNAL_CLOSE":
        return (
            "EXCHANGE_LIQUIDATION"
            if kind == "LIQUIDATION"
            else "RECONCILED_EXTERNAL_CLOSE"
        )
    return None


def comparisons(trades, *, cooldowns=(5, 10, 20), takeover_scores=(60, 80)):
    """Evaluate each observed candidate against its original historical context.

    The previous real close stays fixed even if an earlier shadow candidate was
    excluded. This is NOT a sequential portfolio simulator or saved-loss claim.
    A missing prior-close observation is unknown, not proof of no prior trade.
    """
    if (
        not isinstance(cooldowns, (tuple, list))
        or not 1 <= len(cooldowns) <= 5
        or any(type(n) is not int or not 1 <= n <= 120 for n in cooldowns)
        or len(set(cooldowns)) != len(cooldowns)
        or not isinstance(takeover_scores, (tuple, list))
        or not 1 <= len(takeover_scores) <= 5
        or any(type(n) is not int or not 0 <= n <= 100 for n in takeover_scores)
        or len(set(takeover_scores)) != len(takeover_scores)
    ):
        raise ValueError("REVIEW_VARIANTS_INVALID")
    variants = [(f"平仓后 {m} 分钟内不再入场", m, None) for m in cooldowns]
    variants += [(f"强趋势接管评分至少 {s}", None, s) for s in takeover_scores]
    variants.append(
        (
            f"组合：冷却 {cooldowns[0]} 分钟＋接管评分 {takeover_scores[0]}",
            cooldowns[0],
            takeover_scores[0],
        )
    )
    result = []
    for name, minutes, score in variants:
        kept, excluded, unknown = [], [], []
        decisions = []
        for row in trades:
            if row["maintenance"]:
                continue
            reasons, missing = [], []
            if minutes is not None:
                gap = row.get("prior_close_gap_ms")
                if gap is None:
                    missing.append("PRIOR_CLOSE_NOT_OBSERVED")
                elif gap < minutes * 60000:
                    reasons.append("REENTRY_COOLDOWN")
            if score is not None:
                mode, value = row.get("entry_mode"), row.get("score")
                if mode is None or (
                    mode == "S6B_TREND_TAKEOVER"
                    and (type(value) is not int or not 0 <= value <= 100)
                ):
                    missing.append("ENTRY_EVIDENCE_MISSING")
                elif mode == "S6B_TREND_TAKEOVER" and value < score:
                    reasons.append("TAKEOVER_SCORE")
            outcome = "EXCLUDED" if reasons else "UNKNOWN" if missing else "KEPT"
            {"EXCLUDED": excluded, "UNKNOWN": unknown, "KEPT": kept}[outcome].append(
                row
            )
            decisions.append(
                {"id": row["id"], "outcome": outcome, "reasons": reasons or missing}
            )
        result.append(
            {
                "label": name,
                "cooldown_minutes": minutes,
                "takeover_min_score": score,
                "known_baseline": statistics(kept + excluded),
                "kept_actual": statistics(kept),
                "excluded_actual": statistics(excluded),
                "unknown_actual": statistics(unknown),
                "kept_count": len(kept),
                "excluded_count": len(excluded),
                "unknown_count": len(unknown),
                "decisions": decisions,
                "simulated_pnl": None,
            }
        )
    return result


def build_review(records, *, start_ms, now_ms, truncated=False):
    """Pure reduction of already scoped records. Future labels never select rows."""
    with localcontext() as ctx:
        ctx.prec = 80
        return _build_review(
            records, start_ms=start_ms, now_ms=now_ms, truncated=truncated
        )


def _build_review(records, *, start_ms, now_ms, truncated):
    rows = sorted(records, key=lambda r: (r["decided_ms"], r["id"]))
    trades = []
    for row in rows:
        if not start_ms <= row["decided_ms"] <= now_ms:
            continue
        item = dict(row)
        item["followup_60_basis"] = LEGACY_FOLLOWUP_BASIS
        item["post_flat_60_market_return_pct"] = None
        # Only a close that both occurred AND became known before this decision
        # can constrain that decision. Never use previous trade's future PnL.
        prior = [
            p
            for p in rows
            if p["symbol"] == row["symbol"]
            and p["id"] != row["id"]
            and p.get("closed_ms") is not None
            and p.get("close_known_ms") is not None
            and p["closed_ms"] <= row["decided_ms"]
            and p["close_known_ms"] <= row["decided_ms"]
        ]
        previous = (
            max(prior, key=lambda r: (r["closed_ms"], r["id"])) if prior else None
        )
        item["prior_episode_id"] = previous["id"] if previous else None
        item["prior_close_gap_ms"] = (
            row["decided_ms"] - previous["closed_ms"] if previous else None
        )
        item["held_minutes"] = (
            str(Decimal(row["closed_ms"] - row["opened_ms"]) / 60000)
            if row.get("closed_ms") is not None
            else None
        )
        # Incomplete/partially closed trades cannot contribute settled PnL.
        if item.get("closed_ms") is None or item["closed_ms"] > now_ms:
            item["net_pnl"] = None
        item["risk"] = trade_risk(item)
        trades.append(item)
    groups = defaultdict(list)
    for row in trades:
        groups[
            (
                row["source"],
                row.get("entry_mode"),
                row["side"],
                row.get("strategy_version"),
                row.get("config_digest"),
            )
        ].append(row)
    return {
        "kind": "FACTUAL_REVIEW_AND_FIXED_HISTORY_FILTERS",
        "as_of": datetime.fromtimestamp(
            now_ms / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "timezone": "Asia/Shanghai",
        "window_start_ms": start_ms,
        "cohort": "按交易意图创建时间取已实际成交的订单；只对完整结算的非维护交易计算净收益。",
        "truncated": truncated,
        "summary": statistics(trades),
        "risk_summary": risk_statistics(trades),
        "trades": trades,
        "groups": [
            {
                "source": k[0],
                "entry_mode": k[1],
                "side": k[2],
                "strategy_version": k[3],
                "config_digest": k[4],
                **statistics(v),
                "risk": risk_statistics(v),
            }
            for k, v in groups.items()
        ],
        "comparisons": comparisons(trades),
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "limitations": [
            "历史平均净收益是样本统计，不是下一单的期望收益预测；无独立同分布假设或校准置信区间，不自动批准策略或提高仓位。",
            "1R 为实际开仓成交数量和金额相对冻结决策止损的毛风险，不是账户收益百分比，也不证明交易所实际保护价；不使用后续移动止损反算。",
            "入场风险偏差按实际成交数量缩放原计划后比较；净亏损超过 1R 可能包含费用和资金费，不直接判定为止损执行故障。",
            "风险证据缺失不补零；R 均值仅统计有完整证据的子样本，不能与全样本均值混用；无亏损样本不输出无限盈亏比。",
            "未可靠记录的目标价保持缺失，不推算计划盈亏比；不使用简化破产概率公式。",
            "对照只是筛选真实历史成交样本；保留/排除的净收益均为真实历史结果，不是影子策略收益或避免的亏损。",
            "未重建资金复用、仓位竞争、成交概率、改变后的入场或退出；不按历史收益推算新仓位利润。",
            "前序平仓上下文固定为实际历史路径；缺少前序记录时冷却判断为未知，不默认放行。",
            "影子门槛是研究假设，不修改运行参数；小样本与参数筛选存在过拟合风险。",
            "旧 T+60 以所有平仓成交的加权均价为基准，可能包含分批止盈，不能解释为最后清仓后的一小时涨跌或卖飞收益。",
            "最终清仓后的市场收益需独立带时间戳的行情锚点和完整路径；没有这些证据时保持未知。15/30 分钟及完整 MFE/MAE 不补值。",
            "未接入行情路径的替代止盈回放，不生成假设成交或收益。",
            "账面净收益已包含结算费用；展示费用不能再次扣除。样本截断不代表全量业绩。",
            "退出归因仅取有成交平仓订单的冻结证据；RECONCILED_EXTERNAL_CLOSE 仅确认已对账外部平仓，不证明人工、止损或其他触发原因。",
        ],
    }


def trade_review(conn, scope, *, days=14, now_ms=None, limit=500):
    if type(days) is not int or days not in {1, 7, 14, 30, 90}:
        raise ValueError("ANALYSIS_WINDOW_INVALID")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("ANALYSIS_LIMIT_INVALID")
    now = time.time_ns() // 1000000 if now_ms is None else now_ms
    if type(now) is not int or now < 0:
        raise ValueError("ANALYSIS_CLOCK_INVALID")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("REVIEW_READ_ONLY_REQUIRED")
    start = max(0, now - days * 86400000)
    # Extra prior-day context is bounded too; cutoffs apply to observation time
    # as well as exchange event time. Select only known, genuinely filled entries.
    rows = conn.execute(
        """WITH candidates AS MATERIALIZED (
      SELECT i.* FROM v2_trade_intents i
      WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)
      AND producer IN ('s6','s8') AND created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
      AND EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
        WHERE o.episode_id=i.intent_id AND o.leg='OPEN' AND f.created_at<=to_timestamp(%s/1000.0) AND f.occurred_at_ms<=%s)
      ORDER BY created_at DESC,intent_id LIMIT %s)
    SELECT i.intent_id::text, i.payload->>'symbol',i.payload->>'side',
      (extract(epoch FROM i.created_at)*1000)::bigint,e.snapshot->>'source',
      e.snapshot->'features'->'evaluation'->'market_plan',
      e.snapshot->'features'->'evaluation'->'source_scoring',
      e.snapshot->'features'->'evaluation'->'sizing',
      e.config_digest,e.strategy_version,
      f.opened_ms,CASE WHEN f.oq=f.cq AND f.terminal THEN f.closed_ms END,
      CASE WHEN f.oq=f.cq AND f.terminal THEN f.known_ms END,
      f.oq::text,f.cq::text,f.onot::text,f.cnot::text,f.fees::text,
      CASE WHEN f.oq=f.cq AND f.terminal THEN s.evidence->>'net_pnl' END,
      cl.request_evidence->>'reason',cl.request_evidence->>'origin',cl.request_evidence->>'kind',
      follow.post_close_return_pct::text,follow.observed_at_ms,
      EXISTS(SELECT 1 FROM v2_orders m WHERE m.episode_id=i.intent_id AND m.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE')
    FROM candidates i JOIN v2_decision_evidence e USING(evidence_ref)
    JOIN LATERAL (SELECT min(z.occurred_at_ms) FILTER(WHERE o.leg='OPEN') opened_ms,
      max(z.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
      (extract(epoch FROM max(greatest(z.created_at,o.updated_at)) FILTER(WHERE o.leg='CLOSE'))*1000)::bigint known_ms,
      (bool_and(o.status IN ('FILLED','CANCELLED','REJECTED') AND o.updated_at<=to_timestamp(%s/1000.0))
       AND NOT EXISTS(SELECT 1 FROM v2_orders pending WHERE pending.episode_id=i.intent_id
         AND pending.status NOT IN ('FILLED','CANCELLED','REJECTED'))) terminal,
      sum(z.quantity) FILTER(WHERE o.leg='OPEN') oq,coalesce(sum(z.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
      sum(z.quantity*z.price) FILTER(WHERE o.leg='OPEN') onot,sum(z.quantity*z.price) FILTER(WHERE o.leg='CLOSE') cnot,
      CASE WHEN bool_and(z.fee_currency='USDT') THEN sum(z.fee) END fees
      FROM v2_orders o JOIN v2_fills z USING(order_id) WHERE o.episode_id=i.intent_id
      AND z.created_at<=to_timestamp(%s/1000.0) AND z.occurred_at_ms<=%s) f ON f.oq>0
    LEFT JOIN LATERAL (SELECT evidence FROM v2_settlements WHERE episode_id=i.intent_id AND currency='USDT'
      AND settled_at<=to_timestamp(%s/1000.0) ORDER BY revision DESC LIMIT 1) s ON true
    LEFT JOIN LATERAL (SELECT o.request_evidence FROM v2_orders o JOIN v2_fills z USING(order_id)
      WHERE o.episode_id=i.intent_id AND o.leg='CLOSE'
      AND o.updated_at<=to_timestamp(%s/1000.0)
      AND z.created_at<=to_timestamp(%s/1000.0) AND z.occurred_at_ms<=%s
      ORDER BY z.occurred_at_ms DESC,z.fill_key DESC LIMIT 1) cl ON true
    LEFT JOIN v2_directional_followups follow ON follow.episode_id=i.intent_id AND follow.horizon_minutes=60
      AND follow.observed_at_ms<=%s AND follow.recorded_at<=to_timestamp(%s/1000.0)
    ORDER BY i.created_at DESC,i.intent_id""",
        (
            *asdict(scope).values(),
            max(0, start - 86400000),
            now,
            now,
            now,
            limit + 1,
            now,
            now,
            now,
            now,
            now,
            now,
            now,
            now,
            now,
        ),
    ).fetchall()
    records = []
    for r in rows[:limit]:
        (
            identity,
            symbol,
            side,
            decided,
            source,
            plan,
            scoring,
            sizing,
            digest,
            version,
            opened,
            closed,
            known,
            oq,
            cq,
            onot,
            cnot,
            fees,
            net,
            reason,
            origin,
            kind,
            follow,
            follow_ms,
            maintenance,
        ) = r
        plan, scoring = plan or {}, scoring or {}
        records.append(
            {
                "id": identity,
                "symbol": symbol,
                "side": side,
                "decided_ms": decided,
                "source": source or "UNKNOWN",
                "entry_mode": plan.get("entry_mode"),
                "score": plan.get("score"),
                "raw_strength": scoring.get("raw_strength"),
                "leverage": plan.get("leverage"),
                "sizing": sizing,
                "config_digest": digest,
                "strategy_version": version,
                "opened_ms": opened,
                "closed_ms": closed,
                "close_known_ms": known,
                "opening_quantity": oq,
                "closing_quantity": cq,
                "opening_notional": onot,
                "closing_notional": cnot,
                "fees_usdt": fees,
                "net_pnl": net,
                "maintenance": maintenance,
                "exit_reason": recorded_exit_reason(reason, origin, kind),
                "followup_60_return_pct": follow,
                "followup_60_observed_ms": follow_ms,
                "followup_15_return_pct": None,
                "followup_30_return_pct": None,
                "mfe": None,
                "mae": None,
            }
        )
    result = build_review(
        records, start_ms=start, now_ms=now, truncated=len(rows) > limit
    )
    result.update(
        window_days=days,
        sample_limit=limit,
        context_lookback_hours=24,
        loaded_records=len(records),
        account_scope=asdict(scope),
    )
    return result
