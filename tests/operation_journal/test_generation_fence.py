from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    GenerationFenceCode,
    OperationRecord,
    OperationType,
    validate_recovery_generation,
)
from position_identity.authority import (
    AuthorityProvenance,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus

KEY = ExchangePositionKey.one_way(
    account_principal_id="recovery-fence", environment="SANDBOX",
    symbol="BTCUSDT",
)
EPISODE = str(uuid4())


def _authority(status=AuthorityStatus.ACTIVE):
    return SlotAuthority(
        exchange_position_key=KEY, episode_id=EPISODE, slot_generation=7,
        status=status, provenance=AuthorityProvenance.NATIVE,
        revision=9, legacy_position_id_alias=None, created_at=1, updated_at=2,
    )


def _operation(operation_type, **changes):
    defaults = {
        "operation_id": str(uuid4()),
        "operation_type": operation_type,
        "exchange_position_key": KEY,
        "stage": "INTENT_DURABLE",
        "version": 2,
        "input_json": "{}",
        "created_at": 1,
        "updated_at": 2,
        "position_episode_id": EPISODE,
        "lifecycle_generation": 7,
    }
    defaults.update(changes)
    from operation_journal import OperationStage
    defaults["stage"] = OperationStage(defaults["stage"])
    return OperationRecord(**defaults)


def _desired(operation, *, status=ProtectionStatus.PENDING):
    return DesiredProtectionRecord(
        exchange_position_key=KEY, episode_id=EPISODE, slot_generation=7,
        protection_generation=operation.protection_generation,
        desired_intent_id=str(uuid4()), trigger_price=90, covered_quantity=1,
        closing_side="SELL", status=status, exchange_algo_aliases=(),
        revision=1, last_operation_id=operation.operation_id,
        created_at=1, updated_at=2,
    )


@pytest.mark.parametrize("operation_type", [
    OperationType.CLOSE_FULL,
    OperationType.CLOSE_PARTIAL,
    OperationType.GHOST_FINALIZE,
])
def test_close_and_ghost_require_exact_retained_episode_generation(operation_type):
    operation = _operation(operation_type)
    assert validate_recovery_generation(
        operation, _authority()).code is GenerationFenceCode.CURRENT
    stale = replace(operation, lifecycle_generation=6)
    decision = validate_recovery_generation(stale, _authority())
    assert decision.code is GenerationFenceCode.IDENTITY_MISMATCH
    assert decision.fence_passed is False


def test_fresh_open_requires_allocation_and_active_open_requires_policy():
    fresh = _operation(
        OperationType.OPEN, position_episode_id=None, lifecycle_generation=None)
    flat = validate_recovery_generation(fresh, _authority(AuthorityStatus.FLAT))
    assert flat.code is GenerationFenceCode.ALLOCATION_REQUIRED
    assert flat.fence_passed is False
    active = validate_recovery_generation(fresh, _authority())
    assert active.code is GenerationFenceCode.POLICY_REQUIRED


def test_external_and_quarantined_slots_never_auto_pass():
    external = validate_recovery_generation(
        _operation(OperationType.EXTERNAL_RECONCILE), _authority())
    assert external.code is GenerationFenceCode.POLICY_REQUIRED
    quarantined = validate_recovery_generation(
        _operation(OperationType.CLOSE_FULL),
        _authority(AuthorityStatus.QUARANTINED),
    )
    assert quarantined.code is GenerationFenceCode.QUARANTINED


@pytest.mark.parametrize("operation_type", [
    OperationType.PROTECTION_CREATE,
    OperationType.PROTECTION_REPLACE,
])
def test_protection_requires_exact_live_desired_generation(operation_type):
    operation = _operation(operation_type, protection_generation=3)
    current = validate_recovery_generation(
        operation, _authority(), _desired(operation))
    assert current.code is GenerationFenceCode.CURRENT
    assert current.fence_passed is True

    stale = replace(_desired(operation), protection_generation=4)
    assert validate_recovery_generation(
        operation, _authority(), stale).code is GenerationFenceCode.IDENTITY_MISMATCH
    terminal = _desired(operation, status=ProtectionStatus.SUPERSEDED)
    assert validate_recovery_generation(
        operation, _authority(), terminal).code is GenerationFenceCode.STALE_PROTECTION


def test_protection_missing_desired_or_flat_authority_fails_closed():
    operation = _operation(
        OperationType.PROTECTION_CREATE, protection_generation=1)
    assert validate_recovery_generation(
        operation, _authority()).code is GenerationFenceCode.DESIRED_REQUIRED
    assert validate_recovery_generation(
        operation, _authority(AuthorityStatus.FLAT), _desired(operation)
    ).code is GenerationFenceCode.INACTIVE_SLOT


def test_generation_fence_rejects_untyped_inputs():
    operation = _operation(OperationType.CLOSE_FULL)
    with pytest.raises(TypeError, match="record"):
        validate_recovery_generation({}, _authority())
    with pytest.raises(TypeError, match="authority"):
        validate_recovery_generation(operation, {})
