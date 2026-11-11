"""Whole-account and frozen-input summaries cannot be confused with a backtest."""

import hashlib
import json
from copy import deepcopy

import pytest
from test_v2_daily_mover_report import DAY, NOW, SCOPE, inputs, market

from services.v2_daily_mover_report import build_report, format_digest


def seal(value):
    value = {k: v for k, v in value.items() if k != "content_digest"}
    value["content_digest"] = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return value


def summary(count, net, wins, losses):
    return {
        "settled_count": count,
        "net_usdt": net,
        "wins": wins,
        "losses": losses,
        "breakeven": count - wins - losses,
    }


def report_fixture(*, dense=False):
    ranking, evidence, paths = inputs()
    if dense:
        symbols = ["𠮷" * 30 + f"{i:02d}" + "USDT" for i in range(20)]
        ranking["winners"] = [market(s) for s in symbols[:10]]
        ranking["losers"] = [market(s, "-10") for s in symbols[10:]]
        ranking["eligible_summaries"] = ranking["winners"] + ranking["losers"]
        ranking["coverage"] = {
            "discovered": 20,
            "eligible": 20,
            "excluded": 0,
            "zero_return": 0,
        }
    report = build_report(
        DAY["date"], ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE
    )
    if dense:
        for row in report["rows"]:
            row["evidence"].update(
                primary_reason="MATCHED_DIRECTION_FILLED",
                sampled_attribution=True,
                reason_counts=[
                    {
                        "reason": "TREND_VOLUME_UNCONFIRMED",
                        "stage": "MARKET_FILTER_REJECTED",
                        "count": 250,
                    },
                    {
                        "reason": "TREND_FLOW_UNCONFIRMED",
                        "stage": "MARKET_FILTER_REJECTED",
                        "count": 249,
                    },
                    {
                        "reason": "TREND_NO_FRESH_SETUP",
                        "stage": "MARKET_FILTER_REJECTED",
                        "count": 248,
                    },
                ],
            )
    report["account_evidence"] = evidence
    base = {
        "scope_key": report["scope_key"],
        "account_environment": "SANDBOX",
        "as_of_ms": NOW,
        "window_start_ms": DAY["start_ms"],
        "window_end_ms": DAY["end_ms"],
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
    }
    report["account_day_review"] = seal(
        {
            **base,
            "version": "daily-account-review-v1",
            "status": "AVAILABLE",
            "totals": summary(28, "6.97271373", 12, 16),
            "by_side": {
                "BUY": summary(11, "-9.52299967", 2, 9),
                "SELL": summary(17, "16.49571340", 10, 7),
            },
            "coverage": {
                "verified_settled_count": 28,
                "query_complete": True,
                "truncated": False,
                "full_closed_settlement_population_proven": True,
            },
            "ranking_vs_other": {
                "status": "AVAILABLE",
                "basis": "VERIFIED_RANKING_EPISODE_REFS_NOT_SYMBOL_MEMBERSHIP",
                "ranking": summary(14, "25.90918239", 10, 4),
                "other": summary(14, "-18.93646866", 2, 12),
            },
        }
    )
    report["frozen_replay_review"] = seal(
        {
            **base,
            "version": "daily-mover-frozen-replay-v1",
            "status": "PARTIAL",
            "evidence_as_of_ms": NOW,
            "basis": "CURRENT_EVALUATOR_ON_FROZEN_INPUTS_BASELINE_CHECKED",
            "historical_code_identity": "NOT_PROVEN",
            "summary": {
                "requested_count": 100,
                "replayed_count": 97,
                "baseline_matched_count": 97,
                "baseline_mismatch_count": 0,
                "unknown_count": 3,
                "volume_gate_changed_count": 13,
                "new_market_candidate_count": 2,
            },
        }
    )
    return report


def test_account_header_precedes_rankings_and_shows_complement_and_sides():
    text = format_digest(report_fixture())
    assert text.index("账户已结算交易（库内）28笔") < text.index("涨幅前10")
    for value in (
        "净收益+6.97",
        "12盈/16亏",
        "多单11笔 -9.52",
        "空单17笔 +16.50",
        "榜单关联14笔 +25.91",
        "其余14笔 -18.94",
        "非账户权益日收益",
    ):
        assert value in text


