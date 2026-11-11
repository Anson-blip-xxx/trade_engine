"""Pure evidence, rounding and price-drift tests (no exchange/DB writes)."""

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from test_v2_directional import inputs, sizing

from v2_core.capital_model import guard_risk, portfolio_budget
from v2_core.directional import evaluate_market, size_candidate
from v2_core.runtime_policy import resolve
from v2_core.sizing_budget import fixed_stop_risk


def settings():
    return resolve(
        {
            "capital.enabled": True,
            "portfolio_risk.enabled": True,
            "sizing.risk_fraction": "0.02",
            "portfolio_risk.max_fraction": "0.03",
            "sizing.max_margin_fraction": "0.70",
            "sizing.pool_fraction": "0.70",
            "sizing.cost_buffer_fraction": "0.002",
        }
    )


def plan(side="LONG", stop="0.04"):
    return replace(
        evaluate_market(**inputs()),
        side=side,
        stop_fraction=stop,
        margin_mode="ISOLATED",
        leverage=3,
    )


def proposed(**overrides):
    args = sizing(
        remaining_portfolio_risk="30",
        sizing_mark_price="100",
        price_drift_fraction="0.015",
    )
    args.update(overrides)
    return args


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("mark", ["98", "100", "102"])
def test_budget_includes_fixed_stop_price_drift_fees_and_venue_step(side, mark):
    explanation = {}
    result = size_candidate(
        plan(side),
        **proposed(sizing_mark_price=mark),
        policy=settings(),
        explanation=explanation,
    )
    assert result.reason == "SIZED"
    qty = Decimal(result.quantity)
    unit = Decimal(explanation["unit_stop_risk"])
    assert qty * unit <= 20  # unchanged 2% single-trade ceiling
    assert Fraction(qty) % Fraction(".001") == 0
    # The binding risk quantity is floored, never uplifted for venue minimums.
    if "SINGLE_STOP_RISK" in explanation["binding_quantity_limits"]:
        assert (qty + Decimal(".001")) * unit > 20
    for px in (Decimal(explanation["price_low"]), Decimal(explanation["price_high"])):
        assert (
            fixed_stop_risk(
                quantity=str(qty),
                notional=qty * px,
                stop_price=result.stop_price,
                side=side,
                stop_fraction=".04",
                cost_fraction=".002",
            )
            <= 20
        )


def test_residual_is_not_double_reduced_and_zero_never_rounds_up():
    proof = {}
    result = size_candidate(
        plan(),
        **proposed(remaining_portfolio_risk="1.5", drawdown_factor=".5"),
        policy=settings(),
        explanation=proof,
    )
    assert result.reason == "SIZED"
    risk = Decimal(proof["unit_stop_risk"])
    assert Decimal(result.quantity) * risk <= Decimal("1.5")
    assert (Decimal(result.quantity) + Decimal(".001")) * risk > Decimal("1.5")
    for change in (
        {"remaining_portfolio_risk": "0"},
        {"balance": "0"},
        {"available_margin": "0"},
        {"drawdown_factor": "0"},
        {"analysis_factor": "0"},
        {"min_notional": "1000"},
    ):
        assert (
            size_candidate(plan(), **proposed(**change), policy=settings()).reason
            == "INSUFFICIENT_BUDGET"
        )


