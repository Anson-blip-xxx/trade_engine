"""Fixed-clock controls, future-label isolation and independent job integration."""

from copy import deepcopy
from decimal import Inexact, localcontext

import pytest
from test_v2_daily_mover_job import NOW, WINDOW, bars, job

import services.v2_daily_mover_job as job_module
from services.v2_daily_mover_precursors import (
    HOUR,
    SOURCE,
    VERSION,
    build_precursor_review,
    forward_labels,
)
from services.v2_daily_mover_report import seal_report
from v2_core.evidence import canonical


def inputs(count=30):
    catalog = [{"symbol": f"COIN{i}USDT"} for i in range(count)]
    data = bars(WINDOW["start_ms"], WINDOW["end_ms"])
    return {
        "window": deepcopy(WINDOW),
        "catalog": catalog,
        "samples": {item["symbol"]: {"hourly": deepcopy(data)} for item in catalog},
        "ranking": {
            "market_source": SOURCE,
            "start_ms": WINDOW["start_ms"],
            "end_ms": WINDOW["end_ms"],
            "winners": [{"symbol": "COIN0USDT"}],
            "losers": [{"symbol": "COIN1USDT"}],
        },
        "as_of_ms": NOW,
    }


def test_fixed_clock_both_sides_observed_and_bounded_without_execution_authority():
    report = build_precursor_review(**inputs())
    assert report["version"] == VERSION
    assert len(report["fixed_cohort"]) == 20
    assert 120 <= len(report["cases"]) <= 132
    assert report["feature_status_counts"] == {"OBSERVED": len(report["cases"])}
    assert (
        report["execution_authorized"] is report["automatic_parameter_updates"] is False
    )
    assert all(
        case["future_labels_not_features"]["status"] == "OBSERVED"
        for case in report["cases"]
    )
    for case in report["cases"]:
        assert (case["cutoff_ms"] - WINDOW["start_ms"]) // HOUR in {6, 12, 18}
        assert "future_labels_not_features" not in case["features_at_cutoff"]
    assert len(canonical(report)) < 200_000


def test_cohort_is_invariant_to_returns_ranking_catalog_order_and_missing_samples():
    args = inputs()
    before = build_precursor_review(**args)
    args["catalog"].reverse()
    args["samples"] = {}
    args["ranking"]["winners"] = [{"symbol": "COIN28USDT"}]
    args["ranking"]["losers"] = [{"symbol": "COIN29USDT"}]
    after = build_precursor_review(**args)
    assert before["fixed_cohort"] == after["fixed_cohort"]
    assert before["catalog_selection_digest"] == after["catalog_selection_digest"]
    assert after["feature_status_counts"] == {"UNKNOWN": len(after["cases"])}


def test_future_outcomes_cannot_change_features_or_feature_digest():
    args = inputs(2)
    before = build_precursor_review(**args)
    cutoff = WINDOW["start_ms"] + 6 * HOUR
    for item in args["samples"].values():
        for row in item["hourly"]:
            if row["t"] >= cutoff:
                row.update(o="1000", h="1100", l="900", c="1000", v="1000000")
    after = build_precursor_review(**args)
    first_before = [c for c in before["cases"] if c["cutoff_ms"] == cutoff]
    first_after = [c for c in after["cases"] if c["cutoff_ms"] == cutoff]
    assert [c["features_at_cutoff"] for c in first_before] == [
        c["features_at_cutoff"] for c in first_after
    ]
    assert [c["future_labels_not_features"] for c in first_before] != [
        c["future_labels_not_features"] for c in first_after
    ]


def test_labels_buy_sell_are_symmetric_and_not_pnl():
    args = inputs(2)
    report = build_precursor_review(**args)
    buy, sell = report["cases"][:2]
    from decimal import Decimal

    b, s = (c["future_labels_not_features"] for c in (buy, sell))
    assert Decimal(b["directional_end_return_pct"]) == -Decimal(
        s["directional_end_return_pct"]
    )
    assert Decimal(b["favorable_close_excursion_pct"]) == -Decimal(
        s["adverse_close_excursion_pct"]
    )
    assert b["basis"] == "LIVE_HOURLY_CLOSE_PRICE_RETURN_NOT_PNL"


