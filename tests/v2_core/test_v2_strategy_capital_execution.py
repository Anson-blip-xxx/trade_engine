"""Scoped, atomic new-plan capital admission on disposable PostgreSQL only.

No venue calls, worker activation, or claims of full strategy lifecycle coverage.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
import test_v2_strategy_deployment_execution as fixtures

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountRiskDenied, RiskReference
from v2_core.capital_model import portfolio_budget
from v2_core.ledger import Ledger
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.orders import Orders
from v2_core.runtime_policy import PolicyStore
from v2_core.strategy_plan_admission import StrategyPlanAdmission

database = database_fixture
SCOPE = fixtures.SCOPE


def configure(database, **changes):
    selected = fixtures.binding()
    fixtures.publish(database, selected)
    fixtures.risk(database)
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {
            "sizing.risk_fraction": ".03",
            "portfolio_risk.max_fraction": ".03",
            "sizing.max_margin_fraction": ".70",
            **changes,
        },
        expected_version=policy.read().version,
        reason="qa risk envelope",
    )
    # The production finite account cap is independently retained. Lift only
    # this disposable fixture's cap so portfolio-risk concurrency binds first.
    from v2_core.account_risk import AccountPolicy, AccountRisk

    AccountRisk(database).configure(
        SCOPE, AccountPolicy("USDT", "10000", 10, 0, "qa", 60000), expected_version=1
    )
    return selected


def prepare(database, selected, *, symbol="BTCUSDT", changes=None, quantity=None):
    _, plan, _, _ = fixtures.material(
        selected=selected,
        plan_changes={
            "symbol": symbol,
            "quantity": "4",
            "quantity_step": ".001",
            "stop_price": "95",
            "planned_loss": "20",
            **(changes or {}),
        },
    )
    result = StrategyPlanAdmission(database, fixtures.REGISTRY).admit(
        plan,
        selected,
        rationale="qa capital only",
    )
    episode = result["intent_id"]
    order, _ = Orders(database).prepare(episode, quantity=quantity)
    return episode, order, plan


def permit(database, order, symbol="BTCUSDT", price="100"):
    return Orders(database).transition(
        order,
        expected_version=1,
        status="SUBMITTING",
        evidence={"source": "qa"},
        risk_reference=RiskReference("SANDBOX", symbol, "qa", price, fixtures.now()),
    )


def budget(database):
    settings = PolicyStore(database, SCOPE).read().values
    with database() as conn:
        return portfolio_budget(
            conn, SCOPE, settings=settings, capital={"base": "1000", "factor": "1"}
        )


def test_concurrent_new_plans_reserve_full_risk_atomically(database):
    selected = configure(database)
    first = prepare(database, selected)
    second = prepare(database, selected, symbol="ETHUSDT")

    def claim(item):
        try:
            permit(database, item[1], item[2].symbol)
            return "ACCEPTED"
        except AccountRiskDenied as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, [first, second]))
    assert sorted(outcomes) == ["ACCEPTED", "CAPITAL_PORTFOLIO_RISK_EXCEEDED"]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status='SUBMITTING'"
        ).fetchone() == (1,)
    assert Decimal(budget(database)["held_stop_risk"]) == Decimal("24.808")


@pytest.mark.parametrize("field", ["capital.enabled", "portfolio_risk.enabled"])
def test_disabled_hard_risk_never_downgrades_to_finite_legacy_limits(database, field):
    changes = {field: False}
    if field == "capital.enabled":
        changes["portfolio_risk.enabled"] = False
    selected = configure(database, **changes)
    _, order, _ = prepare(database, selected)
    with pytest.raises(
        AccountRiskDenied, match="STRATEGY_CAPITAL_RISK_POLICY_REQUIRED"
    ):
        permit(database, order)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (order,)
        ).fetchone() == ("PREPARED",)


def test_partial_requested_open_is_not_a_new_plan_capital_bypass(database):
    selected = configure(database)
    _, order, _ = prepare(database, selected, quantity="2")
    with pytest.raises(
        AccountRiskDenied, match="STRATEGY_CAPITAL_SINGLE_OPEN_REQUIRED"
    ):
        permit(database, order)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


def test_partial_fill_retains_full_risk_but_does_not_poison_other_symbols(database):
    selected = configure(database)
    episode, order, _ = prepare(database, selected)
    permit(database, order)
    before = budget(database)
    Ledger(database).record_fill(
        order_id=order,
        exchange_fill_id="qa-partial",
        quantity="2",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=fixtures.now(),
        evidence={"source": "qa"},
    )
    Orders(database).transition(
        order, expected_version=2, status="ACKNOWLEDGED", evidence={"source": "qa"}
    )
    Orders(database).transition(
        order,
        expected_version=3,
        status="CANCELLED",
        evidence={"source": "qa", "fills_complete": True},
    )
    assert budget(database) == before
    # Completing an opening allocation is not yet supported by the new adapter.
    retry, _ = Orders(database).prepare(
        episode,
        request_key="remaining",
        quantity="2",
        evidence={"reason": "qa attempt to expand partial allocation"},
    )
    with pytest.raises(
        AccountRiskDenied, match="STRATEGY_CAPITAL_SINGLE_OPEN_REQUIRED"
    ):
        permit(database, retry)
    # A natural partial fill does not become an unbounded poison-pill error.
    _, other, _ = prepare(
        database,
        selected,
        symbol="ETHUSDT",
        changes={"quantity": ".1", "planned_loss": ".5"},
    )
    permit(database, other, "ETHUSDT")
    assert Decimal(budget(database)["held_stop_risk"]) == Decimal("25.4282")


def test_short_fixed_stop_risk_and_original_floor_survive_unknown(database):
    selected = configure(database)
    _, order, _ = prepare(
        database, selected, changes={"side": "SELL", "stop_price": "105"}
    )
    permit(database, order, price="99")
    assert Decimal(budget(database)["held_stop_risk"]) == Decimal("24.792")
    Orders(database).transition(
        order, expected_version=2, status="UNKNOWN", evidence={}
    )
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"sizing.cost_buffer_fraction": "0"},
        expected_version=policy.read().version,
        reason="qa lower cost",
    )
    assert Decimal(budget(database)["held_stop_risk"]) == Decimal("24.792")


def test_exact_derived_notional_with_more_than_18_places_is_accepted(database):
    selected = configure(database)
    _, order, _ = prepare(
        database,
        selected,
        changes={
            "quantity": ".075",
            "entry_price": ".0000000000000003",
            "stop_price": ".0000000000000002",
            "planned_loss": ".00000000000000001",
        },
    )
    with localcontext() as context:
        context.prec = 6
        permit(database, order, price=".0000000000000003")
    with database() as conn:
        reservation = conn.execute(
            "SELECT notional::text,reference FROM v2_risk_reservations"
        ).fetchone()
    assert Decimal(reservation[0]) == Decimal(".000000000000000023")
    assert Decimal(reservation[1]["capital"]["proposed_stop_risk"]) >= Decimal(
        ".00000000000000001"
    )


def test_stale_snapshot_denial_rolls_back_without_new_order_permission(database):
    selected = configure(database)
    tick = fixtures.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "1000",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=tick,
        deadline=tick,
        observation_id=str(uuid4()),
    )
    _, order, _ = prepare(database, selected)
    with pytest.raises(AccountRiskDenied, match="CAPITAL_SNAPSHOT_STALE"):
        permit(database, order)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


def test_existing_reservation_cannot_expand_after_cost_policy_increase(database):
    from v2_core.account_risk import reserve_open

    selected = configure(database)
    _, order, _ = prepare(database, selected)
    permit(database, order)
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"sizing.cost_buffer_fraction": ".01"},
        expected_version=policy.read().version,
        reason="qa cost increase",
    )
    with (
        database() as conn,
        pytest.raises(
            AccountRiskDenied, match="STRATEGY_CAPITAL_RESERVATION_EXPANSION_REQUIRED"
        ),
    ):
        reserve_open(
            conn,
            order,
            reference=RiskReference("SANDBOX", "BTCUSDT", "qa", "100", fixtures.now()),
            required=True,
        )
    with database() as conn:
        original = conn.execute(
            "SELECT reference->'capital'->>'proposed_stop_risk' FROM v2_risk_reservations"
        ).fetchone()[0]
    assert Decimal(original) == Decimal("24.808")