def test_price_buffer_works_in_both_directions_and_rejects_crossed_stops():
    long = {}
    short = {}
    size_candidate(
        plan(), **proposed(sizing_mark_price="102"), policy=settings(), explanation=long
    )
    size_candidate(
        plan("SHORT"),
        **proposed(sizing_mark_price="98"),
        policy=settings(),
        explanation=short,
    )
    assert Decimal(long["unit_stop_risk"]) >= Decimal("103.53") - 96 + Decimal(
        "103.53"
    ) * Decimal(".002")
    assert Decimal(short["unit_stop_risk"]) >= 104 - Decimal("96.53") + Decimal(
        "101.5"
    ) * Decimal(".002")
    assert (
        size_candidate(
            plan(), **proposed(sizing_mark_price="95"), policy=settings()
        ).reason
        == "PRICE_ENVELOPE_CROSSES_STOP"
    )
    assert (
        size_candidate(
            plan("SHORT"), **proposed(sizing_mark_price="105"), policy=settings()
        ).reason
        == "PRICE_ENVELOPE_CROSSES_STOP"
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"sizing_mark_price": None},
        {"price_drift_fraction": None},
        {"remaining_portfolio_risk": None},
        {"remaining_portfolio_risk": "-1"},
        {"sizing_mark_price": "0"},
        {"price_drift_fraction": ".050001"},
        {"price_drift_fraction": "NaN"},
    ],
)
def test_incomplete_budget_evidence_fails_closed(bad):
    with pytest.raises((ValueError, TypeError)):
        size_candidate(plan(), **proposed(**bad), policy=settings())


def test_default_sizing_is_unchanged_and_decimal_context_cannot_change_result():
    original = size_candidate(plan(), **sizing(), policy=settings())
    assert (
        size_candidate(plan(), **sizing(), policy=settings(), explanation={})
        == original
    )
    expected = size_candidate(
        plan(), **proposed(price="100.12345678"), policy=settings()
    )
    with localcontext() as ctx:
        ctx.prec = 6
        assert (
            size_candidate(plan(), **proposed(price="100.12345678"), policy=settings())
            == expected
        )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_single_risk_floor_is_exact_even_with_coarse_stop_tick(side):
    proof = {}
    configured = {**settings(), "sizing.max_allocation": "0.70"}
    candidate = plan(side, stop=".08")
    result = size_candidate(
        candidate, **proposed(price_tick=".7"), policy=configured, explanation=proof
    )
    assert "SINGLE_STOP_RISK" in proof["binding_quantity_limits"]
    unit = Decimal(proof["unit_stop_risk"])
    qty = Decimal(result.quantity)
    assert qty * unit <= 20 < (qty + Decimal(".001")) * unit
    assert Decimal(result.stop_price) % Decimal(".7") == 0


def test_pool_keeps_thirty_percent_even_at_buffered_fresh_price():
    proof = {}
    configured = {**settings(), "sizing.max_allocation": "1"}
    candidate = replace(plan(), score=100)
    result = size_candidate(
        candidate,
        **proposed(used_pool_margin="690", min_notional="1"),
        policy=configured,
        explanation=proof,
    )
    assert result.reason == "SIZED"
    margin = (
        Decimal(result.quantity)
        * Decimal(proof["price_high"])
        / candidate.leverage
        * Decimal(configured["capital.margin_buffer"])
    )
    assert 690 + margin <= 700
    assert "BUFFERED_POOL_MARGIN" in proof["binding_quantity_limits"]


def test_frozen_wusdt_price_drift_rejection_regression():
    """2026-10-12 04:16 UTC+8, public prices and anonymized capital inputs.

    This reproduces the historical denial and a *hypothetical* resized order;
    it is not a claim that this counterfactual order was submitted or profitable.
    """
    import json

    configured = {**settings(), "sizing.max_margin_fraction": ".1"}
    candidate = replace(plan(stop=".08"), score=88)
    args = sizing(
        price=".02288",
        balance="991.110944460000000000",
        available_margin="4991.11094446",
        used_pool_margin="0",
        atr_pct=".66496003996003996",
        price_tick=".00001",
        quantity_step=".1",
        min_quantity=".1",
        max_quantity="30000",
        min_notional="5",
        max_notional="500000",
    )
    before = size_candidate(candidate, **args, policy=configured)
    assert before.quantity == "10565.3"
    mark = Decimal(".0229476")
    ceiling = Decimal("19.8222188892")
    assert Decimal(before.quantity) * mark * Decimal(".082") > ceiling
    args.update(
        remaining_portfolio_risk="29.7333283338",
        sizing_mark_price=str(mark),
        price_drift_fraction=".015",
    )
    proof = {}
    after = size_candidate(candidate, **args, policy=configured, explanation=proof)
    assert after.quantity == "8662"
    assert after.stop_price == before.stop_price == "0.02105"
    assert Decimal(proof["budgeted_stop_risk"]) <= ceiling
    assert fixed_stop_risk(
        quantity=after.quantity,
        notional=Decimal(after.quantity) * mark,
        stop_price=after.stop_price,
        side="LONG",
        stop_fraction=".08",
        cost_fraction=".002",
    ) == Decimal("16.8345554224")
    replay_proof = {}
    assert (
        size_candidate(
            candidate,
            **json.loads(json.dumps(args)),
            policy=json.loads(json.dumps(configured)),
            explanation=replay_proof,
        )
        == after
    )
    assert replay_proof == proof


