"""Offline paired ablation. Independent hypotheses, not TradingView replicas.

Same input, entry family, execution model and costs across variants. No parameter
search, venue transport or signal publication. Holdout always starts flat, using
earlier bars only for indicator warmup. Results are engineering research only.
"""

import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from zoneinfo import ZoneInfo

from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.quant_indicators import aggregate_minutes, decimal, encode
from v2_core.quant_registry import parameters, warmup
from v2_core.quant_replay import ReplayPolicy, replay
from v2_core.range_volume import VARIANTS, VERSION, RangeVolumePolicy, assess, gate
from v2_core.state import BusinessState, StateKey

STUDY_VERSION = "range-volume-ablation-v1"


def summarize(result):
    trades = result["trades"]
    net = [decimal(t["net_pnl"]) for t in trades]
    profits = sum((p for p in net if p > 0), Decimal(0))
    losses = -sum((p for p in net if p < 0), Decimal(0))
    observations = [d for d in result["decisions"] if "research_gate" in d]
    return {
        **result["summary"],
        "win_rate": encode(Decimal(sum(p > 0 for p in net)) / len(net))
        if net
        else None,
        "profit_factor": encode(profits / losses) if losses else None,
        "profit_factor_status": "DEFINED" if losses else "NO_REALIZED_LOSSES",
        "mean_net_pnl": encode(sum(net, Decimal(0)) / len(net)) if net else None,
        "mean_hold_minutes_lower_bound": encode(
            sum(
                (
                    Decimal(t["closed_bar_at_ms"] - t["opened_at_ms"]) / 60000
                    for t in trades
                ),
                Decimal(0),
            )
            / len(trades)
        )
        if trades
        else None,
        "candidate_count": len(observations),
        "allowed_candidates": sum(d["research_gate"]["allow"] for d in observations),
        "filter_reasons": dict(
            Counter(
                reason for d in observations for reason in d["research_gate"]["reasons"]
            )
        ),
        "open_position": result["open_position"] is not None,
        "pending_signal": result["pending_signal"],
    }


