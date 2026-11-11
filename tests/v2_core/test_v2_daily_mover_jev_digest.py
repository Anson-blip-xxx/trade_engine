"""JEV attribution is scoped to verified settlements, never all candidates."""

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from services.v2_daily_account_review import _empty_jev
from services.v2_daily_mover_report import _jev_participation_digest, format_digest
from tests.v2_core.test_v2_daily_review_digest import (
    report_fixture,
    seal,
    setup_report_fixture,
)

GROUPS = (
    "MODEL_APPROVED",
    "MODEL_REJECTED",
    "FALLBACK",
    "DISABLED",
    "NOT_MODEL_EVALUATED",
    "UNKNOWN",
)


def aggregate(parts):
    with localcontext() as context:
        context.prec = 200
        return {
            "settled_count": sum(part[0] for part in parts),
            "net_usdt": str(sum((Decimal(part[1]) for part in parts), Decimal(0))),
        }


def jev_report(*, groups=None, dense=False, setup=False):
    value = (setup_report_fixture if setup else report_fixture)(dense=dense)
    account = value["account_day_review"]
    groups = groups or {"FALLBACK": (51, "-14.31353779")}
    groups = {name: groups.get(name, (0, "0")) for name in GROUPS}
    total = aggregate(list(groups.values()))
    count = total["settled_count"]
    account["totals"] = {
        **total,
        "wins": 0 if Decimal(total["net_usdt"]) < 0 else count,
        "losses": count if Decimal(total["net_usdt"]) < 0 else 0,
        "breakeven": 0,
    }
    account["by_side"] = {}
    account["ranking_vs_other"] = {"status": "UNKNOWN"}
    account["coverage"]["verified_settled_count"] = count
    jev = _empty_jev(account)
    unknown = groups["UNKNOWN"][0]
    jev.update(
        status="UNKNOWN" if unknown and unknown == count else "PARTIAL" if unknown else "AVAILABLE",
        reason="JEV_ENTRY_EVIDENCE_INCOMPLETE" if unknown else None,
        denominator_settled_count=count,
        denominator_net_usdt=total["net_usdt"],
        by_participation={name: aggregate([part]) for name, part in groups.items()},
        model_evaluated=aggregate([groups["MODEL_APPROVED"], groups["MODEL_REJECTED"]]),
        by_source={"s3": dict(total)} if count else {},
        fallback_causes={"TOKEN_BUDGET_PACED": aggregate([groups["FALLBACK"]])}
        if groups["FALLBACK"][0]
        else {},
    )
    jev["coverage"].update(
        query_complete=True,
        queried_episode_count=count,
        evidence_row_count=count - unknown,
        classified_count=count - unknown,
        unknown_count=unknown,
    )
    account["jev_participation"] = jev
    value["account_day_review"] = seal(account)
    return value


def reseal(value):
    value["account_day_review"] = seal(value["account_day_review"])
    return value


def test_all_51_fallbacks_show_zero_verified_model_participation_and_budget_causes():
    value = jev_report()
    account = value["account_day_review"]
    account["totals"].update(wins=20, losses=31)
    account["jev_participation"]["fallback_causes"] = {
        "TOKEN_BUDGET_PACED": aggregate([(49, "-13.81353779")]),
        "TOKEN_BUDGET_EXHAUSTED": aggregate([(2, "-0.5")]),
    }
    reseal(value)
    before = deepcopy(value)
    text = format_digest(value)
    assert "账户已结算交易（库内）51笔，净收益-14.31" in text
    assert "20盈/31亏" in text
    assert "已结算成交子集51笔，非全部候选、非模型收益证明" in text
    assert "模型实评0笔" in text
    assert "回退51笔（预算节流49、预算耗尽2）" in text
    assert text.index("账户已结算交易") < text.index("JEV（") < text.index("涨幅前10")
    assert len(_jev_participation_digest(value)) == 2
    assert value == before


