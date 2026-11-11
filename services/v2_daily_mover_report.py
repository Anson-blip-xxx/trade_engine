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
    "MIXED_RECORDED_REASONS": "多阶段记录，需看明细",
    "NO_DIRECTIONAL_ENTRY_SIGNAL": "样本中无同向入场事件",
}
_DIRECTIONAL_CLASSIFICATION = "directional-entry-events-v2"
_REASON_TEXT = {
    "TREND_VOLUME_UNCONFIRMED": "量能未确认",
    "regime_conflict": "大盘状态冲突",
    "TREND_NO_FRESH_SETUP": "无新入场结构",
    "TREND_FLOW_UNCONFIRMED": "主动买卖方向未确认",
    "TREND_ENTRY_MOVED": "入场价偏离",
    "TREND_BREAKOUT_BODY_INVALIDATED": "突破确认实体已被回吐",
    "TREND_BREAKOUT_PRICE_UNCONFIRMED": "突破现价证据未确认",
    "TREND_COUNTERMOVE": "当前K线逆向",
    "TREND_NOT_ALIGNED": "均线趋势不一致",
    "PRICE_ENVELOPE_CROSSES_STOP": "成交价区间跨止损",
    "JEV_TREND_UNSUPPORTED": "JEV趋势评分未达标",
    "JEV_CHASE_RISK_HIGH": "JEV追价风险过高",
    "JEV_MARKET_SNAPSHOT_EXPIRED": "JEV行情快照过期",
    "JEV_REVIEW_DEADLINE_EXPIRED": "JEV复查超时",
    "JEV_GATE_UNAVAILABLE": "JEV不可用",
    "TREND_STRUCTURE_UNCONFIRMED": "趋势结构缺证",
    "TREND_STRUCTURE_STALE": "趋势结构过期",
    "TREND_WARMUP": "趋势数据预热不足",
    "TREND_STOP_TOO_WIDE": "结构止损过宽",
    "TREND_TARGET_EXHAUSTED": "计划目标空间耗尽",
    "TREND_ATR_EXCEEDED": "波动超限",
    "TREND_PLANNED_REWARD_RISK_TOO_LOW": "计划净盈亏比不足",
    "SYMBOL_NOT_TRADING": "执行市场不可交易",
    "CANDIDATE_DATA_UNAVAILABLE_AFTER_RETRY": "候选行情取数失败",
    "TV_DIRECTION_CONFLICT": "TV方向冲突",
    "ADVERSE_FUNDING": "资金费率不利",
}
_DETAIL_NUMBERS = frozenset(
    {
        "volume_ratio",
        "volume",
        "prior20_mean_volume",
        "taker_buy_ratio",
        "price",
        "close",
        "open",
        "previous_close",
        "ema20",
        "ema60",
        "ema20_slope",
        "ema60_slope",
        "atr",
        "atr_pct",
        "entry_anchor",
        "reference_price",
        "price_low",
        "price_high",
        "stop_price",
        "unit_stop_risk",
        "trend_supported",
        "chase_risk",
        "liquidity_fragile",
        "trend.min_volume_ratio",
        "entry.flow_long",
        "entry.flow_short",
        "trend.max_chase_atr",
        "trend.max_atr_pct",
        "trend.max_structure_age_ms",
        "min_trend_support",
        "max_chase_risk",
        "max_age_ms",
        "max_wait_ms",
        "deadline_ms",
        "checked_at_ms",
        "started_at_ms",
        "signal_expires_at_ms",
        "price_drift_fraction",
        "planned_net_reward_risk",
        "entry.min_reward_risk",
        "strength",
        "score",
        "short_ratio",
        "funding_rate",
        "expected_move_pct",
        "entry.max_adverse_funding",
        "event_taker_buy_ratio",
        "raw_strength",
        "previous_open",
        "previous_high",
        "previous_low",
        "prior20_high",
        "prior20_low",
        "prior3_high",
        "prior3_low",
        "target_price",
        "structural_stop_price",
        "cost_buffer_fraction",
        "structure_last_closed_at_ms",
        "assembled_at_ms",
        "stop_fraction",
        "analysis_factor",
        "jev.trend_supported",
        "jev.chase_risk",
        "jev.liquidity_fragile",
        "jev.model_expires_at_ms",
        "jev.min_trend_support",
        "jev.max_chase_risk",
        "entry.min_score",
        "entry.short_min_strength",
        "jev.max_wait_ms",
        "jev.max_age_ms",
        "jev.cache_ttl_ms",
        "jev.unavailable_wait_ms",
        "liquidity.max_age_ms",
        "liquidity.max_spread_bps",
        "liquidity.max_impact_bps",
        "liquidity.max_price_basis_bps",
        "entry.reversal_rsi_long",
        "entry.reversal_rsi_short",
        "entry.extension_default",
        "entry.extension_violent",
        "entry.max_atr",
        "entry.max_takeover_atr",
        "entry.takeover_chg_4h",
        "entry.takeover_chg_24h",
        "entry.takeover_pullback_atr",
        "entry.takeover_pullback_fraction",
        "entry.pump_down_guard_chg",
        "trend.stop_buffer_atr",
        "trend.stop_atr",
        "trend.min_stop_fraction",
        "trend.max_stop_fraction",
        "trend.target_atr",
        "stop.max_default",
        "sizing.cost_buffer_fraction",
        "sizing.risk_fraction",
        "sizing.pool_fraction",
        "sizing.min_allocation",
        "sizing.max_allocation",
        *(
            f"jev.{key}"
            for key in (
                "started_at_ms",
                "deadline_ms",
                "checked_at_ms",
                "signal_expires_at_ms",
            )
        ),
        *(
            f"sizing.{key}"
            for key in (
                "balance",
                "available_margin",
                "used_pool_margin",
                "min_quantity",
                "min_notional",
                "quantity_step",
                "price_tick",
                "remaining_portfolio_risk",
                "sizing_mark_price",
                "price_drift_fraction",
                "drawdown_factor",
                "reference_price",
                "mark_price",
                "price_high",
                "price_low",
                "unit_stop_risk",
                "stop_price",
                "rounded_quantity",
                "budgeted_stop_risk",
            )
        ),
        *(
            f"liquidity.{key}"
            for key in (
                "age_ms",
                "spread_bps",
                "price_basis_bps",
                "vwap_impact_bps",
                "worst_impact_bps",
            )
        ),
        *(
            f"{window}.{field}"
            for window, fields in {
                "15m": ("ema20", "atr", "rsi", "taker_buy_ratio"),
                "1h": ("ema20", "atr", "atr_pct"),
                "4h": ("ema20", "chg"),
                "24h": ("ema20", "ema60", "chg"),
            }.items()
            for field in fields
        ),
    }
)
_DETAIL_FLAGS = frozenset(
    {
        "breakout_long",
        "breakout_short",
        "prior3_touched_ema20_long",
        "prior3_touched_ema20_short",
        "stop_valid_through_envelope",
        "breakout_confirmed",
        "sizing.stop_valid_through_envelope",
    }
)
_OBSERVATION_TIMES = frozenset(
    {
        "checked_at_ms",
        "started_at_ms",
        "assembled_at_ms",
        "structure_last_closed_at_ms",
        "jev.checked_at_ms",
        "jev.started_at_ms",
        "market_closed_at_ms",
        "s3_observed_at_ms",
    }
)
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


