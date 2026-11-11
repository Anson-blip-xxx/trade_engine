"""Directional attribution, numeric evidence and bounded private summaries."""

from copy import deepcopy

import pytest

from services.v2_daily_mover_report import build_report, format_digest
from tests.v2_core.test_v2_daily_mover_report import (
    DAY,
    NOW,
    SCOPE,
    inputs,
    market,
    symbol_evidence,
)


def report_with_directional(**changes):
    ranking, evidence, paths = inputs()
    directional = {
        "classification_version": "directional-entry-events-v2",
        "candidate_count": 1,
        "stage_counts": {"MARKET_FILTER_REJECTED": 1},
        "reason_counts": [
            {
                "stage": "MARKET_FILTER_REJECTED",
                "reason": "TREND_VOLUME_UNCONFIRMED",
                "count": 1,
            }
        ],
        "non_candidate_counts": {"NON_ENTRY_EVENT": 7, "OPPOSITE_DIRECTION_EVENT": 3},
        "lifecycle_counts": {"TIMELY_STRATEGY_DECISION": 1},
        "rejection_details": [
            {
                "reason": "TREND_VOLUME_UNCONFIRMED",
                "stage": "MARKET_FILTER_REJECTED",
                "count": 1,
                "reason_samples": [
                    {
                        "source": "s3",
                        "event": "TREND_UP",
                        "observed_ms": DAY["start_ms"],
                        "decided_ms": DAY["start_ms"] + 1000,
                        "completed_ms": DAY["start_ms"] + 2000,
                        "operands": {"volume_ratio": "0.725", "api_key": "not-allowed"},
                        "thresholds": {
                            "trend.min_volume_ratio": "1",
                            "private_config": "not-allowed",
                        },
                        "market_sources": {
                            "market_environment": "SANDBOX",
                            "market_host": "demo-fapi.binance.com",
                            "raw": "not-allowed",
                        },
                    }
                ],
            }
        ],
        **changes,
    }
    evidence["symbols"]["UPUSDT"] = symbol_evidence(
        signal_count=11,
        by_strategy={"s6": directional},
        coverage={
            "signals": "TRUNCATED",
            "signal_sample_basis": "SOURCE_FAIR_FOUR_UTC8_WINDOWS_UNIFORM_RANK",
        },
    )
    return build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


def test_digest_names_actual_rejection_and_numeric_threshold_without_raw_material():
    report = report_with_directional()
    text = format_digest(report)
    assert "量能未确认×1" in text and "量比0.725/阈值1" in text
    assert "反向/非入场事件排除" in text
    assert "not-allowed" not in str(report)
    row = report["rows"][0]["evidence"]
    assert (
        row["candidate_count"] == 1
        and row["non_candidate_counts"]["NON_ENTRY_EVENT"] == 7
    )
    assert row["sampled_attribution"]
    assert (
        row["rejection_details"][0]["reason_samples"][0]["market_sources"][
            "market_environment"
        ]
        == "SANDBOX"
    )


def test_only_opposite_events_are_not_entry_rejections_or_missing_shared_signals():
    report = report_with_directional(
        candidate_count=0, stage_counts={}, reason_counts=[], rejection_details=[]
    )
    assert (
        report["rows"][0]["evidence"]["primary_reason"] == "NO_DIRECTIONAL_ENTRY_SIGNAL"
    )
    assert "样本中无同向入场事件" in format_digest(report)


def test_legacy_reports_remain_readable_and_do_not_claim_fixed_attribution():
    report = report_with_directional(classification_version="old", candidate_count=1)
    assert report["rows"][0]["evidence"]["candidate_count"] is None
    assert "旧版样本口径" in format_digest(report)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        "raw-secret",
        [{"count": True}],
        [{"count": 1, "reason_samples": {"secret": "not-allowed"}}],
    ],
)
def test_malformed_optional_details_never_crash_or_leak(bad):
    report = report_with_directional(rejection_details=bad)
    assert "raw-secret" not in str(report) and "not-allowed" not in str(report)
    assert "量能未确认" in format_digest(report)


def test_future_evidence_sample_dropped_without_changing_counts():
    report = report_with_directional()
    details = deepcopy(report["rows"][0]["evidence"]["rejection_details"])
    details[0]["reason_samples"][0]["decided_ms"] = NOW + 1
    report = report_with_directional(rejection_details=details)
    assert report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"] == []
    assert "量能未确认×1" in format_digest(report)


def test_mixed_reasons_are_explained_without_claiming_all_gates_would_pass():
    report = report_with_directional(
        reason_counts=[
            {
                "stage": "MARKET_FILTER_REJECTED",
                "reason": "TREND_VOLUME_UNCONFIRMED",
                "count": 5,
            },
            {
                "stage": "JEV_UNAVAILABLE_OR_SKIPPED",
                "reason": "JEV_MARKET_SNAPSHOT_EXPIRED",
                "count": 2,
            },
            {"stage": "JEV_VETO", "reason": "JEV_TREND_UNSUPPORTED", "count": 1},
        ]
    )
    text = format_digest(report)
    assert "量能未确认×5" in text and "JEV行情快照过期×2" in text and "等3类" in text
    assert "多种已记录拒绝" not in text


def test_shadow_digest_is_descriptive_and_unknown_payload_cannot_leak():
    report = report_with_directional()
    report["gate_shadow"] = {
        "version": "mover-gate-shadow-v1",
        "execution_authorized": False,
        "not_trade_prediction": True,
        "summary": {
            "replayed_count": 2,
            "metric_only_count": 1,
            "volume_gate_changed_count": 1,
            "other_gate_still_failed_count": 1,
        },
    }
    text = format_digest(report)
    assert "不等于会成交或盈利" in text and "未完成样本外验证" in text
    report["gate_shadow"]["summary"]["replayed_count"] = "secret-not-for-output"
    assert "secret-not-for-output" not in format_digest(report)


