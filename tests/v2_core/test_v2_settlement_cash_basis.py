"""Pure immutable-basis reconstruction; no venue or database access."""

import json
from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from v2_core.evidence import canonical
from v2_core.precision import decimal_text, money_context
from v2_core.settlement_cash_basis import VERSION, apply_basis, build_basis, fill_facts
from v2_core.venue_cash import CashDifferencePolicy, analyze_venue_cash

SCOPE = {
    "exchange": "BINANCE",
    "account_id": "qa-basis-account",
    "environment": "SANDBOX",
    "product": "FUTURES",
}
EPISODE = "11111111-1111-4111-8111-111111111111"
INCOME_DIGEST = "a" * 64


def case(*, tiny=False, cash_rows=()):
    qty = "0.000000000000000001" if tiny else "1.25"
    entry = "1" if tiny else "100"
    exit_price = "1.000000000000000001" if tiny else "100.008"
    pnl = "0" if tiny else "0.00999999"
    fees = ("0", "0") if tiny else ("0.05", "0.046")
    rows = [
        (
            leg,
            Decimal(qty),
            Decimal(price),
            Decimal(fee),
            "USDT",
            f"fill-{tid}",
            {
                "source": "binance-userTrades",
                "trade_id": tid,
                "venue_realized_pnl": actual,
            },
            timestamp,
            f"order-{tid}",
        )
        for leg, price, fee, actual, timestamp, tid in (
            ("OPEN", entry, fees[0], "0", 10, "1"),
            ("CLOSE", exit_price, fees[1], pnl, 20, "2"),
        )
    ]
    with money_context():
        gross = Decimal(qty) * (Decimal(exit_price) - Decimal(entry))
        total_fees = sum(map(Decimal, fees), Decimal(0))
        cash = sum((r[0] for r in cash_rows), Decimal(0))
        report = {
            "intent_id": EPISODE,
            "accounting_revision": 5,
            "opened_quantity": qty,
            "closed_quantity": qty,
            "settlement_currency": "USDT",
            "missing_valuations": [],
            "gross_pnl": decimal_text(gross),
            "fees": decimal_text(total_fees),
            "cash_adjustments": decimal_text(cash),
            "net_pnl": decimal_text(gross - total_fees + cash),
        }
    trace = {
        "intent_id": EPISODE,
        "scope": {**SCOPE, "producer": "s6"},
        "request": {"symbol": "BTCUSDT", "side": "BUY"},
        "configuration": {"mode": "TESTNET_ADMISSION_ONLY"},
        "orders": [
            {"order_id": r[-1], "leg": r[0], "status": "FILLED", "quantity": qty}
            for r in rows
        ],
        "fills": [
            {
                "order_id": r[-1],
                "quantity": qty,
                "price": decimal_text(r[2]),
                "fee": decimal_text(r[3]),
                "fee_currency": "USDT",
                "occurred_at_ms": r[-2],
                "evidence": r[-3],
            }
            for r in rows
        ],
    }
    return {
        "report": report,
        "scope": deepcopy(SCOPE),
        "facts": fill_facts(rows),
        "cash_rows": list(cash_rows),
        "analysis": analyze_venue_cash(
            trace,
            report,
            policy=CashDifferencePolicy("qa-v1", "0.00000001", "0.00000001"),
        ),
        "income_digest": INCOME_DIGEST,
    }


def apply(inputs, basis):
    return apply_basis(
        inputs["report"],
        basis,
        scope=inputs["scope"],
        facts=inputs["facts"],
        cash_rows=inputs["cash_rows"],
        expected_income_digest=INCOME_DIGEST,
    )


def funding(*, key="funding-1", value="-0.01"):
    return (
        Decimal(value),
        "USDT",
        key,
        "FUNDING",
        {
            "income_id": "22222222-2222-4222-8222-222222222222",
            "allocation": {"proof": {"source": "directional-settlement-v1"}},
        },
    )