def test_approved_and_rejected_fills_are_distinct_and_no_model_profit_is_claimed():
    value = jev_report(
        groups={
            "MODEL_APPROVED": (2, "1.25"),
            "MODEL_REJECTED": (1, "-0.5"),
            "FALLBACK": (1, "-0.25"),
            "DISABLED": (1, "1"),
            "NOT_MODEL_EVALUATED": (1, "-2"),
        }
    )
    text = "\n".join(_jev_participation_digest(value))
    assert "已结算成交子集6笔" in text
    assert "模型实评3笔（通过2）" in text
    assert "模型否决后成交1笔" in text
    assert "回退1笔" in text and "禁用1笔" in text and "未实评1笔" in text
    assert "非全部候选、非模型收益证明" in text
    assert "模型收益+" not in text


@pytest.mark.parametrize("approved", [0, 1])
def test_partial_evidence_never_turns_unknown_participation_into_zero(approved):
    value = jev_report(
        groups={
            "MODEL_APPROVED": (approved, str(approved)),
            "FALLBACK": (2, "-1"),
            "UNKNOWN": (1, "-0.1"),
        }
    )
    text = "\n".join(_jev_participation_digest(value))
    assert "未知1笔" in text and "模型实评0笔" not in text
    assert ("已确认模型实评1笔" if approved else "模型参与尚未完整核验") in text


@pytest.mark.parametrize("failure", ["all_unknown", "query_failed"])
def test_unknown_or_failed_query_keeps_account_totals_without_claiming_zero(failure):
    value = jev_report(groups={"UNKNOWN": (51, "-14.31353779")})
    jev = value["account_day_review"]["jev_participation"]
    if failure == "query_failed":
        jev["coverage"]["query_complete"] = False
        jev["reason"] = "private-account-id secret transport exception"
    text = format_digest(reseal(value))
    assert "JEV参与尚未核验" in text and "未知不等于未参与" in text
    assert "模型实评0" not in text and "private-account" not in text and "secret" not in text
    assert "账户已结算交易（库内）51笔，净收益-14.31" in text


@pytest.mark.parametrize(
    "field,bad",
    [
        ("version", "other-version"),
        ("basis", "ALL_CANDIDATES"),
        ("scope_key", "a" * 64),
        ("account_environment", "LIVE"),
        ("as_of_ms", 1),
        ("window_start_ms", 1),
        ("window_end_ms", 1),
        ("execution_authorized", True),
        ("automatic_parameter_updates", True),
        ("promotion_authorized", True),
        ("execution_authorized", 0),
        ("status", "UNKNOWN"),
    ],
)
def test_resigned_invalid_child_cannot_hide_or_rewrite_verified_account(field, bad):
    value = jev_report()
    value["account_day_review"]["jev_participation"][field] = bad
    text = format_digest(reseal(value))
    assert "JEV参与尚未核验" in text and "模型实评0" not in text
    assert "账户已结算交易（库内）51笔，净收益-14.31" in text


@pytest.mark.parametrize(
    "field,bad",
    [
        ("version", "other-version"),
        ("scope_key", "a" * 64),
        ("account_environment", "LIVE"),
        ("as_of_ms", 1),
        ("window_start_ms", 1),
        ("window_end_ms", 1),
        ("execution_authorized", True),
        ("automatic_parameter_updates", True),
        ("promotion_authorized", True),
    ],
)
def test_child_is_never_trusted_without_bound_outer_review(field, bad):
    value = jev_report()
    value["account_day_review"][field] = bad
    # Even a matching inner forgery must not bypass the report-level binding.
    value["account_day_review"]["jev_participation"][field] = bad
    text = format_digest(reseal(value))
    assert "账户整体结算尚未核验" in text and "JEV参与尚未核验" in text
    assert "UPUSDT" in text and "DOWNUSDT" in text


