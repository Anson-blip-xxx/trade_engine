"""Derived holding times must survive the real daily-report display contract."""

from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from services.v2_daily_mover_execution_review import build_execution_review
from services.v2_daily_mover_report import (
    _number,
    _verified_optional_review,
    build_report,
    format_digest,
    seal_report,
)
from tests.v2_core.test_v2_daily_mover_execution_review import (
    END,
    MINUTE,
    SCOPE,
    START,
    raw,
    report,
    trade,
)
from v2_core.account_risk import AccountScope


def duration_trade(identity, duration_ms, **changes):
    return trade(
        identity,
        closed_ms=START + MINUTE + duration_ms,
        settled_ms=START + 2 * MINUTE + duration_ms,
        **changes,
    )


def test_repeating_101_character_hold_reaches_sealed_daily_digest():
    # The failing production duration was 50.6568666... minutes. With the
    # producer's 100-digit context its unquantized text has 101 characters.
    with localcontext() as context:
        context.prec = 100
        assert len(format(Decimal(3_039_412) / MINUTE, "f")) == 101
    rows = [
        duration_trade(str(index), 3_039_412, net="1" if index < 9 else "-0.72309485")
        for index in range(21)
    ]
    evidence = report(*rows)["account_evidence"]
    sample = {
        "symbol": "BTCUSDT",
        "return_pct": "10",
        "open": "100",
        "close": "110",
        "high": "112",
        "low": "99",
        "base_volume": "1000",
        "engine_symbol_supported": True,
    }
    ranking = {
        "start_ms": START,
        "end_ms": END,
        "market_source": "BINANCE_USDM_LIVE",
        "status": "PARTIAL_COVERAGE",
        "coverage": {"discovered": 1, "eligible": 1},
        "winners": [sample],
        "losers": [],
        "eligible_summaries": [sample],
    }
    day = datetime.fromtimestamp(START / 1000, ZoneInfo("Asia/Shanghai")).date()
    value = build_report(
        day.isoformat(),
        ranking,
        evidence,
        {},
        as_of_ms=END,
        scope=AccountScope(**SCOPE),
    )
    value["account_evidence"] = evidence
    value["execution_review"] = build_execution_review(value)
    sealed = seal_report(value)
    review = sealed["execution_review"]
    assert _verified_optional_review(
        sealed, "execution_review", "daily-mover-execution-review-v1"
    ) == review
    assert review["totals"]["net_usdt"] == "0.32286180"
    assert review["totals"]["held_minutes_median"] == "50.656867"
    assert all(row["held_minutes"] == "50.656867" for row in review["trades"])
    text = format_digest(sealed)
    assert "榜单关联已核验结算21笔，净收益+0.32 USDT" in text
    assert "9盈/12亏/0平，持仓中位50.7分钟" in text
    assert "暂无可展示的已核验结算摘要" not in text
    # The generic financial-number boundary remains strict.
    with pytest.raises(ValueError, match="DAILY_REPORT_NUMBER_INVALID"):
        _number("1." + "0" * 99)


@pytest.mark.parametrize("precision", [3, 28, 100])
@pytest.mark.parametrize("rounding", [ROUND_DOWN, ROUND_UP])
def test_derived_minutes_ignore_callers_decimal_context_without_rounding_pnl(
    precision, rounding
):
    value = report(
        duration_trade("gain", 3_039_412, net="0.123456789012345678901234567890"),
        duration_trade("loss", 3_039_413, net="-0.023456789012345678901234567889"),
    )
    expected = build_execution_review(value)
    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        assert build_execution_review(value) == expected
    assert expected["totals"]["net_usdt"] == "0.100000000000000000000000000001"
    assert expected["totals"]["held_minutes_median"] == "50.656875"
    assert expected["trades"][0]["net_usdt"] == raw(value)[0]["net_pnl"]


@pytest.mark.parametrize(
    ("durations", "expected"),
    [
        ([0], "0"),
        ([1], "0.000017"),
        ([2], "0.000033"),
        ([0, 1], "0.000008"),
        # Median must use the original milliseconds: averaging the rounded
        # values 0 and 0.000033 and then rounding again would yield 0.000016.
        ([0, 2], "0.000017"),
        ([59_999], "0.999983"),
        ([60_000], "1.000000"),
        ([3_039_412], "50.656867"),
    ],
)
def test_zero_millisecond_and_even_sample_medians_keep_duration_precision(
    durations, expected
):
    review = build_execution_review(
        report(*(duration_trade(str(index), ms) for index, ms in enumerate(durations)))
    )
    assert review["status"] == "AVAILABLE"
    for summary in (
        review["totals"],
        review["by_side"]["BUY"],
        review["by_exit_reason"]["PEAK_DRAWDOWN"],
    ):
        assert summary["held_minutes_median"] == expected
        assert _number(summary["held_minutes_median"]) is not None
    assert all(_number(row["held_minutes"]) is not None for row in review["trades"])


def test_empty_review_has_no_synthetic_zero_holding_time():
    value = report()
    raw(value).clear()
    value["rows"][0]["evidence"]["trades"].clear()
    review = build_execution_review(value)
    assert review["status"] == "AVAILABLE"
    assert review["totals"]["settled_count"] == 0
    assert review["totals"]["held_minutes_median"] is None


def test_maximum_timestamp_range_produces_bounded_holding_time():
    day_ms, offset_ms = 1440 * MINUTE, 480 * MINUTE
    end = ((2**53 - 1 + offset_ms) // day_ms) * day_ms - offset_ms
    start, closed = end - day_ms, end - 1
    value = report(trade(opened_ms=0, decided_ms=0, closed_ms=closed, settled_ms=closed))
    value["as_of_ms"] = end
    value["window"].update(start_ms=start, end_ms=end)
    value["account_evidence"].update(
        as_of_ms=end, window_start_ms=start, window_end_ms=end
    )
    review = build_execution_review(value)
    assert review["status"] == "AVAILABLE"
    hold = _number(review["totals"]["held_minutes_median"])
    assert abs(hold - Decimal(closed) / MINUTE) <= Decimal("0.0000005")
    assert len(review["trades"][0]["held_minutes"]) < 100
    raw(value)[0]["closed_ms"] = 2**53
    assert build_execution_review(value)["totals"] is None
