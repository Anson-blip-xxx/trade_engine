"""Opt-in offline enrichment. No web/consumer wiring, writes, or venue access.

Load scoped fills inside a short read-only PG snapshot, CLOSE that transaction,
then read bounded archive pages. Never hold PG transactions across archive I/O.
"""

from collections import Counter
from dataclasses import asdict
from fractions import Fraction

from services.v2_profit_capture_review import strict_trade_review
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import MINUTE, Bar, decimal
from v2_core.trade_path import PathFill, analyze_path, fill_window, number


def load_path_inputs(conn, scope, *, now_ms, days=7, max_trades=20):
    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise ValueError("PATH_SANDBOX_SCOPE_REQUIRED")
    if type(max_trades) is not int or not 1 <= max_trades <= 30:
        raise ValueError("PATH_BATCH_BOUND")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("PATH_READ_ONLY_REQUIRED")
    review = strict_trade_review(conn, scope, days=days, now_ms=now_ms, limit=500)
    eligible = [
        row
        for row in review["trades"]
        if row["closed_ms"] is not None
        and row["net_pnl"] is not None
        and not row["maintenance"]
    ]
    selected = sorted(eligible, key=lambda r: (r["opened_ms"], r["id"]), reverse=True)[
        :max_trades
    ]
    cases = []
    for row in selected:
        fills = conn.execute(
            """SELECT f.fill_key,f.occurred_at_ms,o.leg,f.quantity::text,f.price::text
            FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
            JOIN v2_fills f USING(order_id)
            WHERE i.intent_id=%s AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
              AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)
            ORDER BY f.occurred_at_ms,f.fill_key LIMIT 1001""",
            (row["id"], *asdict(scope).values(), now_ms, now_ms),
        ).fetchall()
        cases.append(
            {
                "trade": row,
                "fills": [
                    dict(
                        zip(
                            ("identity", "t", "leg", "quantity", "price"),
                            f,
                            strict=True,
                        )
                    )
                    for f in fills
                ],
            }
        )
    return {
        "kind": "OFFLINE_FACTUAL_TRADE_PATH_REVIEW",
        "account_scope": asdict(scope),
        "as_of_ms": now_ms,
        "as_of": review["as_of"],
        "window_days": days,
        "cohort": review["cohort"],
        "source_records_truncated": review["truncated"],
        "eligible_in_loaded_cohort": len(eligible),
        "selected_count": len(cases),
        "selection_truncated": len(eligible) > len(cases),
        "selection": "LATEST_OPENED_SETTLED_NON_MAINTENANCE_WITHIN_INTENT_WINDOW",
        "cases": cases,
    }


