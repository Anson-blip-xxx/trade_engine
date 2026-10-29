"""Real failing fill economics plus context, sign and corruption matrices."""

from copy import deepcopy
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, Inexact, localcontext
from fractions import Fraction
from random import Random

import pytest

from services.v2_directional_cash import reconcile_cash
from services.v2_directional_exit_market import BinanceDirectionalExitMarket
from services.v2_price_features import price_features
from v2_core.account_risk import AccountScope
from v2_core.directional_exit import _exact
from v2_core.directional_outcomes import exact, settlement_amount
from v2_core.precision import amount, decimal_text, derived_amount, money_context
from v2_core.settlement_precision import rounding_proof

SCOPE = AccountScope("BINANCE", "precision-qa", "SANDBOX", "FUTURES")
REAL_CASES = [
    (
        "BUY",
        [
            ("5", ".10757"),
            ("52", ".10760"),
            ("54", ".10760"),
            ("47", ".10760"),
            ("54", ".10761"),
            ("52", ".10761"),
            ("47", ".10761"),
            ("49", ".10761"),
            ("203", ".10761"),
        ],
        [("281", ".11305", "1.52950346"), ("282", ".11175", "1.16834653")],
    ),
    (
        "SELL",
        [("7825", ".006322"), ("799", ".006318"), ("942", ".006318")],
        [
            ("2869", ".006007", ".90164638"),
            ("829", ".006051", ".22405549"),
            ("5868", ".006055", "1.56248412"),
        ],
    ),
]


def cash_case(side, opening, closing):
    with money_context():
        qty = sum(Decimal(q) for q, _ in opening)
        trace = {
            "intent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "scope": {**asdict(SCOPE), "producer": "s8"},
            "configuration": {"mode": "TESTNET_ADMISSION_ONLY"},
            "request": {"symbol": "QAUSDT", "side": side},
            "cash": [],
            "fills": [],
            "orders": [
                {
                    "order_id": "open",
                    "leg": "OPEN",
                    "status": "FILLED",
                    "quantity": str(qty),
                }
            ],
        }
        rows = []
        for i, (q, p, pnl) in enumerate([*((q, p, "0") for q, p in opening), *closing]):
            is_open = i < len(opening)
            oid = "open" if is_open else f"close-{i}"
            if not is_open:
                trace["orders"].append(
                    {"order_id": oid, "leg": "CLOSE", "status": "FILLED", "quantity": q}
                )
            trace["fills"].append(
                {
                    "order_id": oid,
                    "quantity": q,
                    "price": p,
                    "fee": "0.0001",
                    "fee_currency": "USDT",
                    "occurred_at_ms": 3 if is_open else 10 + i,
                    "evidence": {
                        "source": "binance-userTrades",
                        "trade_id": str(i),
                        "venue_realized_pnl": pnl,
                    },
                }
            )
            for kind, value in (("COMMISSION", "-0.0001"), ("REALIZED_PNL", pnl)):
                rows.append(
                    {
                        "income_id": f"{kind}-{i}",
                        "income_type": kind,
                        "source_id": str(i),
                        "symbol": "QAUSDT",
                        "currency": "USDT",
                        "amount": value,
                        "occurred_at_ms": 10 + i,
                        "evidence": {"source": "binance-income", "trade_id": str(i)},
                    }
                )
        gross = (
            sum(Decimal(q) * Decimal(p) for q, p, _ in closing)
            - sum(Decimal(q) * Decimal(p) for q, p in opening)
        ) * (1 if side == "BUY" else -1)
        report = {
            "intent_id": trace["intent_id"],
            "settlement_currency": "USDT",
            "missing_valuations": [],
            "opened_quantity": str(qty),
            "closed_quantity": str(qty),
            "gross_pnl": str(gross),
            "fees": str(Decimal(".0001") * len(trace["fills"])),
            "accounting_revision": len(trace["fills"]),
        }
    return trace, report, rows


