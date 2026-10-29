"""Pure, bounded daily-mover research reports and private-digest formatting.

No I/O, execution ports, automatic tuning or model calls. Ex-post extremes are
not achievable profits; a SANDBOX fill never becomes a LIVE return denominator.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from statistics import median
from zoneinfo import ZoneInfo

from v2_core.evidence import canonical, digest

VERSION = "daily-mover-report-v1"
DAY = 86_400_000
MINIMUM_DAYS = 7
MINIMUM_CASES = 30
_ZONE = ZoneInfo("Asia/Shanghai")
_STAGE_TEXT = {
    "MATCHED_DIRECTION_FILLED": "已有同向成交",
    "OTHER_DIRECTION_ONLY": "仅有反向成交",
    "CANDIDATE_NOT_FILLED": "有候选但无成交",
    "CANDIDATE_EXECUTION_UNKNOWN": "有候选，成交证据未知",
    "JEV_VETO": "JEV 否决",
    "JEV_UNAVAILABLE_OR_SKIPPED": "JEV 不可用/跳过",
    "CANDIDATE_DATA_UNAVAILABLE": "候选数据不可用",
    "SIGNAL_PREFILTER_REJECTED": "信号预筛拒绝",
    "MARKET_FILTER_REJECTED": "行情过滤拒绝",
    "SIZING_RISK_REJECTED": "仓位风控拒绝",
    "EXECUTION_MARKET_RISK_REJECTED": "执行行情风控拒绝",
    "NO_ENTRY_CANDIDATE": "未形成入场候选",
    "OTHER_RECORDED_REJECTION": "其他已记录拒绝",
    "EXPIRED_NOT_STRATEGY_REJECTION": "信号已过期",
    "ACCOUNT_DECISION_NOT_OBSERVED": "未查到本账户决策",
    "RECEIPT_WITHOUT_DECISION_EVIDENCE": "回执缺少决策证据",
    "DECISION_EVIDENCE_UNAVAILABLE": "决策证据不可用",
    "NO_RETAINED_SIGNAL_UNKNOWN": "未查到保留信号（未知）",
    "EVIDENCE_UNAVAILABLE": "账户证据不可用",
    "ENGINE_SYMBOL_UNSUPPORTED": "引擎暂不支持该符号",
    "MIXED_RECORDED_REASONS": "多种已记录拒绝",
}
_NUMERIC_TRADE = (
    "opening_quantity",
    "closing_quantity",
    "remaining_quantity",
    "entry_vwap",
    "exit_fill_vwap",
    "fees_usdt",
    "gross_pnl_usdt",
)
_NUMERIC_FEATURE = (
    "fixed_day_return_pct",
    "range_pct_of_open",
    "high",
    "low",
    "close_location_in_range_pct",
    "favorable_close_step_pct",
    "best_ordered_close_run_pct",
    "base_volume",
    "largest_hour_volume_share_pct",
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def seal_report(report):
    """Re-seal detached report after explicitly authorized evidence enrichment."""
    if not isinstance(report, dict) or report.get("version") != VERSION:
        raise ValueError("DAILY_REPORT_VERSION_REQUIRED")
    clean = json.loads(
        canonical({k: v for k, v in report.items() if k != "content_digest"})
    )
    clean["content_digest"] = _digest(clean)
    return clean


def _number(value):
    if value is None:
        return None
    if type(value) not in (str, int, Decimal) or len(str(value)) > 100:
        raise ValueError("DAILY_REPORT_NUMBER_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("DAILY_REPORT_NUMBER_INVALID") from None
    if not parsed.is_finite() or (parsed and not -40 <= parsed.adjusted() <= 40):
        raise ValueError("DAILY_REPORT_NUMBER_INVALID")
    return parsed


def _numeric(value):
    parsed = _number(value)
    return None if parsed is None else format(parsed, "f")


def _code(value, default="UNKNOWN"):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_ .:/-]{1,160}", value)
        else default
    )


def _counts(value):
    if not isinstance(value, dict) or len(value) > 100:
        return {}
    result = Counter()
    for key, count in value.items():
        if type(count) is int and 0 <= count <= 1_000_000:
            result[_code(key)] += count
    return dict(sorted(result.items()))


def _coverage(raw):
    """Keep bounded sampling provenance; unknown bucket counts remain null."""
    result = {
        key: _code(raw.get(key))
        for key in (
            "signals",
            "intents",
            "retention_completeness",
            "account_route_active_for_full_day",
            "older_carry_in",
            "signal_sample_basis",
        )
    }
    sources = ("s3", "tv_bridge", "quant")
    statuses = {"TRUNCATED", "BOUNDED_QUERY_COMPLETE", "NOT_SAMPLED", "UNAVAILABLE"}
    declared = raw.get("signal_sources")
    if isinstance(declared, dict):
        result["signal_sources"] = {
            s: declared.get(s) if declared.get(s) in statuses else "UNKNOWN"
            for s in sources
        }
    limits = raw.get("signal_limit_by_source")
    if isinstance(limits, dict):
        result["signal_limit_by_source"] = {
            s: limits.get(s)
            if type(limits.get(s)) is int and 0 <= limits[s] <= 250
            else None
            for s in sources
        }
    windows = raw.get("signal_time_windows")
    if isinstance(windows, list) and len(windows) <= 4:
        cleaned = []
        for window in windows:
            if (
                not isinstance(window, dict)
                or type(window.get("start_ms")) is not int
                or type(window.get("end_ms")) is not int
                or not 0 <= window["start_ms"] < window["end_ms"] < 2**53
                or window["end_ms"] - window["start_ms"] != 6 * 3_600_000
            ):
                continue
            buckets = {}
            for source in sources:
                bucket = window.get("sources", {}).get(source, {})
                bucket = bucket if isinstance(bucket, dict) else {}
                buckets[source] = {
                    "status": bucket.get("status")
                    if bucket.get("status") in statuses
                    else "UNKNOWN",
                    **{
                        key: bucket.get(key)
                        if type(bucket.get(key)) is int and 0 <= bucket[key] <= 251
                        else None
                        for key in (
                            "sample_limit",
                            "examined_unique_signals",
                            "observed_lower_bound",
                        )
                    },
                    "sentinel_observed": bucket.get("sentinel_observed")
                    if type(bucket.get("sentinel_observed")) is bool
                    else None,
                }
            cleaned.append(
                {
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "sources": buckets,
                }
            )
        result["signal_time_windows"] = cleaned
    return result


def _symbol(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 48
        or not value.endswith("USDT")
        or any(not (c.isalnum() or c == "_") for c in value)
    ):
        raise ValueError("DAILY_REPORT_SYMBOL_INVALID")
    return value


def _scope(value):
    value = asdict(value) if is_dataclass(value) else value
    if (
        not isinstance(value, dict)
        or set(value) != {"exchange", "account_id", "environment", "product"}
        or value["exchange"] != "BINANCE"
        or value["product"] != "FUTURES"
        or value["environment"] not in {"SANDBOX", "LIVE"}
        or not isinstance(value["account_id"], str)
        or not 1 <= len(value["account_id"]) <= 128
    ):
        raise ValueError("DAILY_REPORT_SCOPE_INVALID")
    return dict(value)


def _window(day, as_of_ms):
    if isinstance(day, str):
        parsed = date.fromisoformat(day)
        start = (
            int(datetime.combine(parsed, datetime.min.time(), _ZONE).timestamp()) * 1000
        )
        result = {
            "date": day,
            "timezone": "Asia/Shanghai",
            "start_ms": start,
            "end_ms": start + DAY,
        }
    elif isinstance(day, dict):
        result = {k: day.get(k) for k in ("date", "timezone", "start_ms", "end_ms")}
    else:
        raise ValueError("DAILY_REPORT_DAY_INVALID")  # noqa: TRY004 - stable boundary code
    start, end = result["start_ms"], result["end_ms"]
    if (
        type(start) is not int
        or type(end) is not int
        or type(as_of_ms) is not int
        or not 0 <= start < end <= as_of_ms <= 2**53 - 1
        or end - start != DAY
        or (start + 28_800_000) % DAY
        or result["timezone"] != "Asia/Shanghai"
        or datetime.fromtimestamp(start // 1000, _ZONE).date().isoformat()
        != result["date"]
    ):
        raise ValueError("DAILY_REPORT_COMPLETED_DAY_REQUIRED")
    return result


def _trades(raw, *, as_of_ms):
    if not isinstance(raw, list) or len(raw) > 500:
        raise ValueError("DAILY_REPORT_TRADE_BOUND")
    result = []
    for trade in raw:
        if not isinstance(trade, dict):
            raise ValueError("DAILY_REPORT_TRADE_INVALID")  # noqa: TRY004 - stable boundary code
        row = {key: _numeric(trade.get(key)) for key in _NUMERIC_TRADE}
        row.update(
            {
                key: _code(trade.get(key))
                for key in (
                    "side",
                    "entry_mode",
                    "source",
                    "strategy_version",
                    "exit_reason",
                    "settlement_state",
                    "status",
                )
            }
        )
        for key in ("decided_ms", "opened_ms", "closed_ms", "settled_ms"):
            value = trade.get(key)
            if value is not None and (
                type(value) is not int or not 0 <= value <= as_of_ms
            ):
                raise ValueError("DAILY_REPORT_FUTURE_TRADE")
            row[key] = value
        row["partial_exit_observed"] = trade.get("partial_exit_observed") is True
        row["multiple_reductions_observed"] = (
            trade.get("multiple_reductions_observed") is True
        )
        row["carry_in"] = trade.get("carry_in") is True
        settled = (
            trade.get("net_pnl_available") is True
            and trade.get("settlement_state") == "SETTLED"
            and trade.get("fills_valid") is not False
            and row["settled_ms"] is not None
            and row["closed_ms"] is not None
            and row["opened_ms"] is not None
            and row["opened_ms"] <= row["closed_ms"] <= row["settled_ms"]
            and (_number(row["opening_quantity"]) or Decimal(0)) > 0
            and _number(row["opening_quantity"]) == _number(row["closing_quantity"])
        )
        row["actual_settled_net_usdt"] = (
            _numeric(trade.get("net_pnl")) if settled else None
        )
        row["funding_status"] = "NOT_SEPARATELY_LOADED"
        row["net_pnl_basis"] = "WHOLE_EPISODE_SETTLEMENT_NOT_DAY_MARK_TO_MARKET"
        row["held_minutes"] = (
            _numeric(Decimal(row["closed_ms"] - row["opened_ms"]) / 60_000)
            if row["closed_ms"] is not None
            and row["opened_ms"] is not None
            and row["closed_ms"] >= row["opened_ms"]
            else None
        )
        result.append(row)
    return result


def _evidence(raw, *, supported, side, as_of_ms):
    if not isinstance(raw, dict):
        return {
            "status": "UNAVAILABLE",
            "primary_reason": "ENGINE_SYMBOL_UNSUPPORTED"
            if not supported
            else "EVIDENCE_UNAVAILABLE",
            "signal_count": None,
            "signal_sources": {},
            "stage_counts": {},
            "reason_counts": [],
            "jev_outcomes": [],
            "trades": [],
            "coverage": {"retention_completeness": "NOT_PROVEN"},
        }
    trades = _trades(raw.get("trades", []), as_of_ms=as_of_ms)
    directional = raw.get("by_strategy", {}).get("s6" if side == "BUY" else "s8")
    classification = directional if isinstance(directional, dict) else raw
    stages = _counts(classification.get("stage_counts"))
    count = raw.get("signal_count")
    if count is not None and (type(count) is not int or not 0 <= count <= 1_000_000):
        raise ValueError("DAILY_REPORT_SIGNAL_COUNT_INVALID")
    filled = [t for t in trades if (_number(t["opening_quantity"]) or Decimal(0)) > 0]
    reasons = []
    for reason in classification.get("reason_counts", [])[:100]:
        if (
            isinstance(reason, dict)
            and type(reason.get("count")) is int
            and 0 <= reason["count"] <= 1_000_000
        ):
            reasons.append(
                {
                    "stage": _code(reason.get("stage")),
                    "reason": _code(reason.get("reason")),
                    "count": reason["count"],
                }
            )
    if any(t["side"] == side for t in filled):
        primary = "MATCHED_DIRECTION_FILLED"
    elif filled:
        primary = "OTHER_DIRECTION_ONLY"
    elif not supported:
        primary = "ENGINE_SYMBOL_UNSUPPORTED"
    elif stages.get("ENTRY_CANDIDATE", 0):
        primary = (
            "CANDIDATE_NOT_FILLED"
            if raw.get("coverage", {}).get("intents") == "BOUNDED_QUERY_COMPLETE"
            else "CANDIDATE_EXECUTION_UNKNOWN"
        )
    elif count is None:
        primary = "EVIDENCE_UNAVAILABLE"
    elif not count:
        primary = "NO_RETAINED_SIGNAL_UNKNOWN"
    else:
        observed = [key for key, n in stages.items() if n and key in _STAGE_TEXT]
        primary = (
            observed[0]
            if len(observed) == 1
            else "MIXED_RECORDED_REASONS"
            if observed
            else "EVIDENCE_UNAVAILABLE"
        )
    jev = []
    for item in classification.get("jev_outcomes", [])[:30]:
        if (
            isinstance(item, dict)
            and type(item.get("count")) is int
            and 0 <= item["count"] <= 1_000_000
        ):
            jev.append(
                {k: _code(item.get(k)) for k in ("status", "reason", "fallback_cause")}
                | {"count": item["count"]}
            )
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else {}
    sampled = (
        coverage.get("signals") in {"TRUNCATED", "UNAVAILABLE"}
        or coverage.get("signal_sample_basis")
        == "SOURCE_FAIR_FOUR_UTC8_WINDOWS_EARLIEST_WITHIN_WINDOW"
    )
    return {
        "status": "OBSERVED_BOUNDED",
        "primary_reason": primary,
        "signal_count": count,
        "signal_count_basis": "OBSERVED_RETAINED_STRATIFIED_SAMPLE_LOWER_BOUND"
        if sampled
        else "BOUNDED_RETAINED_RECORDS_NOT_FULL_DAY_PROVEN",
        "sampled_attribution": sampled,
        "primary_reason_interpretation": "RECORDED_SAMPLE_CLASSIFICATION_NOT_EXCLUSIVE_DAY_CAUSE",
        "signal_sources": _counts(raw.get("signal_sources")),
        "stage_counts": stages,
        "classification_scope": (
            "MATCHED_DIRECTION_CONSUMER"
            if classification is directional
            else "COMBINED_CONSUMERS_DIRECTION_UNRESOLVED"
        ),
        "reason_counts": reasons,
        "execution_reason_counts": [
            {
                "stage": _code(item.get("stage")),
                "reason": _code(item.get("reason")),
                "count": item["count"],
            }
            for item in raw.get("execution_reason_counts", [])[:100]
            if isinstance(item, dict)
            and type(item.get("count")) is int
            and 0 <= item["count"] <= 1_000_000
        ],
        "jev_outcomes": jev,
        "trades": trades,
        "coverage": _coverage(coverage),
    }


def _path(raw, *, start_ms, end_ms, market_source):
    if (
        not isinstance(raw, dict)
        or raw.get("status") != "OBSERVED"
        or not isinstance(raw.get("features"), dict)
    ):
        return {
            "status": "UNKNOWN",
            "reason": _code(raw.get("reason"))
            if isinstance(raw, dict)
            else "PATH_NOT_LOADED",
            "features": None,
        }
    if market_source == "UNKNOWN" or raw.get("market_source") != market_source:
        return {
            "status": "UNKNOWN",
            "reason": "PRICE_SOURCE_MISMATCH",
            "features": None,
        }
    source = raw["features"]
    features = {key: _numeric(source.get(key)) for key in _NUMERIC_FEATURE}
    for key in ("high_minute_ms", "low_minute_ms"):
        value = source.get(key)
        if type(value) is not int or not start_ms <= value < end_ms:
            return {
                "status": "UNKNOWN",
                "reason": "PATH_WINDOW_NOT_PROVEN",
                "features": None,
            }
        features[key] = value
    return {
        "status": "OBSERVED",
        "features": features,
        "basis": "EX_POST_DESCRIPTIVE_NOT_PREDICTIVE",
    }


def _market_sample(row):
    opening, high, low, close = [
        _number(row.get(k)) for k in ("open", "high", "low", "close")
    ]
    if (
        any(v is None for v in (opening, high, low, close))
        or not 0 < low <= min(opening, close) <= max(opening, close) <= high
    ):
        raise ValueError("DAILY_REPORT_OHLC_INVALID")
    spread = high - low
    return {
        "symbol": _symbol(row["symbol"]),
        "return_pct": _numeric(row["return_pct"]),
        "range_pct_of_open": _numeric(spread / opening * 100),
        "close_location_in_range_pct": _numeric((close - low) / spread * 100)
        if spread
        else None,
    }


def _summary(rows):
    result = {"count": len(rows)}
    for field in ("return_pct", "range_pct_of_open", "close_location_in_range_pct"):
        values = [_number(row[field]) for row in rows if row.get(field) is not None]
        result["median_" + field] = _numeric(median(values)) if values else None
    return result


def _accumulate(current, prior_reports):
    if not isinstance(prior_reports, (list, tuple)) or len(prior_reports) > 300:
        raise ValueError("DAILY_REPORT_HISTORY_BOUND")
    earliest = current["window"]["start_ms"] - 29 * DAY
    market_days, account_days = {}, {}
    ignored = Counter()
    for report in (*prior_reports, current):
        if not isinstance(report, dict) or report.get("version") != VERSION:
            ignored["INCOMPATIBLE_VERSION"] += 1
            continue
        start = report.get("window", {}).get("start_ms")
        known = report.get("as_of_ms")
        if (
            type(start) is not int
            or not earliest <= start <= current["window"]["start_ms"]
            or type(known) is not int
            or known > current["as_of_ms"]
        ):
            ignored["OUTSIDE_OBSERVABLE_30_DAY_WINDOW"] += 1
            continue
        key = (report.get("market_source"), start)
        if report.get("market_source") != current["market_source"]:
            ignored["DIFFERENT_MARKET_SOURCE"] += 1
            continue
        old = market_days.get(key)
        if old is None or (known, report.get("market_digest", "")) > (
            old["as_of_ms"],
            old.get("market_digest", ""),
        ):
            market_days[key] = report
        if report.get("scope_key") == current["scope_key"]:
            old = account_days.get(start)
            if old is None or known >= old["as_of_ms"]:
                account_days[start] = report
    selected, controls, reasons = [], [], Counter()
    for report in market_days.values():
        sample = report.get("market_comparison", {})
        selected.extend(sample.get("selected_samples", []))
        controls.extend(sample.get("control_samples", []))
    for report in account_days.values():
        for row in report.get("rows", []):
            reasons[
                row.get("evidence", {}).get("primary_reason", "EVIDENCE_UNAVAILABLE")
            ] += 1
    ready = (
        len(market_days) >= MINIMUM_DAYS
        and len(selected) >= MINIMUM_CASES
        and len(controls) >= MINIMUM_CASES
    )
    return {
        "window_days": 30,
        "unique_market_days": len(market_days),
        "unique_account_days": len(account_days),
        "selected": _summary(selected),
        "controls": _summary(controls),
        "account_symbol_day_reason_counts": dict(sorted(reasons.items())),
        "ignored_reports": dict(ignored),
        "sample_status": "DESCRIPTIVE_COMPARISON_ONLY"
        if ready
        else "INSUFFICIENT_SAMPLE",
        "minimum_days": MINIMUM_DAYS,
        "minimum_cases_per_cohort": MINIMUM_CASES,
        "causal_edge_proven": False,
        "automatic_parameter_updates": False,
        "limitations": [
            "OUTCOME_SELECTED_COHORT_NOT_RANDOMIZED",
            "WHOLE_DAY_FEATURES_NOT_AVAILABLE_AT_ENTRY",
            "CONTROL_COHORT_NOT_LIQUIDITY_OR_REGIME_MATCHED",
            "SAMPLE_THRESHOLD_NOT_STATISTICAL_SIGNIFICANCE",
        ],
    }


def _hypotheses(rows):
    reasons = Counter(row["evidence"]["primary_reason"] for row in rows)
    return [
        {
            "id": "DETECTION_COVERAGE",
            "status": "RESEARCH_ONLY",
            "observed_cases": sum(
                reasons[k]
                for k in (
                    "NO_RETAINED_SIGNAL_UNKNOWN",
                    "ACCOUNT_DECISION_NOT_OBSERVED",
                    "ENGINE_SYMBOL_UNSUPPORTED",
                )
            ),
            "question": "行情启动前是否已有可用信号，还是采集/标的覆盖或证据留存缺失？",
            "next_evidence": "保存全标的当时可见特征、账户路由与接收/消费时间；区分未产生信号和记录已过保留期。",
            "invalidation": "完整时点证据显示信号及时到达且已评估，则不能归因于漏扫或消费延迟。",
        },
        {
            "id": "FILTER_FALSE_NEGATIVES",
            "status": "RESEARCH_ONLY",
            "observed_cases": sum(
                count
                for code, count in reasons.items()
                if code.endswith("REJECTED") or code == "JEV_VETO"
            ),
            "question": "哪些风控/模型拒绝错过趋势，哪些实际上避免了亏损？",
            "next_evidence": "按冻结理由、方向、行情、流动性匹配未上榜对照，使用决定后的相同成本/持有窗口；按时间保留样本外验证。",
            "invalidation": "拒绝组成本后更差或差异在对照/样本外消失，则不应放松该过滤。",
        },
        {
            "id": "RUNNER_EXIT_POLICY",
            "status": "RESEARCH_ONLY",
            "observed_cases": reasons["MATCHED_DIRECTION_FILLED"],
            "question": "固定止盈或回撤退出是否截断可持有趋势，而非合理避免利润回吐？",
            "next_evidence": "同市场真实分批成交、原止损与逐时路径驱动剩余仓位影子对照；保留资金费/手续费/反证和样本外区间。",
            "invalidation": "延长持有的成本后收益更低、最大回撤更高或保护不足，则否决延长持有规则。",
        },
    ]


def build_report(day, ranking, evidence, paths, *, as_of_ms, scope, prior_reports=()):
    """Detached JSON-compatible report, deterministic for identical evidence."""
    scope, window = _scope(scope), _window(day, as_of_ms)
    if not isinstance(ranking, dict) or not isinstance(paths, dict):
        raise ValueError("DAILY_REPORT_INPUT_INVALID")  # noqa: TRY004 - stable boundary code
    if (
        ranking.get("start_ms") != window["start_ms"]
        or ranking.get("end_ms") != window["end_ms"]
    ):
        raise ValueError("DAILY_REPORT_RANKING_WINDOW_MISMATCH")
    if evidence is not None and (
        not isinstance(evidence, dict)
        or evidence.get("account_scope") != scope
        or evidence.get("window_start_ms") != window["start_ms"]
        or evidence.get("window_end_ms") != window["end_ms"]
        or type(evidence.get("as_of_ms")) is not int
        or evidence["as_of_ms"] > as_of_ms
    ):
        raise ValueError("DAILY_REPORT_EVIDENCE_SCOPE_MISMATCH")
    source = _code(ranking.get("market_source"), "UNKNOWN")
    evidence_rows = (evidence or {}).get("symbols", {})
    selected, rows, seen = [], [], set()
    with localcontext() as ctx:
        ctx.prec = 60
        for group, side in (("winners", "BUY"), ("losers", "SELL")):
            candidates = ranking.get(group, [])
            if not isinstance(candidates, list) or len(candidates) > 10:
                raise ValueError("DAILY_REPORT_TOP_TEN_BOUND")
            for rank, item in enumerate(candidates, 1):
                sample = _market_sample(item)
                symbol = sample["symbol"]
                if symbol in seen or (
                    (_number(sample["return_pct"]) or Decimal(0))
                    * (1 if side == "BUY" else -1)
                    <= 0
                ):
                    raise ValueError("DAILY_REPORT_RANKING_INVALID")
                seen.add(symbol)
                selected.append(sample)
                account = _evidence(
                    evidence_rows.get(symbol),
                    supported=item.get("engine_symbol_supported") is not False,
                    side=side,
                    as_of_ms=as_of_ms,
                )
                rows.append(
                    {
                        "group": group,
                        "rank": rank,
                        "symbol": symbol,
                        "direction": side,
                        "day_return_pct": sample["return_pct"],
                        "evidence": account,
                        "path": _path(
                            paths.get(symbol),
                            start_ms=window["start_ms"],
                            end_ms=window["end_ms"],
                            market_source=source,
                        ),
                        "capture_assessment": {
                            "status": "CROSS_ENVIRONMENT_NOT_COMPARABLE"
                            if scope["environment"] == "SANDBOX"
                            and source == "BINANCE_USDM_LIVE"
                            else "FILL_AWARE_EXECUTABLE_PATH_NOT_PROVEN",
                            "observed_peak_capture_ratio": None,
                            "unrealized_best_profit": None,
                            "comparison_basis": "ACTUAL_SETTLED_PNL_SEPARATE_FROM_HINDSIGHT_PRICE_EXTREMES",
                        },
                    }
                )
        eligible = ranking.get("eligible_summaries", [])
        if not isinstance(eligible, list) or len(eligible) > 3000:
            raise ValueError("DAILY_REPORT_CONTROL_BOUND")
        controls, control_seen = [], set()
        for item in eligible:
            sample = _market_sample(item)
            if sample["symbol"] in control_seen:
                raise ValueError("DAILY_REPORT_CONTROL_DUPLICATE")
            control_seen.add(sample["symbol"])
            if sample["symbol"] not in seen:
                controls.append(sample)
        controls.sort(key=lambda row: row["symbol"])
        report = {
            "version": VERSION,
            "window": window,
            "as_of_ms": as_of_ms,
            "scope_key": digest(canonical(scope)),
            "account_environment": scope["environment"],
            "market_source": source,
            "ranking_status": _code(ranking.get("status")),
            "universe_basis": _code(ranking.get("universe_basis")),
            "coverage": _counts(ranking.get("coverage")),
            "rows": rows,
            "market_comparison": {
                "selected_samples": selected,
                "control_samples": controls,
                "selected": _summary(selected),
                "controls": _summary(controls),
                "basis": "SAME_COMPLETE_DAY_OHLC_DESCRIPTIVE_ONLY",
            },
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "limitations": [
                "TOP_MOVERS_ARE_HINDSIGHT_SELECTION_NOT_BUY_OR_SHORT_RECOMMENDATIONS",
                "NO_REQUIREMENT_OR_ASSUMPTION_OF_BUYING_BOTTOM_AND_SELLING_TOP",
                "MISSING_EVIDENCE_IS_UNKNOWN_NOT_NO_SIGNAL_OR_MODEL_VETO",
                "SANDBOX_FILLS_AND_LIVE_PRICE_EXTREMES_CANNOT_FORM_CAPTURE_PNL",
                "SETTLED_NET_ALREADY_INCLUDES_COSTS_NO_DOUBLE_SUBTRACTION",
                "CURRENT_CATALOG_CANNOT_PROVE_HISTORICAL_DELISTED_COVERAGE",
            ],
        }
        report["market_digest"] = _digest(
            {
                "source": source,
                "window": window,
                "selected": selected,
                "controls": controls,
            }
        )
        report["accumulation"] = _accumulate(report, prior_reports)
        report["hypotheses"] = _hypotheses(rows)
    return seal_report(report)


def _actual_path_digest(report):
    raw = report.get("actual_same_environment_paths")
    if raw is None:
        return []
    source = "BINANCE_USDM_" + report["account_environment"]
    if not isinstance(raw, dict) or raw.get("source") != source:
        return ["同环境持仓路径：数据或来源未确认，暂不比较收益。"]
    cases = raw.get("cases")
    if not isinstance(cases, list) or len(cases) > 6:
        return ["同环境持仓路径：本批数据不足，结果未知。"]
    refs = Counter(
        case.get("episode_ref")
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("episode_ref"), str)
    )
    nets, peaks = [], []
    for case in cases:
        if (
            not isinstance(case, dict)
            or case.get("source") != source
            or not isinstance(case.get("episode_ref"), str)
            or refs[case["episode_ref"]] != 1
        ):
            continue
        try:
            net = _number(case.get("actual_net_pnl"))
            if (
                net is not None
                and case.get("actual_net_basis")
                == "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED"
            ):
                nets.append(net)
            path = case.get("gross_path")
            if isinstance(path, dict) and path.get("status") == "AVAILABLE_LOWER_BOUND":
                peak = _number(path.get("mfe_gross_usdt_lower_bound"))
                if peak is not None and peak >= 0:
                    peaks.append(peak)
        except ValueError:
            continue
    if not cases:
        return ["同环境持仓路径：本批没有可验证案例，不补造收益。"]
    with localcontext() as ctx:
        ctx.prec = 60
        lines = [
            f"同环境复盘样本 {len(cases)} 笔；已核验净收益 {len(nets)} 笔合计 {sum(nets, Decimal(0)):+.2f} USDT（含成本，非全账户日收益）。"
            if nets
            else f"同环境复盘样本 {len(cases)} 笔；已核验净收益暂缺。"
        ]
        lines.append(
            f"{len(peaks)} 笔各单峰值毛浮盈观察区间 {min(peaks):.2f}～{max(peaks):.2f} USDT；这是下界，不是可兑现利润，也不与净收益相加。"
            if peaks
            else "完整持仓毛浮盈路径暂缺，不能判断拿到多少趋势。"
        )
    return lines


def format_digest(report, *, account_alias=None):
    """One <=3500-character private digest; mandatory top rows never truncated."""
    if not isinstance(report, dict) or report.get("version") != VERSION:
        raise ValueError("DAILY_REPORT_VERSION_REQUIRED")
    alias = "当前研究账户"
    if account_alias is not None:
        if not isinstance(account_alias, str):
            raise ValueError("DAILY_REPORT_ALIAS_INVALID")
        alias = (
            "".join(c for c in account_alias if c.isprintable() and c not in "\r\n")[
                :40
            ]
            or alias
        )
    coverage, accumulation = report["coverage"], report["accumulation"]
    source = "LIVE" if report["market_source"] == "BINANCE_USDM_LIVE" else "来源未核实"
    lines = [
        f"📊 每日大行情复盘 · {report['window']['date']} UTC+8",
        f"{alias} · {report['account_environment']}（实际成交） / {source}（榜单）",
        f"固定自然日；覆盖 {coverage.get('eligible', 0)}/{coverage.get('discovered', 0)}，排除 {coverage.get('excluded', 0)}。",
    ]
    if report["universe_basis"] != "HISTORICAL_SNAPSHOT":
        lines.append("当前目录回看：历史下架标的覆盖未证明。")
    if any(row["evidence"].get("sampled_attribution") for row in report["rows"]):
        lines.append("入场理由：分层样本，非全量归因；样本计数不代表全日总量。")
    for group, title in (("winners", "涨幅前10"), ("losers", "跌幅前10")):
        chosen = [row for row in report["rows"] if row["group"] == group]
        lines.append("\n" + title + f"（{len(chosen)}）")
        for row in chosen:
            with localcontext() as ctx:
                ctx.prec = 60
                change = format(_number(row["day_return_pct"]), "+.2f")
            reason = _STAGE_TEXT.get(row["evidence"]["primary_reason"], "原因待核实")
            if row["evidence"].get("sampled_attribution") and row["evidence"][
                "primary_reason"
            ] not in {
                "MATCHED_DIRECTION_FILLED",
                "OTHER_DIRECTION_ONLY",
                "ENGINE_SYMBOL_UNSUPPORTED",
            }:
                reason += "（样本）"
            lines.append(f"{row['rank']}. {row['symbol']} {change}% · {reason}")
    lines += _actual_path_digest(report)
    lines += [
        "",
        f"累计 {accumulation['unique_market_days']} 个行情日 / {accumulation['unique_account_days']} 个账户日；对照 {accumulation['controls']['count']} 个标的日。",
        "样本仍不足，继续积累。"
        if accumulation["sample_status"] == "INSUFFICIENT_SAMPLE"
        else "只形成描述性对照，尚未证明预测优势。",
        "复盘重点：信号覆盖 → 过滤/JEV 理由 → 成交与退出；每项假设均记录下一步证据和推翻条件。",
        "榜单/盘中极值是事后观察，不是可实现利润；测试成交不与实盘价格拼接算收益。不会自动改风控或下单。",
    ]
    flow = report.get("flow_review")
    if (
        isinstance(flow, dict)
        and flow.get("version") == "daily-mover-flow-review-v1"
        and flow.get("status") == "DESCRIPTIVE_RESEARCH_ONLY"
    ):
        cases, counts = flow.get("cases"), flow.get("component_status_counts")
        if (
            isinstance(cases, list)
            and 1 <= len(cases) <= 240
            and isinstance(counts, dict)
        ):
            parts = []
            for name, label in (
                ("spot", "现货"),
                ("open_interest", "持仓量"),
                ("funding", "资金费"),
            ):
                part = counts.get(name, {})
                observed, unknown = part.get("OBSERVED", 0), part.get("UNKNOWN", 0)
                if (
                    type(observed) is not int
                    or type(unknown) is not int
                    or min(observed, unknown) < 0
                    or observed + unknown != len(cases)
                ):
                    parts = []
                    break
                parts.append(f"{label} {observed}/{len(cases)}")
            if parts:
                detail = (
                    "资金流证据（方向/时点，非独立样本）："
                    + "、".join(parts)
                    + "。历史补取，不代表当时提前发现。"
                )
                if len("\n".join([*lines, detail])) <= 3500:
                    lines.append(detail)
    evaluation = report.get("flow_evaluation")
    if (
        isinstance(evaluation, dict)
        and evaluation.get("version") == "mover-flow-fixed-rule-evaluation-v1"
    ):
        state = evaluation.get("status")
        detail = {
            "INSUFFICIENT_SAMPLE": "资金流增量对比：样本不足，尚未证明比只看价格更有效；不改变开仓规则。",
            "DESCRIPTIVE_REVIEW_READY": "资金流增量对比：达到描述性复核门槛，不等于验证盈利；不自动修改开仓规则。",
        }.get(state)
        if detail is not None and len("\n".join([*lines, detail])) <= 3500:
            lines.append(detail)
    text = "\n".join(lines)
    if len(text) > 3500:
        # With <=20 validated bounded symbols this cannot happen in normal
        # reports. Refuse malformed input rather than silently drop a top row.
        raise ValueError("DAILY_REPORT_DIGEST_TOO_LARGE")
    return text
