"""Cost diagnostics cannot replace settled PnL or fabricate missing evidence."""

import pytest

from services.v2_daily_mover_report import _execution_cost_digest, format_digest
from tests.v2_core.test_v2_daily_review_digest import report_fixture, seal
from v2_core.execution_cost_evidence import empty_execution_cost_review


def fixture(*, dense=False):
    report = report_fixture(dense=dense)
    account = report["account_day_review"]
    account["totals"] = {
        "settled_count": 51,
        "net_usdt": "-14.31353779",
        "wins": 21,
        "losses": 30,
        "breakeven": 0,
    }
    account["coverage"]["verified_settled_count"] = 51
    account["by_side"] = {}
    account["ranking_vs_other"] = {"status": "UNKNOWN"}
    count = account["totals"]["settled_count"]
    costs = empty_execution_cost_review()
    costs.update(
        status="PARTIAL",
        reason="FROZEN_PLAN_EVIDENCE_INCOMPLETE",
        denominator_settled_count=count,
        plan_comparable_count=count - 1,
        native_stop_decomposed_count=2,
    )
    costs["coverage"].update(query_complete=True, unknown_count=1)
    account["execution_cost_evidence"] = costs
    report["account_day_review"] = seal(account)
    return report


def test_cost_line_explicitly_limits_scope_and_does_not_call_price_differences_slippage():
    report = fixture()
    lines = _execution_cost_digest(report)
    assert len(lines) == 1
    assert "计划可比50/51笔" in lines[0]
    assert "原始止损可分解2笔" in lines[0]
    assert "结算子集" in lines[0] and "不直接归因为滑点" in lines[0]


@pytest.mark.parametrize(
    "key,value",
    [
        ("denominator_settled_count", 50),
        ("plan_comparable_count", 52),
        ("native_stop_decomposed_count", 51),
        ("native_stop_decomposed_count", True),
        ("execution_authorized", True),
        ("promotion_authorized", True),
        ("version", "untrusted"),
        ("status", "UNKNOWN"),
    ],
)
def test_malformed_cost_evidence_is_unknown_not_a_zero_or_changed_pnl(key, value):
    report = fixture()
    report["account_day_review"]["execution_cost_evidence"][key] = value
    report["account_day_review"] = seal(report["account_day_review"])
    assert "尚不完整" in _execution_cost_digest(report)[0]
    assert report["account_day_review"]["totals"]["net_usdt"] == "-14.31353779"


def test_old_reports_have_no_synthetic_zero_cost_line_and_tampering_fails_closed():
    report = report_fixture()
    assert _execution_cost_digest(report) == []
    report = fixture()
    report["account_day_review"]["execution_cost_evidence"][
        "plan_comparable_count"
    ] -= 1
    assert "尚不完整" in _execution_cost_digest(report)[0]


def test_partial_queries_are_not_announced_as_complete():
    report = fixture()
    report["account_day_review"]["execution_cost_evidence"]["coverage"][
        "query_complete"
    ] = False
    report["account_day_review"] = seal(report["account_day_review"])
    assert "尚不完整" in _execution_cost_digest(report)[0]


def test_dense_digest_keeps_account_loss_and_telegram_length_bound():
    report = fixture(dense=True)
    text = format_digest(report, account_alias="QA")
    assert "-14.31" in text
    assert "计划可比50/51笔" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096
