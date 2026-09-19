"""Exact declaration to one-step exchange verification composition tests."""
from uuid import uuid4

import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    DesiredWriteCode,
    DesiredWriteResult,
)
from position_protection.migrated_declaration import (
    MigratedDeclarationCode,
    MigratedDeclarationResult,
)
from position_protection.migrated_plan import (
    MigratedProtectionPlan,
    MigratedProtectionPlanCode,
)
from position_protection.migrated_verification import (
    MigratedProtectionVerificationService,
)
from position_protection.verification_coordinator import (
    VerificationCoordinatorCode,
    VerificationCoordinatorResult,
    VerificationSession,
)


def _declaration(code=MigratedDeclarationCode.DECLARED):
    desired = DesiredProtectionRecord.initial_pending(
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="verify-migrated", environment="SANDBOX", symbol="BTCUSDT"
        ), episode_id=str(uuid4()), slot_generation=1, desired_intent_id="stop",
        trigger_price=90, covered_quantity=2, closing_side="SELL",
        operation_id="declare", now=1,
    )
    plan = MigratedProtectionPlan(MigratedProtectionPlanCode.READY, desired)
    write = DesiredWriteResult(DesiredWriteCode.APPLIED, desired)
    return MigratedDeclarationResult(code, plan, write)


class Coordinator:
    def __init__(self, code):
        self.code, self.calls = code, []

    def step(self, **kwargs):
        self.calls.append(kwargs)
        return VerificationCoordinatorResult(self.code, kwargs["session"])


@pytest.mark.parametrize("code", tuple(VerificationCoordinatorCode))
def test_every_coordinator_result_is_preserved_after_exact_declaration(code):
    coordinator = Coordinator(code)
    service = MigratedProtectionVerificationService(coordinator)
    declaration = _declaration()
    session = VerificationSession("verify-migrated", 2)
    result = service.verify_once(
        declaration=declaration, session=session, now=3, max_evidence_age=5,
        quantity_tolerance=0.1, trigger_tolerance=0.2,
    )
    assert result.verification.code is code
    assert result.active is (code is VerificationCoordinatorCode.ACTIVE)
    assert coordinator.calls == [{
        "session": session,
        "exchange_position_key": declaration.plan.desired.exchange_position_key,
        "now": 3,
        "max_evidence_age": 5,
        "quantity_tolerance": 0.1,
        "trigger_tolerance": 0.2,
    }]


@pytest.mark.parametrize("code", tuple(c for c in MigratedDeclarationCode if c is not MigratedDeclarationCode.DECLARED))
def test_non_exact_declaration_never_queries_exchange(code):
    coordinator = Coordinator(VerificationCoordinatorCode.ACTIVE)
    result = MigratedProtectionVerificationService(coordinator).verify_once(
        declaration=_declaration(code), session=VerificationSession("blocked", 1),
        now=2, max_evidence_age=1, quantity_tolerance=0, trigger_tolerance=0,
    )
    assert result.verification is None
    assert not result.active
    assert coordinator.calls == []


def test_invalid_coordinator_result_is_not_coerced():
    coordinator = type("Bad", (), {"step": lambda self, **kwargs: None})()
    with pytest.raises(TypeError):
        MigratedProtectionVerificationService(coordinator).verify_once(
            declaration=_declaration(), session=VerificationSession("bad", 1),
            now=2, max_evidence_age=1, quantity_tolerance=0, trigger_tolerance=0,
        )
