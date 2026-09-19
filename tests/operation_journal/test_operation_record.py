from uuid import uuid4

import pytest

from operation_journal import OperationRecord, OperationStage, OperationType
from position_identity.slot import ExchangePositionKey


def _record():
    return OperationRecord.new(
        operation_id=str(uuid4()), operation_type=OperationType.PROTECTION_CREATE,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="journal", environment="SANDBOX", symbol="BTCUSDT"),
        normalized_input={"quantity": 2, "trigger": 90}, now=1,
        request_id="request-1",
    )


def test_new_record_is_canonical_and_immutable_identity_is_stable():
    record = _record()
    assert record.stage is OperationStage.NEW
    assert record.version == 1
    assert record.input_json == '{"quantity":2,"trigger":90}'
    with pytest.raises(ValueError):
        record.transition(stage=OperationStage.INTENT_DURABLE, now=2, request_id="other")


def test_happy_path_advances_revision_once_per_acknowledged_stage():
    record = _record()
    stages = (
        OperationStage.INTENT_DURABLE, OperationStage.SUBMITTING,
        OperationStage.EXCHANGE_ACKED, OperationStage.EFFECT_CONFIRMED,
        OperationStage.LOCAL_PROJECTED, OperationStage.COMPLETED,
    )
    for index, stage in enumerate(stages, start=2):
        record = record.transition(stage=stage, now=index)
        assert record.version == index
        assert record.stage is stage


def test_unknown_never_becomes_failure_without_effect_absence_proof():
    record = _record().transition(stage=OperationStage.INTENT_DURABLE, now=2)
    record = record.transition(stage=OperationStage.SUBMITTING, now=3)
    record = record.transition(stage=OperationStage.UNKNOWN, now=4)
    with pytest.raises(ValueError, match="effect-absence proof"):
        record.transition(stage=OperationStage.FAILED_RETRYABLE, now=5)
    recovered = record.transition(
        stage=OperationStage.FAILED_RETRYABLE, now=5,
        effect_absence_proven=True, next_attempt_at=6,
    )
    assert recovered.next_attempt_at == 6


@pytest.mark.parametrize("stage", [OperationStage.COMPLETED, OperationStage.FAILED_TERMINAL])
def test_terminal_stages_cannot_advance(stage):
    record = _record()
    if stage is OperationStage.COMPLETED:
        for next_stage in (
            OperationStage.INTENT_DURABLE, OperationStage.SUBMITTING,
            OperationStage.EFFECT_CONFIRMED, OperationStage.LOCAL_PROJECTED,
            OperationStage.COMPLETED,
        ):
            record = record.transition(stage=next_stage, now=record.version + 1)
    else:
        record = record.transition(stage=stage, now=2)
    with pytest.raises(ValueError):
        record.transition(stage=OperationStage.INTENT_DURABLE, now=10)


def test_owner_and_lease_are_all_or_nothing_and_json_rejects_nan():
    with pytest.raises(ValueError):
        OperationRecord.new(
            operation_id=str(uuid4()), operation_type=OperationType.OPEN,
            exchange_position_key=_record().exchange_position_key,
            normalized_input={"price": float("nan")}, now=1,
        )
    with pytest.raises(ValueError):
        _record().transition(
            stage=OperationStage.INTENT_DURABLE, now=2, owner_token="worker"
        )


def test_episode_and_generation_references_bind_once():
    episode = str(uuid4())
    record = _record().transition(
        stage=OperationStage.INTENT_DURABLE, now=2,
        position_episode_id=episode, lifecycle_generation=1,
        protection_generation=2,
    )
    with pytest.raises(ValueError, match="bound position_episode_id"):
        record.transition(
            stage=OperationStage.SUBMITTING, now=3,
            position_episode_id=str(uuid4()),
        )
    with pytest.raises(ValueError, match="bound lifecycle_generation"):
        record.transition(
            stage=OperationStage.SUBMITTING, now=3, lifecycle_generation=2,
        )