def insurance():
    return (
        Decimal("-0.84353267"),
        "USDT",
        "insurance-1",
        "CORRECTION",
        {
            "source": "verified-liquidation-insurance-clear-v1",
            "income_id": "33333333-3333-4333-8333-333333333333",
        },
    )


def test_committed_json_basis_reconstructs_actual_and_keeps_theory_separate():
    inputs = case()
    original = deepcopy(inputs)
    basis = json.loads(canonical(build_basis(**inputs)))
    result = apply(inputs, basis)
    assert basis["version"] == result["pnl_basis"] == VERSION
    assert result["gross_pnl"] == "0.00999999"
    assert result["theoretical_gross_pnl"] == "0.01"
    assert result["net_pnl"] == "-0.08600001"
    assert result["cash_adjustments"] == "0"
    assert result["cash_analysis"]["settlement_authorized"] is False
    assert inputs == original
    assert apply(inputs, basis) == result


def test_non_storage_representable_difference_is_analysis_not_adjustment():
    inputs = case(tiny=True)
    basis = build_basis(**inputs)
    result = apply(inputs, basis)
    assert Decimal(result["theoretical_gross_pnl"]) == Decimal("1e-36")
    assert result["gross_pnl"] == result["net_pnl"] == "0"
    assert result["cash_adjustments"] == basis["real_cash_adjustments"] == "0"
    assert Decimal(result["cash_analysis"]["cash_difference"]["decimal"]) == Decimal(
        "-1e-36"
    )


def test_basis_rebuild_does_not_inherit_ambient_decimal_rounding():
    inputs = case(cash_rows=[funding(), insurance()])
    expected = build_basis(**inputs)
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = context.traps[Rounded] = True
        basis = build_basis(**inputs)
        result = apply(inputs, basis)
    assert basis == expected
    assert result["net_pnl"] == "-0.93953268"


@pytest.mark.parametrize(
    "field,value",
    [
        ("episode_id", "44444444-4444-4444-8444-444444444444"),
        ("ledger_revision", 6),
        ("currency", "BNB"),
        ("actual_gross_pnl", "0.01"),
        ("fees", "0.095"),
        ("real_cash_adjustments", "0.00000001"),
        ("net_pnl", "-0.086"),
        ("theoretical_gross_pnl", "0.00999999"),
        ("fills_digest", "b" * 64),
        ("cash_facts_digest", "b" * 64),
        ("income_digest", "b" * 64),
        ("account_scope", {**SCOPE, "account_id": "other-account"}),
    ],
)
def test_changed_basis_cannot_be_applied(field, value):
    inputs = case()
    basis = deepcopy(build_basis(**inputs))
    basis[field] = value
    with pytest.raises(ValueError, match="SETTLEMENT_CASH_BASIS"):
        apply(inputs, basis)


@pytest.mark.parametrize(
    "defect",
    [
        "fill_key",
        "fill_order",
        "fill_quantity",
        "fill_price",
        "fill_time",
        "fill_evidence",
        "fill_actual",
        "fill_fee",
        "account",
        "episode",
        "revision",
    ],
)
def test_changed_current_facts_fail_reconstruction(defect):
    inputs = case()
    basis = deepcopy(build_basis(**inputs))
    if defect.startswith("fill_"):
        field, value = {
            "fill_key": ("key", "other-fill"),
            "fill_order": ("order_id", "other-order"),
            "fill_quantity": ("quantity", "1.26"),
            "fill_price": ("price", "100.009"),
            "fill_time": ("occurred_at_ms", 21),
            "fill_evidence": ("evidence", {"source": "unverified"}),
            "fill_actual": ("venue_realized_pnl", "0.01"),
            "fill_fee": ("fee", "0.047"),
        }[defect]
        inputs["facts"][1][field] = value
    elif defect == "account":
        inputs["scope"]["account_id"] = "other-account"
    elif defect == "episode":
        inputs["report"]["intent_id"] = "other-episode"
    else:
        inputs["report"]["accounting_revision"] += 1
    with pytest.raises(ValueError):
        apply(inputs, basis)


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "z" * 64, None, 42])
def test_income_digest_requires_exact_hex_identity(value):
    inputs = case()
    inputs["income_digest"] = value
    with pytest.raises(ValueError, match="VENUE_CASH_BASIS_INPUT_MISMATCH"):
        build_basis(**inputs)


