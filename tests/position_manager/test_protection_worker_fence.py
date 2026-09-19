"""P10-D3D-1C worker V1/V2 ordering and fail-closed integration."""
from position_identity import ExchangePositionKey
from position_protection.claim import (
    ClaimReleaseCode,
    ClaimReleaseResult,
    ProtectionMutationClaim,
)
from position_protection.fence import FenceCode, FenceResult
from position_protection.task import AlgoProtectionTask
from shared import position_manager as pm

EPISODE_ID = '6a4717d8-7390-4386-9878-56ff2981f93d'


class _NoExchangeInfo:
    status_code = 503


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo',
        environment='DEMO',
        symbol='BTCUSDT',
    )


def _task():
    return AlgoProtectionTask(
        symbol='BTCUSDT', side='SELL', trigger_price=90, qty=2,
        exchange_position_key=_slot(), episode_id=EPISODE_ID,
        slot_generation=3, protection_generation=8,
    )


def _claim():
    return ProtectionMutationClaim(
        exchange_position_key=_slot(), episode_id=EPISODE_ID,
        slot_generation=3, protection_generation=8,
        authority_revision=4, owner_token='worker-A', fencing_token=9,
    )


class FakeFence:
    def __init__(self, events, *, v1=FenceCode.ADMITTED,
                 v2=FenceCode.ADMITTED):
        self.events = events
        self.v1 = v1
        self.v2 = v2

    def acquire(self, task):
        self.events.append('v1')
        claim = _claim() if self.v1 is FenceCode.ADMITTED else None
        return FenceResult(self.v1, claim)

    def validate(self, claim):
        self.events.append('v2')
        admitted = claim if self.v2 is FenceCode.ADMITTED else None
        return FenceResult(self.v2, admitted)

    def release(self, claim):
        self.events.append('release')
        return ClaimReleaseResult(ClaimReleaseCode.RELEASED)


def test_v1_rejection_never_enters_exchange_placement(monkeypatch):
    events = []
    monkeypatch.setattr(
        pm, '_algo_task_fence',
        lambda: FakeFence(events, v1=FenceCode.EPISODE_MISMATCH),
    )
    monkeypatch.setattr(
        pm, '_algo_place_sl_inner',
        lambda *args, **kwargs: events.append('place'),
    )
    logs = []
    monkeypatch.setattr(pm, '_pmlog', logs.append)

    result = pm._algo_execute_fenced_task(_task())

    assert result == {'error': 'EPISODE_MISMATCH'}
    assert events == ['v1']
    assert 'reason=V1_EPISODE_MISMATCH' in logs[0]


def test_v2_rejection_occurs_after_cancel_and_before_create(monkeypatch):
    events = []
    monkeypatch.setattr(
        pm, '_algo_task_fence',
        lambda: FakeFence(events, v2=FenceCode.CLAIM_LOST),
    )
    monkeypatch.setattr(pm.requests, 'get', lambda *args, **kwargs: _NoExchangeInfo())
    monkeypatch.setattr(
        pm, '_cancel_all_algo', lambda symbol: events.append('cancel'))
    monkeypatch.setattr(
        pm, '_light_fapi_post',
        lambda *args, **kwargs: events.append('create') or {'algoId': 10},
    )
    monkeypatch.setattr(pm, '_pmlog', lambda message: None)

    result = pm._algo_execute_fenced_task(_task())

    assert result == {'error': 'PROTECTION_FENCE_REJECTED_BEFORE_CREATE'}
    assert events == ['v1', 'cancel', 'v2', 'release']


def test_v1_v2_success_preserves_cancel_then_create_order(monkeypatch):
    events = []
    monkeypatch.setattr(
        pm, '_algo_task_fence', lambda: FakeFence(events))
    monkeypatch.setattr(pm.requests, 'get', lambda *args, **kwargs: _NoExchangeInfo())
    monkeypatch.setattr(
        pm, '_cancel_all_algo', lambda symbol: events.append('cancel'))
    monkeypatch.setattr(
        pm, '_light_fapi_post',
        lambda *args, **kwargs: events.append('create') or {'code': 0},
    )
    monkeypatch.setattr(pm, '_pmlog', lambda message: None)

    result = pm._algo_execute_fenced_task(_task())

    assert result == {'code': 0}
    assert events == ['v1', 'cancel', 'v2', 'create', 'release']
