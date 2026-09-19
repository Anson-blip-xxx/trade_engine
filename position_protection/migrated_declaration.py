"""One-shot durable declaration boundary for migrated protection plans."""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from position_protection.desired import DesiredWriteCode, DesiredWriteResult
from position_protection.migrated_plan import (
    MigratedProtectionPlan,
    MigratedProtectionPlanCode,
)


class MigratedDeclarationCode(str, Enum):
    DECLARED = "DECLARED"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    RELOAD_DESIRED = "RELOAD_DESIRED"
    RETRY_WHEN_AVAILABLE = "RETRY_WHEN_AVAILABLE"
    RESOLVE_UNKNOWN = "RESOLVE_UNKNOWN"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class MigratedDeclarationResult:
    code: MigratedDeclarationCode
    plan: MigratedProtectionPlan
    write: DesiredWriteResult | None = None

    @property
    def may_verify(self) -> bool:
        return self.code is MigratedDeclarationCode.DECLARED


class DesiredDeclarationPort(Protocol):
    def declare(self, record) -> DesiredWriteResult: ...


class MigratedProtectionDeclarationService:
    """Declare exactly once; verification is permitted only after an exact ACK."""

    def __init__(self, port: DesiredDeclarationPort) -> None:
        if not callable(getattr(port, "declare", None)):
            raise TypeError("port must provide callable declare")
        self._port = port

    def declare(self, plan: MigratedProtectionPlan) -> MigratedDeclarationResult:
        if not isinstance(plan, MigratedProtectionPlan):
            raise TypeError("plan must be MigratedProtectionPlan")
        if plan.code is not MigratedProtectionPlanCode.READY or plan.desired is None:
            return MigratedDeclarationResult(MigratedDeclarationCode.PLAN_BLOCKED, plan)
        write = self._port.declare(plan.desired)
        if not isinstance(write, DesiredWriteResult):
            raise TypeError("port must return DesiredWriteResult")
        if write.applied:
            code = (
                MigratedDeclarationCode.DECLARED
                if write.record == plan.desired
                else MigratedDeclarationCode.QUARANTINE
            )
        elif write.code in (DesiredWriteCode.STALE, DesiredWriteCode.CONFLICT):
            code = MigratedDeclarationCode.RELOAD_DESIRED
        elif write.code is DesiredWriteCode.UNAVAILABLE:
            code = MigratedDeclarationCode.RETRY_WHEN_AVAILABLE
        elif write.code is DesiredWriteCode.UNKNOWN:
            code = MigratedDeclarationCode.RESOLVE_UNKNOWN
        elif write.code in (DesiredWriteCode.MALFORMED, DesiredWriteCode.NOT_FOUND):
            code = MigratedDeclarationCode.QUARANTINE
        else:
            code = MigratedDeclarationCode.REJECT
        return MigratedDeclarationResult(code, plan, write)
