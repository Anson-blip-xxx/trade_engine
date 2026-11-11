"""Bounded chronological market-gate comparison, never an execution backtest.

Only the caller can read an archive. Evaluators receive the same trailing closed
minute candles at each fixed UTC quarter hour, and no later prices or labels.
Historical publication latency, account admission, fills and exits are unknown.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone

from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol as validate_symbol

MINUTE = 60000
DAY = 1440 * MINUTE
STEP = 15 * MINUTE
VERSION = "trend-entry-causal-gate-review-v1"
SIDES = ("BUY", "SELL")
VARIANTS = ("BASELINE", "CANDIDATE")


def _scope(symbols, start_ms, end_ms):
    if (
        not isinstance(symbols, (list, tuple))
        or not 1 <= len(symbols) <= 12
        or len(set(symbols)) != len(symbols)
        or any(type(t) is not int for t in (start_ms, end_ms))
        or not DAY <= start_ms <= end_ms < 2**53
        or start_ms % STEP
        or end_ms % STEP
        or end_ms - start_ms > 2 * DAY
    ):
        raise ValueError("TREND_REVIEW_SCOPE_BOUND")
    return tuple(sorted(validate_symbol(item) for item in symbols))


def collect_review_candles(
    read, symbols, *, start_ms, end_ms, max_archive_minutes=60000,
    max_archive_seconds=90,
):
    """Reuse the audit reader's quota, deadline and bounded 720-minute pages.

    The injected read port must provide the chosen environment. Returned bars
    are historical observations, not evidence they reached a consumer on time.
    """
    from services.v2_profitability_audit import AuditPolicy, BudgetArchive

    targets = _scope(symbols, start_ms, end_ms)
    reader = BudgetArchive(
        read,
        AuditPolicy(
            max_trades=1, trend_cases=0,
            max_archive_minutes=max_archive_minutes,
            max_archive_seconds=max_archive_seconds,
        ),
    )
    candles, errors = {}, {}
    for target in targets:
        try:
            candles[target] = reader(target, start_ms - DAY, end_ms)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            candles[target] = []
            errors[target] = type(exc).__name__
    return {
        "candles": candles,
        "errors": errors,
        "archive_calls": reader.calls,
        "requested_minutes": reader.requested_minutes,
        "historical_availability_at_entry": "NOT_VERIFIED",
    }


def make_frame_evaluator(*, feature_version, trend_enabled):
    """Actual S3 event presence plus a market gate; no absent-event probes.

    Baseline reconstructs PRICE_VERSION/confirmation-v1 semantics in the caller's
    source checkout. It is not an isolated import of a deployed release. The
    caller must freeze its policy and record its source revision separately.
    """
    if type(trend_enabled) is not bool or not isinstance(feature_version, str):
        raise ValueError("TREND_REVIEW_EVALUATOR_CONFIG")

    def evaluate_frame(batch, *, previous, regime, policy):
        from services.v2_directional_replay import market_plan
        from services.v2_s3_candles import build_frame
        from services.v2_s3_runtime import evaluate
        from v2_core.producer import market_envelope

        target = next(iter(batch["candles"]))
        at = batch["closed_at"]
        environment = batch["environment"]
        frame = build_frame(
            batch, environment=environment, feature_version=feature_version
        )
        events, state = evaluate(
            frame["windows"], frame["raw_windows"], previous, at
        )
        market = frame["windows"][target]
        context = {
            "symbol": target, "environment": environment,
            "assembled_at": at, "market_only": True,
            "sources": {
                "s3": market_envelope("s3", environment, target, at, market)
            },
            "directional": {
                "market": market, "price": market["15m"]["close"],
                "regime": regime, "short_ratio": None,
            },
        }
        settings = {**policy, "entry.trend_structure_enabled": trend_enabled}
        if not trend_enabled:
            settings["entry.confirmation_version"] = 1
        decisions = {}
        for side, event_type, strategy in (
            ("BUY", "TREND_UP", "S6"), ("SELL", "TREND_DOWN", "S8")
        ):
            emitted = [event for event in events if event["type"] == event_type]
            if len(emitted) > 1:
                raise ValueError("TREND_REVIEW_DUPLICATE_EVENT")
            if not emitted:
                decisions[side] = {
                    "event_present": False, "eligible": False,
                    "reason": "NO_TREND_EVENT",
                }
                continue
            event = emitted[0]
            signal = {
                "symbol": target, "signal": event_type, "features": event,
                "observed_at": at, "expires_at_ms": at + 30000,
            }
            config = {"source": "s3", "strategy": strategy, "policy": settings}
            if trend_enabled:
                from services.v2_trend_entry import evaluate_trend_market

                plan, proof = evaluate_trend_market(signal, context, config)
            else:
                plan, proof = market_plan(signal, context, config)
            decisions[side] = {
                "event_present": True, "eligible": plan.reason == "CANDIDATE",
                "reason": plan.reason, "event_strength": event["strength"],
                "plan": asdict(plan), "proof": proof,
            }
        return {"sides": decisions, "state": state}

    return evaluate_frame


def _unknown(reason):
    return {
        "status": "UNKNOWN", "reason": reason,
        "event_present": None, "eligible": None,
    }


def review_trend_entries(
    candles_by_symbol, *, start_ms, end_ms, baseline_evaluator,
    candidate_evaluator, cohort_basis, regime_scenario, policy=None,
    environment="SANDBOX",
):
    """Fixed-quarter-hour paired decisions, both sides, chronological per symbol.

    Day-end-selected movers are diagnostic cases, never an out-of-sample cohort.
    Bars at or after a cutoff are not given to either evaluator. A missing minute
    resets detector state and marks the window unknown instead of filling gaps.
    """
    if not isinstance(candles_by_symbol, dict):
        raise TypeError("TREND_REVIEW_INPUT_REQUIRED")
    targets = _scope(list(candles_by_symbol), start_ms, end_ms)
    if (
        environment not in {"SANDBOX", "LIVE"}
        or regime_scenario not in {
            "strong_bull", "weak_bull", "neutral", "weak_bear", "risk-off", "risk_off"
        }
        or not isinstance(cohort_basis, str)
        or not 1 <= len(cohort_basis) <= 240
        or not callable(baseline_evaluator)
        or not callable(candidate_evaluator)
        or policy is not None and not isinstance(policy, dict)
    ):
        raise ValueError("TREND_REVIEW_CONFIGURATION")
    frozen_policy = deepcopy(policy or {})
    evaluators = dict(zip(VARIANTS, (baseline_evaluator, candidate_evaluator), strict=True))
    decisions, summaries = [], []
    for target in targets:
        rows = candles_by_symbol[target]
        if not isinstance(rows, list) or len(rows) > 4320:
            raise ValueError("TREND_REVIEW_CANDLE_BOUND")
        indexed = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("t")) is not int:
                raise ValueError("TREND_REVIEW_CANDLE_TIMESTAMP")
            at = row["t"]
            if at % MINUTE or not start_ms - DAY <= at <= end_ms:
                raise ValueError("TREND_REVIEW_CANDLE_SCOPE")
            if at in indexed:
                raise ValueError("TREND_REVIEW_DUPLICATE_MINUTE")
            indexed[at] = row
        states = {variant: {} for variant in VARIANTS}
        counts = {(variant, side): Counter() for variant in VARIANTS for side in SIDES}
        first = {(variant, side): None for variant in VARIANTS for side in SIDES}
        for cutoff in range(start_ms, end_ms + 1, STEP):
            timestamps = range(cutoff - DAY, cutoff, MINUTE)
            complete = all(t in indexed for t in timestamps)
            batch = {
                "source": "BINANCE_FUTURES", "environment": environment,
                "interval": "1m", "closed_at": cutoff,
                "candles": {
                    target: [deepcopy(indexed[t]) for t in reversed(timestamps)]
                    if complete else []
                },
            }
            fingerprint = digest(canonical(batch)) if complete else None
            paired = {}
            for variant, evaluator in evaluators.items():
                if not complete:
                    states[variant] = {}
                    sides = {side: _unknown("INCOMPLETE_CLOSED_HISTORY") for side in SIDES}
                else:
                    try:
                        evaluated = evaluator(
                            deepcopy(batch), previous=deepcopy(states[variant]),
                            regime=regime_scenario, policy=deepcopy(frozen_policy),
                        )
                        sides = evaluated["sides"]
                        if set(sides) != set(SIDES) or any(
                            type(sides[side].get("eligible")) is not bool
                            or type(sides[side].get("event_present")) is not bool
                            or sides[side]["eligible"] and not sides[side]["event_present"]
                            or not isinstance(sides[side].get("reason"), str)
                            for side in SIDES
                        ):
                            raise ValueError("TREND_REVIEW_DECISION_CONTRACT")
                        states[variant] = deepcopy(evaluated["state"])
                        sides = {side: {**deepcopy(sides[side]), "status": "OBSERVED"} for side in SIDES}
                    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                        states[variant] = {}
                        sides = {side: _unknown("EVALUATOR_" + type(exc).__name__) for side in SIDES}
                paired[variant] = sides
                for side, result in sides.items():
                    counts[variant, side][result["reason"]] += 1
                    if result["eligible"] is True and first[variant, side] is None:
                        first[variant, side] = cutoff
            decisions.append({
                "symbol": target, "closed_at_ms": cutoff,
                "input_digest": fingerprint, "variants": paired,
            })
        summaries.append({
            "symbol": target,
            "variants": {
                variant: {
                    side: {
                        "reason_counts": dict(sorted(counts[variant, side].items())),
                        "first_eligible_ms": first[variant, side],
                        "eligible_count": sum(
                            row["variants"][variant][side]["eligible"] is True
                            for row in decisions if row["symbol"] == target
                        ),
                    } for side in SIDES
                } for variant in VARIANTS
            },
        })
    decisions.sort(key=lambda row: (row["closed_at_ms"], row["symbol"]))
    return {
        "version": VERSION, "status": "RETROSPECTIVE_MARKET_GATE_COMPARISON",
        "environment": environment, "symbols": list(targets),
        "start_ms": start_ms, "end_ms": end_ms, "step_minutes": 15,
        "history_minutes": 1440, "cohort_basis": cohort_basis,
        "regime_scenario": regime_scenario,
        "regime_basis": "FIXED_SCENARIO_NOT_OBSERVED_HISTORICAL_S0",
        "baseline_basis": "PRICE_VERSION_CONFIRMATION_V1_RECONSTRUCTION_NOT_DEPLOYED_BINARY",
        "policy_digest": digest(canonical(frozen_policy)),
        "policy_overrides": frozen_policy,
        "policy_basis": "SUPPLIED_SELECTED_KEYS_REMAINDER_SOURCE_DEFAULTS",
        "decisions": decisions, "summaries": summaries,
        "decision_digest": digest(canonical({"decisions": decisions})),
        "historical_availability_at_entry": "NOT_VERIFIED",
        "detector_state_basis": "EMPTY_AT_START_THEN_SEQUENTIAL_15M_NOT_ORIGINAL_1M_STATE",
        "full_account_or_jev_admission": "NOT_RUN",
        "tick_rounded_sizing_and_reward_risk": "NOT_RUN",
        "alternate_orders_or_pnl": None, "profitability_validated": False,
        "out_of_sample_validated": False, "execution_authorized": False,
    }


def summarize_review(report, *, examples_per_day=2):
    """Compact all observed days/sides, retaining controls and rejection counts."""
    if type(examples_per_day) is not int or not 0 <= examples_per_day <= 4:
        raise ValueError("TREND_REVIEW_EXAMPLE_BOUND")
    grouped = {}
    for row in report["decisions"]:
        day = datetime.fromtimestamp(
            (row["closed_at_ms"] + 8 * 60 * MINUTE) / 1000, timezone.utc
        ).date().isoformat()
        for variant in VARIANTS:
            for side in SIDES:
                key = (day, row["symbol"], variant, side)
                summary = grouped.setdefault(key, {
                    "date_utc8": day, "symbol": row["symbol"],
                    "variant": variant, "side": side,
                    "observations": 0, "eligible_count": 0, "unknown_count": 0,
                    "first_eligible_ms": None, "reason_counts": {}, "examples": [],
                })
                result = row["variants"][variant][side]
                summary["observations"] += 1
                summary["unknown_count"] += int(result["status"] == "UNKNOWN")
                reasons = summary["reason_counts"]
                reasons[result["reason"]] = reasons.get(result["reason"], 0) + 1
                if result["eligible"] is True:
                    summary["eligible_count"] += 1
                    summary["first_eligible_ms"] = summary["first_eligible_ms"] or row["closed_at_ms"]
                    if len(summary["examples"]) < examples_per_day:
                        summary["examples"].append({
                            "closed_at_ms": row["closed_at_ms"],
                            "input_digest": row["input_digest"],
                            "decision": deepcopy(result),
                        })
    return {
        **{key: deepcopy(value) for key, value in report.items() if key != "decisions"},
        "daily_summaries": [grouped[key] for key in sorted(grouped)],
    }