def _detail_values(raw):
    """Project numeric operands, not arbitrary configs, credentials or prompts."""
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        if key in _DETAIL_FLAGS and type(value) is bool:
            clean[key] = value
        elif key in _DETAIL_NUMBERS:
            try:
                clean[key] = _numeric(value)
            except ValueError:
                continue
        elif (
            key == "regime"
            and isinstance(value, str)
            and value
            in {
                "strong_bull",
                "bull_trend",
                "weak_bull",
                "weak_bear",
                "bear_trend",
                "risk-off",
                "risk_off",
                "neutral",
                "range",
            }
        ):
            clean[key] = value
    return clean


def _rejection_details(raw, *, as_of_ms):
    if not isinstance(raw, list):
        return []
    result, remaining = [], 50
    for group in raw[:100]:
        if not isinstance(group, dict) or type(group.get("count")) is not int:
            continue
        if not 1 <= group["count"] <= 1_000_000:
            continue
        samples = []
        raw_samples = group.get("reason_samples")
        for sample in (raw_samples if isinstance(raw_samples, list) else [])[:3]:
            if not remaining or not isinstance(sample, dict):
                continue
            timestamps = {
                k: sample.get(k) for k in ("observed_ms", "decided_ms", "completed_ms")
            }
            if any(
                v is not None and (type(v) is not int or not 0 <= v <= as_of_ms)
                for v in timestamps.values()
            ):
                continue
            ordered_times = [v for v in timestamps.values() if v is not None]
            if ordered_times != sorted(ordered_times):
                continue
            source = sample.get("market_sources")
            operands = _detail_values(sample.get("operands"))
            # Actual observations cannot originate in the future. Planned
            # deadlines/expiry are deliberately excluded from this check.
            invalid_clock = False
            for values in (operands, source if isinstance(source, dict) else {}):
                for key in _OBSERVATION_TIMES & values.keys():
                    try:
                        stamp = _number(values[key])
                        invalid_clock |= stamp is not None and (
                            stamp < 0
                            or stamp > as_of_ms
                            or stamp != stamp.to_integral_value()
                        )
                    except ValueError:
                        invalid_clock = True
            if invalid_clock:
                continue
            sources = {}
            if isinstance(source, dict):
                for k in (
                    "market_environment",
                    "environment",
                    "s3_environment",
                    "structure_environment",
                    "liquidity_environment",
                ):
                    if isinstance(source.get(k), str) and source[k] in {
                        "SANDBOX",
                        "LIVE",
                        "UNKNOWN",
                    }:
                        sources[k] = source[k]
                for k in ("market_host", "host", "liquidity_host"):
                    if isinstance(source.get(k), str) and source[k] in {
                        "demo-fapi.binance.com",
                        "fapi.binance.com",
                    }:
                        sources[k] = source[k]
                for k in (
                    "market_source",
                    "s3_source",
                    "structure_source",
                    "provenance",
                    "status",
                ):
                    if isinstance(source.get(k), str) and source[k] in {
                        "BINANCE_USDM_LIVE",
                        "BINANCE_USDM_TESTNET",
                        "BINANCE_USDM_SANDBOX",
                        "UNKNOWN",
                        "BINANCE_FUTURES",
                        "FROZEN_CONTEXT",
                        "NOT_PROVEN",
                        "OBSERVED",
                    }:
                        sources[k] = source[k]
                version = source.get("market_window_version")
                if isinstance(version, str) and re.fullmatch(
                    r"s3-closed-1m-v[1-6]", version
                ):
                    sources["market_window_version"] = version
                for k in ("market_closed_at_ms", "s3_observed_at_ms"):
                    if type(source.get(k)) is int and 0 <= source[k] <= as_of_ms:
                        sources[k] = source[k]
            ref = sample.get("evidence_ref")
            samples.append(
                {
                    **timestamps,
                    "source": sample.get("source")
                    if isinstance(sample.get("source"), str)
                    and sample.get("source") in {"s3", "tv_bridge", "quant"}
                    else "UNKNOWN",
                    "evidence_ref": ref
                    if isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{64}", ref)
                    else None,
                    "event": _code(sample.get("event")),
                    "operands": operands,
                    "thresholds": _detail_values(sample.get("thresholds")),
                    "market_sources": sources,
                }
            )
            remaining -= 1
        result.append(
            {
                "reason": _code(group.get("reason")),
                "stage": _code(group.get("stage")),
                "count": group["count"],
                "reason_samples": samples,
            }
        )
    return result


