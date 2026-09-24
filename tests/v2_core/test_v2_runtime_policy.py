import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_intent_admission import database as database_fixture

from v2_core.account_risk import AccountScope
from v2_core.managed_portfolio import publish_capital_snapshot, reserve_capital
from v2_core.runtime_policy import PolicyStore, resolve

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": 1},
        {"capital.enabled": 1},
        {"entry.min_score": True},
        {"capital.pool_fraction": "NaN"},
        {"capital.pool_fraction": "Infinity"},
        {"capital.pool_fraction": "0"},
        {"capital.pool_fraction": 0.7},
        {"capital.pool_fraction": "bad"},
        {"sizing.min_allocation": "0.9"},
        {"leverage.low_score": 99},
        {"capital.enabled": True},
    ],
)
def test_invalid_policy_fails_closed(changes):
    with pytest.raises(ValueError):
        resolve(changes)


def test_policy_cas_scope_and_snapshot(database):
    store = PolicyStore(database, SCOPE)
    initial = store.read()
    assert initial.version == 0
    changed = store.patch({"entry.min_score": 40}, expected_version=0, reason="QA")
    assert changed.version == 1
    assert initial.values["entry.min_score"] == 30
    assert changed.values["entry.min_score"] == 40
    assert changed.digest != initial.digest
    assert PolicyStore(database, replace(SCOPE, account_id="other")).read().version == 0
    with pytest.raises(ValueError, match="POLICY_VERSION_CONFLICT"):
        store.patch({"entry.min_score": 20}, expected_version=0, reason="stale")
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        store.patch({}, expected_version=1, reason="")
    with pytest.raises(ValueError, match="TESTNET_CAPITAL_POLICY_ONLY"):
        PolicyStore(database, replace(SCOPE, environment="LIVE")).patch(
            {"capital.enabled": True, "sizing.pool_fraction": "0.70"},
            expected_version=0,
            reason="QA",
        )


def capital(database, *, used="100", available="900"):
    PolicyStore(database, SCOPE).patch(
        {"capital.enabled": True, "sizing.pool_fraction": "0.70"},
        expected_version=0,
        reason="QA capital",
    )
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": used,
            "availableBalance": available,
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=100,
        deadline=200,
        observation_id="qa-capital",
    )


def reserve(database, notional, *, now=150, leverage=2):
    with database() as conn:
        return reserve_capital(
            conn,
            SCOPE,
            episode="00000000-0000-0000-0000-000000000001",
            notional=Decimal(notional),
            leverage=leverage,
            now=now,
        )


def test_capital_includes_venue_margin_and_buffer(database):
    capital(database, used="150", available="850")
    proof = reserve(database, "1000")
    assert Decimal(proof["limit"]) == 700
    assert Decimal(proof["reserved_margin"]) == 550
    with pytest.raises(ValueError, match="ACCOUNT_MARGIN_BUDGET_EXCEEDED"):
        reserve(database, "1000.000000000000000001")


@pytest.mark.parametrize("now", [99, 200, 201])
def test_stale_capital_is_not_admissible(database, now):
    capital(database)
    with pytest.raises(ValueError, match="CAPITAL_SNAPSHOT_STALE"):
        reserve(database, "10", now=now)


def test_reduced_available_capital_shrinks_budget(database):
    capital(database, used="100", available="100")
    proof = reserve(database, "50")
    assert Decimal(proof["capital_base"]) == 200
    assert Decimal(proof["limit"]) == 140
    with pytest.raises(ValueError, match="ACCOUNT_MARGIN_BUDGET_EXCEEDED"):
        reserve(database, "100")


@pytest.mark.parametrize("leverage", [None, True, 0, 6, "2"])
def test_unverified_leverage_blocks_budget(database, leverage):
    capital(database)
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_UNVERIFIED"):
        reserve(database, "10", leverage=leverage)


@pytest.mark.parametrize("quantity, accepted", [("10", 1), ("1", 6)])
def test_concurrent_actual_reservations_cannot_oversubscribe(
    database, monkeypatch, quantity, accepted
):
    import test_v2_account_risk as helpers

    from v2_core.evidence import DecisionEvidence

    original = helpers.evidence()
    snapshot = json.loads(original.snapshot_json)
    snapshot["features"] = {"evaluation": {"market_plan": {"leverage": 2}}}
    proof = DecisionEvidence(
        original.strategy_version, original.config_json, json.dumps(snapshot)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: proof)
    PolicyStore(database, SCOPE).patch(
        {"capital.enabled": True, "sizing.pool_fraction": "0.70"},
        expected_version=0,
        reason="QA concurrent capital",
    )
    risk = helpers.configured(database, max_notional=None, max_positions=None)
    now = helpers.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "1000",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=now,
        deadline=now + 60000,
        observation_id="concurrent-capital",
    )
    orders = [
        helpers.prepare(database, symbol=f"ASSET{i}USDT", quantity=quantity)[2]
        for i in range(6)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(helpers.runner(database).dispatch, orders))
    assert results.count("ACKNOWLEDGED") == accepted
    assert results.count("DENIED") == 6 - accepted
    assert risk.usage(SCOPE)["positions"] == accepted