@pytest.mark.parametrize(
    "mutation",
    [
        "gap",
        "duplicate",
        "nan",
        "ohlc",
        "source",
        "symbol",
        "environment",
        "open_future",
    ],
)
def test_invalid_future_labels_remain_unknown_without_affecting_features(mutation):
    args = inputs(2)
    rows = args["samples"]["COIN0USDT"]["hourly"]
    cutoff = WINDOW["start_ms"] + 6 * HOUR
    before = build_precursor_review(**args)["cases"][0]["features_at_cutoff"]
    if mutation == "gap":
        rows.pop(8)
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[8]))
    elif mutation == "nan":
        rows[8]["c"] = "NaN"
    elif mutation == "ohlc":
        rows[8]["l"] = "999999"
    elif mutation == "source":
        rows[8]["market_source"] = "BINANCE_USDM_SANDBOX"
    elif mutation == "symbol":
        rows[8]["symbol"] = "OTHERUSDT"
    elif mutation == "environment":
        rows[8]["environment"] = "SANDBOX"
    result = forward_labels(
        rows,
        symbol="COIN0USDT",
        cutoff_ms=cutoff,
        side="BUY",
        as_of_ms=cutoff + (5 if mutation == "open_future" else 6) * HOUR,
    )
    assert result["status"] == "UNKNOWN"
    assert build_precursor_review(**args)["cases"][0]["features_at_cutoff"] == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("cutoff_ms", True),
        ("cutoff_ms", -1),
        ("cutoff_ms", 1),
        ("as_of_ms", "1"),
        ("side", "LONG"),
        ("symbol", "BTC"),
    ],
)
def test_label_configuration_is_strict(field, value):
    kwargs = {
        "symbol": "COIN0USDT",
        "cutoff_ms": WINDOW["start_ms"] + 6 * HOUR,
        "side": "BUY",
        "as_of_ms": NOW,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="CONFIGURATION"):
        forward_labels([], **kwargs)


def test_decimal_context_and_input_mutation_do_not_change_research():
    args = inputs(2)
    original = deepcopy(args)
    expected = build_precursor_review(**args)
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        assert build_precursor_review(**args) == expected
    assert args == original


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "catalog_duplicate",
        "cohort",
        "window",
        "top_duplicate",
        "incomplete_day",
    ],
)
def test_invalid_review_configuration_is_rejected(mutation):
    args = inputs()
    if mutation == "source":
        args["ranking"]["market_source"] = "BINANCE_USDM_SANDBOX"
    elif mutation == "catalog_duplicate":
        args["catalog"].append(args["catalog"][0])
    elif mutation == "cohort":
        args["cohort_size"] = 41
    elif mutation == "window":
        args["ranking"]["start_ms"] += HOUR
    elif mutation == "top_duplicate":
        args["ranking"]["losers"] = args["ranking"]["winners"]
    else:
        args["as_of_ms"] = WINDOW["end_ms"] - 1
    with pytest.raises(ValueError, match="PRECURSOR"):
        build_precursor_review(**args)


def test_job_seals_precursors_without_additional_public_requests_or_duplicate_dm():
    deliveries = []
    worker, store, market = job(
        notifier=lambda message: deliveries.append(message) or 123
    )
    worker.run_day(WINDOW)
    report = store.saved_reports[0]
    assert report["precursor_review"]["feature_status_counts"] == {"OBSERVED": 12}
    assert report == seal_report(report)
    assert market.requests == 5
    worker.run_day(WINDOW)
    assert market.requests == 5
    assert len(store.saved_reports) == len(deliveries) == 1


def test_old_report_gets_one_research_revision_without_duplicate_notification():
    deliveries = []
    worker, store, market = job(
        notifier=lambda message: deliveries.append(message) or 123
    )
    worker.run_day(WINDOW)
    existing = next(iter(store.reports.values()))[1]
    del existing["precursor_review"]
    worker.run_day(WINDOW)
    worker.run_day(WINDOW)
    assert len(store.saved_reports) == 2
    assert len(deliveries) == 1
    assert market.requests == 5


def test_optional_research_failure_does_not_block_report_or_leak_error(monkeypatch):
    def fail(**_):
        raise RuntimeError("secret_not_for_report")

    monkeypatch.setattr(job_module, "build_precursor_review", fail)
    worker, store, _ = job()
    worker.run_day(WINDOW)
    report = store.saved_reports[0]
    assert report["precursor_review"]["status"] == "UNKNOWN"
    assert "secret_not_for_report" not in str(report)
    assert report["account_evidence"]["symbols"]
