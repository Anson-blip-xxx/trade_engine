"""One-step verification after exact migrated desired-state declaration."""
from dataclasses import dataclass

from position_protection.migrated_declaration import MigratedDeclarationResult
from position_protection.verification_coordinator import (
    VerificationCoordinatorCode,
    VerificationCoordinatorResult,
    VerificationSession,
)


@dataclass(frozen=True)
class MigratedVerificationResult:
    declaration: MigratedDeclarationResult
    verification: VerificationCoordinatorResult | None = None

    @property
    def active(self) -> bool:
        return (
            self.verification is not None
            and self.verification.code is VerificationCoordinatorCode.ACTIVE
        )


class MigratedProtectionVerificationService:
    """Run at most one coordinator step, and only after an exact declaration ACK."""

    def __init__(self, coordinator) -> None:
        if not callable(getattr(coordinator, "step", None)):
            raise TypeError("coordinator must provide callable step")
        self._coordinator = coordinator

    def verify_once(
        self, *, declaration: MigratedDeclarationResult,
        session: VerificationSession, now: float, max_evidence_age: float,
        quantity_tolerance: float, trigger_tolerance: float,
    ) -> MigratedVerificationResult:
        if not isinstance(declaration, MigratedDeclarationResult):
            raise TypeError("declaration must be MigratedDeclarationResult")
        if not declaration.may_verify:
            return MigratedVerificationResult(declaration)
        desired = declaration.plan.desired
        assert desired is not None
        verification = self._coordinator.step(
            session=session,
            exchange_position_key=desired.exchange_position_key,
            now=now,
            max_evidence_age=max_evidence_age,
            quantity_tolerance=quantity_tolerance,
            trigger_tolerance=trigger_tolerance,
        )
        if not isinstance(verification, VerificationCoordinatorResult):
            raise TypeError("coordinator must return VerificationCoordinatorResult")
        return MigratedVerificationResult(declaration, verification)