@pytest.mark.parametrize("case", REAL_CASES)
@pytest.mark.parametrize("precision", [6, 28, 100])
def test_real_partial_closes_prove_each_truncated_fill(case, precision):
    trace, report, rows = cash_case(*case)
    original = deepcopy((trace, report, rows))
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        proof = reconcile_cash(trace, report, rows, scope=SCOPE)
    assert proof["status"] == "CASH_MATCHED"
    assert proof["precision_reconciliation"]["amount"] == "-0.00000001"
    assert all(
        f["method"] == "TRUNCATE_TOWARD_ZERO_8"
        for f in proof["precision_reconciliation"]["fills"]
    )
    assert (trace, report, rows) == original


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_generated_wins_losses_and_multifill_rounding(side):
    rng = Random(289)
    for _ in range(100):
        a, b = rng.randint(1, 10000), rng.randint(1, 10000)
        opening = [(str(a), ".000000000123456789"), (str(b), ".000000000123456790")]
        entry = sum(Fraction(q) * Fraction(p) for q, p in opening) / (a + b)
        closing = []
        for q in (a, b):
            p = str(Decimal(rng.randint(100000000, 150000000)).scaleb(-18))
            theoretical = q * (Fraction(p) - entry) * (1 if side == "BUY" else -1)
            pnl = Decimal(int(theoretical * 100000000)).scaleb(-8)
            closing.append((str(q), p, str(pnl)))
        trace, report, rows = cash_case(side, opening, closing)
        proof = reconcile_cash(trace, report, rows, scope=SCOPE)
        assert Fraction(proof["gross_pnl"]) == Fraction(report["gross_pnl"]) + Fraction(
            proof["precision_reconciliation"]["amount"]
        )


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_pnl",
        "cancelling_errors",
        "missing_income",
        "wrong_fee",
        "nonzero_open",
        "timeline",
        "report",
        "existing_cash",
    ],
)
def test_small_or_cancelling_discrepancies_never_get_blanket_tolerance(defect):
    trace, report, rows = cash_case(*REAL_CASES[0])
    if defect in {"wrong_pnl", "cancelling_errors"}:
        for fill, delta in zip(
            trace["fills"][-2:],
            [Decimal(".00000001"), Decimal("-.00000001")]
            if defect == "cancelling_errors"
            else [Decimal(".00000001")],
        ):
            tid = fill["evidence"]["trade_id"]
            fill["evidence"]["venue_realized_pnl"] = str(
                Decimal(fill["evidence"]["venue_realized_pnl"]) + delta
            )
            next(
                r
                for r in rows
                if r["income_type"] == "REALIZED_PNL" and r["source_id"] == tid
            )["amount"] = fill["evidence"]["venue_realized_pnl"]
    elif defect == "missing_income":
        rows.pop()
    elif defect == "wrong_fee":
        report["fees"] = "0.00110001"
    elif defect == "nonzero_open":
        trace["fills"][0]["evidence"]["venue_realized_pnl"] = "0.00000001"
        rows[1]["amount"] = "0.00000001"
    elif defect == "timeline":
        trace["fills"][0]["occurred_at_ms"] = trace["fills"][-1]["occurred_at_ms"]
    elif defect == "report":
        report["gross_pnl"] = "2.69784999"
    else:
        trace["cash"] = [{"kind": "CORRECTION", "amount": "-0.00000001"}]
    with pytest.raises(ValueError):
        reconcile_cash(trace, report, rows, scope=SCOPE)


@pytest.mark.parametrize("precision", [6, 28, 100])
def test_numeric_boundaries_do_not_inherit_ambient_precision_or_traps(precision):
    value = Decimal("12345678901234567890.123456789012345678")
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        for formatter in (
            decimal_text,
            exact,
            _exact,
            BinanceDirectionalExitMarket._exact,
        ):
            assert formatter(value) == str(value)
        assert settlement_amount(str(value)) == value
        assert derived_amount(
            Decimal("1e-30"), rounding=ROUND_CEILING, positive=True
        ) == Decimal("1e-18")
        with pytest.raises(ValueError):
            derived_amount(
                Decimal("1e-30"), rounding=ROUND_HALF_EVEN, preserve_nonzero=True
            )


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", "1e20", "1e-19", 1.1, True]
)
def test_external_facts_remain_strict(value):
    with pytest.raises(ValueError):
        amount(value)


def test_low_price_market_indicators_ignore_ambient_inexact_trap():
    bars = [
        {"c": "0.000000000001", "h": "0.0000000000010001", "l": "0.000000000001"}
    ] * 70
    expected = price_features(bars)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        assert price_features(bars) == expected
    assert Decimal(expected["atr"]) > 0


def test_unrepresentable_cash_correction_is_not_rounded_for_storage():
    trace, report, _ = cash_case(
        "BUY",
        [(".000000000000000001", "1")],
        [(".000000000000000001", "1.000000000000000001", "0")],
    )
    with pytest.raises(ValueError):
        rounding_proof(trace, report)
