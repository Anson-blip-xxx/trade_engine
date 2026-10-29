from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from services.v2_daily_mover_report import build_report, format_digest, seal_report
from v2_core.account_risk import AccountScope
from v2_core.daily_movers import previous_day_window

NOW = int(
    datetime(2026, 10, 26, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
)
DAY = previous_day_window(NOW)
SCOPE = AccountScope("BINANCE", "qa-private-account", "SANDBOX", "FUTURES")


def market(symbol, change="10"):
    close = Decimal(100) + Decimal(change)
    return {
        "symbol": symbol,
        "return_pct": change,
        "open": "100",
        "close": str(close),
        "high": str(max(close, Decimal(100)) + 1),
        "low": str(min(close, Decimal(100)) - 1),
        "base_volume": "1000",
        "engine_symbol_supported": True,
    }


def inputs(day=DAY):
    ranking = {
        "start_ms": day["start_ms"],
        "end_ms": day["end_ms"],
        "market_source": "BINANCE_USDM_LIVE",
        "status": "PARTIAL_COVERAGE",
        "universe_basis": "CURRENT_DISCOVERY_SURVIVORSHIP_LIMITED",
        "coverage": {"discovered": 4, "eligible": 3, "excluded": 1, "zero_return": 0},
        "winners": [market("UPUSDT")],
        "losers": [market("DOWNUSDT", "-10")],
        "eligible_summaries": [
            market("UPUSDT"),
            market("DOWNUSDT", "-10"),
            market("CONTROLUSDT", "1"),
        ],
    }
    evidence = {
        "account_scope": {
            "exchange": "BINANCE",
            "account_id": "qa-private-account",
            "environment": "SANDBOX",
            "product": "FUTURES",
        },
        "window_start_ms": day["start_ms"],
        "window_end_ms": day["end_ms"],
        "as_of_ms": NOW,
        "symbols": {"UPUSDT": symbol_evidence(), "DOWNUSDT": symbol_evidence()},
    }
    return ranking, evidence, {}


def symbol_evidence(**changes):
    return {
        "signal_count": 0,
        "signal_sources": {},
        "stage_counts": {},
        "reason_counts": [],
        "jev_outcomes": [],
        "trades": [],
        "coverage": {
            "signals": "BOUNDED_QUERY_COMPLETE",
            "intents": "BOUNDED_QUERY_COMPLETE",
            "retention_completeness": "NOT_PROVEN",
            "account_route_active_for_full_day": "NOT_PROVEN",
        },
        **changes,
    }


def trade(**changes):
    return {
        "side": "BUY",
        "opening_quantity": "1",
        "closing_quantity": "1",
        "entry_vwap": "100",
        "exit_fill_vwap": "103",
        "opened_ms": DAY["start_ms"] + 60_000,
        "closed_ms": DAY["start_ms"] + 120_000,
        "settled_ms": DAY["start_ms"] + 180_000,
        "settlement_state": "SETTLED",
        "net_pnl_available": True,
        "net_pnl": "2.9",
        "fees_usdt": "0.1",
        "exit_reason": "PARTIAL_TAKE_PROFIT",
        "status": "FILLED_SETTLED",
        **changes,
    }


def build(ranking=None, evidence=None, paths=None, **kwargs):
    default_rank, default_evidence, default_paths = inputs()
    return build_report(
        DAY,
        default_rank if ranking is None else ranking,
        default_evidence if evidence is None else evidence,
        default_paths if paths is None else paths,
        as_of_ms=NOW,
        scope=SCOPE,
        **kwargs,
    )


def test_report_is_detached_deterministic_scoped_without_credentials():
    args = inputs()
    before = deepcopy(args)
    result = build_report(DAY, *args, as_of_ms=NOW, scope=SCOPE)
    assert args == before
    assert result == build_report(DAY, *args, as_of_ms=NOW, scope=SCOPE)
    assert "qa-private-account" not in str(result)
    assert len(result["scope_key"]) == len(result["content_digest"]) == 64
    assert (
        not result["execution_authorized"] and not result["automatic_parameter_updates"]
    )
    args[1]["symbols"]["UPUSDT"]["signal_count"] = 999
    assert result["rows"][0]["evidence"]["signal_count"] == 0


def test_missing_retained_signals_is_unknown_not_observed_absence_or_delayed_consumer():
    result = build()
    row = result["rows"][0]["evidence"]
    assert row["primary_reason"] == "NO_RETAINED_SIGNAL_UNKNOWN"
    assert row["coverage"]["retention_completeness"] == "NOT_PROVEN"
    assert row["coverage"]["account_route_active_for_full_day"] == "NOT_PROVEN"


@pytest.mark.parametrize(
    "stage",
    [
        "JEV_VETO",
        "JEV_UNAVAILABLE_OR_SKIPPED",
        "EXPIRED_NOT_STRATEGY_REJECTION",
        "CANDIDATE_DATA_UNAVAILABLE",
        "SIGNAL_PREFILTER_REJECTED",
        "SIZING_RISK_REJECTED",
        "MARKET_FILTER_REJECTED",
        "ACCOUNT_DECISION_NOT_OBSERVED",
    ],
)
def test_distinct_evidence_reasons_never_coalesced(stage):
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(
        signal_count=1, stage_counts={stage: 1}
    )
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert result["rows"][0]["evidence"]["primary_reason"] == stage


def test_mixed_filters_are_not_mislabeled_as_a_single_root_cause():
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(
        signal_count=2, stage_counts={"JEV_VETO": 1, "SIZING_RISK_REJECTED": 1}
    )
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert result["rows"][0]["evidence"]["primary_reason"] == "MIXED_RECORDED_REASONS"


@pytest.mark.parametrize(
    "side,expected",
    [("BUY", "MATCHED_DIRECTION_FILLED"), ("SELL", "OTHER_DIRECTION_ONLY")],
)
def test_actual_fill_direction_and_cost_complete_net_separate_from_live_extrema(
    side, expected
):
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(trades=[trade(side=side)])
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    row = result["rows"][0]
    assert row["evidence"]["primary_reason"] == expected
    assert row["evidence"]["trades"][0]["actual_settled_net_usdt"] == "2.9"
    assert row["capture_assessment"]["status"] == "CROSS_ENVIRONMENT_NOT_COMPARABLE"
    assert row["capture_assessment"]["observed_peak_capture_ratio"] is None
    assert row["capture_assessment"]["unrealized_best_profit"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"net_pnl_available": False},
        {"settlement_state": "REVISION_CHANGED"},
        {"settled_ms": None},
        {"closed_ms": None},
    ],
)
def test_unsettled_labels_remain_missing_not_zero_or_old_revision(change):
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(trades=[trade(**change)])
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert result["rows"][0]["evidence"]["trades"][0]["actual_settled_net_usdt"] is None


