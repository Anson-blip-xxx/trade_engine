"""Pure cash facts stay exact without guessing a venue rounding convention."""

from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from random import Random

import pytest

from v2_core.evidence import canonical
from v2_core.venue_cash import MODEL, CashDifferencePolicy, analyze_venue_cash

POLICY = CashDifferencePolicy("unit-test-review-limits-v1", "0.000001", "0.0001")


def text(value):
    with localcontext() as ctx:
        ctx.prec = 200
        ctx.traps[Inexact] = True
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def case(
    side="BUY",
    opening=(("2997", ".03843"),),
    closing=(("2997", ".03844", ".02996999"),),
):
    qty = sum((Fraction(q) for q, _ in opening), Fraction(0))
    trace = {
        "intent_id": "venue-cash-unit-episode",
        "scope": {
            "exchange": "BINANCE",
            "environment": "SANDBOX",
            "product": "FUTURES",
            "producer": "s8",
            "account_id": "venue-cash-unit-account",
        },
        "configuration": {"mode": "TESTNET_ADMISSION_ONLY"},
        "request": {"symbol": "QAUSDT", "side": side},
        "orders": [
            {
                "order_id": "open",
                "leg": "OPEN",
                "status": "FILLED",
                "quantity": text(qty),
            }
        ],
        "fills": [],
    }
    for index, (quantity, price, actual) in enumerate(
        [*((q, p, "0") for q, p in opening), *closing]
    ):
        is_open = index < len(opening)
        oid = "open" if is_open else f"close-{index}"
        if not is_open:
            trace["orders"].append(
                {
                    "order_id": oid,
                    "leg": "CLOSE",
                    "status": "FILLED",
                    "quantity": quantity,
                }
            )
        trace["fills"].append(
            {
                "order_id": oid,
                "quantity": quantity,
                "price": price,
                "fee": "0",
                "fee_currency": "USDT",
                "occurred_at_ms": 1 if is_open else 100 + index,
                "evidence": {
                    "source": "binance-userTrades",
                    "trade_id": str(index),
                    "venue_realized_pnl": actual,
                },
            }
        )
    sign = 1 if side == "BUY" else -1
    gross = sign * (
        sum((Fraction(q) * Fraction(p) for q, p, _ in closing), Fraction(0))
        - sum((Fraction(q) * Fraction(p) for q, p in opening), Fraction(0))
    )
    return trace, {
        "intent_id": trace["intent_id"],
        "settlement_currency": "USDT",
        "missing_valuations": [],
        "opened_quantity": text(qty),
        "closed_quantity": text(qty),
        "gross_pnl": text(gross),
    }


def rational(value):
    exact = Fraction(int(value["numerator"]), int(value["denominator"]))
    if value["decimal"] is not None:
        assert Fraction(value["decimal"]) == exact
    return exact


@pytest.mark.parametrize("precision", [6, 28, 100])
@pytest.mark.parametrize("producer", ["s6", "s8"])
def test_reported_boundary_is_fact_not_algorithm_or_settlement_permission(
    precision, producer
):
    trace, report = case()
    trace["scope"]["producer"] = producer
    before = deepcopy((trace, report))
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        result = analyze_venue_cash(trace, report, policy=POLICY)
    assert result["model"] == MODEL
    assert result["actual_gross_pnl"] == "0.02996999"
    assert result["theoretical_gross_pnl"] == "0.02997"
    assert rational(result["cash_difference"]) == Fraction(-1, 100_000_000)
    assert result["anomaly"]["status"] == "WITHIN_POLICY"
    assert result["anomaly"]["policy"]["version"] == POLICY.version
    assert result["requires_income_match"] is True
    assert result["requires_final_wallet_match"] is True
    for key in (
        "algorithm_verified",
        "income_verified",
        "wallet_verified",
        "settlement_authorized",
    ):
        assert result[key] is False
    canonical(result)
    assert (trace, report) == before


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("closing_price", [".03842", ".03843", ".03844"])
@pytest.mark.parametrize(
    "delta", ["-.00000001", "-.00000002", ".00000001", ".000000005", "0"]
)
def test_cash_shape_has_no_positive_pnl_grid_direction_or_entry_witness_exceptions(
    side, closing_price, delta
):
    qty, entry = "2997", ".03843"
    exact = (
        Fraction(qty)
        * (Fraction(closing_price) - Fraction(entry))
        * (1 if side == "BUY" else -1)
    )
    trace, report = case(
        side, [(qty, entry)], [(qty, closing_price, text(exact + Fraction(delta)))]
    )
    result = analyze_venue_cash(trace, report, policy=POLICY)
    assert Fraction(result["actual_gross_pnl"]) == exact + Fraction(delta)
    assert rational(result["cash_difference"]) == Fraction(delta)
    assert result["anomaly"]["status"] == ("EXACT" if delta == "0" else "WITHIN_POLICY")


