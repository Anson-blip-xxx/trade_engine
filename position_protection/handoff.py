"""Typed results for the R4 native-open protection handoff."""

from dataclasses import dataclass
from enum import Enum

from position_identity.authority import SlotAuthority
from position_identity.projection import LivePositionProjection
from position_protection.desired import DesiredProtectionRecord


class NativeOpenHandoffCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    CONFLICT = "CONFLICT"
    MALFORMED = "MALFORMED"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NativeOpenHandoffResult:
    code: NativeOpenHandoffCode
    authority: SlotAuthority | None = None
    projection: LivePositionProjection | None = None
    desired: DesiredProtectionRecord | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            NativeOpenHandoffCode.APPLIED,
            NativeOpenHandoffCode.ALREADY_APPLIED,
        )
