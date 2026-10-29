"""Read-only, sampled venue equity. Never treat funding the account as alpha.

The cash import has no continuous coverage certificate. Consequently raw equity
decline is NOT strategy drawdown, and cash-adjusted returns remain unavailable.
"""

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from itertools import pairwise
from zoneinfo import ZoneInfo

from v2_core.ledger import amount
from v2_core.state import StateKey


def _at(ms):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo("Asia/Shanghai")).isoformat()


def equity_report(conn, scope, *, start_ms, now_ms, sample_limit=5000, chart_limit=240):
    if (
        type(start_ms) is not int
        or type(now_ms) is not int
        or not 0 <= start_ms <= now_ms <= 9007199254740991
        or type(sample_limit) is not int
        or not 2 <= sample_limit <= 10000
        or type(chart_limit) is not int
        or not 8 <= chart_limit <= 500
    ):
        raise ValueError("EQUITY_REPORT_BOUNDS_INVALID")
    key = StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    # Bound by indexed state/version BEFORE JSON parsing. Older out-of-window
    # rows count toward the scan bound, which is made visible in the report.
    records = conn.execute(
        "SELECT version,payload,deleted,reason FROM v2_state_history "
        "WHERE state_id=%s ORDER BY version DESC LIMIT %s",
        (key.identity, sample_limit + 1),
    ).fetchall()
    with localcontext() as context:
        context.prec = 80
        return _report(
            conn, scope, records, start_ms, now_ms, sample_limit, chart_limit
        )


