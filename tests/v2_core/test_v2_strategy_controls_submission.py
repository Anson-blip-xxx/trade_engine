"""Committed typed control snapshots through the real guarded fake-venue path."""

from copy import deepcopy

import pytest
from test_v2_strategy_opening_gate import SCOPE, now, readiness, setup

from tests.support.v2_admission import database as database_fixture
from v2_core import entry_controls, liquidity_controls

database = database_fixture
GUARDS = {"capital.model_enabled": True, "capital.reference_wallet": "1000"}


def control_module(name):
    return entry_controls if name == "entry_controls" else liquidity_controls


def configure(database, name, changes, version):
    return control_module(name).configure(
        database,
        SCOPE,
        changes,
        expected_version=version,
        reason="isolated typed controls QA",
    )


def rejected_without_venue_work(database, case):
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.calls
    assert not case.venue.setting_posts
    assert not case.venue.opening_posts
    assert readiness(database, case.opening) is None


@pytest.mark.parametrize("name", ["entry_controls", "liquidity_controls"])
@pytest.mark.parametrize("configured_before", [False, True])
def test_committed_control_change_requires_new_typed_proposal(
    database, name, configured_before
):
    def freeze(features):
        if configured_before:
            features["execution"][name] = configure(database, name, {}, 0).snapshot()

    case = setup(database, policy_changes=GUARDS, context_change=freeze)
    before = deepcopy(case.runtime.data.trace(case.episode)["decision"])
    version = int(configured_before)
    changes = (
        {"sizing.price_drift_fraction": "0.02"}
        if name == "entry_controls"
        else {"max_spread_bps": "19"}
    )
    configure(database, name, changes if configured_before else {}, version)
    # Assert the exact fence against committed rows, so a capital failure cannot
    # accidentally satisfy the transport-level rejection below.
    with (
        database() as conn,
        pytest.raises(ValueError, match="CONTROLS_CHANGED_REPROPOSE"),
    ):
        control_module(name).validate_submission(conn, SCOPE, case.opening)
    rejected_without_venue_work(database, case)
    assert case.runtime.data.trace(case.episode)["decision"] == before


@pytest.mark.parametrize("name", ["entry_controls", "liquidity_controls"])
def test_matching_nonzero_off_snapshot_can_reach_guarded_open(database, name):
    def freeze(features):
        features["execution"][name] = configure(database, name, {}, 0).snapshot()

    case = setup(database, policy_changes=GUARDS, context_change=freeze)
    assert case.runtime.execution.dispatch(case.opening) == "ACKNOWLEDGED"
    assert len(case.venue.opening_posts) == 1
    saved = case.runtime.data.trace(case.episode)["decision"]["features"]["execution"]
    assert saved[name] == control_module(name).read(database, SCOPE).snapshot()
    assert saved[name]["version"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"sizing.remaining_budget_enabled": True},
        {"history.mode": "SOFT"},
        {"reentry.mode": "ENFORCE"},
    ],
)
def test_matching_enforced_entry_controls_do_not_bypass_unsupported_semantics(
    database, changes
):
    def freeze(features):
        features["execution"]["entry_controls"] = configure(
            database, "entry_controls", changes, 0
        ).snapshot()

    case = setup(database, policy_changes=GUARDS, context_change=freeze)
    with (
        database() as conn,
        pytest.raises(ValueError, match="STRATEGY_ENTRY_CONTROLS_UNSUPPORTED"),
    ):
        entry_controls.validate_submission(conn, SCOPE, case.opening)
    rejected_without_venue_work(database, case)


@pytest.mark.parametrize("frozen_enabled", [False, True])
def test_frozen_or_current_committed_reentry_policy_cannot_be_skipped(
    database, frozen_enabled
):
    case = setup(database, policy_changes={"entry.reentry_enabled": frozen_enabled})
    case.policy.patch(
        {"entry.reentry_enabled": not frozen_enabled},
        expected_version=case.policy.read().version,
        reason="committed typed reentry policy change",
    )
    with (
        database() as conn,
        pytest.raises(ValueError, match="STRATEGY_REENTRY_POLICY_UNSUPPORTED"),
    ):
        entry_controls.validate_submission(conn, SCOPE, case.opening)
    rejected_without_venue_work(database, case)


@pytest.mark.parametrize(
    "failure,reason",
    [
        (None, None),
        ("book_expired", "LIQUIDITY_STALE"),
        ("quote_expired", "LIQUIDITY_EXECUTION_STALE"),
        ("wrong_execution_venue", "LIQUIDITY_EXECUTION_SOURCE_ENVIRONMENT_INVALID"),
        ("missing_quote", "LIQUIDITY_EXECUTION_EVIDENCE_MISSING"),
    ],
)
def test_committed_typed_liquidity_facts_drive_guarded_submission(
    database, failure, reason
):
    from v2_core.liquidity_submission import validate_opening_liquidity

    stamp = now()

    def freeze(features):
        context = features["execution"]
        context["liquidity_controls"] = configure(
            database, "liquidity_controls", {"mode": "ENFORCE"}, 0
        ).snapshot()
        context["liquidity_evidence"] = {
            "version": "binance-depth-v1",
            "symbol": "BTCUSDT",
            "source_environment": "LIVE",
            "source_host": "fapi.binance.com",
            "observed_at_ms": stamp - 10000
            if failure == "book_expired"
            else stamp - 20,
            "received_at_ms": stamp - 10,
            "last_update_id": 1,
            "bids": [["99.99", "10"]],
            "asks": [["100.01", "10"]],
        }
        if failure != "missing_quote":
            context["execution_quote"] = {
                "symbol": "BTCUSDT",
                "environment": "LIVE"
                if failure == "wrong_execution_venue"
                else "SANDBOX",
                "source_host": "demo-fapi.binance.com",
                "book_at_ms": stamp - 40000
                if failure == "quote_expired"
                else stamp - 20,
                "spread_fraction": "0.0002",
            }

    case = setup(
        database,
        policy_changes=GUARDS,
        plan_changes={
            "observed_at_ms": stamp - 100,
            "decided_at_ms": stamp - 10,
            "expires_at_ms": stamp + 60000,
        },
        context_change=freeze,
    )
    if failure is None:
        assert case.runtime.execution.dispatch(case.opening) == "ACKNOWLEDGED"
        assert len(case.venue.opening_posts) == 1
        assert readiness(database, case.opening)["deadline_ms"] == stamp - 20 + 5000
    else:
        with database() as conn, pytest.raises(ValueError, match=reason):
            validate_opening_liquidity(
                conn,
                SCOPE,
                case.runtime.execution.snapshot(case.opening),
                now_ms=case.tick[0],
            )
        rejected_without_venue_work(database, case)