def test_replay_replaces_old_metric_only_and_never_claims_orders_or_profits():
    report = report_fixture()
    report["gate_shadow"] = {
        "version": "mover-gate-shadow-v1",
        "execution_authorized": False,
        "not_trade_prediction": True,
        "summary": {
            "replayed_count": 0,
            "metric_only_count": 97,
            "volume_gate_changed_count": 13,
            "other_gate_still_failed_count": 0,
        },
    }
    text = format_digest(report)
    assert "冻结输入重算97条、基线吻合97条" in text
    assert "量能单门改变13条、行情候选新增2条" in text
    assert "3条未知" in text and "历史代码身份未证明" in text
    assert "不等于成交或盈利" in text and "仅指标对照97条" not in text


@pytest.mark.parametrize("key", ["account_day_review", "frozen_replay_review"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_key", "a" * 64),
        ("account_environment", "LIVE"),
        ("as_of_ms", NOW + 1),
        ("window_start_ms", DAY["start_ms"] - 86400000),
        ("window_end_ms", DAY["end_ms"] + 1),
        ("execution_authorized", True),
        ("automatic_parameter_updates", True),
        ("promotion_authorized", True),
    ],
)
def test_resigned_cross_scope_or_unsafe_review_never_displays(key, field, value):
    report = report_fixture()
    report[key][field] = value
    report[key] = seal(report[key])
    text = format_digest(report)
    if key == "account_day_review":
        assert "账户整体结算尚未核验" in text and "净收益+6.97" not in text
    else:
        assert "冻结输入重算：证据尚未核验" in text and "行情候选新增2条" not in text


def test_invalid_digest_and_summary_counts_fail_closed_without_losing_report():
    report = report_fixture()
    report["account_day_review"]["totals"]["net_usdt"] = "999"
    report["frozen_replay_review"]["summary"]["new_market_candidate_count"] = 98
    report["frozen_replay_review"] = seal(report["frozen_replay_review"])
    text = format_digest(report)
    assert "账户整体结算尚未核验" in text and "冻结输入重算：证据尚未核验" in text
    assert "UPUSDT" in text and "DOWNUSDT" in text and "999" not in text


@pytest.mark.parametrize("status,complete", [("PARTIAL", False), ("AVAILABLE", False)])
def test_partial_population_not_presented_as_whole_account(status, complete):
    report = report_fixture()
    part = report["account_day_review"]
    part["status"] = status
    part["coverage"]["full_closed_settlement_population_proven"] = complete
    report["account_day_review"] = seal(part)
    text = format_digest(report)
    assert "仅核验子集28笔" in text and "整体结果未知" in text
    assert "账户已结算交易（库内）" not in text and "其余14笔" not in text


def test_bad_side_or_ranking_partition_not_reported_as_financial_split():
    report = report_fixture()
    part = report["account_day_review"]
    part["by_side"]["BUY"]["net_usdt"] = "0"
    part["ranking_vs_other"]["other"]["net_usdt"] = "0"
    report["account_day_review"] = seal(part)
    text = format_digest(report)
    assert "净收益+6.97" in text and "多单11笔" not in text and "其余14笔" not in text


def test_mismatched_evidence_cutoff_does_not_replay():
    report = report_fixture()
    report["account_evidence"]["as_of_ms"] += 1
    assert "冻结输入重算：证据尚未核验" in format_digest(report)


def test_empty_ranking_still_has_account_header():
    report = report_fixture()
    report["rows"] = []
    assert "账户已结算交易（库内）28笔" in format_digest(report)


def test_dense_twenty_unicode_rows_keep_header_and_replay_under_telegram_budget():
    report = report_fixture(dense=True)
    original = deepcopy(report)
    text = format_digest(report, account_alias="𠮷" * 40)
    assert len(text.encode("utf-16-le")) // 2 <= 3500
    assert all(row["symbol"] in text for row in report["rows"])
    assert "账户已结算交易（库内）28笔" in text and "冻结输入重算97条" in text
    assert report == original


