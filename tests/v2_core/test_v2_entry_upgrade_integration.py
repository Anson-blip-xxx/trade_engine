import json
from dataclasses import asdict

import pytest
from test_v2_directional_replay import SCOPE, scenario
from test_v2_runtime_policy import database
from test_v2_venue_readiness import case

from v2_core.capital_model import rehearsal_profile
from v2_core.entry_controls import configure, from_snapshot
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey

__all__ = ["case", "database"]


def test_sizing_budget_is_frozen_in_replay_without_order_or_policy_change(database):
    replay, _, signal_id, context = scenario(database)
    policy = PolicyStore(database, SCOPE)
    policy.patch(rehearsal_profile("1000"), expected_version=0, reason="QA base")
    old_policy = policy.read()
    controls = configure(
        database,
        SCOPE,
        {"sizing.remaining_budget_enabled": True},
        expected_version=0,
        reason="QA sizing rollout",
    )
    context["entry_controls"] = controls.snapshot()
    context["directional"]["sizing"].update(
        remaining_portfolio_risk="3",
        sizing_mark_price="101",
        price_drift_fraction="0.015",
    )
    result = replay.consume(signal_id, context=context)
    assert result["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    frozen = replay.decision(signal_id)["snapshot"]["features"]
    assert (
        frozen["evaluation"]["sizing_budget"]["version"] == "remaining-risk-sizing-v1"
    )
    assert frozen["context"]["entry_controls"] == controls.snapshot()
    assert policy.read() == old_policy
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_changed_controls_fence_prepared_order_without_send(case):
    data, order, _, _, runner, sent = case
    from test_v2_venue_readiness import SCOPE as venue_scope

    PolicyStore(data._connect, venue_scope).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA baseline"
    )
    configure(
        data._connect,
        venue_scope,
        {"sizing.remaining_budget_enabled": True},
        expected_version=0,
        reason="QA enable after prepared",
    )
    assert runner.dispatch(order) == "REJECTED"
    assert sent == []
    saved = BusinessState(data._connect).read(
        StateKey(**asdict(venue_scope), namespace="opening-readiness-v1", key=order)
    )
    assert (
        "ENTRY_CONTROLS_CHANGED_REPROPOSE" in json.loads(saved.payload_json)["blockers"]
    )


def test_control_snapshot_rejects_changed_values_and_other_scope(database):
    PolicyStore(database, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA baseline"
    )
    controls = configure(
        database, SCOPE, {}, expected_version=0, reason="QA proof scope"
    )
    assert from_snapshot(controls.snapshot(), SCOPE) == controls
    altered = controls.snapshot()
    altered["values"]["history.mode"] = "SOFT"
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        from_snapshot(altered, SCOPE)
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        from_snapshot(controls.snapshot(), {**asdict(SCOPE), "account_id": "wrong"})
