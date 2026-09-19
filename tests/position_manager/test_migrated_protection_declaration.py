"""Migrated desired-protection declaration tests."""
from dataclasses import replace
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
    MigratedProtectionDeclarationService,
)
from position_protection.migrated_plan import (
    MigratedProtectionPlan,
    MigratedProtectionPlanCode,
)


def _desired():
    return DesiredProtectionRecord.initial_pending(
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="declare", environment="SANDBOX", symbol="BTCUSDT"
        ),
        episode_id=str(uuid4()), slot_generation=1,
        desired_intent_id="migrated", trigger_price=90, covered_quantity=2,
        closing_side="SELL", operation_id="declare-1", now=1,
    ).transition(
        status="PENDING", operation_id="declare-1", now=1,
        exchange_algo_aliases=("77",), allow_same_status_alias_repair=True,
    )


class Port:
    def __init__(self, result):
        self.result, self.calls = result, []

    def declare(self, record):
        self.calls.append(record)
        return self.result


def test_exact_applied_and_already_applied_records_permit_verification():
    for code in (DesiredWriteCode.APPLIED, DesiredWriteCode.ALREADY_APPLIED):
        desired = _desired()
        port = Port(DesiredWriteResult(code, desired))
        result = MigratedProtectionDeclarationService(port).declare(
            MigratedProtectionPlan(MigratedProtectionPlanCode.READY, desired)
        )
        assert result.code is MigratedDeclarationCode.DECLARED
        assert result.may_verify
        assert port.calls == [desired]


def test_applied_code_with_non_exact_record_quarantines():
    desired = _desired()
    different = replace(desired, desired_intent_id="other")
    result = MigratedProtectionDeclarationService(
        Port(DesiredWriteResult(DesiredWriteCode.ALREADY_APPLIED, different))
    ).declare(MigratedProtectionPlan(MigratedProtectionPlanCode.READY, desired))
    assert result.code is MigratedDeclarationCode.QUARANTINE
    assert not result.may_verify


@pytest.mark.parametrize("write_code, expected", [
    (DesiredWriteCode.STALE, MigratedDeclarationCode.RELOAD_DESIRED),
    (DesiredWriteCode.CONFLICT, MigratedDeclarationCode.RELOAD_DESIRED),
    (DesiredWriteCode.UNAVAILABLE, MigratedDeclarationCode.RETRY_WHEN_AVAILABLE),
    (DesiredWriteCode.UNKNOWN, MigratedDeclarationCode.RESOLVE_UNKNOWN),
    (DesiredWriteCode.MALFORMED, MigratedDeclarationCode.QUARANTINE),
    (DesiredWriteCode.INVALID, MigratedDeclarationCode.REJECT),
])
def test_non_ack_write_codes_remain_distinct(write_code, expected):
    desired = _desired()
    result = MigratedProtectionDeclarationService(
        Port(DesiredWriteResult(write_code))
    ).declare(MigratedProtectionPlan(MigratedProtectionPlanCode.READY, desired))
    assert result.code is expected
    assert not result.may_verify


def test_blocked_plan_never_calls_port_and_bad_port_contract_is_not_swallowed():
    port = Port(DesiredWriteResult(DesiredWriteCode.APPLIED))
    blocked = MigratedProtectionPlan(MigratedProtectionPlanCode.QUARANTINE)
    result = MigratedProtectionDeclarationService(port).declare(blocked)
    assert result.code is MigratedDeclarationCode.PLAN_BLOCKED
    assert port.calls == []
    with pytest.raises(TypeError):
        MigratedProtectionDeclarationService(Port(None)).declare(
            MigratedProtectionPlan(MigratedProtectionPlanCode.READY, _desired())
        )