def setup_report_fixture(*, dense=False):
    report = report_fixture(dense=dense)
    base = {
        "scope_key": report["scope_key"],
        "account_environment": report["account_environment"],
        "as_of_ms": report["as_of_ms"],
        "window_start_ms": report["window"]["start_ms"],
        "window_end_ms": report["window"]["end_ms"],
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
    }
    study = seal(
        {
            **base,
            "version": "setup-shadow-study-v1",
            "status": "PARTIAL",
            "plan_digest": "c" * 64,
            "input_digest": "d" * 64,
            "summary": {
                "selected_count": 8,
                "baseline_matched_count": 5,
                "baseline_mismatch_count": 1,
                "unknown_count": 2,
                "alternatives": {
                    "BREAKOUT_VOLUME075": {"new_market_candidate_count": 4},
                    "PULLBACK_VOLUME075": {"new_market_candidate_count": 2},
                },
            },
        }
    )
    cases = []
    for i, states in enumerate(
        (
            ("OBSERVED", "OBSERVED", "PENDING"),
            ("OBSERVED", "UNKNOWN", "PENDING"),
            ("UNKNOWN", "UNKNOWN", "UNKNOWN"),
        )
    ):
        cases.append(
            {
                "opportunity_ref": f"{i:064x}",
                "windows": {
                    horizon: {
                        "status": status,
                        "return_pct": "98765.4321" if status == "OBSERVED" else None,
                    }
                    for horizon, status in zip(("30", "60", "240"), states, strict=True)
                },
            }
        )
    report["setup_study"] = study
    report["setup_path_review"] = seal(
        {
            **base,
            "version": "setup-shadow-observed-paths-v1",
            "status": "AVAILABLE",
            "study_content_digest": study["content_digest"],
            "plan_digest": study["plan_digest"],
            "input_digest": study["input_digest"],
            "cases": cases,
        }
    )
    return report


def test_setup_research_describes_candidates_and_observations_without_implying_trades():
    report = setup_report_fixture()
    before = deepcopy(report)
    text = format_digest(report)
    assert "分层量能研究（不限榜单）：8个固定去重机会，5可比" in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text
    assert "有界时段样本，非全量；未验证盈利，不改开仓规则" in text
    assert "30/60/240分钟有效路径：2/1/0（固定抽取3个机会）" in text
    assert "仅价格观察，不是成交或净收益" in text
    assert "冻结输入重算97条" in text
    assert "账户已结算交易（库内）28笔" in text
    assert "98765.4321" not in text
    assert "回测胜率" not in text
    assert report == before


@pytest.mark.parametrize("review_key", ["setup_study", "setup_path_review"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_key", "a" * 64),
        ("account_environment", "LIVE"),
        ("as_of_ms", NOW + 1),
        ("window_start_ms", DAY["start_ms"] - 86_400_000),
        ("window_end_ms", DAY["end_ms"] + 1),
        ("execution_authorized", True),
        ("automatic_parameter_updates", True),
        ("promotion_authorized", True),
        ("version", "other-version"),
    ],
)
def test_setup_digest_rejects_resigned_foreign_scope_window_version_and_permissions(
    review_key, field, value
):
    report = setup_report_fixture()
    report[review_key] = seal({**report[review_key], field: value})
    text = format_digest(report)
    assert "30/60/240分钟有效路径" not in text
    if review_key == "setup_study":
        assert "分层量能研究：证据尚未核验" in text
        assert "突破方案新增4" not in text
    else:
        assert "突破方案新增4、回踩方案新增2行情候选" in text
    assert "账户已结算交易（库内）28笔" in text
    assert "UPUSDT" in text and "DOWNUSDT" in text


@pytest.mark.parametrize(
    "field", ["study_content_digest", "input_digest", "plan_digest"]
)
def test_setup_path_digest_must_match_the_attached_study_and_inputs(field):
    report = setup_report_fixture()
    report["setup_path_review"] = seal({**report["setup_path_review"], field: "e" * 64})
    text = format_digest(report)
    assert "30/60/240分钟有效路径" not in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text
    assert "账户已结算交易（库内）28笔" in text


@pytest.mark.parametrize("field", ["input_digest", "plan_digest"])
def test_replaced_study_cannot_display_path_counts_from_its_previous_inputs(field):
    report = setup_report_fixture()
    report["setup_study"] = seal({**report["setup_study"], field: "e" * 64})
    text = format_digest(report)
    assert "30/60/240分钟有效路径" not in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text


