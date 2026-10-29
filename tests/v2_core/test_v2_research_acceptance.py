from copy import deepcopy
from dataclasses import asdict, replace
from decimal import localcontext

import pytest

from v2_core.research_acceptance import (
    SCHEMA_VERSION,
    AcceptancePolicy,
    ResearchManifest,
    ResearchMetrics,
    assess,
)


def manifest():
    return ResearchManifest(
        schema_version=SCHEMA_VERSION,
        run_id="qa-oos-001",
        code_digest="a" * 64,
        data_digest="b" * 64,
        model_digest="c" * 64,
        feature_version="closed-bars-v1",
        source_environment="LIVE",
        metrics_basis="SIMULATED_OOS",
        capital_scope="SINGLE_SYMBOL",
        historical_arrival_proven=True,
        historical_universe_proven=True,
        funding_complete=True,
        cost_stress_tested=True,
        independent_validation=True,
        search_trials=12,
        train_end_ms=1000,
        test_start_ms=1000,
        test_end_ms=2000,
        sample_count=120,
        fold_count=3,
        metrics=ResearchMetrics("20", "0.10", "10", "0.15"),
    )


def reasons(report):
    return {item["reason"] for item in report["blockers"]}


def test_complete_research_passes_without_execution_rollout_or_calibration_claims():
    source = manifest()
    report = assess(source)
    assert report["status"] == "RESEARCH_PASSED"
    assert report["blockers"] == []
    assert all(item["status"] == "PASSED" for item in report["checks"])
    assert report["execution_authorized"] is False
    assert report["automatic_rollout"] is False
    assert report["model_calibration_status"] == "NOT_ASSESSED"
    assert report["metrics_basis"] == "SIMULATED_OOS"
    assert "NOT_RELEASE_APPROVAL" in report["purpose"]
    assert any("do not guarantee future profits" in s for s in report["limitations"])
    assert assess(asdict(source)) == report
    assert assess(replace(source, model_digest=None))["status"] == "RESEARCH_PASSED"


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "run_id",
        "code_digest",
        "data_digest",
        "feature_version",
        "source_environment",
        "metrics_basis",
        "capital_scope",
        "historical_arrival_proven",
        "historical_universe_proven",
        "funding_complete",
        "cost_stress_tested",
        "independent_validation",
        "search_trials",
        "train_end_ms",
        "test_start_ms",
        "test_end_ms",
        "sample_count",
        "fold_count",
        "metrics",
    ],
)
def test_each_required_manifest_field_is_explicit_and_missing_never_passes(field):
    payload = asdict(manifest())
    payload.pop(field)
    report = assess(payload)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["blockers"] and report["execution_authorized"] is False


@pytest.mark.parametrize(
    "field",
    [
        "historical_arrival_proven",
        "historical_universe_proven",
        "funding_complete",
        "cost_stress_tested",
        "independent_validation",
    ],
)
def test_unproven_evidence_is_not_repaired_by_positive_performance(field):
    report = assess(replace(manifest(), **{field: False}))
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert "REQUIRED_EVIDENCE_NOT_ESTABLISHED" in reasons(report)
    assert assess(replace(manifest(), **{field: 1}))["status"] == "INVALID_DATA"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, 0.1, -1, None])
@pytest.mark.parametrize(
    "field",
    [
        "realized_net_pnl",
        "max_drawdown_fraction",
        "cost_stress_net_pnl",
        "cost_stress_max_drawdown_fraction",
    ],
)
def test_metrics_reject_nonfinite_or_nondecimal_values_and_keep_absence_distinct(
    field, value
):
    changed = replace(manifest().metrics, **{field: value})
    report = assess(replace(manifest(), metrics=changed))
    assert report["status"] == (
        "INSUFFICIENT_EVIDENCE" if value is None else "INVALID_DATA"
    )
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("field", ["sample_count", "fold_count", "search_trials"])
@pytest.mark.parametrize("value", [-1, True, "100", 3.0])
def test_counts_are_bounded_integers_not_booleans(field, value):
    assert assess(replace(manifest(), **{field: value}))["status"] == "INVALID_DATA"


@pytest.mark.parametrize("field", ["train_end_ms", "test_start_ms", "test_end_ms"])
@pytest.mark.parametrize("value", [-1, True, 1000.0, 2**53])
def test_invalid_timestamps_are_invalid_data(field, value):
    assert assess(replace(manifest(), **{field: value}))["status"] == "INVALID_DATA"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"train_end_ms": 1001}, "TRAIN_TEST_OVERLAP"),
        ({"test_end_ms": 1000}, "TEST_WINDOW_EMPTY_OR_REVERSED"),
        ({"test_end_ms": 999}, "TEST_WINDOW_EMPTY_OR_REVERSED"),
    ],
)
def test_overlap_or_empty_test_window_is_invalid_even_if_other_evidence_is_missing(
    change, reason
):
    report = assess(replace(manifest(), funding_complete=None, **change))
    assert report["status"] == "INVALID_DATA"
    assert reason in reasons(report)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"sample_count": 99}, "INSUFFICIENT_OOS_SAMPLES"),
        ({"sample_count": 0}, "INSUFFICIENT_OOS_SAMPLES"),
        ({"fold_count": 2}, "INSUFFICIENT_OOS_FOLDS"),
    ],
)
def test_positive_pnl_does_not_replace_minimum_samples(change, reason):
    report = assess(replace(manifest(), **change))
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert reason in reasons(report)


