from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    ExchangeAccess,
    GenerationFenceCode,
    GenerationFenceDecision,
    GenerationResolution,
    GenerationResolutionCode,
    GenerationWitness,
    OperationRecord,
    OperationStage,
    OperationType,
    RecoveryAdmission,
    RecoveryAdmissionCode,
    RecoveryAdmissionCoordinator,
    RecoveryClaimCode,
    RecoveryClaimResult,
    RecoveryDecision,
    RecoveryGenerationReader,
    RecoveryWorkItem,
    decide_recovery,
    plan_recovery_batch,
)
from position_identity.authority import (
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey

KEY = ExchangePositionKey.one_way(
    account_principal_id="recovery-admission", environment="SANDBOX",
    symbol="BTCUSDT",
)
EPISODE = str(uuid4())


def _record(*, stage=OperationStage.INTENT_DURABLE):
    return OperationRecord(
        operation_id=str(uuid4()), operation_type=OperationType.CLOSE_FULL,
        exchange_position_key=KEY, stage=stage, version=2, input_json="{}",
        created_at=1, updated_at=2, position_episode_id=EPISODE,
        lifecycle_generation=4, owner_token="owner", lease_expires_at=30,
    )


def _item(*, stage=OperationStage.INTENT_DURABLE):
    record = _record(stage=stage)
    return RecoveryWorkItem(record, decide_recovery(record))


def _witness(record):
    authority = SlotAuthority(
        exchange_position_key=KEY, episode_id=EPISODE, slot_generation=4,
        status=AuthorityStatus.ACTIVE, provenance=AuthorityProvenance.NATIVE,
        revision=2, legacy_position_id_alias=None, created_at=1, updated_at=2,
    )
    return GenerationWitness(
        operation_id=record.operation_id, operation_type=record.operation_type,
        authority=authority, desired=None,
    )


def _passed(record):
    return GenerationResolution(
        GenerationResolutionCode.PASSED,
        GenerationFenceDecision(
            GenerationFenceCode.CURRENT, True, "current"),
        witness=_witness(record),
    )


class _Resolver:
    def __init__(self, result=None, *, raises=False):
        self.result = result
        self.raises = raises
        self.resolve_calls = []
        self.revalidate_calls = []

    def resolve(self, record):
        self.resolve_calls.append(record)
        if self.raises:
            raise OSError("generation backend down")
        return self.result

    def revalidate(self, record, witness):
        self.revalidate_calls.append((record, witness))
        if self.raises:
            raise OSError("generation backend down")
        return self.result


class _AuthorityReader:
    def __init__(self, authority):
        self.authority = authority

    def get_slot_authority(self, key):
        assert key == KEY
        return AuthorityReadResult(AuthorityReadCode.FOUND, self.authority)

def test_mutating_item_requires_ownership_even_after_generation_passes():
    item = _item()
    resolver = _Resolver(_passed(item.record))
    result = RecoveryAdmissionCoordinator(resolver).admit(item)
    assert result.code is RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED
    assert result.witness == _witness(item.record)
    assert resolver.resolve_calls == [item.record]


def test_fence_free_local_work_is_ready_without_dependency_read():
    item = _item(stage=OperationStage.NEW)
    resolver = _Resolver(None)
    result = RecoveryAdmissionCoordinator(resolver).admit(item)
    assert result.code is RecoveryAdmissionCode.READY
    assert result.generation is None
    assert resolver.resolve_calls == []


def test_query_only_directive_is_ready_after_generation_passes():
    item = _item(stage=OperationStage.UNKNOWN)
    resolver = _Resolver(_passed(item.record))
    result = RecoveryAdmissionCoordinator(resolver).admit(item)
    assert item.decision.exchange_access is ExchangeAccess.QUERY_ONLY
    assert result.code is RecoveryAdmissionCode.READY
    assert result.witness == _witness(item.record)


def test_policy_gated_work_never_reads_generation_dependency():
    record = _record(stage=OperationStage.COMPENSATION_REQUIRED)
    item = RecoveryWorkItem(record, decide_recovery(record))
    resolver = _Resolver(None)
    result = RecoveryAdmissionCoordinator(resolver).admit(item)
    assert result.code is RecoveryAdmissionCode.POLICY_GATED
    assert resolver.resolve_calls == []


def test_forged_decision_fails_before_dependency_read():
    item = _item()
    forged = replace(
        item,
        decision=RecoveryDecision(
            directive=item.decision.directive,
            exchange_access=ExchangeAccess.FORBIDDEN,
            generation_fence_required=False,
            effect_absence_proof_required=False,
            policy_decision_required=False,
            automatic=True,
        ),
    )
    resolver = _Resolver(None)
    result = RecoveryAdmissionCoordinator(resolver).admit(forged)
    assert result.code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    assert resolver.resolve_calls == []


@pytest.mark.parametrize(
    ("generation_code", "admission_code"),
    [
        (GenerationResolutionCode.BLOCKED,
         RecoveryAdmissionCode.GENERATION_REJECTED),
        (GenerationResolutionCode.CANONICAL_CHANGED,
         RecoveryAdmissionCode.GENERATION_REJECTED),
        (GenerationResolutionCode.AUTHORITY_UNAVAILABLE,
         RecoveryAdmissionCode.DEPENDENCY_UNKNOWN),
        (GenerationResolutionCode.DESIRED_MALFORMED,
         RecoveryAdmissionCode.DEPENDENCY_UNKNOWN),
    ],
)
def test_resolution_failures_are_partitioned_fail_closed(
        generation_code, admission_code):
    item = _item()
    resolution = GenerationResolution(generation_code, message="blocked")
    result = RecoveryAdmissionCoordinator(
        _Resolver(resolution)).admit(item)
    assert result.code is admission_code
    assert result.generation is resolution


def test_dependency_exception_invalid_response_and_missing_witness_fail_closed():
    item = _item()
    assert RecoveryAdmissionCoordinator(
        _Resolver(raises=True)).admit(
            item).code is RecoveryAdmissionCode.DEPENDENCY_UNKNOWN
    assert RecoveryAdmissionCoordinator(
        _Resolver(object())).admit(
            item).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    assert RecoveryAdmissionCoordinator(
        _Resolver(GenerationResolution(
            GenerationResolutionCode.PASSED))).admit(
                item).code is RecoveryAdmissionCode.INVARIANT_VIOLATION


def test_revalidate_mutation_pending_admission_uses_exact_witness():
    item = _item()
    resolver = _Resolver(_passed(item.record))
    coordinator = RecoveryAdmissionCoordinator(resolver)
    admission = coordinator.admit(item)
    result = coordinator.revalidate(admission)
    assert result.code is RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED
    assert resolver.revalidate_calls == [(item.record, admission.witness)]


def test_revalidate_changed_witness_rejects_and_never_claims_ready():
    item = _item()
    resolver = _Resolver(_passed(item.record))
    coordinator = RecoveryAdmissionCoordinator(resolver)
    admission = coordinator.admit(item)
    resolver.result = GenerationResolution(
        GenerationResolutionCode.CANONICAL_CHANGED,
        message="changed",
    )
    result = coordinator.revalidate(admission)
    assert result.code is RecoveryAdmissionCode.GENERATION_REJECTED


def test_fence_free_revalidation_is_a_noop_and_nonready_is_rejected():
    item = _item(stage=OperationStage.NEW)
    resolver = _Resolver(None)
    coordinator = RecoveryAdmissionCoordinator(resolver)
    admission = coordinator.admit(item)
    assert coordinator.revalidate(admission) is admission
    blocked = RecoveryAdmission(RecoveryAdmissionCode.POLICY_GATED, item)
    assert coordinator.revalidate(
        blocked).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    assert resolver.revalidate_calls == []


def test_revalidate_rejects_forged_work_item_and_fence_free_shape():
    item = _item(stage=OperationStage.NEW)
    forged_item = replace(
        item,
        decision=replace(item.decision, generation_fence_required=True),
    )
    resolver = _Resolver(None)
    coordinator = RecoveryAdmissionCoordinator(resolver)
    forged = RecoveryAdmission(RecoveryAdmissionCode.READY, forged_item)
    assert coordinator.revalidate(
        forged).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    wrong_shape = RecoveryAdmission(
        RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED, item)
    assert coordinator.revalidate(
        wrong_shape).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    policy_record = _record(stage=OperationStage.FAILED_TERMINAL)
    policy_item = RecoveryWorkItem(policy_record, decide_recovery(policy_record))
    policy_ready = RecoveryAdmission(RecoveryAdmissionCode.READY, policy_item)
    assert coordinator.revalidate(
        policy_ready).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    mutating_item = _item()
    false_ready = RecoveryAdmission(
        RecoveryAdmissionCode.READY,
        mutating_item,
        generation=_passed(mutating_item.record),
    )
    assert coordinator.revalidate(
        false_ready).code is RecoveryAdmissionCode.INVARIANT_VIOLATION
    assert resolver.revalidate_calls == []


def test_constructor_and_public_methods_reject_untyped_inputs():
    with pytest.raises(TypeError, match="provide resolve"):
        RecoveryAdmissionCoordinator(object())
    with pytest.raises(TypeError, match="provide revalidate"):
        RecoveryAdmissionCoordinator(type("OnlyResolve", (), {
            "resolve": lambda self, record: None,
        })())
    coordinator = RecoveryAdmissionCoordinator(_Resolver(None))
    with pytest.raises(TypeError, match="RecoveryWorkItem"):
        coordinator.admit(object())
    with pytest.raises(TypeError, match="RecoveryAdmission"):
        coordinator.revalidate(object())


def test_claim_to_admission_pipeline_rejects_authority_revision_drift():
    record = _record()
    batch = plan_recovery_batch(RecoveryClaimResult(
        RecoveryClaimCode.CLAIMED, (record,)))
    authority_reader = _AuthorityReader(_witness(record).authority)
    coordinator = RecoveryAdmissionCoordinator(
        RecoveryGenerationReader(authority_reader))

    admission = coordinator.admit(batch.automatic_items[0])
    assert admission.code is RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED

    authority_reader.authority = replace(
        authority_reader.authority, revision=3)
    revalidated = coordinator.revalidate(admission)
    assert revalidated.code is RecoveryAdmissionCode.GENERATION_REJECTED
    assert revalidated.generation.code is (
        GenerationResolutionCode.CANONICAL_CHANGED)