def post_flat_path(bars, *, side, closed_ms, as_of_ms, horizon_minutes=60, anchor=None):
    """Factual market path after flat, never hypothetical PnL or exit VWAP.

    `bars` must be complete, ordered, fully closed 1m bars from the first
    minute whose open is at/after the anchor/flat time through the first minute
    ending at/after flat+horizon. With no explicit mark, the close of the first
    fully-after-flat minute is the anchor. That omitted boundary is reported.

    An explicit mark is {kind: MARKET_MARK, t, price, observed_ms, source}; it
    must be at/after flat and no more than two minutes late. Source provenance
    is caller-supplied, not independently authenticated by this pure function.
    """
    if (
        side not in {"BUY", "SELL"}
        or type(closed_ms) is not int
        or type(as_of_ms) is not int
        or not 0 <= closed_ms <= as_of_ms
        or type(horizon_minutes) is not int
        or not 1 <= horizon_minutes <= 1440
    ):
        raise ValueError("POST_FLAT_PARAMETERS_INVALID")
    result = {
        "status": "UNKNOWN",
        "basis": None,
        "return_pct": None,
        "favorable_excursion_pct": None,
        "adverse_excursion_pct": None,
        "flat_ms": closed_ms,
        "requested_horizon_minutes": horizon_minutes,
        "anchor_ms": None,
        "anchor_delay_ms": None,
        "observed_end_ms": None,
        "market_return_not_trade_pnl": True,
        "costs_included": False,
        "counterfactual_execution_proven": False,
    }
    target_end = (
        (closed_ms + horizon_minutes * MINUTE + MINUTE - 1) // MINUTE
    ) * MINUTE
    if target_end > as_of_ms:
        return {**result, "status": "HORIZON_NOT_YET_OBSERVABLE"}
    try:
        if (
            not isinstance(bars, (list, tuple))
            or not len(bars) <= 1442
            or any(not isinstance(b, Bar) for b in bars)
        ):
            raise ValueError("INVALID_BARS")
        anchor_time = closed_ms
        if anchor is not None:
            if not isinstance(anchor, dict) or anchor.get("kind") != "MARKET_MARK":
                raise ValueError("INVALID_ANCHOR")
            anchor_time, observed = anchor.get("t"), anchor.get("observed_ms")
            source = anchor.get("source")
            if (
                type(anchor_time) is not int
                or type(observed) is not int
                or not closed_ms
                <= anchor_time
                <= min(closed_ms + 2 * MINUTE, target_end)
                or not anchor_time <= observed <= as_of_ms
                or not isinstance(source, str)
                or not 1 <= len(source) <= 128
            ):
                raise ValueError("INVALID_ANCHOR")
            price = decimal(anchor["price"])
            if price <= 0:
                raise ValueError("INVALID_ANCHOR")
        first = ((anchor_time + MINUTE - 1) // MINUTE) * MINUTE
        expected = list(range(first, target_end, MINUTE))
        if (
            not expected
            or [b.t for b in bars] != expected
            or any(b.t + MINUTE > as_of_ms for b in bars)
        ):
            return {**result, "status": "INCOMPLETE_OR_INVALID_MINUTE_PATH"}
        if anchor is None:
            anchor_time, price = bars[0].t + MINUTE, bars[0].c
            basis = "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE"
            proof = {"source": "CALLER_CLOSED_1M_ARCHIVE", "bar": bars[0].payload()}
            path = bars[1:]  # first minute high/low precedes its close anchor
        else:
            basis, proof, path = "EXPLICIT_TIMED_MARK_AFTER_FLAT", dict(anchor), bars
        if target_end <= anchor_time:
            return {**result, "status": "NO_POST_ANCHOR_OBSERVATION"}
        sign = 1 if side == "BUY" else -1
        base = Fraction(price)
        returns = [Fraction(0)] + [
            sign * (Fraction(p) / base - 1) * 100 for b in path for p in (b.h, b.l, b.c)
        ]
        return {
            **result,
            "status": "OBSERVED",
            "basis": basis,
            "return_pct": number(sign * (Fraction(bars[-1].c) / base - 1) * 100),
            "favorable_excursion_pct": number(max(returns)),
            "adverse_excursion_pct": number(min(returns)),
            "anchor_ms": anchor_time,
            "anchor_delay_ms": anchor_time - closed_ms,
            "observed_end_ms": target_end,
            "horizon_delay_ms": target_end - (closed_ms + horizon_minutes * MINUTE),
            "observed_duration_ms": target_end - anchor_time,
            "anchor_price": str(price),
            "end_price": str(bars[-1].c),
            "anchor_provenance": proof,
            "path_digest": digest(
                canonical(
                    {
                        "side": side,
                        "flat_ms": closed_ms,
                        "anchor": proof,
                        "bars": [b.payload() for b in bars],
                    }
                )
            ),
            "availability_at_decision_proven": False,
        }
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return {**result, "status": "INVALID_ANCHOR_OR_PATH"}


def enrich_path_inputs(inputs, *, read_bars, max_minutes=2880, max_total_minutes=10080):
    """Input is frozen PG evidence, not a live cursor or web request context."""
    if (
        inputs.get("kind") != "OFFLINE_FACTUAL_TRADE_PATH_REVIEW"
        or inputs.get("account_scope", {}).get("environment") != "SANDBOX"
        or not isinstance(inputs.get("cases"), list)
        or len(inputs["cases"]) > 30
        or type(max_minutes) is not int
        or not 1 <= max_minutes <= 10080
        or type(max_total_minutes) is not int
        or not 1 <= max_total_minutes <= 30240
    ):
        raise ValueError("PATH_REVIEW_BOUNDS")
    reports, consumed = [], 0
    for case in inputs["cases"]:
        row, encoded = case["trade"], case["fills"]
        record = {
            "trade_id": row["id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "source": row["source"],
            "strategy_version": row["strategy_version"],
            "config_digest": row["config_digest"],
            "settled_net_pnl_usdt": row["net_pnl"],
        }
        try:
            fills = [PathFill(**f) for f in encoded]
            ordered, start, end = fill_window(fills, as_of_ms=inputs["as_of_ms"])
            if ordered[0].t != row["opened_ms"] or ordered[-1].t != row["closed_ms"]:
                raise ValueError("FILL_WINDOW_MISMATCH")
            for leg, prefix in (("OPEN", "opening"), ("CLOSE", "closing")):
                group = [f for f in fills if f.leg == leg]
                if sum((Fraction(f.quantity) for f in group), Fraction(0)) != Fraction(
                    row[prefix + "_quantity"]
                ):
                    raise ValueError("FILL_QUANTITY_MISMATCH")
                if sum(
                    (Fraction(f.quantity) * Fraction(f.price) for f in group),
                    Fraction(0),
                ) != Fraction(row[prefix + "_notional"]):
                    raise ValueError("FILL_NOTIONAL_MISMATCH")
            minutes = max(0, (end - start) // MINUTE)
            if minutes > max_minutes:
                record["path"] = {"status": "TRADE_HISTORY_BUDGET_EXCEEDED"}
            elif consumed + minutes > max_total_minutes:
                record["path"] = {"status": "BATCH_HISTORY_BUDGET_EXCEEDED"}
            else:
                consumed += minutes  # failed queries still consume research budget
                bars = []
                try:
                    for page_start in range(start, end, 120 * MINUTE):
                        page_end = min(end, page_start + 120 * MINUTE)
                        raw = read_bars(row["symbol"], page_start, page_end)
                        if not isinstance(raw, list) or len(raw) > 120:
                            raise ValueError("ARCHIVE_PAGE_BOUND")
                        page = [
                            Bar(**{k: b[k] for k in ("t", "o", "h", "l", "c", "v")})
                            for b in raw
                        ]
                        if any(not page_start <= b.t < page_end for b in page):
                            raise ValueError("ARCHIVE_PAGE_WINDOW")
                        bars.extend(sorted(page, key=lambda b: b.t))
                except Exception:  # noqa: BLE001 - no raw DB/transport messages
                    record["path"] = {"status": "ARCHIVE_UNAVAILABLE_OR_INVALID"}
                else:
                    record["path"] = analyze_path(
                        fills, bars, side=row["side"], as_of_ms=inputs["as_of_ms"]
                    )
                    record["input_digest"] = digest(
                        canonical(
                            {
                                "scope": inputs["account_scope"],
                                "as_of_ms": inputs["as_of_ms"],
                                "trade_id": row["id"],
                                "symbol": row["symbol"],
                                "side": row["side"],
                                "strategy_version": row["strategy_version"],
                                "config_digest": row["config_digest"],
                                "fills": encoded,
                                "bars": [b.payload() for b in bars],
                            }
                        )
                    )
                    record["net_minus_gross_usdt"] = number(
                        Fraction(row["net_pnl"])
                        - Fraction(record["path"]["realized_gross_usdt"])
                    )
        except (ValueError, TypeError, KeyError, ArithmeticError):
            record["path"] = {"status": "INVALID_OR_INCOMPLETE_FILL_EVIDENCE"}
        reports.append(record)
    return {
        **{k: v for k, v in inputs.items() if k != "cases"},
        "reports": reports,
        "status_counts": dict(Counter(r["path"]["status"] for r in reports)),
        "requested_minutes": consumed,
        "limits": {
            "max_trade_minutes": max_minutes,
            "max_batch_minutes": max_total_minutes,
        },
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "limitations": [
            "毛路径=累计已实现毛收益+剩余仓位按行情估值；费用和资金费不计入路径极值，净收益直接取实际结算，不再次扣费。",
            "MFE/MAE 是完整且仓位不变分钟的高低价与成交点得到的下界，不是逐笔精确极值，也不是可成交利润。",
            "开平仓边界与分钟内仓位变化处不使用整根 K 线高低价；缺失行情不补值、不按剩余片段输出路径指标。",
            "同毫秒多价格成交组只用于精确记账，不假定组内价格顺序；该组价格不用于路径极值。",
            "回吐按毛口径计算；捕获比例的分母是已观察峰值，不能当完整行情的真实利润捕获率；负值保留。",
            "无进展是连续分钟收盘采样未创新高，不是盘中精确停滞时长；变化分钟中断计数。",
            "历史归档是事后读取，未证明每根数据在当时已到达；这些标签不得反馈当时的准入或作为样本外收益证明。",
            "按最近开仓且已结算的有限样本选择，开放仓位及截断样本不在本报告，不能当全账户业绩。",
        ],
    }