def test_twenty_rows_with_numeric_examples_fit_private_notifier_bound():
    base = report_with_directional()
    ranking, evidence, paths = inputs()
    ranking["winners"] = [market(f"LONG{i}USDT", "10") for i in range(10)]
    ranking["losers"] = [market(f"SHORT{i}USDT", "-10") for i in range(10)]
    ranking["eligible_summaries"] = ranking["winners"] + ranking["losers"]
    report = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    for row in report["rows"]:
        row["evidence"] = deepcopy(base["rows"][0]["evidence"])
    text = format_digest(report)
    assert len(text.encode("utf-16-le")) // 2 <= 3500
    assert all(row["symbol"] in text for row in report["rows"])


def test_shadow_is_built_from_frozen_metrics_without_claiming_replay_or_profit():
    report = report_with_directional()
    shadow = report["gate_shadow"]
    assert shadow["summary"]["metric_only_count"] == 1
    assert shadow["summary"]["replayed_count"] == 0
    assert shadow["summary"]["volume_gate_changed_count"] == 0  # 0.725 < both cutoffs
    assert shadow["execution_authorized"] is False
    assert "仅指标对照1条" in format_digest(report)


def test_missing_frozen_threshold_is_unknown_not_current_policy_default():
    report = report_with_directional()
    details = deepcopy(report["rows"][0]["evidence"]["rejection_details"])
    details[0]["reason_samples"][0]["thresholds"] = {}
    shadow = report_with_directional(rejection_details=details)["gate_shadow"]
    assert shadow["summary"]["unknown_count"] == 1
    assert shadow["summary"]["metric_only_count"] == 0


def test_shadow_failure_does_not_prevent_factual_report(monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("must-not-escape")

    monkeypatch.setattr(
        "services.v2_mover_gate_shadow.evaluate_mover_gate_shadow", failed
    )
    report = report_with_directional()
    assert report["gate_shadow"]["status"] == "UNAVAILABLE"
    assert "量能未确认×1" in format_digest(report)
    assert "must-not-escape" not in str(report)


def test_unknown_major_reason_and_candidate_status_are_not_hidden():
    report = report_with_directional(
        stage_counts={"ENTRY_CANDIDATE": 1, "MARKET_FILTER_REJECTED": 101},
        reason_counts=[
            {"stage": "MARKET_FILTER_REJECTED", "reason": "UNTRANSLATED", "count": 100},
            {
                "stage": "MARKET_FILTER_REJECTED",
                "reason": "TREND_VOLUME_UNCONFIRMED",
                "count": 1,
            },
        ],
    )
    text = format_digest(report)
    assert "行情过滤拒绝×100" in text and "有候选，成交证据未知" in text


def test_mixed_old_and_new_classification_does_not_claim_complete_migration():
    report = report_with_directional()
    report["rows"][1]["evidence"]["signal_count"] = 1
    text = format_digest(report)
    assert "混合口径" in text and "旧版未排除" in text


def test_malformed_nested_optional_enums_degrade_safely():
    report = report_with_directional()
    details = deepcopy(report["rows"][0]["evidence"]["rejection_details"])
    sample = details[0]["reason_samples"][0]
    sample["operands"]["regime"] = []
    sample["source"] = {}
    sample["market_sources"] = {"market_source": [], "market_environment": {}}
    report = report_with_directional(rejection_details=details)
    assert (
        report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"][0][
            "market_sources"
        ]
        == {}
    )
    assert "量能未确认" in format_digest(report)


@pytest.mark.parametrize(
    "key",
    [
        "checked_at_ms",
        "jev.checked_at_ms",
        "assembled_at_ms",
        "structure_last_closed_at_ms",
    ],
)
def test_future_actual_observations_rejected_but_planned_deadlines_are_retained(key):
    report = report_with_directional()
    details = deepcopy(report["rows"][0]["evidence"]["rejection_details"])
    sample = details[0]["reason_samples"][0]
    sample["operands"]["jev.deadline_ms"] = str(NOW + 1000)
    assert report_with_directional(rejection_details=details)["rows"][0]["evidence"][
        "rejection_details"
    ][0]["reason_samples"]
    sample["operands"][key] = str(NOW + 1000)
    assert (
        report_with_directional(rejection_details=details)["rows"][0]["evidence"][
            "rejection_details"
        ][0]["reason_samples"]
        == []
    )


def test_prefixed_sizing_jev_liquidity_evidence_survives_safe_projection():
    report = report_with_directional()
    details = deepcopy(report["rows"][0]["evidence"]["rejection_details"])
    sample = details[0]["reason_samples"][0]
    sample["operands"].update(
        {
            "sizing.price_low": "0.055",
            "sizing.stop_valid_through_envelope": False,
            "jev.checked_at_ms": str(NOW),
            "jev.trend_supported": "0.58",
            "liquidity.spread_bps": "2",
        }
    )
    sample["thresholds"].update(
        {"jev.min_trend_support": "0.6", "liquidity.max_spread_bps": "10"}
    )
    sample["market_sources"].update(
        {"structure_source": "BINANCE_FUTURES", "liquidity_environment": "SANDBOX"}
    )
    clean = report_with_directional(rejection_details=details)["rows"][0]["evidence"][
        "rejection_details"
    ][0]["reason_samples"][0]
    assert clean["operands"]["sizing.stop_valid_through_envelope"] is False
    assert clean["operands"]["sizing.price_low"] == "0.055"
    assert clean["thresholds"]["jev.min_trend_support"] == "0.6"
    assert clean["market_sources"]["structure_source"] == "BINANCE_FUTURES"
