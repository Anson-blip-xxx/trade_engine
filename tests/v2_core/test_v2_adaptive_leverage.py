from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_directional import inputs, sizing
from test_v2_intent_admission import database as database_fixture

from v2_core.adaptive_leverage import choose, safe_margin, venue_facts
from v2_core.capital_model import rehearsal_profile
from v2_core.directional import evaluate_market, size_candidate
from v2_core.runtime_policy import resolve

database = database_fixture


def test_daemon_permission_weights_cover_every_transport_read():
    from services.v2_testnet_daemon_entry import PrivateRatePermit
    from v2_core.transport import BinanceSignedTransport

    assert set(PrivateRatePermit._READS) == BinanceSignedTransport._READS
    calls = []

    class Budget:
        def permit(self, weight):
            calls.append(weight)
            return True

    permit = PrivateRatePermit(Budget(), entries=False, protection=False, exits=False)
    assert permit("GET", "/fapi/v1/leverageBracket")
    assert calls == [1]
    assert not permit("POST", "/fapi/v1/leverage")


def policy():
    return resolve(
        {
            **rehearsal_profile("10000"),
            "leverage.adaptive_enabled": True,
            "leverage.boost_enabled": True,
            "leverage.medium": 3,
        }
    )


def facts():
    return venue_facts(
        [
            {
                "symbol": "BTCUSDT",
                "brackets": [
                    {
                        "notionalFloor": 0,
                        "notionalCap": 100000,
                        "initialLeverage": 20,
                        "maintMarginRatio": "0.005",
                    }
                ],
            }
        ],
        {
            "symbol": "BTCUSDT",
            "bidPrice": "100",
            "askPrice": "100.01",
            "bidQty": "1000",
            "askQty": "1000",
            "time": 1000,
        },
        "BTCUSDT",
        started=900,
        finished=1000,
        max_age_ms=1000,
    )


def scenario():
    data = inputs(kind="PULSE_UP")
    data["event"]["strength"] = "95"
    data["market"]["15m"]["taker_buy_ratio"] = "0.6"
    plan = evaluate_market(**data, policy=policy())
    snapshot = {
        "market": data["market"],
        "price": "100",
        "funding_rate": "0",
        "sizing": {"drawdown_factor": "1"},
        "leverage_venue": facts(),
    }
    return plan, snapshot


def test_tv_boost_requires_every_factor_and_does_not_increase_risk():
    plan, snapshot = scenario()
    lev, proof = choose(
        plan,
        snapshot,
        source="tv_bridge",
        age_ms=0,
        settings=policy(),
        notional=Decimal(120),
    )
    assert lev == 8 and all(proof["checks"].values())
    assert (
        choose(
            plan,
            snapshot,
            source="s3",
            age_ms=0,
            settings=policy(),
            notional=Decimal(120),
        )[0]
        == 5
    )
    for leverage in (2, 3, 5, 8):
        result = size_candidate(
            replace(plan, leverage=leverage), **sizing(), policy=policy()
        )
        assert Decimal(result.notional) * Decimal(".042") <= 5


@pytest.mark.parametrize(
    "field,value", [("funding_rate", ".005"), ("leverage_venue", None)]
)
def test_missing_or_adverse_facts_never_boost(field, value):
    plan, snapshot = scenario()
    snapshot[field] = value
    assert (
        choose(
            plan,
            snapshot,
            source="tv_bridge",
            age_ms=0,
            settings=policy(),
            notional=Decimal(120),
        )[0]
        != 8
    )


def test_wide_stop_and_maintenance_fail_high_leverage_without_tightening_stop():
    plan, snapshot = scenario()
    plan = replace(plan, stop_fraction="0.10")
    assert (
        choose(
            plan,
            snapshot,
            source="tv_bridge",
            age_ms=0,
            settings=policy(),
            notional=Decimal(120),
        )[0]
        <= 5
    )
    assert not safe_margin(8, Decimal(".10"), Decimal(120), facts(), policy())
    assert not safe_margin(8, Decimal(".04"), Decimal(100000), facts(), policy())