@pytest.mark.parametrize("review_key", ["setup_study", "setup_path_review"])
def test_tampered_setup_content_without_resealing_cannot_display(review_key):
    report = setup_report_fixture()
    if review_key == "setup_study":
        report[review_key]["summary"]["alternatives"]["BREAKOUT_VOLUME075"][
            "new_market_candidate_count"
        ] = 5
    else:
        report[review_key]["cases"][0]["windows"]["240"]["status"] = "OBSERVED"
    text = format_digest(report)
    assert "30/60/240分钟有效路径" not in text
    if review_key == "setup_study":
        assert "分层量能研究：证据尚未核验" in text
        assert "突破方案新增5" not in text
    assert "账户已结算交易（库内）28笔" in text


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [
            {
                "windows": {
                    h: {"status": "UNKNOWN", "return_pct": None}
                    for h in ("30", "60", "240")
                }
            }
        ],
    ],
)
def test_unknown_setup_path_evidence_is_never_rendered_as_zero_observations(cases):
    report = setup_report_fixture()
    report["setup_path_review"] = seal(
        {**report["setup_path_review"], "status": "UNKNOWN", "cases": cases}
    )
    text = format_digest(report)
    assert "分层后续路径：证据尚未核验，不据此判断盈亏" in text
    assert "30/60/240分钟有效路径" not in text
    assert "有效路径：0/0/0" not in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text


def test_unknown_setup_study_is_not_presented_as_zero_new_candidates():
    report = setup_report_fixture()
    report["setup_study"] = seal(
        {**report["setup_study"], "status": "UNKNOWN", "summary": {}}
    )
    text = format_digest(report)
    assert "分层量能研究：证据尚未核验" in text
    assert "突破方案新增0" not in text and "回踩方案新增0" not in text
    assert "30/60/240分钟有效路径" not in text


@pytest.mark.parametrize(
    "change",
    [
        {"selected_count": 9},
        {"unknown_count": None},
        {"unknown_count": False},
        {"baseline_matched_count": 97},
        {
            "alternatives": {
                "BREAKOUT_VOLUME075": {"new_market_candidate_count": 6},
                "PULLBACK_VOLUME075": {"new_market_candidate_count": 2},
            }
        },
    ],
)
def test_invalid_setup_summary_is_unknown_without_synthetic_zero_counts(change):
    report = setup_report_fixture()
    report["setup_study"]["summary"].update(change)
    report["setup_study"] = seal(report["setup_study"])
    text = format_digest(report)
    assert "分层量能研究：证据尚未核验" in text
    assert "突破方案新增" not in text
    assert "30/60/240分钟有效路径" not in text
    assert "账户已结算交易（库内）28笔" in text


@pytest.mark.parametrize(
    "damage", ["missing_window", "non_dict_window", "too_many_cases"]
)
def test_malformed_setup_path_cases_do_not_fabricate_observation_counts(damage):
    report = setup_report_fixture()
    paths = report["setup_path_review"]
    if damage == "missing_window":
        paths["cases"][0]["windows"].pop("30")
    elif damage == "non_dict_window":
        paths["cases"][0]["windows"]["30"] = None
    else:
        paths["cases"] = paths["cases"] * 5
    report["setup_path_review"] = seal(paths)
    text = format_digest(report)
    assert "30/60/240分钟有效路径" not in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text


def test_dense_unicode_report_keeps_all_twenty_symbols_and_setup_summaries_within_utf16_budget():
    report = setup_report_fixture(dense=True)
    original = deepcopy(report)
    text = format_digest(report, account_alias="𠮷" * 40)
    assert len(text.encode("utf-16-le")) // 2 <= 3500
    assert len(report["rows"]) == 20
    assert all(row["symbol"] in text for row in report["rows"])
    assert "账户已结算交易（库内）28笔" in text
    assert "冻结输入重算97条" in text
    assert "突破方案新增4、回踩方案新增2行情候选" in text
    assert "30/60/240分钟有效路径：2/1/0" in text
    assert "仅价格观察，不是成交或净收益" in text
    assert report == original
