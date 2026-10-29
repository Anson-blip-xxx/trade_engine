"""Bounded, read-only profit review; labels are facts, never trading authority.

The existing web review is left on its current permission/query contract. This
offline entry explicitly requires the stronger current-revision label reads.
No parameter fitting, venue requests, database writes, or model calls occur.
"""

from collections import Counter, defaultdict
from decimal import Decimal, localcontext

from services.v2_reentry_evaluation import outcomes
from services.v2_trade_review import build_review, comparisons, trade_review
from v2_core.account_risk import AccountScope
from v2_core.quant_indicators import decimal


def strict_trade_review(conn, scope, *, days=14, now_ms, limit=500):
    """At most five extra bounded label queries, all on the caller's snapshot.

    Failure propagates; a timeout or missing read permission must never masquerade
    as zero PnL or an empty account. OPEN/unknown labels are explicitly missing.
    """
    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise ValueError("PROFIT_REVIEW_SANDBOX_SCOPE_REQUIRED")
    review = trade_review(conn, scope, days=days, now_ms=now_ms, limit=limit)
    labels = {}
    rows = review["trades"]
    for start in range(0, len(rows), 100):
        labels.update(
            outcomes(
                conn, scope, [r["id"] for r in rows[start : start + 100]], now=now_ms
            )
        )
    checked = []
    for row in rows:
        label = labels.get(row["id"], {"state": "LABEL_UNAVAILABLE", "net_pnl": None})
        checked.append(
            {
                **row,
                "net_pnl": label["net_pnl"] if label["state"] == "SETTLED" else None,
                "closed_ms": label.get("closed_ms")
                if label["state"] == "SETTLED"
                else row.get("closed_ms"),
                "close_known_ms": (row.get("close_known_ms") or label.get("settled_ms"))
                if label["state"] == "SETTLED"
                else row.get("close_known_ms"),
                "settlement_state": label["state"],
                "settled_ms": label.get("settled_ms"),
                "settlement_revision": label.get("settlement_revision"),
            }
        )
    # Recompute statistics/risk after invalid labels are removed. Preserve prior
    # historical context from the original bounded query, not the window subset.
    result = build_review(
        checked,
        start_ms=review["window_start_ms"],
        now_ms=now_ms,
        truncated=review["truncated"],
    )
    for row, original in zip(result["trades"], checked, strict=True):
        row["prior_episode_id"] = original.get("prior_episode_id")
        row["prior_close_gap_ms"] = original.get("prior_close_gap_ms")
    result["comparisons"] = comparisons(result["trades"])
    for key in (
        "window_days",
        "sample_limit",
        "context_lookback_hours",
        "loaded_records",
        "account_scope",
    ):
        result[key] = review[key]
    result["settlement_label_contract"] = (
        "CURRENT_ACCOUNTING_REVISION_COMPLETE_USDT_SETTLEMENT"
    )
    result["as_of_ms"] = now_ms
    result["label_query_batches"] = (len(rows) + 99) // 100
    result["settlement_states"] = dict(Counter(r["settlement_state"] for r in checked))
    return result


def _net(row):
    if row.get("settlement_state") != "SETTLED" or row.get("maintenance"):
        return None
    try:
        return decimal(row["net_pnl"])
    except (ValueError, TypeError, KeyError):
        return None


def _gross(row):
    try:
        oq, cq = decimal(row["opening_quantity"]), decimal(row["closing_quantity"])
        opened, closed = (
            decimal(row["opening_notional"]),
            decimal(row["closing_notional"]),
        )
        if (
            oq <= 0
            or oq != cq
            or min(opened, closed) <= 0
            or row["side"] not in {"BUY", "SELL"}
        ):
            return None
        return (closed - opened) * (1 if row["side"] == "BUY" else -1)
    except (ValueError, TypeError, KeyError):
        return None


def _summary(rows, minimum):
    values = [v for row in rows if (v := _net(row)) is not None]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    gains, lost = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
    pairs = [(r, _net(r), _gross(r)) for r in rows if _net(r) is not None]
    complete_gross = [(net, gross) for _, net, gross in pairs if gross is not None]
    fees = []
    for row, _, _ in pairs:
        try:
            fees.append(decimal(row["fees_usdt"]))
        except (ValueError, TypeError, KeyError):
            pass
    mean_win = gains / len(wins) if wins else None
    mean_loss = lost / len(losses) if losses else None
    return {
        "observed": len(rows),
        "settled": len(values),
        "unvalued": len(rows) - len(values),
        "sample_status": "DESCRIPTIVE_ONLY"
        if len(values) >= minimum
        else "INSUFFICIENT_SAMPLE",
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(values) - len(wins) - len(losses),
        "net_pnl_usdt": str(sum(values, Decimal(0))) if values else None,
        "mean_net_usdt": str(sum(values, Decimal(0)) / len(values)) if values else None,
        "win_rate_pct": str(Decimal(len(wins)) * 100 / len(values)) if values else None,
        "average_win_usdt": str(mean_win) if mean_win is not None else None,
        "average_loss_usdt": str(mean_loss) if mean_loss is not None else None,
        "profit_factor": str(gains / lost) if lost else None,
        "payoff_ratio": str(mean_win / mean_loss)
        if mean_win is not None and mean_loss
        else None,
        "gross_complete_count": len(complete_gross),
        "gross_pnl_usdt": str(sum((v[1] for v in complete_gross), Decimal(0)))
        if complete_gross
        else None,
        "net_minus_gross_usdt": str(sum((n - g for n, g in complete_gross), Decimal(0)))
        if complete_gross
        else None,
        "fee_complete_count": len(fees),
        "observed_fees_usdt": str(sum(fees, Decimal(0))) if fees else None,
        "settlement_states": dict(
            Counter(r.get("settlement_state", "UNKNOWN") for r in rows)
        ),
    }


