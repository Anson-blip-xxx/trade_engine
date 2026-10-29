"""Research contract: missingness, mirrored samples and immutable clock split."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Inexact, localcontext

import pytest

from v2_core.evidence import canonical, digest
from v2_core.move_precursors import analyze_move_precursor
from v2_core.mover_flow import analyze_flow_evidence
from v2_core.mover_flow_evaluation import HOUR, evaluate_flow_history


def start(day):
    return (
        int(
            datetime.combine(
                date.fromisoformat(day),
                datetime.min.time(),
                timezone(timedelta(hours=8)),
            ).timestamp()
        )
        * 1000
    )


def record(day="2026-10-27", symbols=2, price="2", outcome="6"):
    cohort = [f"COIN{i}USDT" for i in range(symbols)]
    prices, flows = [], []
    for symbol in cohort:
        for hour in (6, 12, 18):
            cutoff = start(day) + hour * HOUR
            for side in ("BUY", "SELL"):
                prices.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "cutoff_ms": cutoff,
                        "fixed_cohort": True,
                        "top_mover_diagnostic": False,
                        "features_at_cutoff": {
                            "status": "OBSERVED",
                            "features": {"directional_return_pct": price},
                        },
                        "future_labels_not_features": {
                            "status": "OBSERVED",
                            "basis": "LIVE_HOURLY_CLOSE_PRICE_RETURN_NOT_PNL",
                            "start_ms": cutoff,
                            "end_ms": cutoff + 6 * HOUR,
                            "horizon_hours": 6,
                            "directional_end_return_pct": outcome,
                        },
                    }
                )
                flows.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "cutoff_ms": cutoff,
                        "components": {
                            "spot": {
                                "status": "OBSERVED",
                                "features": {
                                    "notional_unit": "USDT",
                                    "baseline": {
                                        "side_adjusted_imbalance_ratio": "0.2"
                                    },
                                    "recent": {"side_adjusted_imbalance_ratio": "0.3"},
                                },
                            },
                            "open_interest": {
                                "status": "OBSERVED",
                                "features": {"unit": "BASE_ASSET", "change_base": "3"},
                            },
                            "funding": {
                                "status": "OBSERVED",
                                "features": {
                                    "actual_rate": "0.0001",
                                    "age_ms": HOUR,
                                    "funding_time_ms": cutoff - HOUR,
                                    "rate_basis": "ACTUAL_SETTLED_FUNDING_RATE",
                                },
                            },
                        },
                    }
                )
    return {
        "day": day,
        "precursor_review": {
            "version": "daily-fixed-clock-precursors-v1",
            "source": "BINANCE_USDM_LIVE",
            "status": "DESCRIPTIVE_RESEARCH_ONLY",
            "selection_basis": "FIXED_DAY_SYMBOL_HASH_WITHOUT_RETURN_OR_COMPLETENESS_FILTER",
            "baseline_hours": 3,
            "recent_hours": 3,
            "cutoff_hours_utc8": [6, 12, 18],
            "fixed_cohort": cohort,
            "cases": prices,
        },
        "flow_review": {
            "version": "daily-mover-flow-review-v1",
            "source": "BINANCE_LIVE_PUBLIC",
            "status": "DESCRIPTIVE_RESEARCH_ONLY",
            "cases": flows,
        },
    }


def evaluate(records, day="2026-12-31"):
    return evaluate_flow_history(records, as_of_ms=start(day))


def exploratory(result):
    return result["splits"]["EXPLORATORY"]


def test_one_day_has_one_direction_per_slot_and_cannot_validate_edge():
    result = evaluate([record()])
    summary = exploratory(result)
    assert summary["counts"] == {
        "planned_slots": 6,
        "buy": 6,
        "sell": 0,
        "flat_abstentions": 0,
        "price_unknown": 0,
        "day_symbol_clusters": 2,
    }
    assert summary["price_baseline"] == {
        "slots": 6,
        "outcome_observed": 6,
        "outcome_missing": 0,
        "continuations": 6,
        "noncontinuations": 0,
        "big_moves_ge_5pct": 6,
        "median_end_return_pct": "6",
    }
    assert result["status"] == "INSUFFICIENT_SAMPLE"
    assert not result["execution_authorized"]
    assert not result["automatic_parameter_updates"]
    assert not result["promotion_authorized"]
    assert len(result["history"]["holdout_missing_days"]) == 14


def test_future_outcomes_do_not_change_direction_flags_or_decision_digest():
    data = record()
    before = evaluate([data])
    for case in data["precursor_review"]["cases"]:
        case["future_labels_not_features"]["directional_end_return_pct"] = "-15"
        case["top_mover_diagnostic"] = True
    after = evaluate([data])
    assert before["decision_digest"] == after["decision_digest"]
    assert before["source_digests"] != after["source_digests"]
    assert before["content_digest"] != after["content_digest"]
    assert exploratory(after)["price_baseline"]["noncontinuations"] == 6


def test_mirrored_side_is_ignored_and_volume_does_not_change_rule():
    data = record(price="-2", outcome="-6")
    expected = evaluate([data])
    data["precursor_review"]["cases"] = [
        c for c in data["precursor_review"]["cases"] if c["side"] == "BUY"
    ]
    for case in data["precursor_review"]["cases"]:
        case["features_at_cutoff"]["features"]["volume_ratio"] = "999999"
    actual = evaluate([data])
    assert expected["decision_digest"] == actual["decision_digest"]
    summary = exploratory(actual)
    assert summary["counts"]["sell"] == 6
    assert summary["price_baseline"]["continuations"] == 6
    assert summary["comparisons"]["funding"]["groups"]["RECEIVES_OR_ZERO"]["slots"] == 6
    assert summary["comparisons"]["spot"]["groups"]["OTHER"]["slots"] == 6


def test_all_fixed_slots_retained_missing_price_and_flow_are_not_zero():
    data = record(symbols=20)
    data["precursor_review"]["cases"] = [
        c for c in data["precursor_review"]["cases"] if c["symbol"] != "COIN0USDT"
    ]
    for case in data["flow_review"]["cases"]:
        case["components"]["spot"] = {
            "status": "UNKNOWN",
            "reason": "NO_EXACT_SPOT_MAPPING",
        }
    result = exploratory(evaluate([data]))
    assert result["counts"]["planned_slots"] == 60
    assert result["counts"]["price_unknown"] == 3
    assert result["price_baseline"]["slots"] == 57
    spot = result["comparisons"]["spot"]
    assert spot["available_baseline"]["slots"] == 0
    assert spot["unknown_slots"] == 57
    assert spot["unknown_reasons"] == {"NO_EXACT_SPOT_MAPPING": 57}
    assert result["comparisons"]["open_interest"]["available_baseline"]["slots"] == 57


def test_component_baseline_uses_exact_available_slots_including_missing_labels():
    data = record()
    for case in data["flow_review"]["cases"]:
        if case["symbol"] == "COIN0USDT":
            case["components"]["spot"] = {"status": "UNKNOWN"}
    for case in data["precursor_review"]["cases"]:
        if case["symbol"] == "COIN1USDT":
            case["future_labels_not_features"] = {
                "status": "UNKNOWN",
                "reason": "FORWARD_WINDOW_UNAVAILABLE",
            }
    result = exploratory(evaluate([data]))
    spot = result["comparisons"]["spot"]
    assert spot["available_baseline"]["slots"] == 3
    assert spot["available_baseline"]["outcome_missing"] == 3
    assert spot["available_baseline"]["median_end_return_pct"] is None
    assert result["price_baseline"]["outcome_missing"] == 3


@pytest.mark.parametrize(
    "outcome,continuations,big",
    [("0", 0, 0), ("-1", 0, 0), ("4.99999999", 6, 0), ("5", 6, 6)],
)
def test_fixed_outcome_thresholds_count_failures(outcome, continuations, big):
    stats = exploratory(evaluate([record(outcome=outcome)]))["price_baseline"]
    assert stats["continuations"] == continuations
    assert stats["noncontinuations"] == 6 - continuations
    assert stats["big_moves_ge_5pct"] == big


def test_zero_price_abstains_without_erasing_planned_slots():
    result = exploratory(evaluate([record(price="0")]))
    assert result["counts"]["flat_abstentions"] == 6
    assert result["counts"]["planned_slots"] == 6
    assert result["price_baseline"]["slots"] == 0


def test_top_only_excluded_but_fixed_top_overlap_remains():
    data = record(symbols=3)
    data["precursor_review"]["fixed_cohort"].remove("COIN2USDT")
    for case in data["precursor_review"]["cases"]:
        case["top_mover_diagnostic"] = True
        case["fixed_cohort"] = False  # Membership list, not potentially stale flag.
    result = exploratory(evaluate([data]))
    assert result["counts"]["planned_slots"] == 6


def test_chronology_is_fixed_not_random_and_input_order_is_irrelevant():
    records = [
        record(day) for day in ("2026-11-12", "2026-10-29", "2026-10-28", "2026-11-11")
    ]
    result = evaluate(records)
    assert result == evaluate(list(reversed(records)))
    assert [d["split"] for d in result["days"]] == [
        "EXPLORATORY",
        "RESERVED_TIME_HOLDOUT",
        "RESERVED_TIME_HOLDOUT",
        "POST_WINDOW",
    ]


def test_large_sample_only_enables_descriptive_review_no_promotion():
    records = [
        record((date(2026, 10, 29) + timedelta(days=i)).isoformat(), symbols=8)
        for i in range(14)
    ]
    result = evaluate(records)
    assert result["status"] == "DESCRIPTIVE_REVIEW_READY"
    assert result["history"]["holdout_days"] == 14
    assert result["history"]["holdout_day_symbol_clusters"] == 112
    assert result["history"]["holdout_missing_days"] == []
    assert not result["promotion_authorized"]
    assert evaluate(records[:-1])["status"] == "INSUFFICIENT_SAMPLE"
    assert (
        evaluate(
            [
                record((date(2026, 10, 29) + timedelta(days=i)).isoformat())
                for i in range(14)
            ]
        )["status"]
        == "INSUFFICIENT_SAMPLE"
    )


def test_unknown_history_days_and_future_days_are_explicit():
    result = evaluate(
        [
            {"day": "2026-10-26", "history_error": "DAY_PROJECTION_TOO_LARGE"},
            record("2026-10-27"),
        ],
        day="2026-10-27",
    )
    assert [d["reason"] for d in result["days"]] == [
        "DAY_PROJECTION_TOO_LARGE",
        "DAY_NOT_COMPLETE",
    ]
    assert result["history"]["unknown_days"] == 2
    assert result["status"] == "UNKNOWN"
    assert evaluate_flow_history(None, as_of_ms=0)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("precursor_review", "version", "future-v2"),
        ("precursor_review", "source", "BINANCE_USDM_SANDBOX"),
        ("flow_review", "version", "future-v2"),
        ("flow_review", "source", "BINANCE_USDM_SANDBOX"),
    ],
)
def test_unknown_versions_and_sources_are_not_silently_accepted(target, key, value):
    data = record()
    data[target][key] = value
    result = evaluate([data])
    if target == "precursor_review":
        assert result["days"][0]["status"] == "UNKNOWN"
    else:
        summary = exploratory(result)
        assert summary["price_baseline"]["slots"] == 6
        assert all(c["unknown_slots"] == 6 for c in summary["comparisons"].values())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999999999", True, 1.5])
def test_nonfinite_and_unbounded_price_values_are_unknown(value):
    report = evaluate([record(price=value)])
    result = exploratory(report)
    assert (
        result["counts"]["price_unknown"] == 6
        or report["days"][0]["status"] == "UNKNOWN"
    )
    assert result["price_baseline"]["slots"] == 0


def test_undefined_spot_ratio_is_unknown_not_other():
    data = record()
    for case in data["flow_review"]["cases"]:
        case["components"]["spot"]["features"]["recent"][
            "side_adjusted_imbalance_ratio"
        ] = None
    result = exploratory(evaluate([data]))["comparisons"]["spot"]
    assert result["unknown_slots"] == 6
    assert result["groups"]["OTHER"]["slots"] == 0


def test_duplicate_days_raise_and_duplicate_case_keys_become_unknown():
    data = record()
    with pytest.raises(ValueError, match="DUPLICATE_DAY"):
        evaluate([data, deepcopy(data)])
    data["precursor_review"]["cases"].append(
        deepcopy(data["precursor_review"]["cases"][0])
    )
    result = exploratory(evaluate([data]))
    assert result["counts"]["planned_slots"] == 6
    assert result["price_unknown_reasons"] == {"DUPLICATE_CASE_KEY": 6}


def test_as_of_is_not_digest_material_after_outcomes_mature():
    data = record()
    assert evaluate([data], day="2026-10-28") == evaluate([data], day="2026-10-29")


def test_pure_deterministic_under_decimal_context_and_protocol_is_not_mutable():
    data = record()
    original = deepcopy(data)
    expected = evaluate([data])
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        assert evaluate([data]) == expected
    assert data == original
    expected["protocol"]["big_move_research_threshold_pct"] = "999"
    assert evaluate([data])["protocol"]["big_move_research_threshold_pct"] == "5"


def test_sixty_day_output_remains_bounded_and_has_no_case_echo():
    records = [
        record((date(2026, 9, 20) + timedelta(days=i)).isoformat(), symbols=20)
        for i in range(60)
    ]
    result = evaluate(records)
    assert len(canonical(result)) < 130_000
    assert all("cases" not in d for d in result["days"])
    with pytest.raises(ValueError, match="HISTORY_BOUND"):
        evaluate(records + [record("2026-12-01")])


def test_actual_descriptor_producers_accept_full_precision_decimal_outputs():
    data = record(symbols=1)
    begin = start(data["day"])
    rows = [
        {
            "t": begin + hour * HOUR,
            "o": str(800 + hour),
            "h": str(802 + hour),
            "l": str(799 + hour),
            "c": str(801 + hour),
            "v": "1",
        }
        for hour in range(24)
    ]
    envelope = {"status": "OBSERVED", "fetched_at_ms": begin + 25 * HOUR}
    bundle = {
        "version": "mover-flow-public-v1",
        "source": "BINANCE_LIVE_PUBLIC",
        "symbol": "COIN0USDT",
        "start_ms": begin,
        "end_ms": begin + 24 * HOUR,
        "mapping": {
            "status": "MATCHED",
            "basis": "CURRENT_EXACT_SYMBOL_BASE_QUOTE",
            "spot_symbol": "COIN0USDT",
            "base_asset": "COIN0",
            "quote_asset": "USDT",
        },
        "spot": {
            **envelope,
            "source": "BINANCE_SPOT_LIVE",
            "rows": [
                [
                    begin + hour * HOUR,
                    "1",
                    "1",
                    "1",
                    "1",
                    "3",
                    begin + (hour + 1) * HOUR - 1,
                    "3",
                    3,
                    "2",
                    "2",
                    "0",
                ]
                for hour in range(24)
            ],
        },
        "open_interest": {
            **envelope,
            "source": "BINANCE_USDM_LIVE",
            "rows": [
                {
                    "symbol": "COIN0USDT",
                    "timestamp": begin + hour * HOUR,
                    "sumOpenInterest": str(100 + hour),
                }
                for hour in range(25)
            ],
        },
        "funding": {
            **envelope,
            "source": "BINANCE_USDM_LIVE",
            "rows": [
                {
                    "symbol": "COIN0USDT",
                    "fundingTime": begin + hour * HOUR,
                    "fundingRate": "0.0001",
                }
                for hour in (0, 8, 16)
            ],
        },
    }
    for case in data["precursor_review"]["cases"]:
        assessment = analyze_move_precursor(
            rows,
            symbol="COIN0USDT",
            side=case["side"],
            cutoff_ms=case["cutoff_ms"],
            interval_minutes=60,
            window_bars=3,
        )
        case["features_at_cutoff"] = {
            key: assessment[key]
            for key in ("status", "reason", "features", "input_digest")
        }
    for case in data["flow_review"]["cases"]:
        case["components"] = analyze_flow_evidence(
            bundle, cutoff_ms=case["cutoff_ms"], side=case["side"]
        )["components"]
    assert (
        len(
            data["precursor_review"]["cases"][0]["features_at_cutoff"]["features"][
                "directional_return_pct"
            ]
        )
        > 40
    )
    assert (
        len(
            data["flow_review"]["cases"][0]["components"]["spot"]["features"]["recent"][
                "side_adjusted_imbalance_ratio"
            ]
        )
        > 60
    )
    summary = exploratory(evaluate([data]))
    assert summary["counts"]["buy"] == 3
    assert all(
        c["available_baseline"]["slots"] == 3 for c in summary["comparisons"].values()
    )


def test_supplied_flow_digest_is_verified_without_invalidating_price_baseline():
    data = record()
    flow = data["flow_review"]
    flow["content_digest"] = digest(canonical(flow))
    assert (
        exploratory(evaluate([data]))["comparisons"]["spot"]["available_baseline"][
            "slots"
        ]
        == 6
    )
    flow["cases"][0]["components"]["open_interest"]["features"]["change_base"] = "-1"
    result = exploratory(evaluate([data]))
    assert result["price_baseline"]["slots"] == 6
    assert result["comparisons"]["spot"]["unknown_reasons"] == {
        "REVIEW_DIGEST_MISMATCH": 6
    }


def test_large_realistic_sources_are_hashed_oversized_sources_remain_unknown():
    data = record()
    data["flow_review"]["padding"] = "x" * 600_000
    result = evaluate([data])
    assert result["source_digests"][0]["flow_digest"]
    assert (
        exploratory(result)["comparisons"]["spot"]["available_baseline"]["slots"] == 6
    )
    data["flow_review"]["padding"] = "x" * (2 * 1024 * 1024)
    result = evaluate([data])
    assert result["source_digests"][0]["flow_digest"] is None
    assert exploratory(result)["comparisons"]["spot"]["unknown_reasons"] == {
        "SOURCE_DOCUMENT_INVALID_OR_OVERSIZED": 6
    }


def test_usable_days_require_observed_directional_outcomes():
    data = record()
    data["precursor_review"]["cases"] = []
    result = evaluate([data])
    assert result["history"]["cohort_days"] == 1
    assert result["history"]["usable_days"] == 0


def test_component_zero_boundaries_and_spot_persistence():
    data = record()
    for case in data["flow_review"]["cases"]:
        case["components"]["spot"]["features"]["baseline"][
            "side_adjusted_imbalance_ratio"
        ] = "0"
        case["components"]["open_interest"]["features"]["change_base"] = "0"
        case["components"]["funding"]["features"]["actual_rate"] = "0"
    comparisons = exploratory(evaluate([data]))["comparisons"]
    assert comparisons["spot"]["groups"]["OTHER"]["slots"] == 6
    assert comparisons["open_interest"]["groups"]["NONINCREASING"]["slots"] == 6
    assert comparisons["funding"]["groups"]["RECEIVES_OR_ZERO"]["slots"] == 6


@pytest.mark.parametrize(
    "key,value",
    [
        ("selection_basis", "TOP_MOVERS"),
        ("baseline_hours", 6),
        ("recent_hours", "3"),
        ("cutoff_hours_utc8", [5, 12, 18]),
        ("cutoff_hours_utc8", [6.0, 12, 18]),
    ],
)
def test_precursor_method_metadata_must_match_fixed_protocol(key, value):
    data = record()
    data["precursor_review"][key] = value
    result = evaluate([data])
    assert result["days"][0]["status"] == "UNKNOWN"
    assert result["days"][0]["reason"] == "PRECURSOR_METHOD_MISMATCH"
