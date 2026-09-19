from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    GenerationFenceCode,
    GenerationResolutionCode,
    OperationRecord,
    OperationStage,
    OperationType,
    RecoveryGenerationReader,
)
from position_identity.authority import (
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    DesiredReadCode,
    DesiredReadResult,
    ProtectionStatus,
)

KEY = ExchangePositionKey.one_way(
    account_principal_id="generation-reader", environment="SANDBOX",
    symbol="BTCUSDT",
)
EPISODE = str(uuid4())


class _Reader:
    def __init__(self, result, *, raises=False):
        self.result = result
        self.raises = raises
        self.keys = []

    def get_slot_authority(self, key):
        self.keys.append(key)
        if self.raises:
            raise OSError("authority down")
        return self.result

    def get(self, key):
        self.keys.append(key)
        if self.raises:
            raise OSError("desired down")
        return self.result


def _authority():
    return SlotAuthority(
        exchange_position_key=KEY, episode_id=EPISODE, slot_generation=4,
        status=AuthorityStatus.ACTIVE, provenance=AuthorityProvenance.NATIVE,
        revision=2, legacy_position_id_alias=None, created_at=1, updated_at=2,
    )


def _operation(operation_type=OperationType.CLOSE_FULL):
    return OperationRecord(
        operation_id=str(uuid4()), operation_type=operation_type,
        exchange_position_key=KEY, stage=OperationStage.INTENT_DURABLE,
        version=2, input_json="{}", created_at=1, updated_at=2,
        position_episode_id=EPISODE, lifecycle_generation=4,
        protection_generation=(2 if operation_type in {
            OperationType.PROTECTION_CREATE,
            OperationType.PROTECTION_REPLACE,
        } else None),
    )


def _desired(operation):
    return DesiredProtectionRecord(
        exchange_position_key=KEY, episode_id=EPISODE, slot_generation=4,
        protection_generation=2, desired_intent_id=str(uuid4()),
        trigger_price=90, covered_quantity=1, closing_side="SELL",
        status=ProtectionStatus.PENDING, exchange_algo_aliases=(), revision=1,
        last_operation_id=operation.operation_id, created_at=1, updated_at=2,
    )


def test_non_protection_reads_authority_once_and_skips_desired():
    operation = _operation()
    authority_reader = _Reader(AuthorityReadResult(
        AuthorityReadCode.FOUND, _authority()))
    desired_reader = _Reader(None)
    result = RecoveryGenerationReader(
        authority_reader, desired_reader).resolve(operation)
    assert result.code is GenerationResolutionCode.PASSED
    assert result.decision.code is GenerationFenceCode.CURRENT
    assert authority_reader.keys == [KEY]
    assert desired_reader.keys == []


def test_protection_reads_both_once_and_applies_exact_fence():
    operation = _operation(OperationType.PROTECTION_CREATE)
    authority_reader = _Reader(AuthorityReadResult(
        AuthorityReadCode.FOUND, _authority()))
    desired_reader = _Reader(DesiredReadResult(
        DesiredReadCode.FOUND, _desired(operation)))
    result = RecoveryGenerationReader(
        authority_reader, desired_reader).resolve(operation)
    assert result.code is GenerationResolutionCode.PASSED
    assert authority_reader.keys == [KEY]
    assert desired_reader.keys == [KEY]

    desired_reader.result = DesiredReadResult(
        DesiredReadCode.FOUND,
        replace(_desired(operation), protection_generation=3),
    )
    stale = RecoveryGenerationReader(
        authority_reader, desired_reader).resolve(operation)
    assert stale.code is GenerationResolutionCode.BLOCKED
    assert stale.decision.code is GenerationFenceCode.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("read_code", "resolution"),
    [
        (AuthorityReadCode.NOT_FOUND, GenerationResolutionCode.AUTHORITY_NOT_FOUND),
        (AuthorityReadCode.MALFORMED, GenerationResolutionCode.AUTHORITY_MALFORMED),
        (AuthorityReadCode.BACKEND_ERROR, GenerationResolutionCode.AUTHORITY_UNAVAILABLE),
    ],
)
def test_authority_failures_are_typed_and_never_read_desired(read_code, resolution):
    authority_reader = _Reader(AuthorityReadResult(read_code, message="reason"))
    desired_reader = _Reader(None)
    result = RecoveryGenerationReader(
        authority_reader, desired_reader).resolve(
            _operation(OperationType.PROTECTION_CREATE))
    assert result.code is resolution
    assert result.message == "reason"
    assert desired_reader.keys == []


@pytest.mark.parametrize(
    ("read_code", "resolution"),
    [
        (DesiredReadCode.NOT_FOUND, GenerationResolutionCode.DESIRED_NOT_FOUND),
        (DesiredReadCode.MALFORMED, GenerationResolutionCode.DESIRED_MALFORMED),
        (DesiredReadCode.UNAVAILABLE, GenerationResolutionCode.DESIRED_UNAVAILABLE),
    ],
)
def test_desired_failures_are_typed(read_code, resolution):
    operation = _operation(OperationType.PROTECTION_REPLACE)
    result = RecoveryGenerationReader(
        _Reader(AuthorityReadResult(AuthorityReadCode.FOUND, _authority())),
        _Reader(DesiredReadResult(read_code, message="reason")),
    ).resolve(operation)
    assert result.code is resolution
    assert result.message == "reason"


def test_missing_reader_exceptions_and_invalid_responses_fail_closed():
    operation = _operation(OperationType.PROTECTION_CREATE)
    authority = _Reader(AuthorityReadResult(
        AuthorityReadCode.FOUND, _authority()))
    assert RecoveryGenerationReader(
        authority).resolve(operation).code is GenerationResolutionCode.DESIRED_UNAVAILABLE
    assert RecoveryGenerationReader(
        _Reader(None, raises=True)).resolve(
            operation).code is GenerationResolutionCode.AUTHORITY_UNAVAILABLE
    assert RecoveryGenerationReader(
        _Reader("not-a-result")).resolve(
            operation).code is GenerationResolutionCode.INVALID_RESPONSE
    assert RecoveryGenerationReader(
        authority, _Reader(None, raises=True)).resolve(
            operation).code is GenerationResolutionCode.DESIRED_UNAVAILABLE
    assert RecoveryGenerationReader(
        authority, _Reader("not-a-result")).resolve(
            operation).code is GenerationResolutionCode.INVALID_RESPONSE


def test_inconsistent_found_responses_fail_closed():
    operation = _operation(OperationType.PROTECTION_CREATE)
    assert RecoveryGenerationReader(
        _Reader(AuthorityReadResult(AuthorityReadCode.FOUND))).resolve(
            operation).code is GenerationResolutionCode.INVALID_RESPONSE
    assert RecoveryGenerationReader(
        _Reader(AuthorityReadResult(AuthorityReadCode.FOUND, _authority())),
        _Reader(DesiredReadResult(DesiredReadCode.FOUND)),
    ).resolve(operation).code is GenerationResolutionCode.INVALID_RESPONSE


def test_reader_constructor_and_resolve_reject_untyped_inputs():
    with pytest.raises(TypeError, match="get_slot_authority"):
        RecoveryGenerationReader(object())
    reader = RecoveryGenerationReader(_Reader(None))
    with pytest.raises(TypeError, match="OperationRecord"):
        reader.resolve({})
