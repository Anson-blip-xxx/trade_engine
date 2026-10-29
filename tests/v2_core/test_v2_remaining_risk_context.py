"""Isolated PostgreSQL account evidence and serialized final admission."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_directional_account_context import SCOPE, build, signal
from test_v2_intent_admission import database as database_fixture
from test_v2_remaining_risk_sizing import plan, proposed, settings

from v2_core.capital_model import portfolio_budget, rehearsal_profile
from v2_core.directional import size_candidate
from v2_core.entry_controls import configure
from v2_core.runtime_policy import PolicyStore

database = database_fixture


def enabled(database):
    provider, request, public, clock, _ = build(database)
    request.account["totalMarginBalance"] = "1000"
    # Capital uses min(wallet, equity, available + used); the legacy fixture's
    # available=700 plus used=100 would correctly mean an 800 capital base and
    # trigger the existing drawdown halt, unrelated to residual sizing.
    request.account["availableBalance"] = "900"
    PolicyStore(database, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA residual budget"
    )
    controls = configure(
        database,
        SCOPE,
        {"sizing.remaining_budget_enabled": True},
        expected_version=0,
        reason="QA residual risk sizing",
    )

    def with_mark(path, args):
        response = public(path, args)
        if path == "/fapi/v1/premiumIndex":
            response["markPrice"] = "101.50"
        return response

    with_mark.environment = "SANDBOX"
    provider.public = with_mark
    return provider, request, public, clock, controls


def test_fresh_mark_and_original_portfolio_risk_are_frozen_with_control_version(
    database,
):
    provider, _, _, _, controls = enabled(database)
    result = provider(signal())
    assert Decimal(result["sizing"]["remaining_portfolio_risk"]) == 30
    assert Decimal(result["sizing"]["sizing_mark_price"]) == Decimal("101.5")
    assert (
        result["sizing"]["price_drift_fraction"] == ".015"
        or result["sizing"]["price_drift_fraction"] == "0.015"
    )
    assert result["evidence"]["entry_controls"] == controls.snapshot()
    with database() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (result["evidence"]["state_id"],),
        ).fetchone()[0]
    assert payload["entry_controls"] == controls.snapshot()
    assert (
        payload["sizing_budget_inputs"]["sizing_mark_price"]
        == result["sizing"]["sizing_mark_price"]
    )


def test_missing_mark_fails_closed_only_when_feature_is_enabled(database):
    provider, _, public, _, _ = enabled(database)
    provider.public = public  # Legacy fixture has funding but no mark.
    with pytest.raises(ValueError, match="SIZING_MARK_PRICE_MISSING"):
        provider(signal())


def test_usdc_cannot_spend_usdt_capital_budget(database):
    provider, request, _, _, _ = enabled(database)
    with pytest.raises(ValueError, match="SIZING_BUDGET_REQUIRES_USDT_CAPITAL_GUARDS"):
        provider({**signal(), "symbol": "BTCUSDC"})
    assert request.calls == []


def test_zero_available_margin_is_frozen_not_treated_as_missing_data(database):
    provider, request, _, _, _ = enabled(database)
    request.account["availableBalance"] = "0"
    result = provider(signal())
    assert result["sizing"]["available_margin"] == "0"


def test_concurrent_admission_retains_buffered_original_risk(database, monkeypatch):
    import test_v2_account_risk as helpers
    from test_v2_capital_model import account

    from v2_core.evidence import DecisionEvidence
    from v2_core.managed_portfolio import publish_capital_snapshot

    # Independent callers sized against the same unoccupied portfolio. They
    # must still serialize/re-read HELD risk in the existing reservation lock.
    changed = {
        **rehearsal_profile("10000"),
        **{
            key: value
            for key, value in settings().items()
            if key
            in {
                "sizing.risk_fraction",
                "sizing.max_margin_fraction",
                "sizing.pool_fraction",
                "sizing.cost_buffer_fraction",
            }
        },
    }
    profile = PolicyStore(database, SCOPE).patch(
        changed, expected_version=0, reason="QA atomic residuals"
    )
    explanation = {}
    candidate = plan()
    sized = size_candidate(
        candidate, **proposed(), policy=profile.values, explanation=explanation
    )
    original = helpers.evidence()
    snapshot = json.loads(original.snapshot_json)
    snapshot["features"] = {
        "evaluation": {
            "market_plan": asdict(candidate),
            "sizing": asdict(sized),
            "sizing_budget": explanation,
        }
    }
    proof = DecisionEvidence(
        original.strategy_version, original.config_json, json.dumps(snapshot)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: proof)
    risk = helpers.configured(database, max_notional=None, max_positions=None)
    now = helpers.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account(),
        started=now,
        deadline=now + 60000,
        observation_id="qa-residual-concurrency",
    )
    orders = [
        helpers.prepare(database, symbol=f"ASSET{i}USDT", quantity=sized.quantity)[2]
        for i in range(6)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(helpers.runner(database).dispatch, orders))
    assert results.count("ACKNOWLEDGED") == 2
    assert results.count("DENIED") == 4
    assert risk.usage(SCOPE)["positions"] == 2
    with database() as conn:
        remaining = portfolio_budget(
            conn,
            SCOPE,
            settings=profile.values,
            capital={"base": "1000", "factor": "1"},
        )
    assert Decimal(remaining["held_stop_risk"]) == 2 * Decimal(
        explanation["budgeted_stop_risk"]
    )
    assert Decimal(remaining["held_stop_risk"]) <= 30
