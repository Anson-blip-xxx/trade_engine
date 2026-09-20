"""Fail-closed admission boundary before any recovery executor is selected."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from operation_journal.coordinator import RecoveryWorkItem
from operation_journal.generation_reader import (
    GenerationResolution,
    GenerationResolutionCode,
    GenerationWitness,
)
from operation_journal.model import OperationRecord
from operation_journal.recovery import ExchangeAccess, decide_recovery


class GenerationResolver(Protocol):
    def resolve(self, record: OperationRecord) -> GenerationResolution: ...

    def revalidate(
            self,
            record: OperationRecord,
            expected: GenerationWitness,
            ) -> GenerationResolution: ...


class RecoveryAdmissionCode(str, Enum):
    READY = "READY"
    MUTATION_OWNERSHIP_REQUIRED = "MUTATION_OWNERSHIP_REQUIRED"
    POLICY_GATED = "POLICY_GATED"
    GENERATION_REJECTED = "GENERATION_REJECTED"
    DEPENDENCY_UNKNOWN = "DEPENDENCY_UNKNOWN"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


@dataclass(frozen=True)
class RecoveryAdmission:
    code: RecoveryAdmissionCode
    work_item: RecoveryWorkItem
    generation: GenerationResolution | None = None
    message: str = ""

    @property
    def witness(self) -> GenerationWitness | None:
        if self.generation is None:
            return None
        return self.generation.witness


_GENERATION_REJECTIONS = frozenset({
    GenerationResolutionCode.BLOCKED,
    GenerationResolutionCode.INVALID_WITNESS,
    GenerationResolutionCode.CANONICAL_CHANGED,
})


class RecoveryAdmissionCoordinator:
    """Compose a recovery decision with injected generation reads only."""

    def __init__(self, generation_resolver: GenerationResolver):
        if not callable(getattr(generation_resolver, "resolve", None)):
            raise TypeError("generation_resolver must provide resolve")
        if not callable(getattr(generation_resolver, "revalidate", None)):
            raise TypeError("generation_resolver must provide revalidate")
        self._generation_resolver = generation_resolver

    def admit(self, item: RecoveryWorkItem) -> RecoveryAdmission:
        if not isinstance(item, RecoveryWorkItem):
            raise TypeError("item must be RecoveryWorkItem")
        if item.decision != decide_recovery(item.record):
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                message="work item decision does not match its operation",
            )
        if (not item.decision.automatic or
                item.decision.policy_decision_required):
            return RecoveryAdmission(
                RecoveryAdmissionCode.POLICY_GATED,
                item,
                message="recovery decision requires explicit policy approval",
            )
        if not item.decision.generation_fence_required:
            if item.decision.exchange_access is ExchangeAccess.CONDITIONAL_MUTATION:
                return RecoveryAdmission(
                    RecoveryAdmissionCode.INVARIANT_VIOLATION,
                    item,
                    message="conditional mutation cannot bypass generation fencing",
                )
            return RecoveryAdmission(RecoveryAdmissionCode.READY, item)
        return self._resolve(item, revalidate=False)

    def revalidate(self, admission: RecoveryAdmission) -> RecoveryAdmission:
        if not isinstance(admission, RecoveryAdmission):
            raise TypeError("admission must be RecoveryAdmission")
        item = admission.work_item
        if (not isinstance(item, RecoveryWorkItem) or
                item.decision != decide_recovery(item.record)):
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                generation=admission.generation,
                message="admission work item is not canonical",
            )
        if (not item.decision.automatic or
                item.decision.policy_decision_required):
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                generation=admission.generation,
                message="policy-gated work cannot be revalidated as admitted",
            )
        if admission.code not in {
                RecoveryAdmissionCode.READY,
                RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED,
                }:
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                admission.work_item,
                generation=admission.generation,
                message="only an admitted directive can be revalidated",
            )
        expected_code = RecoveryAdmissionCode.READY
        if item.decision.exchange_access is ExchangeAccess.CONDITIONAL_MUTATION:
            expected_code = RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED
        if admission.code is not expected_code:
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                generation=admission.generation,
                message="admission code contradicts exchange access",
            )
        if not item.decision.generation_fence_required:
            if admission.generation is not None:
                return RecoveryAdmission(
                    RecoveryAdmissionCode.INVARIANT_VIOLATION,
                    item,
                    generation=admission.generation,
                    message="fence-free admission has an invalid shape",
                )
            return admission
        if admission.witness is None:
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                generation=admission.generation,
                message="fenced admission omitted its witness",
            )
        return self._resolve(
            item,
            revalidate=True,
            witness=admission.witness,
        )

    def _resolve(
            self,
            item: RecoveryWorkItem,
            *,
            revalidate: bool,
            witness: GenerationWitness | None = None,
            ) -> RecoveryAdmission:
        try:
            if revalidate:
                result = self._generation_resolver.revalidate(
                    item.record, witness)
            else:
                result = self._generation_resolver.resolve(item.record)
        except Exception as exc:  # noqa: BLE001 - injected dependency boundary
            return RecoveryAdmission(
                RecoveryAdmissionCode.DEPENDENCY_UNKNOWN,
                item,
                message=str(exc),
            )
        if not isinstance(result, GenerationResolution):
            return RecoveryAdmission(
                RecoveryAdmissionCode.INVARIANT_VIOLATION,
                item,
                message="generation resolver returned an invalid response",
            )
        if result.code is GenerationResolutionCode.PASSED:
            if result.witness is None:
                return RecoveryAdmission(
                    RecoveryAdmissionCode.INVARIANT_VIOLATION,
                    item,
                    generation=result,
                    message="PASSED generation resolution omitted its witness",
                )
            code = RecoveryAdmissionCode.READY
            if item.decision.exchange_access is ExchangeAccess.CONDITIONAL_MUTATION:
                code = RecoveryAdmissionCode.MUTATION_OWNERSHIP_REQUIRED
            return RecoveryAdmission(code, item, generation=result)
        if result.code in _GENERATION_REJECTIONS:
            return RecoveryAdmission(
                RecoveryAdmissionCode.GENERATION_REJECTED,
                item,
                generation=result,
                message=result.message,
            )
        return RecoveryAdmission(
            RecoveryAdmissionCode.DEPENDENCY_UNKNOWN,
            item,
            generation=result,
            message=result.message,
        )