def test_nonzero_difference_without_explicit_policy_requires_review():
    result = analyze_venue_cash(*case())
    assert result["anomaly"] == {
        "status": "REVIEW_REQUIRED",
        "reasons": ["CASH_DIFFERENCE_POLICY_REQUIRED"],
        "policy": None,
    }


def test_exact_case_does_not_need_difference_policy():
    result = analyze_venue_cash(*case(closing=[("2997", ".03844", ".02997")]))
    assert result["anomaly"]["status"] == "EXACT"
    assert result["difference_summary"]["mismatch_count"] == 0


def test_repeating_average_and_per_fill_delta_are_retained_without_rounding():
    trace, report = case(
        opening=[("1", "1"), ("2", "2")],
        closing=[("1", "3", "1.33333333"), ("2", "1", "-1.33333333")],
    )
    result = analyze_venue_cash(trace, report, policy=POLICY)
    assert rational(result["opening_average_price"]) == Fraction(5, 3)
    assert result["opening_average_price"]["decimal"] is None
    assert result["actual_gross_pnl"] == result["theoretical_gross_pnl"] == "0"
    assert result["cash_difference"]["decimal"] == "0"
    deltas = [rational(p["cash_difference"]) for p in result["fills"]]
    assert deltas == [Fraction(-1, 300_000_000), Fraction(1, 300_000_000)]
    assert result["anomaly"]["status"] == "WITHIN_POLICY"
    assert rational(result["difference_summary"]["absolute_sum"]) == Fraction(
        1, 150_000_000
    )
    assert result["difference_summary"]["mismatch_count"] == 2
    assert analyze_venue_cash(trace, report)["anomaly"]["status"] == "REVIEW_REQUIRED"


def test_high_precision_theory_is_not_forced_into_numeric_38_18_correction():
    trace, report = case(
        opening=[("1e-18", "1")], closing=[("1e-18", "1.000000000000000001", "0")]
    )
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        result = analyze_venue_cash(trace, report, policy=POLICY)
    assert result["actual_gross_pnl"] == "0"
    assert Fraction(result["theoretical_gross_pnl"]) == Fraction(1, 10**36)
    assert rational(result["cash_difference"]) == Fraction(-1, 10**36)
    assert result["anomaly"]["status"] == "WITHIN_POLICY"


def test_report_actual_gross_does_not_replace_explicit_theory_baseline():
    trace, report = case()
    report["theoretical_gross_pnl"] = report["gross_pnl"]
    report["gross_pnl"] = ".02996999"
    result = analyze_venue_cash(trace, report, policy=POLICY)
    assert result["theoretical_gross_pnl"] == "0.02997"
    report["theoretical_gross_pnl"] = ".02996999"
    with pytest.raises(ValueError, match="VENUE_LEDGER_ECONOMICS_MISMATCH"):
        analyze_venue_cash(trace, report, policy=POLICY)


def test_partial_cancelled_close_and_multiple_fills_of_same_close_are_supported():
    trace, report = case(
        opening=[("3", "1")], closing=[("1", "2", "1"), ("2", "2", "2")]
    )
    first, second = trace["orders"][1:]
    first["quantity"], first["status"] = "4", "CANCELLED"
    trace["fills"][-1]["order_id"] = first["order_id"]
    second["status"] = "REJECTED"
    result = analyze_venue_cash(trace, report)
    assert len(result["fills"]) == 2
    assert result["anomaly"]["status"] == "EXACT"


def test_cancelled_partial_open_is_supported_when_fully_closed():
    trace, report = case()
    trace["orders"][0].update(status="CANCELLED", quantity="3000")
    assert (
        analyze_venue_cash(trace, report, policy=POLICY)["actual_gross_pnl"]
        == "0.02996999"
    )


@pytest.mark.parametrize(
    "delta,expected",
    [(".000001", "WITHIN_POLICY"), (".000001000000000001", "REVIEW_REQUIRED")],
)
def test_review_limits_are_exact_and_do_not_modify_reported_cash(delta, expected):
    trace, report = case(opening=[("1", "1")], closing=[("1", "1", delta)])
    result = analyze_venue_cash(trace, report, policy=POLICY)
    assert result["anomaly"]["status"] == expected
    assert Fraction(result["actual_gross_pnl"]) == Fraction(delta)
    assert rational(result["cash_difference"]) == Fraction(delta)


