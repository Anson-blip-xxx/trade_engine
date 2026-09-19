"""Fail-closed activation readiness for dormant protection verification."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from position_protection.binance_query import BinanceVerificationEndpoints


class VerificationActivationBlocker(str, Enum):
    FEATURE_DISABLED = "FEATURE_DISABLED"
    ENDPOINTS_UNAPPROVED = "ENDPOINTS_UNAPPROVED"
    DURABLE_SCHEDULER_UNAPPROVED = "DURABLE_SCHEDULER_UNAPPROVED"
    OPERATOR_OUTCOME_SINK_UNAPPROVED = "OPERATOR_OUTCOME_SINK_UNAPPROVED"


class VerificationActivationCode(str, Enum):
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    READY = "READY"


def _reference(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value.strip()


@dataclass(frozen=True)
class VerificationActivationManifest:
    enabled: bool = False
    endpoints: BinanceVerificationEndpoints | None = None
    endpoint_contract_ref: str = ""
    durable_scheduler_contract_ref: str = ""
    operator_runbook_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        if self.endpoints is not None and not isinstance(
            self.endpoints, BinanceVerificationEndpoints
        ):
            raise TypeError("endpoints must be BinanceVerificationEndpoints or None")
        for field in (
            "endpoint_contract_ref",
            "durable_scheduler_contract_ref",
            "operator_runbook_ref",
        ):
            object.__setattr__(self, field, _reference(getattr(self, field), field))


@dataclass(frozen=True)
class VerificationActivationDecision:
    code: VerificationActivationCode
    blockers: tuple[VerificationActivationBlocker, ...]

    @property
    def ready(self) -> bool:
        return self.code is VerificationActivationCode.READY


def assess_verification_activation(
    manifest: VerificationActivationManifest,
) -> VerificationActivationDecision:
    """Assess readiness without reading configuration or performing I/O."""
    if not isinstance(manifest, VerificationActivationManifest):
        raise TypeError("manifest must be VerificationActivationManifest")
    if not manifest.enabled:
        return VerificationActivationDecision(
            VerificationActivationCode.DISABLED,
            (VerificationActivationBlocker.FEATURE_DISABLED,),
        )
    blockers = []
    if manifest.endpoints is None or not manifest.endpoint_contract_ref:
        blockers.append(VerificationActivationBlocker.ENDPOINTS_UNAPPROVED)
    if not manifest.durable_scheduler_contract_ref:
        blockers.append(VerificationActivationBlocker.DURABLE_SCHEDULER_UNAPPROVED)
    if not manifest.operator_runbook_ref:
        blockers.append(
            VerificationActivationBlocker.OPERATOR_OUTCOME_SINK_UNAPPROVED
        )
    if blockers:
        return VerificationActivationDecision(
            VerificationActivationCode.BLOCKED, tuple(blockers)
        )
    return VerificationActivationDecision(VerificationActivationCode.READY, ())