def build_profit_capture_review(review, *, holdout_start_ms, min_sample_size=30):
    """Caller supplies a chronological cutoff; no PnL-based split selection.

    Training labels must have actually settled before the cutoff. Cross-boundary
    trades are embargoed, not silently counted as losses or holdout trades.
    The cutoff is recorded, not claimed to have been prospectively registered.
    """
    scope = review.get("account_scope", {})
    if (
        scope.get("environment") != "SANDBOX"
        or review.get("settlement_label_contract")
        != "CURRENT_ACCOUNTING_REVISION_COMPLETE_USDT_SETTLEMENT"
    ):
        raise ValueError("PROFIT_REVIEW_VALIDATED_SCOPE_REQUIRED")
    now, start = review.get("as_of_ms"), review.get("window_start_ms")
    if (
        type(now) is not int
        or type(start) is not int
        or type(holdout_start_ms) is not int
        or not start < holdout_start_ms <= now
    ):
        raise ValueError("PROFIT_REVIEW_SPLIT_INVALID")
    if type(min_sample_size) is not int or not 1 <= min_sample_size <= 500:
        raise ValueError("PROFIT_REVIEW_SAMPLE_BOUND")
    records = review.get("trades")
    if not isinstance(records, list) or len(records) > 500:
        raise ValueError("PROFIT_REVIEW_RECORD_BOUND")
    ids, partitioned, excluded = set(), [], Counter()
    for row in records:
        identity = row.get("id")
        if not isinstance(identity, str) or identity in ids:
            raise ValueError("PROFIT_REVIEW_DUPLICATE_OR_INVALID_ID")
        ids.add(identity)
        decided, closed, settled = (
            row.get("decided_ms"),
            row.get("closed_ms"),
            row.get("settled_ms"),
        )
        if row.get("maintenance"):
            excluded["MAINTENANCE"] += 1
            continue
        if (
            row.get("side") not in {"BUY", "SELL"}
            or type(decided) is not int
            or not start <= decided <= now
        ):
            excluded["INVALID_DECISION"] += 1
            continue
        if _net(row) is not None and (
            type(closed) is not int
            or type(settled) is not int
            or not decided <= closed <= settled <= now
        ):
            excluded["INVALID_LABEL_TIMING"] += 1
            continue
        if decided >= holdout_start_ms:
            period = "HOLDOUT"
        elif (
            type(settled) is int
            and settled < holdout_start_ms
            and _net(row) is not None
        ):
            period = "TRAIN"
        else:
            excluded["EMBARGOED_UNAVAILABLE_AT_CUTOFF"] += 1
            continue
        partitioned.append((period, row))
    with localcontext() as ctx:
        ctx.prec = 80
        groups = defaultdict(list)
        for period, row in partitioned:
            key = (
                period,
                row["side"],
                row.get("entry_mode") or "UNKNOWN",
                row.get("strategy_version") or "UNKNOWN",
                row.get("config_digest") or "UNKNOWN",
            )
            groups[key].append(row)
        summaries = {
            period: _summary(
                [r for p, r in partitioned if p == period], min_sample_size
            )
            for period in ("TRAIN", "HOLDOUT")
        }
        return {
            "kind": "FACTUAL_PROFIT_CAPTURE_TEMPORAL_REVIEW",
            "as_of_ms": now,
            "timezone": "Asia/Shanghai",
            "window_start_ms": start,
            "holdout_start_ms": holdout_start_ms,
            "split_basis": "CALLER_FIXED_TIME_NOT_PNL_OPTIMIZED",
            "cutoff_prospectively_registered": False,
            "minimum_descriptive_sample": min_sample_size,
            "source_truncated": bool(review.get("truncated")),
            "excluded": dict(excluded),
            "periods": summaries,
            "sides": [
                {
                    "period": period,
                    "side": side,
                    **_summary(
                        [
                            r
                            for p, r in partitioned
                            if p == period and r["side"] == side
                        ],
                        min_sample_size,
                    ),
                }
                for period in ("TRAIN", "HOLDOUT")
                for side in ("BUY", "SELL")
            ],
            "groups": [
                {
                    **dict(
                        zip(
                            (
                                "period",
                                "side",
                                "entry_mode",
                                "strategy_version",
                                "config_digest",
                            ),
                            key,
                        )
                    ),
                    **_summary(rows, min_sample_size),
                }
                for key, rows in sorted(groups.items())
            ],
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "hypothetical_pnl": None,
            "profit_improvement_proven": False,
            "limitations": [
                "净收益只采用当前账本版本且完整的 USDT 结算，费用与资金费已包含，不再次扣费。",
                "毛收益与净减毛仅对数量匹配、金额完整的子样本统计；缺失费用不补零。",
                "训练标签必须在切分点前已知，跨边界或未结算记录隔离；保持时序并不证明独立同分布或消除行情变化。",
                "按方向、入场模式、策略和配置版本分组，不用同一批收益优化门槛，也不将样本不足解读为无效策略。",
                "样本外是时间分割标签而非真实预注册证明；这是实成交复盘，不是替代策略回测或收益承诺。",
            ],
        }


def profit_capture_review(
    conn, scope, *, now_ms, holdout_start_ms, days=14, limit=500, min_sample_size=30
):
    review = strict_trade_review(conn, scope, days=days, now_ms=now_ms, limit=limit)
    return build_profit_capture_review(
        review, holdout_start_ms=holdout_start_ms, min_sample_size=min_sample_size
    )
