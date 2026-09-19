"""P10-D3D-1C V1/V2 protection fence unit contract."""

from position_identity import (
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    ExchangePositionKey,
    SlotAuthority,
)
from position_protection.claim import (
    ClaimAcquireCode,
    ClaimAcquireResult,
    ClaimReleaseCode,
    ClaimReleaseResult,
    ClaimValidateCode,
    ClaimValidateResult,
    ProtectionMutationClaim,
)
from position_protection.fence import FenceCode, ProtectionTaskFence
from position_protection.task import AlgoProtectionTask

EPISODE_ID = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo',
        environment='DEMO',
        symbol=symbol,
    )


def _task(**overrides):
    values = {
        'symbol': 'BTCUSDT', 'side': 'SELL', 'trigger_price': 90, 'qty': 2,
        'exchange_position_key': _slot(), 'episode_id': EPISODE_ID,
        'slot_generation': 3, 'protection_generation': 8,
    }
    values.update(overrides)
    return AlgoProtectionTask(**values)


def _authority(**overrides):
    values = {
        'exchange_position_key': _slot(), 'episode_id': EPISODE_ID,
        'slot_generation': 3, 'status': AuthorityStatus.ACTIVE,
        'provenance': AuthorityProvenance.NATIVE, 'revision': 4,
        'legacy_position_id_alias': None, 'created_at': 1, 'updated_at': 2,
    }
    values.update(overrides)
    return SlotAuthority(**values)


class Reader:
    def __init__(self, result):
        self.result = result

    def get_slot_authority(self, key):
        return self.result


class Claims:
    def __init__(self):
        self.acquire_code = ClaimAcquireCode.ACQUIRED
        self.validate_code = ClaimValidateCode.VALID
        self.released = []

    def acquire(self, task, *, expected_authority_revision):
        if self.acquire_code is not ClaimAcquireCode.ACQUIRED:
            return ClaimAcquireResult(self.acquire_code)
        return ClaimAcquireResult(
            ClaimAcquireCode.ACQUIRED,
            ProtectionMutationClaim(
                task.exchange_position_key, task.episode_id,
                task.slot_generation, task.protection_generation,
                expected_authority_revision, 'worker-A', 9,
            ),
        )

    def validate(self, claim):
        return ClaimValidateResult(self.validate_code)

    def release(self, claim):
        self.released.append(claim)
        return ClaimReleaseResult(ClaimReleaseCode.RELEASED)


def _fence(authority_result=None, claims=None):
    authority_result = authority_result or AuthorityReadResult(
        AuthorityReadCode.FOUND, _authority())
    return ProtectionTaskFence(
        authority_reader=Reader(authority_result),
        claim_store=claims or Claims(),
    )


def test_v1_admits_exact_active_authority_and_v2_renews_claim():
    fence = _fence()
    admitted = fence.acquire(_task())
    assert admitted.code is FenceCode.ADMITTED
    assert admitted.claim.authority_revision == 4
    assert fence.validate(admitted.claim).code is FenceCode.ADMITTED


def test_v1_rejects_unreadable_inactive_episode_and_generation_mismatch():
    backend = AuthorityReadResult(
        AuthorityReadCode.BACKEND_ERROR, message='redis down')
    assert _fence(backend).acquire(_task()).code \
        is FenceCode.AUTHORITY_UNAVAILABLE

    inactive = AuthorityReadResult(
        AuthorityReadCode.FOUND,
        _authority(status=AuthorityStatus.QUARANTINED),
    )
    assert _fence(inactive).acquire(_task()).code \
        is FenceCode.AUTHORITY_INACTIVE

    different_episode = AuthorityReadResult(
        AuthorityReadCode.FOUND,
        _authority(episode_id='episode-B'),
    )
    assert _fence(different_episode).acquire(_task()).code \
        is FenceCode.EPISODE_MISMATCH

    different_generation = AuthorityReadResult(
        AuthorityReadCode.FOUND,
        _authority(slot_generation=4),
    )
    assert _fence(different_generation).acquire(_task()).code \
        is FenceCode.SLOT_GENERATION_MISMATCH


def test_v1_claim_busy_and_v2_claim_loss_fail_closed():
    claims = Claims()
    claims.acquire_code = ClaimAcquireCode.BUSY
    assert _fence(claims=claims).acquire(_task()).code is FenceCode.CLAIM_BUSY

    claims.acquire_code = ClaimAcquireCode.ACQUIRED
    fence = _fence(claims=claims)
    admitted = fence.acquire(_task())
    claims.validate_code = ClaimValidateCode.STALE
    assert fence.validate(admitted.claim).code is FenceCode.CLAIM_LOST


def test_release_delegates_exact_claim():
    claims = Claims()
    fence = _fence(claims=claims)
    claim = fence.acquire(_task()).claim
    result = fence.release(claim)
    assert result.code is ClaimReleaseCode.RELEASED
    assert claims.released == [claim]