def _shadow_structure_class(sample, direction):
    """Label recorded conditions, not successful admission or future trend."""
    values = sample.get("operands", {})
    suffix = "long" if direction == "BUY" else "short"
    breakout = values.get("breakout_" + suffix)
    touched = values.get("prior3_touched_ema20_" + suffix)
    if type(breakout) is not bool:
        return "UNKNOWN"
    if breakout:
        return "BREAKOUT_CONDITION"
    if type(touched) is not bool:
        return "UNKNOWN"
    if not touched:
        return "NO_FRESH_SETUP"
    numbers = [
        _number(values.get(key))
        for key in (
            "previous_close",
            "previous_open",
            "close",
            "previous_high" if direction == "BUY" else "previous_low",
        )
    ]
    if any(value is None for value in numbers):
        return "UNKNOWN"
    previous, opened, close, edge = numbers
    sign = 1 if direction == "BUY" else -1
    with localcontext() as ctx:
        ctx.prec = 60
        resumed = sign * (previous - opened) <= 0 and sign * (close - edge) > 0
    return "PULLBACK_RESUME_CONDITION" if resumed else "NO_FRESH_SETUP"


def _gate_shadow(rows, *, environment):
    """Bounded metric sensitivity, never reconstruct missing historical context."""
    from services.v2_mover_gate_shadow import evaluate_mover_gate_shadow

    buckets = []
    for row in rows:
        samples, seen = [], set()
        for group in row["evidence"].get("rejection_details", []):
            for item in group["reason_samples"]:
                identity = item["evidence_ref"] or _digest(
                    {
                        key: item[key]
                        for key in ("source", "event", "observed_ms", "decided_ms")
                    }
                )
                if identity in seen:
                    continue
                seen.add(identity)
                samples.append(
                    {
                        "symbol": row["symbol"],
                        "environment": environment,
                        "require_market_environment": True,
                        "market_sources": item["market_sources"],
                        "structure_class": _shadow_structure_class(
                            item, row["direction"]
                        ),
                        "side": row["direction"],
                        "evidence_ref": item["evidence_ref"],
                        "config": {
                            "policy": {
                                key: value
                                for key, value in item["thresholds"].items()
                                if key == "trend.min_volume_ratio"
                            }
                        },
                        "frozen_metrics": {
                            key: value
                            for key, value in item["operands"].items()
                            if key in {"volume_ratio", "taker_buy_ratio"}
                            and value is not None
                        },
                    }
                )
        buckets.append(samples)
    # Interleave symbols so one heavily rejected coin cannot take the entire cap.
    samples = [bucket[i] for i in range(50) for bucket in buckets if i < len(bucket)][
        :100
    ]
    result = evaluate_mover_gate_shadow(samples, environment=environment)
    result["selection_basis"] = (
        "BOUNDED_REJECTION_EXAMPLES_SYMBOL_ROUND_ROBIN_NOT_INDEPENDENT_TRADES"
    )
    cohorts = {}
    for sample, outcome in zip(samples, result["samples"], strict=True):
        key = (sample["side"], sample["structure_class"])
        counts = cohorts.setdefault(key, Counter())
        counts["sample_count"] += 1
        counts["volume_gate_changed"] += outcome["volume"]["gate_changed"] is True
        counts["full_replay"] += outcome["mode"] == "FULL_FROZEN_REPLAY"
        counts["unknown_volume"] += outcome["volume"]["status"] == "UNKNOWN"
    result["structure_cohorts"] = [
        {"side": side, "structure_class": setup, **dict(counts)}
        for (side, setup), counts in sorted(cohorts.items())
    ]
    result["structure_cohort_basis"] = (
        "FROZEN_CONDITIONS_NOT_ADMISSION_OR_REALIZED_PROFIT;"
        "NO_PHASE_SPECIFIC_POLICY_IS_ACTIVATED"
    )
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
    directional_v2 = (
        classification.get("classification_version") == _DIRECTIONAL_CLASSIFICATION
    )
    candidate_count = classification.get("candidate_count") if directional_v2 else None
    if candidate_count is not None and (
        type(candidate_count) is not int or not 0 <= candidate_count <= 1_000_000
    ):
        raise ValueError("DAILY_REPORT_CANDIDATE_COUNT_INVALID")
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
    elif candidate_count == 0:
        primary = "NO_DIRECTIONAL_ENTRY_SIGNAL"
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
    sampled = coverage.get("signals") in {"TRUNCATED", "UNAVAILABLE"} or coverage.get(
        "signal_sample_basis"
    ) in {
        "SOURCE_FAIR_FOUR_UTC8_WINDOWS_EARLIEST_WITHIN_WINDOW",
        "SOURCE_FAIR_FOUR_UTC8_WINDOWS_UNIFORM_RANK",
    }
    return {
        "status": "OBSERVED_BOUNDED",
        "primary_reason": primary,
        "signal_count": count,
        "candidate_count": candidate_count,
        "classification_version": _DIRECTIONAL_CLASSIFICATION
        if directional_v2
        else "LEGACY_CONSUMER_ONLY_UNFILTERED_EVENTS",
        "non_candidate_counts": _counts(classification.get("non_candidate_counts"))
        if directional_v2
        else {},
        "lifecycle_counts": _counts(classification.get("lifecycle_counts"))
        if directional_v2
        else {},
        "rejection_details": _rejection_details(
            classification.get("rejection_details"), as_of_ms=as_of_ms
        ),
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
        or not window["end_ms"] <= evidence["as_of_ms"] <= as_of_ms
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
                    as_of_ms=(evidence or {}).get("as_of_ms", as_of_ms),
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
        try:
            report["gate_shadow"] = _gate_shadow(rows, environment=scope["environment"])
        except Exception:  # noqa: BLE001 - optional research must not lose factual review
            report["gate_shadow"] = {
                "version": "mover-gate-shadow-v1",
                "status": "UNAVAILABLE",
                "reason": "OPTIONAL_SHADOW_FAILED",
                "execution_authorized": False,
                "not_trade_prediction": True,
            }
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


def _units(text):
    """Same bound as the notifier: Telegram counts UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _sample_comparison(evidence, reason):
    for detail in evidence.get("rejection_details", []):
        if detail["reason"] != reason:
            continue
        for sample in detail["reason_samples"]:
            operands, thresholds = sample["operands"], sample["thresholds"]
            pair = {
                "TREND_VOLUME_UNCONFIRMED": (
                    "volume_ratio",
                    "trend.min_volume_ratio",
                    "量比",
                ),
                "JEV_TREND_UNSUPPORTED": (
                    "jev.trend_supported",
                    "jev.min_trend_support",
                    "评分",
                ),
            }.get(reason)
            if (
                pair
                and operands.get(pair[0]) is not None
                and thresholds.get(pair[1]) is not None
            ):
                actual, limit = _number(operands[pair[0]]), _number(thresholds[pair[1]])
                if actual is not None and limit is not None:
                    stamp = sample.get("decided_ms")
                    time_label = (
                        datetime.fromtimestamp(stamp / 1000, _ZONE).strftime("%H:%M")
                        if stamp
                        else "样例"
                    )
                    return f"；{time_label}{pair[2]}{actual:.3g}/阈值{limit:.3g}"
    return ""


def _row_reason(evidence, *, examples=True, max_reasons=2):
    primary = evidence["primary_reason"]
    label = _STAGE_TEXT.get(primary, "原因待核实")
    reasons = sorted(
        (item for item in evidence.get("reason_counts", []) if item["count"] > 0),
        key=lambda item: (-item["count"], item["reason"]),
    )
    if reasons:
        detail = "、".join(
            f"{_REASON_TEXT.get(item['reason'], _STAGE_TEXT.get(item['stage'], '其他已记录原因'))}×{item['count']}"
            for item in reasons[:max_reasons]
        )
        if len(reasons) > max_reasons:
            detail += f"等{len(reasons)}类"
        label = (
            (label + "；其他样本：" + detail)
            if primary
            in {
                "MATCHED_DIRECTION_FILLED",
                "OTHER_DIRECTION_ONLY",
                "CANDIDATE_NOT_FILLED",
                "CANDIDATE_EXECUTION_UNKNOWN",
            }
            else detail
        )
        if examples:
            label += _sample_comparison(evidence, reasons[0]["reason"])
    if evidence.get("sampled_attribution") and primary not in {
        "MATCHED_DIRECTION_FILLED",
        "OTHER_DIRECTION_ONLY",
        "ENGINE_SYMBOL_UNSUPPORTED",
    }:
        label += "（样本）"
    return label


def _verified_optional_review(report, key, version, *, ensure_ascii=False):
    review = report.get(key)
    if (
        not isinstance(review, dict)
        or review.get("version") != version
        or not isinstance(review.get("status"), str)
    ):
        return None
    if any(
        review.get(flag) is not False
        for flag in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    ):
        return None
    try:
        encoded = json.dumps(
            {k: v for k, v in review.items() if k != "content_digest"},
            ensure_ascii=ensure_ascii,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(encoded) > 512 * 1024 or hashlib.sha256(
            encoded
        ).hexdigest() != review.get("content_digest"):
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    return review


def _execution_review_digest(report):
    review = _verified_optional_review(
        report, "execution_review", "daily-mover-execution-review-v1"
    )
    if review is None or review.get("status") not in {"AVAILABLE", "PARTIAL"}:
        return []
    evidence = report.get("account_evidence")
    if (
        not isinstance(evidence, dict)
        or any(
            key not in report or review.get(key) != report[key]
            for key in ("account_environment", "scope_key", "as_of_ms")
        )
        or type(evidence.get("as_of_ms")) is not int
        or review.get("evidence_as_of_ms") != evidence["as_of_ms"]
    ):
        return []
    totals = review.get("totals")
    if (
        not isinstance(totals, dict)
        or any(
            type(totals.get(k)) is not int or not 0 <= totals[k] <= 500
            for k in ("settled_count", "wins", "losses", "breakeven")
        )
        or totals["settled_count"]
        != totals["wins"] + totals["losses"] + totals["breakeven"]
    ):
        return []
    try:
        hold = _number(totals.get("held_minutes_median"))
        net = _number(totals.get("net_usdt"))
    except ValueError:
        return []
    if not totals["settled_count"] or hold is None or hold < 0 or net is None:
        return []
    lines = [
        f"榜单关联已核验结算{totals['settled_count']}笔，净收益{net:+.2f} USDT（含已记录成本，非全账户日收益）。",
        f"榜单关联结算结构：{totals['wins']}盈/{totals['losses']}亏/{totals['breakeven']}平，持仓中位{hold:.1f}分钟（非全账户）。",
    ]
    paths = review.get("post_flat_evidence")
    if isinstance(paths, dict) and paths.get("anchor_is_actual_exit_price") is False:
        keys = (
            "observed_count",
            "favorable_end_count",
            "adverse_end_count",
            "unchanged_end_count",
        )
        if all(type(paths.get(k)) is int and 0 <= paths[k] <= 6 for k in keys) and (
            paths["observed_count"] > 0
            and paths["observed_count"] == sum(paths[k] for k in keys[1:])
        ):
            lines.append(
                f"平仓后路径：{paths['favorable_end_count']}笔期末有利、{paths['adverse_end_count']}笔反向；"
                "以平仓后完整分钟收盘为锚，非退出价或可兑现收益，不据此统一延长持仓。"
            )
    return lines


def _bound_review(report, key, version):
    review = _verified_optional_review(report, key, version)
    window = report.get("window")
    if review is None or not isinstance(window, dict):
        return None
    binding = {
        "scope_key": report.get("scope_key"),
        "account_environment": report.get("account_environment"),
        "as_of_ms": report.get("as_of_ms"),
        "window_start_ms": window.get("start_ms"),
        "window_end_ms": window.get("end_ms"),
    }
    if any(value is None or review.get(key) != value for key, value in binding.items()):
        return None
    return review


def _financial_summary(value):
    if not isinstance(value, dict):
        return None
    fields = ("settled_count", "wins", "losses", "breakeven")
    if any(type(value.get(k)) is not int or not 0 <= value[k] <= 500 for k in fields):
        return None
    if value["settled_count"] != sum(value[k] for k in fields[1:]):
        return None
    try:
        net = _number(value.get("net_usdt"))
    except ValueError:
        return None
    if net is None or (not value["settled_count"] and net != 0):
        return None
    return {**{k: value[k] for k in fields}, "net": net}


def _summaries_partition(total, first, second):
    if first is None or second is None:
        return False
    with localcontext() as ctx:
        ctx.prec = 100
        return all(total[k] == first[k] + second[k] for k in total)


def _account_day_digest(report):
    if "account_day_review" not in report:
        return []  # Historical report compatibility; no synthetic whole-account zero.
    unknown = ["账户整体结算尚未核验；榜单收益不能代表整体。"]
    review = _bound_review(report, "account_day_review", "daily-account-review-v1")
    if review is None or review.get("status") not in {"AVAILABLE", "PARTIAL"}:
        return unknown
    total = _financial_summary(review.get("totals"))
    coverage = review.get("coverage")
    if total is None or not isinstance(coverage, dict):
        return unknown
    if (
        coverage.get("verified_settled_count") != total["settled_count"]
        or coverage.get("truncated") is not False
    ):
        return unknown
    complete = (
        review["status"] == "AVAILABLE"
        and coverage.get("query_complete") is True
        and coverage.get("full_closed_settlement_population_proven") is True
    )
    if not complete:
        return [
            f"账户结算仅核验子集{total['settled_count']}笔，净收益{total['net']:+.2f} USDT；整体结果未知。"
        ]
    lines = [
        (
            f"账户已结算交易（库内）{total['settled_count']}笔，净收益{total['net']:+.2f} USDT；"
            f"{total['wins']}盈/{total['losses']}亏/{total['breakeven']}平。"
        )
    ]
    sides = review.get("by_side")
    if isinstance(sides, dict):
        buy, sell = (_financial_summary(sides.get(side)) for side in ("BUY", "SELL"))
        if _summaries_partition(total, buy, sell):
            lines.append(
                f"多单{buy['settled_count']}笔 {buy['net']:+.2f}；空单{sell['settled_count']}笔 {sell['net']:+.2f} USDT。"
            )
    split = review.get("ranking_vs_other")
    if (
        isinstance(split, dict)
        and split.get("status") == "AVAILABLE"
        and split.get("basis") == "VERIFIED_RANKING_EPISODE_REFS_NOT_SYMBOL_MEMBERSHIP"
    ):
        ranking, other = (
            _financial_summary(split.get(key)) for key in ("ranking", "other")
        )
        if _summaries_partition(total, ranking, other):
            lines.append(
                f"榜单关联{ranking['settled_count']}笔 {ranking['net']:+.2f}；其余{other['settled_count']}笔 {other['net']:+.2f} USDT。"
            )
    lines.append("按最后平仓日计整笔结算、含已记录成本；非账户权益日收益。")
    return lines


def _execution_cost_digest(report):
    attached = report.get("account_day_review")
    if not isinstance(attached, dict) or "execution_cost_evidence" not in attached:
        return []
    unknown = ["成交成本核对尚不完整；缺失的计划或费用不按零处理。"]
    review = _bound_review(report, "account_day_review", "daily-account-review-v1")
    if review is None or review.get("status") not in {"AVAILABLE", "PARTIAL"}:
        return unknown
    total, costs = (
        _financial_summary(review.get("totals")),
        review.get("execution_cost_evidence"),
    )
    if (
        total is None
        or not isinstance(costs, dict)
        or costs.get("version") != "execution-cost-evidence-v1"
        or costs.get("basis")
        != "FROZEN_ENTRY_PLAN_VS_VERIFIED_WHOLE_EPISODE_SETTLEMENT"
        or costs.get("status") not in {"AVAILABLE", "PARTIAL"}
        or any(
            costs.get(k) is not False
            for k in (
                "execution_authorized",
                "automatic_parameter_updates",
                "promotion_authorized",
            )
        )
    ):
        return unknown
    denominator, comparable, stops = (
        costs.get(k)
        for k in (
            "denominator_settled_count",
            "plan_comparable_count",
            "native_stop_decomposed_count",
        )
    )
    coverage = costs.get("coverage")
    if (
        any(type(v) is not int for v in (denominator, comparable, stops))
        or not 0 <= stops <= comparable <= denominator <= 500
        or denominator != total["settled_count"]
        or not isinstance(coverage, dict)
        or coverage.get("query_complete") is not True
        or type(coverage.get("unknown_count")) is not int
        or coverage["unknown_count"] != denominator - comparable
    ):
        return unknown
    return [
        f"成交成本核对（结算子集）：计划可比{comparable}/{denominator}笔，原始止损可分解{stops}笔；缺失不按零计，价格差不直接归因为滑点。"
    ]


def _jev_financial_summary(value):
    if not isinstance(value, dict):
        return None
    count = value.get("settled_count")
    if type(count) is not int or not 0 <= count <= 500:
        return None
    try:
        net = _number(value.get("net_usdt"))
    except ValueError:
        return None
    if net is None or (not count and net != 0):
        return None
    return {"settled_count": count, "net": net if net else Decimal(0)}


def _jev_partition(total, parts):
    if total is None or any(part is None for part in parts):
        return False
    # The report number contract allows 100-character values and +/-40
    # adjusted exponents. Preserve their full span, even across cancellation.
    with localcontext() as ctx:
        ctx.prec = 200
        return total["settled_count"] == sum(
            part["settled_count"] for part in parts
        ) and total["net"] == sum((part["net"] for part in parts), Decimal(0))


def _jev_participation_digest(report):
    attached = report.get("account_day_review")
    if not isinstance(attached, dict) or "jev_participation" not in attached:
        return []  # Historical reports do not imply zero model participation.
    unknown = [
        "JEV参与尚未核验（已结算成交子集，非全部候选、非模型收益证明）；未知不等于未参与。"
    ]
    review = _bound_review(report, "account_day_review", "daily-account-review-v1")
    if review is None or review.get("status") not in {"AVAILABLE", "PARTIAL"}:
        return unknown
    total = _financial_summary(review.get("totals"))
    coverage, jev = review.get("coverage"), review.get("jev_participation")
    if (
        total is None
        or not isinstance(coverage, dict)
        or type(coverage.get("verified_settled_count")) is not int
        or coverage["verified_settled_count"] != total["settled_count"]
        or coverage.get("truncated") is not False
        or not isinstance(jev, dict)
        or jev.get("version") != "daily-account-jev-participation-v1"
        or jev.get("basis") != "FROZEN_ENTRY_EVIDENCE_FOR_VERIFIED_SETTLED_EPISODES"
        or jev.get("status") not in {"AVAILABLE", "PARTIAL"}
        or any(
            jev.get(flag) is not False
            for flag in (
                "execution_authorized",
                "automatic_parameter_updates",
                "promotion_authorized",
            )
        )
        or any(
            jev.get(key) != review[key]
            for key in (
                "scope_key",
                "account_environment",
                "as_of_ms",
                "window_start_ms",
                "window_end_ms",
            )
        )
        or any(
            type(jev.get(key)) is not int or not 0 <= jev[key] < 2**53
            for key in ("as_of_ms", "window_start_ms", "window_end_ms")
        )
    ):
        return unknown
    denominator = _jev_financial_summary(
        {
            "settled_count": jev.get("denominator_settled_count"),
            "net_usdt": jev.get("denominator_net_usdt"),
        }
    )
    if not _jev_partition(denominator, [total]):
        return unknown
    names = {
        "MODEL_APPROVED",
        "MODEL_REJECTED",
        "FALLBACK",
        "DISABLED",
        "NOT_MODEL_EVALUATED",
        "UNKNOWN",
    }
    groups, sources, causes = (
        jev.get(key) for key in ("by_participation", "by_source", "fallback_causes")
    )
    if (
        not isinstance(groups, dict)
        or set(groups) != names
        or not isinstance(sources, dict)
        or not set(sources) <= {"s2", "s3", "tv_bridge", "quant", "UNKNOWN"}
        or not isinstance(causes, dict)
        or len(causes) > 20
        or any(not isinstance(key, str) or len(key) > 100 for key in causes)
    ):
        return unknown
    groups = {name: _jev_financial_summary(value) for name, value in groups.items()}
    causes = {name: _jev_financial_summary(value) for name, value in causes.items()}
    model = _jev_financial_summary(jev.get("model_evaluated"))
    if (
        not _jev_partition(denominator, list(groups.values()))
        or not _jev_partition(
            model, [groups["MODEL_APPROVED"], groups["MODEL_REJECTED"]]
        )
        or not _jev_partition(
            denominator, [_jev_financial_summary(value) for value in sources.values()]
        )
        or not _jev_partition(groups["FALLBACK"], list(causes.values()))
    ):
        return unknown
    proof = jev.get("coverage")
    count_keys = (
        "queried_episode_count",
        "evidence_row_count",
        "classified_count",
        "unknown_count",
    )
    if (
        not isinstance(proof, dict)
        or proof.get("query_complete") is not True
        or any(
            type(proof.get(k)) is not int or not 0 <= proof[k] <= 500
            for k in count_keys
        )
        or proof["queried_episode_count"] != denominator["settled_count"]
        or proof["unknown_count"] != groups["UNKNOWN"]["settled_count"]
        or proof["classified_count"] + proof["unknown_count"]
        != denominator["settled_count"]
        or not proof["classified_count"]
        <= proof["evidence_row_count"]
        <= denominator["settled_count"]
        or (jev["status"] == "AVAILABLE" and proof["unknown_count"] != 0)
        or (
            jev["status"] == "PARTIAL"
            and not 0 < proof["unknown_count"] < denominator["settled_count"]
        )
    ):
        return unknown
    if not denominator["settled_count"]:
        return ["JEV：暂无已核验结算成交可归因；非全部候选、非模型收益证明。"]
    model_text = (
        "模型参与尚未完整核验"
        if proof["unknown_count"] and not model["settled_count"]
        else ("已确认" if proof["unknown_count"] else "")
        + f"模型实评{model['settled_count']}笔"
    )
    if groups["MODEL_APPROVED"]["settled_count"]:
        model_text += f"（通过{groups['MODEL_APPROVED']['settled_count']}）"
    lines = [
        f"JEV（已结算成交子集{denominator['settled_count']}笔，非全部候选、非模型收益证明）：{model_text}。"
    ]
    details = []
    for key, label in (
        ("MODEL_REJECTED", "模型否决后成交"),
        ("FALLBACK", "回退"),
        ("DISABLED", "禁用"),
        ("NOT_MODEL_EVALUATED", "未实评"),
        ("UNKNOWN", "未知"),
    ):
        if groups[key]["settled_count"]:
            detail = f"{label}{groups[key]['settled_count']}笔"
            if key == "FALLBACK":
                budget = [
                    f"{label}{causes[cause]['settled_count']}"
                    for cause, label in (
                        ("TOKEN_BUDGET_PACED", "预算节流"),
                        ("TOKEN_BUDGET_EXHAUSTED", "预算耗尽"),
                    )
                    if cause in causes and causes[cause]["settled_count"]
                ]
                if budget:
                    detail += "（" + "、".join(budget) + "）"
            details.append(detail)
    if details:
        lines.append("；".join(details) + "。")
    return lines


def _frozen_replay_digest(report):
    if "frozen_replay_review" not in report:
        return []
    unknown = ["冻结输入重算：证据尚未核验；不把指标比较当成完整决策回放。"]
    review = _bound_review(
        report, "frozen_replay_review", "daily-mover-frozen-replay-v1"
    )
    evidence = report.get("account_evidence")
    if (
        review is None
        or review.get("status") not in {"AVAILABLE", "PARTIAL"}
        or not isinstance(evidence, dict)
        or type(evidence.get("as_of_ms")) is not int
        or review.get("evidence_as_of_ms") != evidence["as_of_ms"]
        or review.get("basis") != "CURRENT_EVALUATOR_ON_FROZEN_INPUTS_BASELINE_CHECKED"
        or review.get("historical_code_identity") != "NOT_PROVEN"
    ):
        return unknown
    counts = review.get("summary")
    keys = (
        "requested_count",
        "replayed_count",
        "baseline_matched_count",
        "baseline_mismatch_count",
        "unknown_count",
        "volume_gate_changed_count",
        "new_market_candidate_count",
    )
    if not isinstance(counts, dict) or any(
        type(counts.get(k)) is not int or not 0 <= counts[k] <= 100 for k in keys
    ):
        return unknown
    if not (
        counts["replayed_count"] + counts["unknown_count"] == counts["requested_count"]
        and counts["baseline_matched_count"] + counts["baseline_mismatch_count"]
        == counts["replayed_count"]
        and counts["new_market_candidate_count"]
        <= counts["volume_gate_changed_count"]
        <= counts["baseline_matched_count"]
    ):
        return unknown
    return [
        (
            f"冻结输入重算{counts['replayed_count']}条、基线吻合{counts['baseline_matched_count']}条，"
            f"量能单门改变{counts['volume_gate_changed_count']}条、行情候选新增{counts['new_market_candidate_count']}条；"
            f"{counts['unknown_count']}条未知、{counts['baseline_mismatch_count']}条基线不符。"
            "当前代码重算，历史代码身份未证明；不等于成交或盈利，未完成样本外验证。"
        )
    ]


def _setup_study_digest(report):
    if "setup_study" not in report:
        return []
    unknown = ["分层量能研究：证据尚未核验，不改变开仓规则。"]
    study = _bound_review(report, "setup_study", "setup-shadow-study-v1")
    if study is None or study.get("status") not in {"AVAILABLE", "PARTIAL"}:
        return unknown
    summary = study.get("summary")
    if not isinstance(summary, dict):
        return unknown
    keys = (
        "selected_count",
        "baseline_matched_count",
        "baseline_mismatch_count",
        "unknown_count",
    )
    if any(
        type(summary.get(k)) is not int or not 0 <= summary[k] <= 96 for k in keys
    ) or summary["selected_count"] != sum(summary[k] for k in keys[1:]):
        return unknown
    alternatives = summary.get("alternatives")
    if not isinstance(alternatives, dict):
        return unknown
    added = []
    for name in ("BREAKOUT_VOLUME075", "PULLBACK_VOLUME075"):
        item = alternatives.get(name)
        count = (
            item.get("new_market_candidate_count") if isinstance(item, dict) else None
        )
        if (
            type(count) is not int
            or not 0 <= count <= summary["baseline_matched_count"]
        ):
            return unknown
        added.append(count)
    lines = [
        (
            f"分层量能研究（不限榜单）：{summary['selected_count']}个固定去重机会，"
            f"{summary['baseline_matched_count']}可比；突破方案新增{added[0]}、回踩方案新增{added[1]}行情候选。"
            "有界时段样本，非全量；未验证盈利，不改开仓规则。"
        )
    ]
    paths = _bound_review(report, "setup_path_review", "setup-shadow-observed-paths-v1")
    if paths is None or any(
        paths.get(k) != study.get(source)
        for k, source in (
            ("study_content_digest", "content_digest"),
            ("plan_digest", "plan_digest"),
            ("input_digest", "input_digest"),
        )
    ):
        return lines
    if paths.get("status") not in {"AVAILABLE", "NO_SAMPLES"}:
        return lines + ["分层后续路径：证据尚未核验，不据此判断盈亏。"]
    cases = paths.get("cases")
    if not isinstance(cases, list) or len(cases) > 12:
        return lines
    counts = []
    for horizon in ("30", "60", "240"):
        windows = [
            case.get("windows", {}).get(horizon)
            for case in cases
            if isinstance(case, dict) and isinstance(case.get("windows"), dict)
        ]
        if len(windows) != len(cases) or any(not isinstance(w, dict) for w in windows):
            return lines
        counts.append(sum(w.get("status") == "OBSERVED" for w in windows))
    lines.append(
        f"30/60/240分钟有效路径：{counts[0]}/{counts[1]}/{counts[2]}（固定抽取{len(cases)}个机会）；仅价格观察，不是成交或净收益。"
    )
    return lines


def format_digest(
    report, *, account_alias=None, _include_examples=True, _max_reasons=2
):
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
    execution_coverage = _verified_optional_review(
        report, "coverage_review", "mover-coverage-review-v1"
    )
    current_absent = set()
    if (
        execution_coverage is not None
        and execution_coverage.get("account_environment")
        == report["account_environment"]
        and execution_coverage.get("as_of_ms") == report["as_of_ms"]
        and execution_coverage.get("market_source") == report["market_source"]
        and isinstance(execution_coverage.get("rows"), list)
        and len(execution_coverage["rows"]) == len(report["rows"]) <= 20
        and all(
            isinstance(review_row, dict)
            and review_row.get("symbol") == report_row.get("symbol")
            and review_row.get("direction") == report_row.get("direction")
            for review_row, report_row in zip(
                execution_coverage["rows"], report["rows"], strict=True
            )
        )
    ):
        snapshot = execution_coverage.get("snapshot", {})
        observed = (
            snapshot.get("observed_at_ms") if isinstance(snapshot, dict) else None
        )
        if type(observed) is int and 0 <= observed <= report["as_of_ms"]:
            current_absent = {
                row.get("symbol")
                for row in execution_coverage.get("rows", [])
                if isinstance(row, dict)
                and isinstance(row.get("symbol"), str)
                and row.get("current_listing") == "ABSENT_FROM_CURRENT_CATALOG"
            }
    source = "LIVE" if report["market_source"] == "BINANCE_USDM_LIVE" else "来源未核实"
    lines = [
        f"📊 每日大行情复盘 · {report['window']['date']} UTC+8",
        f"{alias} · {report['account_environment']}（实际成交） / {source}（榜单）",
    ]
    # Mandatory, before rankings: selected winners must not hide losses in the
    # remaining closed trades. This block is never evicted by optional budgets.
    lines += _account_day_digest(report)
    lines += _jev_participation_digest(report)
    lines += _execution_cost_digest(report)
    lines.append(
        f"固定自然日；覆盖 {coverage.get('eligible', 0)}/{coverage.get('discovered', 0)}，排除 {coverage.get('excluded', 0)}。"
    )
    if report["universe_basis"] != "HISTORICAL_SNAPSHOT":
        lines.append("当前目录回看：历史下架标的覆盖未证明。")
    if current_absent:
        label = "Testnet" if report["account_environment"] == "SANDBOX" else "LIVE"
        lines.append(
            f"复盘时{label}目录未收录{len(current_absent)}个榜单合约；当日上市状态仍未知。"
        )
    if any(row["evidence"].get("sampled_attribution") for row in report["rows"]):
        lines.append("入场理由：分层样本，非全量归因；样本计数不代表全日总量。")
    versions = {
        row["evidence"].get("classification_version")
        for row in report["rows"]
        if row["evidence"].get("signal_count")
    }
    if versions == {_DIRECTIONAL_CLASSIFICATION}:
        lines.append(
            "同向入场事件单独统计；反向/非入场事件排除。过期来源未证明时不归因于消费延迟。"
        )
    elif _DIRECTIONAL_CLASSIFICATION in versions:
        lines.append(
            "混合口径：新版已排除反向/非入场事件，旧版未排除；过期数不能直接当作错过开仓。"
        )
    else:
        lines.append("旧版样本口径：可能含反向/非入场事件；过期数不代表错过开仓。")
    for group, title in (("winners", "涨幅前10"), ("losers", "跌幅前10")):
        chosen = [row for row in report["rows"] if row["group"] == group]
        lines.append("\n" + title + f"（{len(chosen)}）")
        for row in chosen:
            with localcontext() as ctx:
                ctx.prec = 60
                change = format(_number(row["day_return_pct"]), "+.2f")
            reason = _row_reason(
                row["evidence"], examples=_include_examples, max_reasons=_max_reasons
            )
            if (
                row["symbol"] in current_absent
                and row["evidence"].get("primary_reason")
                == "NO_RETAINED_SIGNAL_UNKNOWN"
            ):
                reason += "；复盘时执行目录未收录"
            lines.append(f"{row['rank']}. {row['symbol']} {change}% · {reason}")
    # New reports must not bypass strict execution review via the legacy raw
    # path formatter. Keep legacy compatibility only for historical versions.
    if "execution_review" not in report:
        lines += _actual_path_digest(report)
    execution_lines = _execution_review_digest(report)
    if "execution_review" in report and not execution_lines:
        execution_lines = ["暂无可展示的已核验结算摘要；不把缺失证据当作零收益。"]
    replay_lines = _frozen_replay_digest(report) + _setup_study_digest(report)
    for detail in execution_lines:
        # Preserve room for mandatory methodology; optional detail may disappear
        # on a dense twenty-symbol report, never an entire ranking row.
        if _units("\n".join([*lines, detail, *replay_lines])) <= 2900:
            lines.append(detail)
    lines += [
        "",
        f"累计 {accumulation['unique_market_days']} 个行情日 / {accumulation['unique_account_days']} 个账户日；对照 {accumulation['controls']['count']} 个标的日。",
        "样本仍不足，继续积累。"
        if accumulation["sample_status"] == "INSUFFICIENT_SAMPLE"
        else "只形成描述性对照，尚未证明预测优势。",
        "复盘重点：信号覆盖 → 过滤/JEV 理由 → 成交与退出；每项假设均记录下一步证据和推翻条件。",
        "榜单/盘中极值是事后观察，不是可实现利润；测试成交不与实盘价格拼接算收益。不会自动改风控或下单。",
    ]
    lines += replay_lines
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
                if _units("\n".join([*lines, detail])) <= 3500:
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
        if detail is not None and _units("\n".join([*lines, detail])) <= 3500:
            lines.append(detail)
    shadow = report.get("gate_shadow")
    if (
        "frozen_replay_review" not in report
        and isinstance(shadow, dict)
        and shadow.get("version") == "mover-gate-shadow-v1"
        and shadow.get("execution_authorized") is False
        and shadow.get("not_trade_prediction") is True
    ):
        summary = shadow.get("summary")
        if isinstance(summary, dict):
            fields = (
                "replayed_count",
                "metric_only_count",
                "volume_gate_changed_count",
                "other_gate_still_failed_count",
            )
            if all(
                type(summary.get(k)) is int and 0 <= summary[k] <= 10000 for k in fields
            ):
                detail = (
                    f"规则影子对照：完整回放{summary['replayed_count']}条、仅指标对照"
                    f"{summary['metric_only_count']}条，量能单门改变{summary['volume_gate_changed_count']}条；"
                    "指标对照不判断其他门是否通过，不等于会成交或盈利，未完成样本外验证。"
                )
                if _units("\n".join([*lines, detail])) <= 3500:
                    lines.append(detail)
    text = "\n".join(lines)
    if _units(text) > 3500 and _include_examples:
        # Numeric examples are optional. Preserve every top symbol and its
        # reason counts rather than losing the entire digest to payload limits.
        return format_digest(
            report,
            account_alias=account_alias,
            _include_examples=False,
            _max_reasons=_max_reasons,
        )
    if _units(text) > 3500 and _max_reasons > 1:
        return format_digest(
            report, account_alias=account_alias, _include_examples=False, _max_reasons=1
        )
    if _units(text) > 3500:
        # With <=20 validated bounded symbols this cannot happen in normal
        # reports. Refuse malformed input rather than silently drop a top row.
        raise ValueError("DAILY_REPORT_DIGEST_TOO_LARGE")
    return text
