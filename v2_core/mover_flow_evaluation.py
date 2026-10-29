"""Fixed-rule, event-time research comparisons; no fitting, I/O or execution.

The universe is the independently selected fixed cohort, including missing cases.
BUY/SELL mirrors are one observation. Historical receipt remains unproven.
"""

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest

VERSION = "mover-flow-fixed-rule-evaluation-v1"
HOUR = 3_600_000
DAY = 24 * HOUR
_CONTEXT = Context(prec=80, rounding=ROUND_HALF_EVEN)
_SPLITS = ("EXPLORATORY", "RESERVED_TIME_HOLDOUT", "POST_WINDOW")
_GROUPS = {
    "spot": ("SAME_DIRECTION_PERSISTENT", "OTHER"),
    "open_interest": ("INCREASING", "NONINCREASING"),
    "funding": ("DIRECTION_PAYS", "RECEIVES_OR_ZERO"),
}
_PROTOCOL = {
    "baseline": "PRICE_MOMENTUM_SIGN_3H",
    "direction_basis": "SIGN_OF_BUY_SNAPSHOT_PRE_CUTOFF_3H_RETURN_ZERO_ABSTAINS",
    "cohort_basis": "FIXED_COHORT_ONLY_TOP_ONLY_EXCLUDED_OVERLAP_INCLUDED",
    "cutoff_hours_utc8": [6, 12, 18],
    "outcome_horizon_hours": 6,
    "continuation": "DIRECTIONAL_END_RETURN_PCT_GT_0",
    "big_move_research_threshold_pct": "5",
    "spot_rule": "BOTH_BASELINE_AND_RECENT_SIDE_ADJUSTED_IMBALANCE_GT_0",
    "open_interest_rule": "BASE_UNIT_CHANGE_GT_0",
    "funding_rule": "LAST_SETTLED_RATE_TIMES_DIRECTION_GT_0",
    "exploratory_through": "2026-10-28",
    "reserved_holdout_start": "2026-10-29",
    "reserved_holdout_end": "2026-11-11",
    "minimum_holdout_days": 14,
    "minimum_holdout_day_symbol_clusters": 100,
    "review_readiness_basis": "DAYS_AND_CLUSTERS_WITH_OUTCOME_AND_ALL_THREE_COMPONENT_FLAGS",
    "readiness_meaning": "DESCRIPTIVE_REVIEW_ONLY_NEVER_VALIDATION_OF_EDGE",
    "comparison_basis": "SAME_COMPONENT_AVAILABLE_BASELINE_SLOTS_INCLUDING_MISSING_OUTCOMES",
    "historical_receipt_proven": False,
}


def _symbol(value):
    return (
        isinstance(value, str)
        and 5 <= len(value) <= 44
        and value.endswith("USDT")
        and all(c.isalnum() or c == "_" for c in value[:-4])
    )


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("INVALID_DECIMAL")  # noqa: TRY004 - stable evidence code
    if len(str(value)) > 120:
        raise ValueError("DECIMAL_BOUND")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_DECIMAL") from None
    if not result.is_finite():
        raise ValueError("INVALID_DECIMAL")
    if (
        not -100 <= result.as_tuple().exponent <= 40
        or len(result.as_tuple().digits) > 80
    ):
        raise ValueError("DECIMAL_BOUND")
    if result and not -40 <= result.adjusted() <= 40:
        raise ValueError("DECIMAL_BOUND")
    return result


