import json
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_v2_capital_model import account
from test_v2_runtime_policy import SCOPE, database

from v2_core.capital_model import current_health, health, rehearsal_profile
from v2_core.drawdown_observation import configure, control, sizing_factor
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.runtime_policy import PolicyStore, resolve
from v2_core.state import BusinessState, StateKey

__all__ = ["database"]


def policy():
    return resolve({**rehearsal_profile("10000"), "recovery.enabled": True})


@pytest.mark.parametrize("mode", ["HALTED", "PAUSED", "PROBE", "ACTIVE"])
def test_observation_preserves_losses_peak_and_recovery_without_scaling(mode):
    settings = policy()
    stopped = health(account("9900"), settings, now_ms=1)
    stopped["recovery"].update(mode=mode, attempts=2)
    off = {**settings, "capital.drawdown_guard_enabled": False}
    seen = health(account("9800"), off, stopped, environment="SANDBOX", now_ms=99999999)
    assert seen["factor"] == "1" and seen["recovery"]["mode"] == "OBSERVE_ONLY"
    assert Decimal(seen["base"]) == 800 and Decimal(seen["drawdown"]) == Decimal(".2")
    assert seen["peak"] == stopped["peak"] and seen["anchor"] == stopped["anchor"]
    assert seen["suspended_recovery"] == stopped["recovery"]
    twice = health(account("9750"), off, seen, environment="SANDBOX")
    assert twice["suspended_recovery"] == stopped["recovery"]
    restored = health(account("9700"), settings, twice, environment="SANDBOX")
    assert restored["factor"] == "0" and restored["recovery"]["mode"] == "HALTED"


@pytest.mark.parametrize("environment", [None, "LIVE", "sandbox"])
def test_observation_never_applies_outside_explicit_testnet(environment):
    settings = {**policy(), "capital.drawdown_guard_enabled": False}
    with pytest.raises(ValueError, match="TESTNET_ONLY"):
        health(account(), settings, environment=environment)
    with pytest.raises(ValueError, match="TESTNET_ONLY"):
        sizing_factor("0", {}, settings, environment)


def test_legacy_factor_is_observed_but_not_applied_and_zero_funds_stay_zero():
    off = {**policy(), "capital.drawdown_guard_enabled": False}
    assert sizing_factor("0", {"factor": "1"}, off, "SANDBOX") == 1
    assert sizing_factor("0", {"factor": "1"}, policy(), "SANDBOX") == 0
    assert health(account("8000"), off, environment="SANDBOX")["base"] == "0E-18"


def test_audited_scoped_control_does_not_change_policy_or_other_accounts(database):
    store = PolicyStore(database, SCOPE)
    settings = store.patch(
        policy(), expected_version=0, reason="QA capital model"
    ).values
    before = store.read()
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account("9900"),
        started=100,
        deadline=200,
        observation_id="before",
    )
    previous = current_health(database, SCOPE, account("9900"), settings)
    assert previous["factor"] == "0"
    assert (
        configure(
            database,
            SCOPE,
            enabled=False,
            expected_version=0,
            reason="QA sample observation",
        )
        == 1
    )
    result = current_health(database, SCOPE, account("9850"), settings)
    assert result["factor"] == "1" and result["drawdown_control_version"] == 1
    assert result["anchor"] == previous["anchor"] and result["peak"] == previous["peak"]
    assert control(database, replace(SCOPE, account_id="other")) == (True, 0)
    assert control(database, replace(SCOPE, environment="LIVE")) == (True, 0)
    assert store.read() == before
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        configure(
            database,
            SCOPE,
            enabled=True,
            expected_version=0,
            reason="QA stale configuration",
        )
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account("9850"),
        started=300,
        deadline=400,
        observation_id="off",
    )
    configure(
        database,
        SCOPE,
        enabled=True,
        expected_version=1,
        reason="QA restore protections",
    )
    assert current_health(database, SCOPE, account("9850"), settings)["factor"] == "0"
    key = StateKey(
        **asdict(SCOPE), namespace="capital-drawdown-control-v1", key="active"
    )
    with database() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM v2_state_history WHERE state_id=%s",
                (key.identity,),
            ).fetchone()[0]
            == 2
        )
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_configuration_requires_capital_guards_and_denies_live(database):
    with pytest.raises(ValueError, match="REQUIRES_CAPITAL_GUARDS"):
        configure(
            database,
            SCOPE,
            enabled=False,
            expected_version=0,
            reason="QA unsupported profile",
        )
    with pytest.raises(ValueError, match="TESTNET_ONLY"):
        configure(
            database,
            replace(SCOPE, environment="LIVE"),
            enabled=False,
            expected_version=0,
            reason="QA live refusal",
        )


def test_legacy_drawdown_is_still_persisted_but_not_used_in_account_context(database):
    from test_v2_directional_account_context import build, signal
    from test_v2_directional_replay import SCOPE as context_scope

    provider, request, _, _, _ = build(database)
    PolicyStore(database, context_scope).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA baseline"
    )
    configure(
        database,
        context_scope,
        enabled=False,
        expected_version=0,
        reason="QA collect samples",
    )
    request.account.update(
        totalWalletBalance="700", totalMarginBalance="700", availableBalance="600"
    )
    result = provider(signal())
    assert result["sizing"]["drawdown_factor"] == "1"
    key = StateKey(
        **asdict(context_scope), namespace="capital-snapshot-v1", key="latest"
    )
    saved = json.loads(BusinessState(database).read(key).payload_json)
    assert saved["capital_health"]["recovery"]["mode"] == "OBSERVE_ONLY"
    assert result["evidence"]["drawdown_version"] >= 1