def test_risk_reduction_caps_leverage_and_stale_signal_cannot_boost():
    plan, snapshot = scenario()
    assert (
        choose(
            plan,
            snapshot,
            source="tv_bridge",
            age_ms=30001,
            settings=policy(),
            notional=Decimal(120),
        )[0]
        == 3
    )
    snapshot["sizing"]["drawdown_factor"] = ".2"
    assert (
        choose(
            plan,
            snapshot,
            source="tv_bridge",
            age_ms=0,
            settings=policy(),
            notional=Decimal(120),
        )[0]
        == 2
    )


def test_venue_missing_timestamp_is_not_a_fresh_book():
    with pytest.raises(ValueError, match="STALE"):
        venue_facts(
            [{"symbol": "BTCUSDT"}],
            {"symbol": "BTCUSDT"},
            "BTCUSDT",
            started=0,
            finished=10,
            max_age_ms=10,
        )


def test_replay_uses_tv_context_and_records_selected_leverage(database):
    import json

    from test_v2_directional_replay import scenario as replay_scenario

    from services.v2_directional_replay import replay_decision

    worker, _runtime, signal_id, context = replay_scenario(database)
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    _plan, market = scenario()
    signal["signal"] = "PULSE_UP"
    signal["features"].update(type="PULSE_UP", strength=95)
    context["directional"].update(market=market["market"], leverage_venue=facts())
    config = {
        **json.loads(worker.config_json),
        "source": "tv_bridge",
        "policy": policy(),
    }
    result = replay_decision(signal, context, config)
    evidence = json.loads(result.features_json)
    assert result.rationale == "DIRECTIONAL_REPLAY_ONLY"
    assert evidence["market_plan"]["leverage"] == 8
    assert evidence["leverage_decision"]["selected"] == 8
    assert evidence["leverage_decision"]["checks"]["tv_source"] is True


def test_boost_cannot_bypass_disabled_portfolio_guard():
    from v2_core.capital_model import guard_risk

    with pytest.raises(ValueError, match="SAFETY_UNVERIFIED"):
        guard_risk(
            None,
            None,
            episode=None,
            notional=Decimal(100),
            leverage=8,
            settings=resolve(),
            capital={},
        )


@pytest.mark.parametrize(
    "fault", [None, "missing_check", "wrong_symbol", "disabled", "downgraded"]
)
def test_final_capital_guard_requires_complete_boost_evidence(
    database, monkeypatch, fault
):
    import json
    from dataclasses import asdict

    import test_v2_account_risk as helpers
    from test_v2_runtime_policy import SCOPE

    from v2_core.capital_model import guard_risk
    from v2_core.evidence import DecisionEvidence

    plan, snapshot = scenario()
    _, proof = choose(
        plan,
        snapshot,
        source="tv_bridge",
        age_ms=0,
        settings=policy(),
        notional=Decimal(120),
    )
    selected_policy = policy()
    if fault == "missing_check":
        del proof["checks"]["tv_source"]
    elif fault == "wrong_symbol":
        proof["venue"]["symbol"] = "ETHUSDT"
    elif fault == "disabled":
        selected_policy["leverage.boost_enabled"] = False
    elif fault == "downgraded":
        selected_policy["leverage.boost"] = 5
    original = helpers.evidence()
    saved = json.loads(original.snapshot_json)
    saved["features"] = {
        "evaluation": {
            "market_plan": asdict(replace(plan, leverage=8)),
            "leverage_decision": proof,
        }
    }
    evidence = DecisionEvidence(
        original.strategy_version, original.config_json, json.dumps(saved)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: evidence)
    intent, _, _, _ = helpers.prepare(database, quantity="1")

    def check():
        with database() as conn:
            return guard_risk(
                conn,
                SCOPE,
                episode=intent.intent_id,
                notional=Decimal(100),
                leverage=8,
                settings=selected_policy,
                capital={"base": "1000", "factor": "1"},
            )

    if fault:
        with pytest.raises(ValueError, match="SAFETY_UNVERIFIED"):
            check()
    else:
        assert Decimal(check()["proposed_stop_risk"]) == Decimal("4.2")