def test_sandbox_does_not_satisfy_live_source_policy():
    source = replace(manifest(), source_environment="SANDBOX")
    assert assess(source)["status"] == "INSUFFICIENT_EVIDENCE"
    report = assess(source, policy=AcceptancePolicy(required_source="SANDBOX"))
    assert report["status"] == "RESEARCH_PASSED"
    assert report["source_environment"] == "SANDBOX"
    assert report["execution_authorized"] is False


@pytest.mark.parametrize(
    "metrics",
    [
        ResearchMetrics("-1", "0.10", "10", "0.15"),
        ResearchMetrics("20", "0.21", "10", "0.15"),
        ResearchMetrics("20", "0.10", "-1", "0.15"),
        ResearchMetrics("20", "0.10", "10", "0.21"),
    ],
)
def test_valid_performance_below_threshold_is_rejected_not_invalid(metrics):
    report = assess(replace(manifest(), metrics=metrics))
    assert report["status"] == "RESEARCH_REJECTED"
    assert report["manifest_digest"] is not None


@pytest.mark.parametrize("value", ["-0.01", "1.01"])
def test_drawdown_is_a_nonnegative_bounded_fraction(value):
    source = replace(
        manifest(), metrics=replace(manifest().metrics, max_drawdown_fraction=value)
    )
    assert assess(source)["status"] == "INVALID_DATA"


def test_cost_stress_flag_does_not_replace_stress_results():
    source = replace(
        manifest(), metrics=replace(manifest().metrics, cost_stress_net_pnl=None)
    )
    report = assess(source)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert "REQUIRED_METRIC_MISSING" in reasons(report)


def test_thresholds_are_configurable_and_policy_is_fingerprinted():
    source = replace(manifest(), sample_count=80, fold_count=2)
    policy = AcceptancePolicy(
        min_oos_samples=80, min_folds=2, min_net_pnl="10", max_drawdown_fraction="0.15"
    )
    assert assess(source)["status"] == "INSUFFICIENT_EVIDENCE"
    result = assess(source, policy=policy)
    assert result["status"] == "RESEARCH_PASSED"
    stricter = replace(policy, min_net_pnl="10.01")
    rejected = assess(source, policy=stricter)
    assert rejected["status"] == "RESEARCH_REJECTED"
    assert rejected["manifest_digest"] == result["manifest_digest"]
    assert rejected["policy_digest"] != result["policy_digest"]


@pytest.mark.parametrize(
    "change",
    [
        {"min_oos_samples": 0},
        {"min_oos_samples": 29},
        {"min_oos_samples": True},
        {"min_folds": 1},
        {"min_folds": True},
        {"min_folds": 101},
        {"min_net_pnl": "-1"},
        {"min_net_pnl": "NaN"},
        {"min_net_pnl": True},
        {"max_drawdown_fraction": "Infinity"},
        {"max_drawdown_fraction": "0"},
        {"max_drawdown_fraction": "1.01"},
        {"max_drawdown_fraction": False},
        {"required_source": "ANY"},
    ],
)
def test_configuration_cannot_remove_evidence_floors_or_use_nonfinite_thresholds(
    change,
):
    with pytest.raises(ValueError):
        AcceptancePolicy(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "future-v99"},
        {"run_id": " padded"},
        {"code_digest": "not-a-digest"},
        {"data_digest": True},
        {"model_digest": "d" * 63},
        {"metrics_basis": "ACTUAL_SETTLED"},
        {"capital_scope": "SUM_OF_SINGLE_SYMBOL_RUNS"},
        {"source_environment": "UNKNOWN"},
        {"search_trials": 0},
    ],
)
def test_invalid_manifest_identity_scope_or_actual_statistics_cannot_pass(change):
    assert assess(replace(manifest(), **change))["status"] == "INVALID_DATA"


def test_hashes_do_not_prove_calibration_and_unknown_claims_are_rejected():
    payload = asdict(manifest())
    payload["model_calibrated"] = True
    report = assess(payload)
    assert report["status"] == "INVALID_DATA"
    assert report["model_calibration_status"] == "NOT_ASSESSED"
    payload.pop("model_calibrated")
    payload["metrics"]["win_probability"] = "0.99"
    assert assess(payload)["status"] == "INVALID_DATA"


def test_determinism_input_isolation_and_decimal_context_independence():
    payload = asdict(manifest())
    before = deepcopy(payload)
    result = assess(payload)
    with localcontext() as ctx:
        ctx.prec = 2
        assert assess(payload) == result
    assert payload == before
    changed = deepcopy(payload)
    changed["data_digest"] = "d" * 64
    assert assess(changed)["manifest_digest"] != result["manifest_digest"]
    result["metrics"]["realized_net_pnl"] = "0"
    assert payload == before


@pytest.mark.parametrize("value", [None, [], True, "report"])
def test_nonobject_manifest_fails_closed(value):
    assert assess(value)["status"] == "INVALID_DATA"