@pytest.mark.parametrize(
    "field", ["opened_ms", "closed_ms", "settled_ms", "decided_ms"]
)
def test_future_trade_evidence_fails_closed(field):
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(trades=[trade(**{field: NOW + 1})])
    with pytest.raises(ValueError, match="FUTURE_TRADE"):
        build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


def test_account_scope_mismatch_refused():
    ranking, evidence, paths = inputs()
    evidence["account_scope"]["account_id"] = "another"
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


def test_live_market_without_account_evidence_can_still_be_ranked_but_not_attributed():
    ranking, _, paths = inputs()
    result = build_report(DAY, ranking, None, paths, as_of_ms=NOW, scope=SCOPE)
    assert result["rows"][0]["evidence"]["primary_reason"] == "EVIDENCE_UNAVAILABLE"
    assert result["rows"][0]["evidence"]["signal_count"] is None


def test_hindsight_control_cohort_excludes_selected_symbols():
    result = build()
    assert result["market_comparison"]["selected"]["count"] == 2
    assert result["market_comparison"]["controls"]["count"] == 1
    assert result["market_comparison"]["control_samples"][0]["symbol"] == "CONTROLUSDT"
    assert result["accumulation"]["sample_status"] == "INSUFFICIENT_SAMPLE"


def test_same_market_day_multiple_accounts_and_retries_not_recounted():
    initial = build()
    other = deepcopy(initial)
    other["scope_key"] = "0" * 64
    other["rows"][0]["evidence"]["primary_reason"] = "JEV_VETO"
    result = build(prior_reports=[initial, initial, other])
    accumulated = result["accumulation"]
    assert accumulated["unique_market_days"] == accumulated["unique_account_days"] == 1
    assert accumulated["selected"]["count"] == 2
    assert accumulated["controls"]["count"] == 1
    assert accumulated["account_symbol_day_reason_counts"] == {
        "NO_RETAINED_SIGNAL_UNKNOWN": 2
    }


def test_prior_days_outside_window_or_future_knowledge_do_not_enter_accumulation():
    prior = build()
    old = deepcopy(prior)
    old["window"]["start_ms"] -= 30 * 86_400_000
    future = deepcopy(prior)
    future["as_of_ms"] = NOW + 1
    result = build(prior_reports=[old, future])
    assert result["accumulation"]["unique_market_days"] == 1
    assert result["accumulation"]["ignored_reports"] == {
        "OUTSIDE_OBSERVABLE_30_DAY_WINDOW": 2
    }