def _report(conn, scope, records, start, now, limit, chart_limit):
    excluded, by_time = Counter(), defaultdict(list)
    regression = False
    previous_time = None
    for version, payload, deleted, reason in reversed(records[:limit]):
        try:
            if deleted or reason != "VERIFIED_CAPITAL_SNAPSHOT":
                excluded["DELETED_OR_UNVERIFIED"] += 1
                continue
            if payload.get("account_scope") != asdict(scope):
                excluded["SCOPE_MISMATCH"] += 1
                continue
            at, deadline = payload["started_at_ms"], payload["valid_until_ms"]
            if (
                type(at) is not int
                or type(deadline) is not int
                or not 0 <= at < deadline <= 9007199254740991
                or not isinstance(payload.get("observation_id"), str)
                or not payload["observation_id"]
            ):
                raise ValueError("INVALID_SNAPSHOT")
            if at > now:
                excluded["FUTURE_OBSERVATION"] += 1
                continue
            if previous_time is not None and at < previous_time:
                regression = True
            previous_time = at
            if at < start:
                excluded["BEFORE_WINDOW"] += 1
                continue
            account = payload["account"]
            wallet, equity, used, available = (
                amount(account[k])
                for k in (
                    "totalWalletBalance",
                    "totalMarginBalance",
                    "totalInitialMargin",
                    "availableBalance",
                )
            )
            if min(wallet, used, available) < 0:
                raise ValueError("INVALID_BALANCE")
            # Negative equity is a real possibility; do not hide a deficit.
            point = {
                "at_ms": at,
                "at": _at(at),
                "version": version,
                "wallet": str(wallet),
                "equity": str(equity),
                "unrealized": str(equity - wallet),
                "used_margin": str(used),
                "available_margin": str(available),
                "valid_until_ms": deadline,
                "margin_to_equity_pct": str(used / equity * 100)
                if equity > 0
                else None,
            }
            by_time[at].append(point)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            excluded["INVALID_SNAPSHOT"] += 1
    points = []
    for at in sorted(by_time):
        same = by_time[at]
        amounts = {
            tuple(
                Decimal(p[k])
                for k in ("wallet", "equity", "used_margin", "available_margin")
            )
            for p in same
        }
        if len(amounts) != 1:
            excluded["CONFLICTING_TIMESTAMP"] += len(same)
            continue
        excluded["DUPLICATE_TIMESTAMP"] += len(same) - 1
        points.append(same[-1])
    first, last = (points[0], points[-1]) if points else (None, None)
    peak, decline, peak_at, trough_at = None, Decimal(0), None, None
    for point in points:
        value = Decimal(point["equity"])
        if peak is None or value > peak:
            peak, current_peak_at = value, point["at_ms"]
        if peak > 0 and (peak - value) / peak > decline:
            decline = (peak - value) / peak
            peak_at, trough_at = current_peak_at, point["at_ms"]
    latest_valid = bool(last and records and last["version"] == records[0][0])
    current_fresh = bool(latest_valid and last["at_ms"] <= now < last["valid_until_ms"])
    cash = {
        "coverage_status": "NOT_PROVEN",
        "events": 0,
        "truncated": False,
        "groups": [],
        "wallet_reconciliation_residual": None,
    }
    if len(points) >= 2:
        # Account-wide cash facts, including transfers, NOT episode PnL; never
        # add this total to the already-net trade settlement report.
        incomes = conn.execute(
            "SELECT income_type,currency,amount FROM v2_exchange_income "
            "WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s) "
            "AND occurred_at_ms>%s AND occurred_at_ms<=%s "
            "ORDER BY occurred_at_ms,income_id LIMIT %s",
            (*asdict(scope).values(), first["at_ms"], last["at_ms"], limit + 1),
        ).fetchall()
        groups, total = (
            defaultdict(lambda: {"count": 0, "amount": Decimal(0)}),
            Decimal(0),
        )
        for kind, currency, value in incomes[:limit]:
            groups[kind, currency]["count"] += 1
            groups[kind, currency]["amount"] += value
            if currency == "USDT":
                total += value
        cash.update(
            events=min(len(incomes), limit),
            truncated=len(incomes) > limit,
            groups=[
                {
                    "type": k,
                    "currency": c,
                    "events": v["count"],
                    "amount": str(v["amount"]),
                }
                for (k, c), v in sorted(groups.items())
            ],
            observed_usdt_cash=str(total),
            wallet_reconciliation_residual=str(
                Decimal(last["wallet"]) - Decimal(first["wallet"]) - total
            )
            if len(incomes) <= limit and all(c == "USDT" for _, c, _ in incomes)
            else None,
        )
    return {
        "status": "NO_SAMPLES" if not points else "SAMPLED_ONLY",
        "unit": "VENUE_ACCOUNT_REPORTED_USDT",
        "start_ms": start,
        "as_of": _at(now),
        "sample_limit": limit,
        "scanned_records": min(len(records), limit),
        "scan_truncated": len(records) > limit,
        "samples": len(points),
        "excluded": {k: v for k, v in excluded.items() if v},
        "clock_regression": regression,
        "first_at": first["at"] if first else None,
        "last_at": last["at"] if last else None,
        "latest": last,
        "latest_record_valid": latest_valid,
        "fresh": current_fresh,
        "max_gap_ms": max(
            (b["at_ms"] - a["at_ms"] for a, b in pairwise(points)), default=None
        ),
        "raw_equity_change": str(Decimal(last["equity"]) - Decimal(first["equity"]))
        if len(points) >= 2
        else None,
        "raw_sampled_peak_decline_pct": str(decline * 100)
        if len(points) >= 2 and peak > 0
        else None,
        "decline_peak_at": _at(peak_at) if peak_at is not None else None,
        "decline_trough_at": _at(trough_at) if trough_at is not None else None,
        "strategy_return_pct": None,
        "strategy_drawdown_pct": None,
        "cash": cash,
        "chart": _chart(points, chart_limit),
        "limitations": [
            "账户余额为交易所账户汇总，可能包含手工或其他系统交易；不等于本系统策略净值。",
            "原始净值包含浮动损益，也受充值、提现和划转影响；净值变化与峰值降幅不是策略收益率/最大回撤。",
            "收入采集无连续覆盖证明；未发现转账不代表没有转账，对账残差为零也不证明完整。暂不计算资金流调整收益。",
            "快照时间是采集开始时间，不是平台精确结算切点；边界附近的现金事件也可能造成对账差额，需核对原始证据。",
            "统计来自有界快照采样，缺口不插值；图表按桶保留首尾和净值高低点，指标使用压缩前全部有效样本。",
            "风险预算本金与真实钱包余额是不同口径；保证金占净值比例不代表还能安全开仓的额度。",
        ],
        "execution_authorized": False,
    }


def _chart(points, limit):
    if len(points) <= limit:
        selected = points
    else:
        buckets = max(1, limit // 4)
        selected = []
        for i in range(buckets):
            group = points[
                len(points) * i // buckets : len(points) * (i + 1) // buckets
            ]
            keep = {
                p["at_ms"]: p
                for p in (
                    group[0],
                    group[-1],
                    min(group, key=lambda p: Decimal(p["equity"])),
                    max(group, key=lambda p: Decimal(p["equity"])),
                )
            }
            selected.extend(keep[t] for t in sorted(keep))
    return [{k: p[k] for k in ("at_ms", "at", "equity", "wallet")} for p in selected]
