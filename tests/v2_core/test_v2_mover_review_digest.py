"""Optional research annotations cannot bypass account/time evidence boundaries."""

from copy import deepcopy

import pytest

from services.v2_daily_mover_coverage import build_coverage_review, unavailable_snapshot
from services.v2_daily_mover_execution_review import build_execution_review
from services.v2_daily_mover_report import (
    _execution_review_digest,
    build_report,
    format_digest,
)
from tests.v2_core.test_v2_daily_mover_execution_review import (
    digest,
    path_case,
    trade,
    with_paths,
)
from tests.v2_core.test_v2_daily_mover_execution_review import (
    report as execution_report,
)
from tests.v2_core.test_v2_daily_mover_report import DAY, NOW, SCOPE, inputs, market


def seal_review(value):
    value["content_digest"] = digest(
        {k: v for k, v in value.items() if k != "content_digest"}
    )
    return value


def report():
    ranking, evidence, paths = inputs()
    value = build_report(
        DAY["date"], ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE
    )
    value["account_evidence"] = evidence
    return value


def coverage(value):
    snapshot = unavailable_snapshot(
        environment="SANDBOX",
        symbols=[r["symbol"] for r in value["rows"]],
        as_of_ms=NOW,
    )
    snapshot.update(
        status="OBSERVED", reason="CATALOG_OBSERVED", observed_at_ms=NOW - 1
    )
    snapshot["rows"] = [
        {
            "symbol": r["symbol"],
            "present": False,
            "status": "ABSENT_FROM_CURRENT_CATALOG",
            "supported": None,
        }
        for r in value["rows"]
    ]
    value["coverage_review"] = build_coverage_review(value, snapshot)
    return value


def test_real_unicode_execution_review_is_visible_and_bound_to_own_scope():
    row = trade(symbol="牛来USDT")
    value = execution_report(row)
    value["rows"][0]["symbol"] = row["symbol"]
    value["account_evidence"]["symbols"] = {row["symbol"]: {"trades": [row]}}
    with_paths(value, path_case(row))
    value["execution_review"] = build_execution_review(value)
    lines = _execution_review_digest(value)
    assert any("+2.90 USDT" in line for line in lines)
    assert any("1盈/0亏/0平" in line for line in lines)
    assert any("1笔期末有利" in line for line in lines)
    assert "private-account" not in str(lines)


@pytest.mark.parametrize(
    "changes",
    [
        {"account_environment": "LIVE"},
        {"scope_key": "b" * 64},
        {"as_of_ms": NOW},
        {"evidence_as_of_ms": NOW},
        {"execution_authorized": True},
        {"promotion_authorized": True},
    ],
)
def test_valid_hash_does_not_make_cross_scope_or_cutoff_summary_valid(changes):
    value = execution_report()
    review = build_execution_review(value)
    review.update(changes)
    value["execution_review"] = seal_review(review)
    assert _execution_review_digest(value) == []


def test_new_review_failure_cannot_fall_back_to_legacy_unverified_path_pnl():
    value = report()
    raw = trade(net="999")
    with_paths(value, path_case(raw))
    value["execution_review"] = {"status": "UNKNOWN"}
    text = format_digest(value)
    assert "999" not in text
    assert "暂无可展示的已核验结算摘要" in text


@pytest.mark.parametrize("bad", ["NaN", "1e999", [], {}, True])
def test_optional_invalid_number_does_not_break_digest(bad):
    value = execution_report()
    review = build_execution_review(value)
    review["totals"]["held_minutes_median"] = bad
    value["execution_review"] = seal_review(review)
    assert _execution_review_digest(value) == []


def test_current_catalog_annotation_preserves_historical_uncertainty():
    value = coverage(report())
    text = format_digest(value)
    assert "Testnet目录未收录2个" in text
    assert "当日上市状态仍未知" in text
    assert "未查到保留信号" in text
    assert text.count("复盘时执行目录未收录") == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"account_environment": "LIVE"},
        {"as_of_ms": NOW + 1},
        {"market_source": "BINANCE_USDM_SANDBOX"},
        {"rows": None},
        {"rows": []},
    ],
)
def test_catalog_summary_rejects_valid_hash_for_wrong_envelope(changes):
    value = coverage(report())
    value["coverage_review"].update(changes)
    seal_review(value["coverage_review"])
    assert "目录未收录" not in format_digest(value)


def test_coverage_digest_detects_edits_and_rebound_symbols():
    value = coverage(report())
    value["coverage_review"]["rows"][0]["symbol"] = "WRONGUSDT"
    assert "目录未收录" not in format_digest(value)
    seal_review(value["coverage_review"])
    assert "目录未收录" not in format_digest(value)


def test_twenty_unicode_rows_keep_every_symbol_within_utf16_budget():
    ranking, evidence, paths = inputs()
    ranking["winners"] = [market(f"牛来{i}USDT", "10") for i in range(10)]
    ranking["losers"] = [market(f"下跌{i}USDT", "-10") for i in range(10)]
    ranking["eligible_summaries"] = ranking["winners"] + ranking["losers"]
    value = build_report(
        DAY["date"], ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE
    )
    coverage(value)
    value["execution_review"] = {"status": "UNKNOWN"}
    original = deepcopy(value)
    text = format_digest(value, account_alias="🚀" * 40)
    assert len(text.encode("utf-16-le")) // 2 <= 3500
    for row in ranking["eligible_summaries"]:
        assert row["symbol"] in text
    assert value == original