@pytest.mark.parametrize("defect", ["actual", "theory", "episode"])
def test_analysis_must_match_bound_episode_and_economics(defect):
    inputs = case()
    if defect == "actual":
        inputs["analysis"]["actual_gross_pnl"] = "0.01"
    elif defect == "theory":
        inputs["analysis"]["theoretical_gross_pnl"] = "0.00999999"
    else:
        inputs["analysis"]["intent_id"] = "other-episode"
    with pytest.raises(ValueError, match="VENUE_CASH_BASIS_INPUT_MISMATCH"):
        build_basis(**inputs)


def test_real_funding_and_insurance_are_included_once_and_order_independent():
    inputs = case(cash_rows=[funding(), insurance()])
    basis = build_basis(**inputs)
    inputs["cash_rows"].reverse()
    result = apply(inputs, basis)
    assert basis["real_cash_adjustments"] == "-0.85353267"
    assert result["net_pnl"] == "-0.93953268"
    assert result["cash_adjustments"] == "-0.85353267"


@pytest.mark.parametrize("defect", ["key", "income_identity", "allocation"])
def test_same_total_cash_with_different_provenance_cannot_rebuild(defect):
    inputs = case(cash_rows=[funding()])
    basis = deepcopy(build_basis(**inputs))
    row = list(inputs["cash_rows"][0])
    if defect == "key":
        row[2] = "other-funding"
    elif defect == "income_identity":
        row[4]["income_id"] = "44444444-4444-4444-8444-444444444444"
    else:
        row[4]["allocation"] = {"proof": {"source": "different-allocation"}}
    inputs["cash_rows"][0] = tuple(row)
    with pytest.raises(ValueError, match="SETTLEMENT_CASH_BASIS_FACTS_CHANGED"):
        apply(inputs, basis)


@pytest.mark.parametrize(
    "kind,evidence",
    [
        ("CORRECTION", {"source": "directional-settlement-precision-v1"}),
        ("CORRECTION", {"source": "exclusive-testnet-venue-precision-v1"}),
        ("CORRECTION", {"source": "manual-correction"}),
        ("CORRECTION", {"source": "binance-insurance-clear-v1", "income_id": "qa"}),
        ("CORRECTION", {"source": "verified-liquidation-insurance-clear-v1"}),
        ("FUNDING", {}),
        ("FUNDING", {"income_id": "qa", "allocation": "unverified"}),
    ],
)
def test_legacy_bridges_and_unverified_adjustments_cannot_mix_with_venue_basis(
    kind, evidence
):
    inputs = case(cash_rows=[(Decimal("-0.01"), "USDT", "qa", kind, evidence)])
    with pytest.raises(ValueError, match="VENUE_CASH_REJECTS_UNCLASSIFIED_ADJUSTMENT"):
        build_basis(**inputs)


@pytest.mark.parametrize("defect", ["currency", "missing_valuation", "open", "empty"])
def test_only_closed_fully_valued_usdt_episodes_have_cash_basis(defect):
    inputs = case()
    if defect == "currency":
        inputs["report"]["settlement_currency"] = "USDC"
    elif defect == "missing_valuation":
        inputs["report"]["missing_valuations"] = [{"currency": "BNB"}]
    elif defect == "open":
        inputs["report"]["closed_quantity"] = "1"
    else:
        inputs["report"]["opened_quantity"] = inputs["report"]["closed_quantity"] = "0"
    with pytest.raises(ValueError, match="CLOSED_USDT_CASH_BASIS_REQUIRED"):
        build_basis(**inputs)


@pytest.mark.parametrize("basis", [None, {}, {"version": "future-version"}])
def test_unknown_basis_versions_are_not_applied(basis):
    with pytest.raises(ValueError, match="UNSUPPORTED_SETTLEMENT_CASH_BASIS"):
        apply(case(), basis)