def test_opposite_huge_errors_cannot_cancel_below_anomaly_limits():
    result = analyze_venue_cash(
        *case(opening=[("2", "1")], closing=[("1", "1", "1000"), ("1", "1", "-1000")]),
        policy=POLICY,
    )
    assert result["cash_difference"]["decimal"] == "0"
    assert result["anomaly"]["status"] == "REVIEW_REQUIRED"
    assert result["anomaly"]["reasons"] == [
        "PER_FILL_CASH_DIFFERENCE_LIMIT",
        "EPISODE_ABSOLUTE_CASH_DIFFERENCE_LIMIT",
    ]
    assert result["difference_summary"]["absolute_sum"]["decimal"] == "2000"


def test_sum_absolute_limit_can_trigger_even_when_each_fill_passes():
    policy = CashDifferencePolicy("unit-sum-limit-v1", ".000001", ".000001")
    result = analyze_venue_cash(
        *case(
            opening=[("2", "1")],
            closing=[("1", "1", ".000001"), ("1", "1", "-.000001")],
        ),
        policy=policy,
    )
    assert result["anomaly"]["reasons"] == ["EPISODE_ABSOLUTE_CASH_DIFFERENCE_LIMIT"]


@pytest.mark.parametrize("bad", [False, True, {}, [], "policy"])
def test_policy_cannot_be_an_implicit_boolean_or_untyped_config(bad):
    with pytest.raises(ValueError, match="EXPLICIT_CASH_DIFFERENCE_POLICY_REQUIRED"):
        analyze_venue_cash(*case(), policy=bad)


@pytest.mark.parametrize(
    "version,per_fill,total",
    [
        ("", "0", "0"),
        (" v1", "0", "0"),
        ("v1", "-.1", "1"),
        ("v1", "1", "-.1"),
        ("v1", 0.1, "1"),
        ("v1", "NaN", "1"),
        ("v1", "1", "Infinity"),
    ],
)
def test_policy_validates_nonnegative_exact_bounded_values(version, per_fill, total):
    with pytest.raises(ValueError):
        CashDifferencePolicy(version, per_fill, total)


@pytest.mark.parametrize(
    "key,value",
    [
        ("exchange", "OTHER"),
        ("environment", "LIVE"),
        ("product", "SPOT"),
        ("producer", "pm"),
        ("account_id", ""),
    ],
)
def test_scope_is_explicit_and_testnet_only(key, value):
    trace, report = case()
    trace["scope"][key] = value
    with pytest.raises(ValueError, match="UNSUPPORTED_VENUE_CASH_SCOPE"):
        analyze_venue_cash(trace, report, policy=POLICY)


