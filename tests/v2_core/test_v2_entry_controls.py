from dataclasses import replace

import pytest
from test_v2_runtime_policy import SCOPE, database

from v2_core.capital_model import rehearsal_profile
from v2_core.entry_controls import configure, read, resolve
from v2_core.runtime_policy import PolicyStore

__all__ = ["database"]


@pytest.mark.parametrize(
    "changes",
    [
        {"capital.pool_fraction": "1"},
        {"sizing.remaining_budget_enabled": 1},
        {"sizing.price_drift_fraction": "NaN"},
        {"sizing.price_drift_fraction": "-0.1"},
        {"sizing.price_drift_fraction": 0.01},
        {"sizing.price_drift_fraction": "0.051"},
        {"history.mode": "ENFORCE"},
        {"history.min_samples": 1},
        {"history.min_samples": 100, "history.max_samples": 50},
        {"history.soft_factor": "1.1"},
        {"reentry.enforce_reasons": ["STRUCTURE_NOT_REARMED"]},
        {"reentry.enforce_sides": ["BUY", "BUY"]},
        {"reentry.policy": {}},
    ],
)
def test_bad_control_fails_closed(changes):
    with pytest.raises(ValueError, match="ENTRY_CONTROLS"):
        resolve(changes)


def test_default_controls_preserve_behavior():
    values = resolve({})
    assert values["sizing.remaining_budget_enabled"] is False
    assert values["history.mode"] == values["reentry.mode"] == "OFF"


def test_control_audit_scope_cas_and_shared_policy_compatibility(database):
    policy = PolicyStore(database, SCOPE)
    policy.patch(rehearsal_profile("1000"), expected_version=0, reason="QA baseline")
    before = policy.read()
    first = configure(
        database,
        SCOPE,
        {
            "sizing.remaining_budget_enabled": True,
            "history.mode": "SHADOW",
            "reentry.mode": "SHADOW",
        },
        expected_version=0,
        reason="QA isolated rollout",
    )
    assert first.version == 1
    assert policy.read() == before
    assert read(database, replace(SCOPE, account_id="another-account")).version == 0
    assert read(database, replace(SCOPE, environment="LIVE")).version == 0
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        configure(database, SCOPE, {}, expected_version=0, reason="QA stale writer")
    second = configure(
        database,
        SCOPE,
        {"sizing.remaining_budget_enabled": False},
        expected_version=1,
        reason="QA scoped rollback",
    )
    assert second.version == 2 and second.digest != first.digest
    assert first.values["sizing.remaining_budget_enabled"] is True
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_controls_cannot_write_live_or_disable_missing_guards(database):
    with pytest.raises(ValueError, match="TESTNET_ONLY"):
        configure(
            database,
            replace(SCOPE, environment="LIVE"),
            {},
            expected_version=0,
            reason="QA reject live",
        )
    with pytest.raises(ValueError, match="REQUIRE_CAPITAL_GUARDS"):
        configure(
            database, SCOPE, {}, expected_version=0, reason="QA reject missing model"
        )
