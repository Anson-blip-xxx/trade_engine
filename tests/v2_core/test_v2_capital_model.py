from decimal import Decimal

import pytest
from test_v2_directional import inputs, sizing
from test_v2_runtime_policy import SCOPE
from test_v2_runtime_policy import database as database_fixture

from v2_core.capital_model import capital_base, health, rehearsal_profile
from v2_core.directional import evaluate_market, size_candidate
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.runtime_policy import PolicyStore, resolve

database = database_fixture


def account(wallet="10000", unrealized="0", used="0"):
    equity = Decimal(wallet) + Decimal(unrealized)
    return {
        "verified_net_pnl": str(Decimal(wallet) - Decimal(10000)),
        "totalWalletBalance": wallet,
        "totalMarginBalance": str(equity),
        "totalInitialMargin": used,
        "availableBalance": str(equity - Decimal(used)),
    }


@pytest.mark.parametrize(
    "wallet,unrealized,expected",
    [
        ("10000", "0", "1000"),
        ("10100", "0", "1050"),
        ("9900", "0", "900"),
        ("10000", "100", "1000"),
        ("10000", "-100", "900"),
        ("8800", "0", "0"),
    ],
)
def test_initial_capital_and_asymmetric_compounding(wallet, unrealized, expected):
    settings = resolve(rehearsal_profile("10000"))
    assert capital_base(account(wallet, unrealized), settings) == Decimal(expected)


@pytest.mark.parametrize(
    "wallet,factor", [("10000", "1"), ("9970", "0.5"), ("9940", "0.25"), ("9900", "0")]
)
def test_drawdown_changes_risk_without_timed_martingale(wallet, factor):
    settings = resolve(rehearsal_profile("10000"))
    assert health(account(wallet), settings)["factor"] == factor


def test_high_water_survives_reconstruction(database):
    store = PolicyStore(database, SCOPE)
    store.patch(rehearsal_profile("10000"), expected_version=0, reason="QA rehearsal")
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account("10000"),
        started=100,
        deadline=200,
        observation_id="profit",
    )
    from v2_core.capital_model import current_health

    result = current_health(database, SCOPE, account("9970"), store.read().values)
    assert Decimal(result["peak"]) == 1000
    assert Decimal(result["base"]) == 970
    assert result["factor"] == "0.5"


def test_profit_high_water_changes_drawdown():
    settings = resolve(rehearsal_profile("10000"))
    peak = health(account("10400"), settings)
    now = health(account("10200"), settings, peak)
    assert Decimal(now["peak"]) == 1200
    assert now["factor"] == "0.25"


def test_unverified_deposit_is_not_reinvested(database):
    from v2_core.capital_model import current_health

    result = current_health(
        database, SCOPE, account("20000"), resolve(rehearsal_profile("10000"))
    )
    assert Decimal(result["base"]) == 1000


def test_capital_halt_is_visible_to_independent_health_monitor(database):
    from services.v2_trading_health import TradingHealth

    PolicyStore(database, SCOPE).patch(
        rehearsal_profile("10000"), expected_version=0, reason="QA halt"
    )
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account("9900"),
        started=100,
        deadline=200,
        observation_id="halt",
    )
    diagnostic = TradingHealth(
        database, account_id=SCOPE.account_id, clock_ms=lambda: 150
    )
    assert diagnostic() == frozenset({"CAPITAL_DRAWDOWN_HALT"})


def test_sizing_includes_costs_and_margin_cap():
    settings = resolve(rehearsal_profile("10000"))
    plan = evaluate_market(**inputs(), policy=settings)
    sized = size_candidate(plan, **sizing(), policy=settings)
    assert sized.reason == "SIZED"
    assert plan.margin_mode == "ISOLATED" and plan.leverage == 2
    notional = Decimal(sized.notional)
    assert notional / plan.leverage <= 50
    assert notional * (Decimal(plan.stop_fraction) + Decimal(".002")) <= 5
    shrunk = size_candidate(
        plan, **sizing(balance="940", drawdown_factor="0.25"), policy=settings
    )
    assert Decimal(shrunk.notional) < notional / 2


def test_high_volatility_does_not_increase_leverage():
    settings = resolve(rehearsal_profile("10000"))
    data = inputs()
    data["market"]["1h"]["atr_pct"] = "4"
    data["event"]["strength"] = "95"
    assert evaluate_market(**data, policy=settings).leverage == 1


def test_risk_guard_serializes_portfolio_ceiling(database, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor

    import test_v2_account_risk as helpers

    from v2_core.evidence import DecisionEvidence

    original = helpers.evidence()
    snapshot = json.loads(original.snapshot_json)
    snapshot["features"] = {
        "evaluation": {
            "market_plan": {
                "leverage": 2,
                "stop_fraction": "0.048",
                "margin_mode": "ISOLATED",
            }
        }
    }
    proof = DecisionEvidence(
        original.strategy_version, original.config_json, json.dumps(snapshot)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: proof)
    PolicyStore(database, SCOPE).patch(
        rehearsal_profile("10000"), expected_version=0, reason="QA capital"
    )
    risk = helpers.configured(database, max_notional=None, max_positions=None)
    now = helpers.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account(),
        started=now,
        deadline=now + 60000,
        observation_id="concurrent-risk",
    )
    orders = [
        helpers.prepare(database, symbol=f"ASSET{i}USDT", quantity="1")[2]
        for i in range(9)
    ]
    from psycopg.errors import UniqueViolation

    with pytest.raises(UniqueViolation, match="v2_one_active_episode"):
        helpers.prepare(database, symbol="ASSET0USDT", quantity="1")
    with ThreadPoolExecutor(max_workers=9) as pool:
        results = list(pool.map(helpers.runner(database).dispatch, orders))
    assert results.count("ACKNOWLEDGED") == 6
    assert results.count("DENIED") == 3
    assert risk.usage(SCOPE)["positions"] == 6