def _text(value):
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _reason(value, fallback):
    return (
        value
        if isinstance(value, str)
        and 1 <= len(value) <= 80
        and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in value)
        else fallback
    )


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("EVALUATION_DAY_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("EVALUATION_DAY_INVALID") from None
    if parsed.isoformat() != value:
        raise ValueError("EVALUATION_DAY_INVALID")
    start = (
        int(
            datetime.combine(
                parsed, datetime.min.time(), timezone(timedelta(hours=8))
            ).timestamp()
        )
        * 1000
    )
    if start < 0:
        raise ValueError("EVALUATION_DAY_INVALID")
    return start


def _split(day):
    if day <= _PROTOCOL["exploratory_through"]:
        return _SPLITS[0]
    if day <= _PROTOCOL["reserved_holdout_end"]:
        return _SPLITS[1]
    return _SPLITS[2]


def _review(value, *, flow=False):
    expected = (
        "daily-mover-flow-review-v1" if flow else "daily-fixed-clock-precursors-v1"
    )
    source = "BINANCE_LIVE_PUBLIC" if flow else "BINANCE_USDM_LIVE"
    if not isinstance(value, dict):
        return "REVIEW_UNAVAILABLE"
    if value.get("version") != expected:
        return "REVIEW_VERSION_UNSUPPORTED"
    if value.get("source") != source:
        return "REVIEW_SOURCE_MISMATCH"
    if value.get("status") != "DESCRIPTIVE_RESEARCH_ONLY":
        return _reason(value.get("reason"), "REVIEW_NOT_OBSERVED")
    if not flow and (
        value.get("selection_basis")
        != "FIXED_DAY_SYMBOL_HASH_WITHOUT_RETURN_OR_COMPLETENESS_FILTER"
        or type(value.get("baseline_hours")) is not int
        or value["baseline_hours"] != 3
        or type(value.get("recent_hours")) is not int
        or value["recent_hours"] != 3
        or value.get("cutoff_hours_utc8") != [6, 12, 18]
        or any(type(hour) is not int for hour in value["cutoff_hours_utc8"])
    ):
        return "PRECURSOR_METHOD_MISMATCH"
    if not isinstance(value.get("cases"), list) or len(value["cases"]) > 360:
        return "REVIEW_CASES_BOUND"
    if "content_digest" in value:
        try:
            expected_digest = digest(
                canonical({k: v for k, v in value.items() if k != "content_digest"})
            )
        except (TypeError, ValueError, RecursionError):
            return "REVIEW_DIGEST_INVALID"
        if value["content_digest"] != expected_digest:
            return "REVIEW_DIGEST_MISMATCH"
    return None


def _index(review, cohort):
    """Use only BUY snapshots; mirrored SELL rows never create extra samples."""
    result = {}
    for case in review["cases"]:
        if not isinstance(case, dict):
            continue
        symbol = case.get("symbol")
        if symbol not in cohort or case.get("side") != "BUY":
            continue
        cutoff = case.get("cutoff_ms")
        if type(cutoff) is not int:
            raise ValueError("INVALID_CASE_KEY")
        key = (symbol, cutoff)
        if key in result:
            raise ValueError("DUPLICATE_CASE_KEY")
        result[key] = case
    return result


def _price(case):
    feature = case.get("features_at_cutoff") if isinstance(case, dict) else None
    if not isinstance(feature, dict) or feature.get("status") != "OBSERVED":
        return None, _reason(
            feature.get("reason") if isinstance(feature, dict) else None,
            "PRICE_UNAVAILABLE",
        )
    values = feature.get("features")
    try:
        value = _number(values["directional_return_pct"])
        return (1 if value > 0 else -1 if value < 0 else 0), None
    except (KeyError, TypeError, ValueError):
        return None, "PRICE_VALUE_INVALID"


def _flag(case, name, direction):
    components = case.get("components") if isinstance(case, dict) else None
    component = components.get(name) if isinstance(components, dict) else None
    if not isinstance(component, dict) or component.get("status") != "OBSERVED":
        return None, _reason(
            component.get("reason") if isinstance(component, dict) else None,
            "COMPONENT_UNAVAILABLE",
        )
    values = component.get("features")
    try:
        if name == "spot":
            if values.get("notional_unit") != "USDT":
                raise ValueError()
            baseline = _number(values["baseline"]["side_adjusted_imbalance_ratio"])
            recent = _number(values["recent"]["side_adjusted_imbalance_ratio"])
            if abs(baseline) > 1 or abs(recent) > 1:
                raise ValueError()
            passed = direction * baseline > 0 and direction * recent > 0
        elif name == "open_interest":
            if values.get("unit") != "BASE_ASSET":
                raise ValueError()
            passed = _number(values["change_base"]) > 0
        else:
            if values.get("rate_basis") != "ACTUAL_SETTLED_FUNDING_RATE":
                raise ValueError()
            rate = _number(values["actual_rate"])
            age = values.get("age_ms")
            if abs(rate) > 1 or type(age) is not int or not 0 < age <= 12 * HOUR:
                raise ValueError()
            if values.get("funding_time_ms") != case["cutoff_ms"] - age:
                raise ValueError()
            passed = direction * rate > 0
        return _GROUPS[name][0 if passed else 1], None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, "COMPONENT_VALUE_INVALID"


def _outcome(case, *, cutoff, direction, as_of_ms):
    label = case.get("future_labels_not_features") if isinstance(case, dict) else None
    if not isinstance(label, dict) or label.get("status") != "OBSERVED":
        return None, _reason(
            label.get("reason") if isinstance(label, dict) else None,
            "OUTCOME_UNAVAILABLE",
        )
    if (
        label.get("basis") != "LIVE_HOURLY_CLOSE_PRICE_RETURN_NOT_PNL"
        or type(label.get("start_ms")) is not int
        or label["start_ms"] != cutoff
        or type(label.get("end_ms")) is not int
        or label["end_ms"] != cutoff + 6 * HOUR
        or type(label.get("horizon_hours")) is not int
        or label["horizon_hours"] != 6
        or label["end_ms"] > as_of_ms
    ):
        return None, "OUTCOME_SCOPE_OR_MATURITY_INVALID"
    try:
        return direction * _number(label["directional_end_return_pct"]), None
    except (KeyError, ValueError):
        return None, "OUTCOME_VALUE_INVALID"


def _metrics(slots, *, compact=False):
    values = sorted(s["outcome"] for s in slots if s["outcome"] is not None)
    size = len(values)
    median = (
        None
        if not size
        else values[size // 2]
        if size % 2
        else (values[size // 2 - 1] + values[size // 2]) / 2
    )
    result = {
        "slots": len(slots),
        "outcome_observed": size,
        "outcome_missing": len(slots) - size,
        "continuations": sum(v > 0 for v in values),
        "noncontinuations": sum(v <= 0 for v in values),
        "big_moves_ge_5pct": sum(v >= 5 for v in values),
        "median_end_return_pct": _text(median) if median is not None else None,
    }
    if compact:
        return {
            k: result[k]
            for k in (
                "slots",
                "outcome_observed",
                "continuations",
                "median_end_return_pct",
            )
        }
    return result


def _summary(slots, *, compact=False):
    directional = [s for s in slots if s["direction"] in (-1, 1)]
    counts = {
        "planned_slots": len(slots),
        "buy": sum(s["direction"] == 1 for s in slots),
        "sell": sum(s["direction"] == -1 for s in slots),
        "flat_abstentions": sum(s["direction"] == 0 for s in slots),
        "price_unknown": sum(s["direction"] is None for s in slots),
        "day_symbol_clusters": len({(s["day"], s["symbol"]) for s in slots}),
    }
    comparisons = {}
    for name, groups in _GROUPS.items():
        available = [s for s in directional if s["flags"][name] is not None]
        unknown = [s for s in directional if s["flags"][name] is None]
        comparison = {
            "available_baseline": _metrics(available, compact=compact),
            "groups": {
                group: _metrics(
                    [s for s in available if s["flags"][name] == group], compact=compact
                )
                for group in groups
            },
            "unknown_slots": len(unknown),
        }
        if not compact:
            comparison["unknown_reasons"] = dict(
                sorted(Counter(s["flag_reasons"][name] for s in unknown).items())
            )
        comparisons[name] = comparison
    result = {
        "counts": counts,
        "price_baseline": _metrics(directional),
        "comparisons": comparisons,
    }
    if not compact:
        result["price_unknown_reasons"] = dict(
            sorted(
                Counter(
                    s["price_reason"] for s in slots if s["direction"] is None
                ).items()
            )
        )
        result["outcome_unknown_reasons"] = dict(
            sorted(
                Counter(
                    s["outcome_reason"] for s in directional if s["outcome"] is None
                ).items()
            )
        )
    return result


def _source_digest(value):
    if not isinstance(value, dict):
        return None
    try:
        encoded = canonical(value)
        return (
            digest(encoded) if len(encoded.encode("utf-8")) <= 2 * 1024 * 1024 else None
        )
    except (TypeError, ValueError, RecursionError):
        return None


def evaluate_flow_history(records, *, as_of_ms):
    """Summarize <=60 unique civil days; invalid identities/configuration raise.

    Missing/unsupported reviews produce explicit UNKNOWN days/components. Future
    days are not assessed. ``as_of_ms`` is only a maturity check, never output or
    digest material, making unchanged mature input idempotent across job runs.
    """
    if type(as_of_ms) is not int or not 0 <= as_of_ms < 2**53:
        raise ValueError("EVALUATION_AS_OF_INVALID")
    if records is None:
        records = []
    if not isinstance(records, (list, tuple)) or len(records) > 60:
        raise ValueError("EVALUATION_HISTORY_BOUND")
    indexed = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("EVALUATION_RECORD_INVALID")  # noqa: TRY004 - configuration code
        day = record.get("day")
        start = _day(day)
        if day in indexed:
            raise ValueError("EVALUATION_DUPLICATE_DAY")
        indexed[day] = (record, start)
    all_slots, days, sources, decisions = [], [], [], []
    with localcontext(_CONTEXT):
        for day in sorted(indexed):
            record, start = indexed[day]
            precursor, flow = record.get("precursor_review"), record.get("flow_review")
            sources.append(
                {
                    "day": day,
                    "precursor_digest": _source_digest(precursor),
                    "flow_digest": _source_digest(flow),
                }
            )
            error = "DAY_NOT_COMPLETE" if start + DAY > as_of_ms else _review(precursor)
            if error is None and sources[-1]["precursor_digest"] is None:
                error = "SOURCE_DOCUMENT_INVALID_OR_OVERSIZED"
            if error == "REVIEW_UNAVAILABLE" and record.get("history_error"):
                error = _reason(record["history_error"], "REVIEW_UNAVAILABLE")
            cohort = (
                precursor.get("fixed_cohort") if isinstance(precursor, dict) else None
            )
            if error is None and (
                not isinstance(cohort, list)
                or not 1 <= len(cohort) <= 40
                or any(not _symbol(s) for s in cohort)
                or len(cohort) != len(set(cohort))
            ):
                error = "FIXED_COHORT_INVALID"
            if error is not None:
                days.append(
                    {
                        "day": day,
                        "split": _split(day),
                        "status": "UNKNOWN",
                        "reason": error,
                        **_summary([], compact=True),
                    }
                )
                decisions.append({"day": day, "reason": error})
                continue
            flow_error = _review(flow, flow=True)
            if flow_error is None and sources[-1]["flow_digest"] is None:
                flow_error = "SOURCE_DOCUMENT_INVALID_OR_OVERSIZED"
            try:
                prices = _index(precursor, cohort)
            except ValueError as exc:
                prices, error = {}, str(exc)
            try:
                flows = _index(flow, cohort) if flow_error is None else {}
            except ValueError as exc:
                flows, flow_error = {}, str(exc)
            slots = []
            for symbol in sorted(cohort):
                for hour in _PROTOCOL["cutoff_hours_utc8"]:
                    cutoff = start + hour * HOUR
                    case = prices.get((symbol, cutoff))
                    direction, price_reason = _price(case)
                    if error:
                        direction, price_reason = None, error
                    flags, flag_reasons = {}, {}
                    for name in _GROUPS:
                        if direction not in (-1, 1):
                            flags[name], flag_reasons[name] = None, "NO_PRICE_DIRECTION"
                        elif flow_error:
                            flags[name], flag_reasons[name] = None, flow_error
                        else:
                            flags[name], flag_reasons[name] = _flag(
                                flows.get((symbol, cutoff)), name, direction
                            )
                    outcome, outcome_reason = (None, "NO_PRICE_DIRECTION")
                    if direction in (-1, 1):
                        outcome, outcome_reason = _outcome(
                            case, cutoff=cutoff, direction=direction, as_of_ms=as_of_ms
                        )
                    identity = {
                        "day": day,
                        "symbol": symbol,
                        "cutoff_ms": cutoff,
                        "source": "BINANCE_USDM_LIVE",
                    }
                    decision = {
                        **identity,
                        "case_id": digest(canonical(identity)),
                        "direction": direction,
                        "price_reason": price_reason,
                        "flags": flags,
                        "flag_reasons": flag_reasons,
                    }
                    decisions.append(decision)
                    slots.append(
                        {
                            **decision,
                            "outcome": outcome,
                            "outcome_reason": outcome_reason,
                        }
                    )
            all_slots.extend(slots)
            days.append(
                {
                    "day": day,
                    "split": _split(day),
                    "status": "UNKNOWN" if error else "DESCRIPTIVE_RESEARCH_ONLY",
                    "reason": error,
                    **_summary(slots, compact=True),
                }
            )
        complete = [
            s
            for s in all_slots
            if s["outcome"] is not None
            and all(v is not None for v in s["flags"].values())
        ]
        held = [s for s in complete if _split(s["day"]) == "RESERVED_TIME_HOLDOUT"]
        held_days = len({s["day"] for s in held})
        held_clusters = len({(s["day"], s["symbol"]) for s in held})
        ready = held_days >= 14 and held_clusters >= 100
        report = {
            "version": VERSION,
            "status": "DESCRIPTIVE_REVIEW_READY"
            if ready
            else "INSUFFICIENT_SAMPLE"
            if all_slots
            else "UNKNOWN",
            "reason": None
            if ready
            else "RESERVED_HOLDOUT_SAMPLE_INSUFFICIENT"
            if all_slots
            else "NO_USABLE_FIXED_COHORT_HISTORY",
            "protocol": deepcopy(_PROTOCOL),
            "protocol_digest": digest(canonical(_PROTOCOL)),
            "history": {
                "input_days": len(indexed),
                "cohort_days": sum(d["counts"]["planned_slots"] > 0 for d in days),
                "usable_days": len(
                    {s["day"] for s in all_slots if s["outcome"] is not None}
                ),
                "unknown_days": sum(d["status"] == "UNKNOWN" for d in days),
                "day_symbol_clusters": len(
                    {(s["day"], s["symbol"]) for s in all_slots}
                ),
                "holdout_days": held_days,
                "holdout_day_symbol_clusters": held_clusters,
                "holdout_missing_days": [
                    (date(2026, 10, 29) + timedelta(days=offset)).isoformat()
                    for offset in range(14)
                    if (date(2026, 10, 29) + timedelta(days=offset)).isoformat()
                    not in {s["day"] for s in held}
                ],
            },
            "splits": {
                split: _summary([s for s in all_slots if _split(s["day"]) == split])
                for split in _SPLITS
            },
            "days": days,
            "source_digests": sources,
            "decision_digest": digest(
                canonical({"protocol": _PROTOCOL, "decisions": decisions})
            ),
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "promotion_authorized": False,
            "limitations": [
                "Historical receipt and historical catalog/spot availability are unproven; retrospective event-time research only.",
                "Mirrored sides are one slot; symbols, cutoffs and market days remain dependent, not independent trials.",
                "Each component is compared with its exact availability-matched price baseline; missingness may be informative.",
                "Conditional subgroup differences do not establish incremental predictive value or causation.",
                "The 5 percent label is a fixed research threshold, never a trading or parameter-update instruction.",
                "Returns exclude execution, fees and funding; no win-rate, probability, significance or PnL claim is made.",
                "The reserved split is a fixed research plan; this report alone does not prove preregistration or blind analysis.",
                "Review readiness counts distinct days and day-symbol clusters and never validates an edge or authorizes promotion.",
            ],
        }
        return {**report, "content_digest": digest(canonical(report))}
