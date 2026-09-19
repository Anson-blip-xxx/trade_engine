"""Controlled migrated-protection planning tests."""
from uuid import uuid4

import pytest

from position_identity.authority import AuthorityProvenance, SlotAuthority
from position_identity.migration_handoff import (
    LegacyMigrationHandoffCode,
    LegacyMigrationHandoffResult,
)
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.migrated_plan import (
    MigratedProtectionPlanCode,
    plan_migrated_protection,
)


def _handoff(code=LegacyMigrationHandoffCode.APPLIED):
    key = ExchangePositionKey.one_way(account_principal_id="migration-plan", environment="SANDBOX", symbol="BTCUSDT")
    episode = str(uuid4())
    authority = SlotAuthority.initial_flat(key, now=1).allocate_episode(
        episode_id=episode, provenance=AuthorityProvenance.MIGRATED,
        status="ACTIVE", legacy_position_id_alias="legacy-1", now=2,
    )
    projection = LivePositionProjection(
        exchange_position_key=key, episode_id=episode,
        slot_generation=authority.slot_generation,
        identity_provenance=AuthorityProvenance.MIGRATED, state_revision=1,
        last_operation_id="migrate", side="LONG", system="S6", quantity=2,
        entry_price=100, opened_at=1, updated_at=2,
        legacy_position_id_alias="legacy-1",
    )
    return LegacyMigrationHandoffResult(code, authority, projection)


def _plan(handoff=None, row=None):
    return plan_migrated_protection(
        handoff=handoff or _handoff(),
        legacy_row=row or {"sl": 90, "algo_sl_id": 77},
        desired_intent_id="migrated-stop", operation_id="plan-1", now=3,
    )


def test_acknowledged_handoff_builds_same_identity_pending_desired_with_alias():
    handoff = _handoff()
    result = _plan(handoff)
    assert result.code is MigratedProtectionPlanCode.READY
    desired = result.desired
    assert desired.exchange_position_key == handoff.authority.exchange_position_key
    assert desired.episode_id == handoff.authority.episode_id
    assert desired.slot_generation == handoff.authority.slot_generation
    assert desired.covered_quantity == handoff.projection.quantity
    assert desired.closing_side == "SELL"
    assert desired.trigger_price == 90
    assert desired.exchange_algo_aliases == ("77",)


@pytest.mark.parametrize("code, expected", [
    (LegacyMigrationHandoffCode.UNKNOWN, MigratedProtectionPlanCode.RESOLVE_HANDOFF_UNKNOWN),
    (LegacyMigrationHandoffCode.UNAVAILABLE, MigratedProtectionPlanCode.RETRY_HANDOFF),
    (LegacyMigrationHandoffCode.CONFLICT, MigratedProtectionPlanCode.QUARANTINE),
    (LegacyMigrationHandoffCode.QUARANTINED, MigratedProtectionPlanCode.QUARANTINE),
    (LegacyMigrationHandoffCode.INVALID, MigratedProtectionPlanCode.REJECT),
])
def test_non_acknowledged_handoff_never_builds_desired(code, expected):
    result = _plan(LegacyMigrationHandoffResult(code))
    assert result.code is expected
    assert result.desired is None


@pytest.mark.parametrize("row", [
    {"sl": 90}, {"sl": 90, "algo_sl_id": ""},
    {"sl": 90, "algo_sl_id": True}, {"sl": 0, "algo_sl_id": 77},
])
def test_missing_or_invalid_legacy_protection_quarantines(row):
    assert _plan(row=row).code is MigratedProtectionPlanCode.QUARANTINE


def test_identity_mismatch_quarantines_without_desired():
    handoff = _handoff()
    other = _handoff().projection
    bad = LegacyMigrationHandoffResult(handoff.code, handoff.authority, other)
    result = _plan(bad)
    assert result.code is MigratedProtectionPlanCode.QUARANTINE
    assert result.desired is None