@pytest.mark.parametrize(
    "path,bad",
    [
        (("denominator_settled_count",), 50),
        (("denominator_settled_count",), True),
        (("denominator_net_usdt",), "-14.31353778"),
        (("by_participation", "FALLBACK", "settled_count"), 50),
        (("by_participation", "FALLBACK", "net_usdt"), "-14.31353778"),
        (("by_participation", "DISABLED", "net_usdt"), "0.1"),
        (("by_participation", "DISABLED", "settled_count"), -1),
        (("by_participation", "DISABLED", "settled_count"), 501),
        (("by_participation", "DISABLED", "settled_count"), "0"),
        (("by_participation", "DISABLED", "settled_count"), False),
        (("by_participation", "FALLBACK", "net_usdt"), "NaN"),
        (("by_participation", "FALLBACK", "net_usdt"), "Infinity"),
        (("model_evaluated", "settled_count"), 1),
        (("model_evaluated", "net_usdt"), "0.1"),
        (("by_source", "s3", "net_usdt"), "0"),
        (("by_source", "s3", "settled_count"), 50),
        (("fallback_causes", "TOKEN_BUDGET_PACED", "net_usdt"), "0"),
        (("fallback_causes", "TOKEN_BUDGET_PACED", "settled_count"), 50),
        (("coverage", "query_complete"), False),
        (("coverage", "query_complete"), 1),
        (("coverage", "queried_episode_count"), 50),
        (("coverage", "evidence_row_count"), 50),
        (("coverage", "classified_count"), 50),
        (("coverage", "unknown_count"), 1),
        (("coverage", "unknown_count"), False),
        (("status",), "PARTIAL"),
    ],
)
def test_all_denominators_groups_and_coverage_must_reconcile_exactly(path, bad):
    value = jev_report()
    target = value["account_day_review"]["jev_participation"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    text = format_digest(reseal(value))
    assert "JEV参与尚未核验" in text and "回退51" not in text
    assert "账户已结算交易（库内）51笔，净收益-14.31" in text


def test_unsigned_child_change_invalidates_outer_digest():
    value = jev_report()
    value["account_day_review"]["jev_participation"]["denominator_settled_count"] = 500
    assert "JEV参与尚未核验" in format_digest(value)


@pytest.mark.parametrize("bad", [None, [], "raw-secret-error", 1])
@pytest.mark.parametrize("field", ["by_participation", "by_source", "fallback_causes", "coverage", "model_evaluated"])
def test_malformed_child_sections_cannot_break_account_summary(field, bad):
    value = jev_report()
    value["account_day_review"]["jev_participation"][field] = bad
    text = format_digest(reseal(value))
    assert "JEV参与尚未核验" in text and "raw-secret-error" not in text
    assert "账户已结算交易（库内）51笔" in text


def test_unrecognized_raw_cause_and_reason_are_never_echoed():
    value = jev_report()
    jev = value["account_day_review"]["jev_participation"]
    jev["reason"] = "secret account-id SQL password"
    jev["fallback_causes"] = {
        "raw-secret-account-error": aggregate([(51, "-14.31353779")])
    }
    text = format_digest(reseal(value))
    assert "回退51笔" in text
    assert all(secret not in text for secret in ("secret", "account-id", "password"))


def test_net_partitions_remain_exact_under_low_precision_and_large_cancellation():
    value = jev_report(
        groups={
            "MODEL_APPROVED": (1, "10000000000000000000000000000000000000000.0000000000000000000000000000000000000001"),
            "MODEL_REJECTED": (1, "-1e40"),
        }
    )
    with localcontext() as context:
        context.prec = 3
        assert "模型实评2笔" in "\n".join(_jev_participation_digest(value))
    value["account_day_review"]["jev_participation"]["model_evaluated"]["net_usdt"] = "0"
    assert "JEV参与尚未核验" in "\n".join(_jev_participation_digest(reseal(value)))


@pytest.mark.parametrize("setup", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_twenty_dense_unicode_rows_and_other_reviews_fit_utf16_budget(setup, mixed):
    groups = {name: (80, "1.25") for name in GROUPS} if mixed else None
    value = jev_report(dense=True, setup=setup, groups=groups)
    before = deepcopy(value)
    text = format_digest(value, account_alias="𠮷" * 40)
    assert len(text.encode("utf-16-le")) // 2 <= 3500
    assert all(row["symbol"] in text for row in value["rows"])
    assert ("已确认模型实评160笔" if mixed else "模型实评0笔") in text
    assert ("回退80笔" if mixed else "回退51笔") in text
    assert ("账户已结算交易（库内）480笔" if mixed else "账户已结算交易（库内）51笔") in text
    assert "冻结输入重算97条" in text
    if setup:
        assert "分层量能研究" in text
    assert value == before


def test_legacy_absence_and_empty_population_do_not_invent_model_participation():
    assert _jev_participation_digest(report_fixture()) == []
    value = jev_report(groups={"MODEL_APPROVED": (0, "0")})
    text = "\n".join(_jev_participation_digest(value))
    assert "暂无已核验结算成交可归因" in text and "模型实评0" not in text