@pytest.mark.parametrize(
    "defect",
    [
        "side",
        "symbol",
        "mode",
        "intent",
        "currency",
        "valuation",
        "report_gross",
        "duplicate_order",
        "leg",
        "status",
        "extra_open",
        "unknown_order",
        "source",
        "duplicate_trade",
        "trade_id",
        "timestamp",
        "timestamp_bool",
        "fee_currency",
        "quantity",
        "price",
        "venue_float",
        "fee_float",
        "open_nonzero",
        "same_time",
        "opening_after",
        "requested",
        "rejected_filled",
        "report_quantity",
        "unclosed",
        "missing_open",
        "missing_close",
    ],
)
def test_corrupt_or_unsupported_economic_shapes_fail_closed(defect):
    trace, report = case()
    if defect in {"side", "symbol"}:
        trace["request"][defect] = "OTHER"
    elif defect == "mode":
        trace["configuration"]["mode"] = "LIVE"
    elif defect == "intent":
        report["intent_id"] = "other"
    elif defect == "currency":
        report["settlement_currency"] = "USDC"
    elif defect == "valuation":
        report["missing_valuations"] = [{}]
    elif defect == "report_gross":
        report["gross_pnl"] = ".02996999"
    elif defect == "duplicate_order":
        trace["orders"].append(deepcopy(trace["orders"][0]))
    elif defect == "leg":
        trace["orders"][-1]["leg"] = "OTHER"
    elif defect == "status":
        trace["orders"][-1]["status"] = "UNKNOWN"
    elif defect == "extra_open":
        trace["orders"].append(
            {
                "order_id": "another-open",
                "leg": "OPEN",
                "status": "REJECTED",
                "quantity": "1",
            }
        )
    elif defect == "unknown_order":
        trace["fills"][-1]["order_id"] = "unknown"
    elif defect == "source":
        trace["fills"][-1]["evidence"]["source"] = "estimated"
    elif defect == "duplicate_trade":
        trace["fills"][-1]["evidence"]["trade_id"] = trace["fills"][0]["evidence"][
            "trade_id"
        ]
    elif defect == "trade_id":
        trace["fills"][-1]["evidence"]["trade_id"] = ""
    elif defect in {"timestamp", "timestamp_bool"}:
        trace["fills"][-1]["occurred_at_ms"] = -1 if defect == "timestamp" else True
    elif defect == "fee_currency":
        trace["fills"][-1]["fee_currency"] = "BNB"
    elif defect in {"quantity", "price"}:
        trace["fills"][-1][defect] = "0"
    elif defect == "venue_float":
        trace["fills"][-1]["evidence"]["venue_realized_pnl"] = 0.02996999
    elif defect == "fee_float":
        trace["fills"][-1]["fee"] = 0.0
    elif defect == "open_nonzero":
        trace["fills"][0]["evidence"]["venue_realized_pnl"] = ".00000001"
    elif defect in {"same_time", "opening_after"}:
        trace["fills"][0]["occurred_at_ms"] = trace["fills"][-1]["occurred_at_ms"] + (
            defect == "opening_after"
        )
    elif defect == "requested":
        trace["orders"][-1]["quantity"] = "2998"
    elif defect == "rejected_filled":
        trace["orders"][-1]["status"] = "REJECTED"
    elif defect == "report_quantity":
        report["opened_quantity"] = "2998"
    elif defect == "unclosed":
        trace["fills"][-1]["quantity"] = trace["orders"][-1]["quantity"] = "2996"
    elif defect == "missing_open":
        trace["fills"] = trace["fills"][1:]
    elif defect == "missing_close":
        trace["fills"] = trace["fills"][:-1]
    with pytest.raises(ValueError):
        analyze_venue_cash(trace, report, policy=POLICY)


@pytest.mark.parametrize(
    "bad", [None, 0.02997, "NaN", "Infinity", "1e-101", "1e101", "", "1" * 201]
)
def test_derived_report_requires_bounded_exact_decimal_text(bad):
    trace, report = case()
    report["gross_pnl"] = bad
    with pytest.raises(ValueError):
        analyze_venue_cash(trace, report, policy=POLICY)


def test_fill_and_order_permutation_do_not_change_evidence():
    trace, report = case(
        opening=[("1", "1"), ("2", "2")],
        closing=[("1", "3", "1.33333333"), ("2", "1", "-1.33333333")],
    )
    expected = analyze_venue_cash(trace, report, policy=POLICY)
    trace["fills"].reverse()
    trace["orders"].reverse()
    assert analyze_venue_cash(trace, report, policy=POLICY) == expected


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_randomized_closed_multifill_episodes_preserve_rational_identities(side):
    rng = Random(73149)
    for _ in range(100):
        quantities = [rng.randint(1, 10000), rng.randint(1, 10000)]
        opening = [
            (str(q), text(Fraction(rng.randint(1000000, 1500000), 100000000)))
            for q in quantities
        ]
        entry = sum((Fraction(q) * Fraction(p) for q, p in opening), Fraction(0)) / sum(
            quantities
        )
        closing = []
        for q in quantities:
            p = Fraction(rng.randint(1000000, 1500000), 100000000)
            expected = q * (p - entry) * (1 if side == "BUY" else -1)
            actual = Fraction(
                int(expected * 100000000) + rng.choice([-1, 0, 1]), 100000000
            )
            closing.append((str(q), text(p), text(actual)))
        trace, report = case(side, opening, closing)
        result = analyze_venue_cash(trace, report, policy=POLICY)
        diffs = [rational(fill["cash_difference"]) for fill in result["fills"]]
        assert rational(result["cash_difference"]) == sum(diffs)
        assert Fraction(result["actual_gross_pnl"]) == sum(
            Fraction(pnl) for _, _, pnl in closing
        )
        assert Fraction(result["theoretical_gross_pnl"]) == Fraction(
            report["gross_pnl"]
        )
        assert rational(result["difference_summary"]["absolute_sum"]) == sum(
            abs(delta) for delta in diffs
        )
        assert rational(result["difference_summary"]["absolute_max"]) == max(
            abs(delta) for delta in diffs
        )
        assert result["anomaly"]["status"] in {"EXACT", "WITHIN_POLICY"}
        canonical(result)
