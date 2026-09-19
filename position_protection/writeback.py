"""Typed R3 conditional protection-alias writeback results."""
from dataclasses import dataclass
from enum import Enum

from position_protection.desired import DesiredProtectionRecord


class WritebackCode(str, Enum):
    APPLIED = 'APPLIED'
    ALREADY_APPLIED = 'ALREADY_APPLIED'
    STALE = 'STALE'
    CLAIM_LOST = 'CLAIM_LOST'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    CONFLICT = 'CONFLICT'
    INVALID = 'INVALID'
    UNKNOWN = 'UNKNOWN'


@dataclass(frozen=True)
class WritebackResult:
    code: WritebackCode
    desired: DesiredProtectionRecord | None = None
    message: str = ''

    @property
    def applied(self) -> bool:
        return self.code in (
            WritebackCode.APPLIED, WritebackCode.ALREADY_APPLIED)