class Rows:
    def __init__(self, *, rows=(), candidate=None):
        self.rows, self.candidate = rows, candidate
        self.queries = []

    def execute(self, sql, args):
        self.queries.append((sql, args))
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.candidate


class Scope:
    key = "isolated-qa"


def test_original_reserved_risk_remains_held_and_missing_evidence_fails_closed():
    fake = Rows(rows=[("100", ".04", "7", "1", "96", "LONG", None, None)])
    result = portfolio_budget(
        fake, Scope(), settings=settings(), capital={"base": "1000", "factor": ".5"}
    )
    assert Decimal(result["held_stop_risk"]) == 7
    assert Decimal(result["remaining_stop_risk"]) == 8
    assert "LEFT JOIN" in fake.queries[0][0]
    fake.rows = [("100", None, None, "1", None, None, None, None)]
    with pytest.raises((ValueError, TypeError)):
        portfolio_budget(
            fake, Scope(), settings=settings(), capital={"base": "1000", "factor": "1"}
        )


def test_original_fixed_stop_larger_than_percentage_is_not_understated():
    fake = Rows(rows=[("102", ".04", None, "1", "96", "LONG", None, None)])
    result = portfolio_budget(
        fake, Scope(), settings=settings(), capital={"base": "1000", "factor": "1"}
    )
    assert Decimal(result["held_stop_risk"]) == Decimal("6.204")


def test_guard_rechecks_current_price_and_current_competing_reservations():
    from dataclasses import asdict

    from v2_core.account_risk import AccountScope

    scope = AccountScope("BINANCE", "qa-budget", "SANDBOX", "FUTURES")
    proof = {}
    candidate = plan()
    sized = size_candidate(
        candidate, **proposed(), policy=settings(), explanation=proof
    )
    fake = Rows(
        candidate=(
            asdict(candidate),
            None,
            "BTCUSDT",
            proof,
            asdict(sized),
            sized.quantity,
            None,
        )
    )
    allowed = guard_risk(
        fake,
        scope,
        episode="00000000-0000-0000-0000-000000000001",
        notional=Decimal(sized.notional),
        leverage=3,
        settings=settings(),
        capital={"base": "1000", "factor": "1"},
    )
    assert Decimal(allowed["proposed_stop_risk"]) <= 20
    # A sibling taking risk after advisory sizing remains visible to final admission.
    fake.rows = [("400", ".04", "20", "4", "96", "LONG", None, None)]
    with pytest.raises(ValueError, match="CAPITAL_PORTFOLIO_RISK_EXCEEDED"):
        guard_risk(
            fake,
            scope,
            episode="00000000-0000-0000-0000-000000000001",
            notional=Decimal(sized.notional),
            leverage=3,
            settings=settings(),
            capital={"base": "1000", "factor": "1"},
        )
    fake.rows = []
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        guard_risk(
            fake,
            scope,
            episode="00000000-0000-0000-0000-000000000001",
            notional=Decimal(sized.quantity) * 106,
            leverage=3,
            settings=settings(),
            capital={"base": "1000", "factor": "1"},
        )