def compare(
    bars,
    *,
    timeframe_minutes,
    plugin="donchian",
    params=None,
    policy=None,
    range_policy=None,
    split_percent=70,
    max_seconds=120,
    monotonic=time.monotonic,
):
    if (
        plugin not in ("donchian", "squeeze")
        or type(split_percent) is not int
        or not 50 <= split_percent <= 90
        or type(max_seconds) is not int
        or not 1 <= max_seconds <= 300
        or not isinstance(bars, (tuple, list))
        or not 1 <= len(bars) <= 3000
    ):
        raise ValueError("STUDY_BOUNDS")
    p = parameters(params)
    policy = ReplayPolicy() if policy is None else policy
    range_policy = RangeVolumePolicy() if range_policy is None else range_policy
    if not isinstance(policy, ReplayPolicy) or not isinstance(
        range_policy, RangeVolumePolicy
    ):
        raise TypeError("STUDY_POLICY")
    assess(bars, timeframe_minutes=timeframe_minutes, policy=range_policy)
    split = len(bars) * split_percent // 100
    required = max(warmup(p), range_policy.required)
    if split < required + 10 or len(bars) - split < 10:
        raise ValueError("STUDY_INSUFFICIENT_SPLIT_HISTORY")
    boundary = bars[split].t
    started = monotonic()
    folds = {}
    # No learned threshold from the holdout, no account capital carried across.
    for fold, series, entry_start in (
        ("development", bars[:split], None),
        ("holdout", bars[split - required :], boundary),
    ):
        runs = {}
        for variant in VARIANTS:
            if monotonic() - started >= max_seconds:
                raise TimeoutError("STUDY_COMPUTE_BUDGET")
            runs[variant] = replay(
                series,
                timeframe_minutes=timeframe_minutes,
                plugin=plugin,
                params=p,
                policy=policy,
                entry_start_ms=entry_start,
                entry_gate=partial(
                    gate,
                    timeframe_minutes=timeframe_minutes,
                    variant=variant,
                    policy=range_policy,
                ),
            )
        base = runs["baseline"]
        baseline_pnl = decimal(base["summary"]["realized_net_pnl"])
        variants = {}
        for variant, result in runs.items():
            by_time = {
                d["closed_at_ms"]: d["research_gate"]["allow"]
                for d in result["decisions"]
                if "research_gate" in d
            }
            rejected = [
                t for t in base["trades"] if not by_time[t["signal_at_ms"]]
            ]
            variants[variant] = {
                "summary": summarize(result),
                "net_pnl_difference_from_baseline": encode(
                    decimal(result["summary"]["realized_net_pnl"]) - baseline_pnl
                ),
                "baseline_opportunity_audit": {
                    "blocked_profitable_trades": sum(
                        decimal(t["net_pnl"]) > 0 for t in rejected
                    ),
                    "blocked_losing_trades": sum(
                        decimal(t["net_pnl"]) < 0 for t in rejected
                    ),
                    "blocked_baseline_net_pnl": encode(
                        sum((decimal(t["net_pnl"]) for t in rejected), Decimal(0))
                    ),
                    "interpretation": "MATCHED_BASELINE_SIGNALS_NOT_ADDITIVE_PORTFOLIO_CAUSAL_EFFECT",
                },
                "trades": result["trades"],
                "entry_attempts": result["entry_attempts"],
            }
        folds[fold] = {
            "variants": variants,
            "candidate_observations": [
                d for d in base["decisions"] if "research_gate" in d
            ],
        }
    stress = {}
    stressed_policy = replace(
        policy,
        fee_rate=encode(decimal(policy.fee_rate) * 2),
        slippage_fraction=encode(decimal(policy.slippage_fraction) * 2),
    )
    for variant in VARIANTS:
        if monotonic() - started >= max_seconds:
            raise TimeoutError("STUDY_COMPUTE_BUDGET")
        stress[variant] = summarize(
            replay(
                bars[split - required :],
                timeframe_minutes=timeframe_minutes,
                plugin=plugin,
                params=p,
                policy=stressed_policy,
                entry_start_ms=boundary,
                entry_gate=partial(
                    gate,
                    timeframe_minutes=timeframe_minutes,
                    variant=variant,
                    policy=range_policy,
                ),
            )
        )
    return {
        "version": STUDY_VERSION,
        "feature_version": VERSION,
        "execution_authorized": False,
        "publication": "SHADOW_ONLY",
        "plugin": plugin,
        "timeframe_minutes": timeframe_minutes,
        "input_digest": digest(canonical({"bars": [b.payload() for b in bars]})),
        "bar_count": len(bars),
        "split_percent": split_percent,
        "holdout_starts_at_ms": boundary,
        "holdout_starts_at_utc8": datetime.fromtimestamp(
            boundary / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "policy": asdict(policy),
        "parameters": p,
        "range_policy": asdict(range_policy),
        "folds": folds,
        "double_cost_holdout": stress,
        "assumptions": base["assumptions"],
        "limitations": [
            "NOT_A_REPLAY_OF_LIVE_TV_OR_S3_DECISIONS",
            "NO_HISTORICAL_FUNDING_SUPPLIED",
            "DEFAULT_VENUE_FILTERS_ARE_ASSUMPTIONS_NOT_HISTORICAL_RULES",
            "INDEPENDENT_SINGLE_SYMBOL_EQUITY_NOT_SUMMABLE_AS_PORTFOLIO",
            "NO_LIQUIDITY_OR_LISTING_ELIGIBILITY_VALIDATION",
            "FIXED_SPLIT_WITH_FIXED_PARAMETERS_NOT_WALK_FORWARD_VALIDATION",
            "SHORT_SAMPLE_NOT_PROFITABILITY_OR_RELEASE_APPROVAL",
        ],
    }


def archive_study(
    archive, *, target, end_ms, minutes=2880, timeframe_minutes=5, **kwargs
):
    symbol(target)
    if (
        archive.environment != "SANDBOX"
        or type(end_ms) is not int
        or type(minutes) is not int
        or not 1440 <= minutes <= 14400
        or type(timeframe_minutes) is not int
        or timeframe_minutes not in (1, 5, 15, 30, 60)
        or end_ms < minutes * 60000
        or end_ms % 60000
        or minutes // timeframe_minutes > 3000
    ):
        raise ValueError("STUDY_ARCHIVE_SCOPE")
    closed = end_ms // (timeframe_minutes * 60000) * timeframe_minutes * 60000
    start = closed - minutes * 60000
    rows = archive.read(target, start, closed)
    if len(rows) != minutes:
        return {
            "status": "INCOMPLETE_HISTORY",
            "symbol": target,
            "execution_authorized": False,
            "required_minutes": minutes,
            "available_minutes": len(rows),
        }
    if rows[-1]["t"] != start or rows[0]["t"] != closed - 60000:
        raise ValueError("STUDY_ARCHIVE_BOUNDARY")
    bars = aggregate_minutes(
        list(reversed(rows)), timeframe_minutes=timeframe_minutes, closed_at_ms=closed
    )
    result = compare(bars, timeframe_minutes=timeframe_minutes, **kwargs)
    return {
        "status": "SIMULATED",
        "environment": "SANDBOX",
        "symbol": target,
        "closed_at_ms": closed,
        "source_digest": digest(canonical({"minutes": rows})),
        **result,
    }


def persist(connect, report):
    """Immutable PG-first research record. No account, signal or order writes."""
    if (
        report.get("status") != "SIMULATED"
        or report.get("environment") != "SANDBOX"
        or report.get("version") != STUDY_VERSION
        or report.get("execution_authorized") is not False
        or report.get("publication") != "SHADOW_ONLY"
    ):
        raise ValueError("STUDY_PERSIST_SCOPE")
    symbol(report["symbol"])
    payload = canonical(report)
    if len(payload.encode()) > 4_000_000:
        raise ValueError("STUDY_RECORD_BOUND")
    identity = digest(payload)
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "range-volume-study-v1",
        identity,
    )
    store = BusinessState(connect)
    saved = store.read(key)
    if saved is None:
        store.change(
            key,
            expected_version=0,
            request_key="study:" + identity,
            payload=report,
            reason="RANGE_VOLUME_SHADOW_RESEARCH",
        )
        saved = store.read(key)
    if (
        saved is None
        or saved.deleted
        or saved.version != 1
        or saved.payload_json != payload
    ):
        raise ValueError("STUDY_IMMUTABLE_CONFLICT")
    return {
        "study_id": identity,
        "state_id": key.identity,
        "version": saved.version,
        "execution_authorized": False,
        "publication": "SHADOW_ONLY",
    }