def test_more_winners_without_controls_never_claims_sufficient_evidence():
    prior = []
    for i in range(1, 9):
        report = build()
        report["window"]["start_ms"] -= i * 86_400_000
        report["market_comparison"]["selected_samples"] *= 20
        report["market_comparison"]["control_samples"] = []
        prior.append(report)
    result = build(prior_reports=prior)
    assert result["accumulation"]["selected"]["count"] > 30
    assert result["accumulation"]["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert not result["accumulation"]["causal_edge_proven"]


def test_pattern_threshold_is_descriptive_not_trading_permission():
    prior = []
    for i in range(1, 9):
        report = build()
        report["window"]["start_ms"] -= i * 86_400_000
        report["market_comparison"]["selected_samples"] *= 5
        report["market_comparison"]["control_samples"] *= 5
        prior.append(report)
    result = build(prior_reports=prior)
    assert result["accumulation"]["sample_status"] == "DESCRIPTIVE_COMPARISON_ONLY"
    assert not result["automatic_parameter_updates"]
    assert all(h["next_evidence"] and h["invalidation"] for h in result["hypotheses"])


def test_digest_preserves_all_twenty_unicode_and_ascii_symbols_and_unknowns():
    ranking, evidence, paths = inputs()
    ranking["winners"] = [market(f"UP{i}USDT", str(10 + i)) for i in range(9)] + [
        market("币安人生USDT", "80")
    ]
    ranking["winners"][-1]["engine_symbol_supported"] = False
    ranking["losers"] = [market(f"DOWN{i}USDT", str(-10 - i)) for i in range(10)]
    ranking["eligible_summaries"] = ranking["winners"] + ranking["losers"]
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    digest = format_digest(result, account_alias="测试\n账户")
    assert len(digest) <= 3500
    assert "qa-private-account" not in digest
    assert "测试账户" in digest
    assert "币安人生USDT" in digest and "引擎暂不支持" in digest
    for row in result["rows"]:
        assert row["symbol"] in digest
    assert "历史下架标的覆盖未证明" in digest


def test_report_omits_raw_secret_fields_from_evidence():
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"]["api_key"] = "must-not-appear"
    evidence["symbols"]["UPUSDT"]["evidence"] = [{"raw_response": "must-not-appear"}]
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert "must-not-appear" not in str(result)


def test_unclosed_day_and_rolling_day_are_rejected():
    ranking, evidence, paths = inputs()
    with pytest.raises(ValueError, match="COMPLETED_DAY"):
        build_report(
            DAY, ranking, evidence, paths, as_of_ms=DAY["end_ms"] - 1, scope=SCOPE
        )
    shifted = {
        **DAY,
        "start_ms": DAY["start_ms"] + 60_000,
        "end_ms": DAY["end_ms"] + 60_000,
    }
    with pytest.raises(ValueError, match="COMPLETED_DAY"):
        build_report(shifted, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


@pytest.mark.parametrize("value", ["NaN", "Infinity", True, 1.0, "1e100"])
def test_bad_financial_numbers_rejected(value):
    ranking, evidence, paths = inputs()
    ranking["winners"][0]["return_pct"] = value
    with pytest.raises(ValueError, match="NUMBER_INVALID"):
        build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


def test_missing_path_is_not_flat_market_or_no_trend():
    result = build()
    assert result["rows"][0]["path"] == {
        "status": "UNKNOWN",
        "reason": "PATH_NOT_LOADED",
        "features": None,
    }


def test_duplicate_top_rows_rejected():
    ranking, evidence, paths = inputs()
    ranking["winners"] *= 2
    with pytest.raises(ValueError, match="RANKING_INVALID"):
        build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


def test_rejection_explanation_uses_the_rank_direction_not_opposing_consumer():
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(
        signal_count=10,
        stage_counts={"NO_ENTRY_CANDIDATE": 10, "JEV_VETO": 1},
        by_strategy={
            "s6": {"stage_counts": {"JEV_VETO": 1}, "reason_counts": []},
            "s8": {"stage_counts": {"NO_ENTRY_CANDIDATE": 10}},
        },
    )
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    observed = result["rows"][0]["evidence"]
    assert observed["primary_reason"] == "JEV_VETO"
    assert observed["classification_scope"] == "MATCHED_DIRECTION_CONSUMER"


@pytest.mark.parametrize(
    "change",
    [
        {"closing_quantity": "0.5"},
        {"fills_valid": False},
        {"settled_ms": DAY["start_ms"]},
    ],
)
def test_partial_or_inconsistent_fills_cannot_gain_a_settled_net_label(change):
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(trades=[trade(**change)])
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert result["rows"][0]["evidence"]["trades"][0]["actual_settled_net_usdt"] is None


def test_path_source_mismatch_cannot_describe_production_mover():
    result = build(
        paths={
            "UPUSDT": {"status": "OBSERVED", "market_source": "SANDBOX", "features": {}}
        }
    )
    assert result["rows"][0]["path"]["reason"] == "PRICE_SOURCE_MISMATCH"


def test_path_is_whitelisted_and_cannot_smuggle_actual_or_achievable_profit():
    result = build(
        paths={
            "UPUSDT": {
                "status": "OBSERVED",
                "market_source": "BINANCE_USDM_LIVE",
                "features": {
                    "high_minute_ms": DAY["start_ms"] + 60_000,
                    "low_minute_ms": DAY["start_ms"],
                    "range_pct_of_open": "22",
                    "raw_key": "must-not-appear",
                },
                "actual_realized_net": "9999",
                "post_entry": {"hypothetical_pnl": "9999"},
            }
        }
    )
    assert result["rows"][0]["path"]["status"] == "OBSERVED"
    assert result["rows"][0]["path"]["features"]["range_pct_of_open"] == "22"
    assert "9999" not in str(result)
    assert "must-not-appear" not in str(result)


def test_digest_does_not_claim_unknown_market_source_is_live():
    ranking, evidence, paths = inputs()
    del ranking["market_source"]
    result = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert "来源未核实（榜单）" in format_digest(result)


def test_date_string_and_core_day_dictionary_have_identical_result():
    args = inputs()
    assert build_report(DAY["date"], *args, as_of_ms=NOW, scope=SCOPE) == build_report(
        DAY, *args, as_of_ms=NOW, scope=SCOPE
    )


def test_unavailable_signal_read_is_unknown_and_does_not_abort_daily_report():
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(signal_count=None)
    evidence["symbols"]["UPUSDT"]["coverage"]["signals"] = "UNAVAILABLE"
    report = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert report["rows"][0]["evidence"]["signal_count"] is None
    assert report["rows"][0]["evidence"]["primary_reason"] == "EVIDENCE_UNAVAILABLE"
    assert len(report["rows"]) == 2


def test_missing_intent_query_does_not_prove_candidate_had_no_fill():
    ranking, evidence, paths = inputs()
    evidence["symbols"]["UPUSDT"] = symbol_evidence(
        signal_count=1, stage_counts={"ENTRY_CANDIDATE": 1}
    )
    evidence["symbols"]["UPUSDT"]["coverage"]["intents"] = "UNAVAILABLE"
    report = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    assert (
        report["rows"][0]["evidence"]["primary_reason"] == "CANDIDATE_EXECUTION_UNKNOWN"
    )


def test_enrichment_requires_new_stable_content_digest_and_cannot_smuggle_secret_keys():
    report = build()
    original = report["content_digest"]
    report["research_only"] = True
    updated = seal_report(report)
    assert updated["content_digest"] != original
    assert seal_report(updated) == updated
    assert report["content_digest"] == original
    report["api_key"] = "not-allowed"
    with pytest.raises(ValueError, match="credential"):
        seal_report(report)


def test_stratified_coverage_preserves_source_windows_and_unknown_counts():
    ranking, evidence, paths = inputs()
    provenance = evidence["symbols"]["UPUSDT"]["coverage"]
    provenance.update(
        signals="TRUNCATED",
        signal_sample_basis="SOURCE_FAIR_FOUR_UTC8_WINDOWS_EARLIEST_WITHIN_WINDOW",
        signal_sources={
            "s3": "TRUNCATED",
            "tv_bridge": "UNAVAILABLE",
            "quant": "BOUNDED_QUERY_COMPLETE",
        },
        signal_limit_by_source={"s3": 84, "tv_bridge": 83, "quant": 83},
        signal_time_windows=[
            {
                "start_ms": DAY["start_ms"] + i * 6 * 3_600_000,
                "end_ms": DAY["start_ms"] + (i + 1) * 6 * 3_600_000,
                "sources": {
                    "s3": {
                        "status": "TRUNCATED",
                        "sample_limit": 21,
                        "examined_unique_signals": 21,
                        "observed_lower_bound": 22,
                        "sentinel_observed": True,
                    },
                    "tv_bridge": {
                        "status": "UNAVAILABLE",
                        "sample_limit": 21,
                        "examined_unique_signals": None,
                        "observed_lower_bound": None,
                        "sentinel_observed": None,
                    },
                },
            }
            for i in range(4)
        ],
    )
    report = build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)
    observed = report["rows"][0]["evidence"]
    assert observed["sampled_attribution"]
    assert observed["signal_count_basis"].endswith("SAMPLE_LOWER_BOUND")
    assert observed["coverage"]["signal_limit_by_source"] == {
        "s3": 84,
        "tv_bridge": 83,
        "quant": 83,
    }
    assert len(observed["coverage"]["signal_time_windows"]) == 4
    bucket = observed["coverage"]["signal_time_windows"][0]["sources"]
    assert bucket["s3"]["observed_lower_bound"] == 22
    assert bucket["tv_bridge"]["observed_lower_bound"] is None
    assert bucket["tv_bridge"]["sentinel_observed"] is None
    text = format_digest(report)
    assert "分层样本，非全量归因" in text
    assert "样本计数不代表全日总量" in text


def test_digest_summarizes_same_environment_actual_net_separately_from_gross_bound():
    report = build()
    report["actual_same_environment_paths"] = {
        "source": "BINANCE_USDM_SANDBOX",
        "cases": [
            {
                "episode_ref": "private-episode",
                "source": "BINANCE_USDM_SANDBOX",
                "actual_net_pnl": "2.9",
                "actual_net_basis": "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED",
                "gross_path": {
                    "status": "AVAILABLE_LOWER_BOUND",
                    "mfe_gross_usdt_lower_bound": "10",
                },
            }
        ],
    }
    text = format_digest(report)
    assert "1 笔合计 +2.90 USDT" in text
    assert "10.00～10.00 USDT" in text
    assert "不与净收益相加" in text
    assert "非全账户日收益" in text
    assert "private-episode" not in text
    assert "12.90" not in text
    assert len(text) <= 3500


def test_digest_never_uses_live_paths_as_sandbox_actual_profit():
    report = build()
    report["actual_same_environment_paths"] = {
        "source": "BINANCE_USDM_LIVE",
        "cases": [{"actual_net_pnl": "9999"}],
    }
    text = format_digest(report)
    assert "数据或来源未确认" in text
    assert "9999" not in text


def test_digest_missing_or_unverified_actual_paths_do_not_promise_verified_result():
    report = build()
    assert "同环境复盘样本" not in format_digest(report)
    report["actual_same_environment_paths"] = {
        "source": "BINANCE_USDM_SANDBOX",
        "cases": [
            {
                "episode_ref": "case",
                "source": "BINANCE_USDM_SANDBOX",
                "actual_net_pnl": "9999",
                "actual_net_basis": "UNVERIFIED",
                "gross_path": None,
            }
        ],
    }
    text = format_digest(report)
    assert "已核验净收益暂缺" in text
    assert "9999" not in text


def test_duplicate_actual_episode_cannot_double_count_digest_profit():
    report = build()
    case = {
        "episode_ref": "same",
        "source": "BINANCE_USDM_SANDBOX",
        "actual_net_pnl": "100",
        "actual_net_basis": "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED",
    }
    report["actual_same_environment_paths"] = {
        "source": "BINANCE_USDM_SANDBOX",
        "cases": [case, case],
    }
    text = format_digest(report)
    assert "合计 +200.00" not in text
    assert "已核验净收益暂缺" in text


@pytest.mark.parametrize(
    "status,phrase",
    [
        ("INSUFFICIENT_SAMPLE", "样本不足，尚未证明比只看价格更有效"),
        ("DESCRIPTIVE_REVIEW_READY", "达到描述性复核门槛，不等于验证盈利"),
    ],
)
def test_flow_evaluation_digest_never_promises_profit_or_entry_changes(status, phrase):
    report = build()
    report["flow_evaluation"] = {
        "version": "mover-flow-fixed-rule-evaluation-v1",
        "status": status,
    }
    text = format_digest(report)
    assert phrase in text and len(text) <= 3500


@pytest.mark.parametrize(
    "evaluation",
    [
        None,
        [],
        {"version": "other", "status": "INSUFFICIENT_SAMPLE"},
        {"version": "mover-flow-fixed-rule-evaluation-v1", "status": "private-error"},
    ],
)
def test_invalid_flow_evaluation_omits_optional_digest_without_leaking(evaluation):
    report = build()
    report["flow_evaluation"] = evaluation
    text = format_digest(report)
    assert "资金流增量对比" not in text and "private-error" not in text
