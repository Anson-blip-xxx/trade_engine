"""P10-D3D-1C deterministic Redis mutation-claim contract tests."""
import json
import threading

from position_identity import (
    AuthorityProvenance,
    AuthorityStatus,
    ExchangePositionKey,
    SlotAuthority,
)
from position_protection.claim import (
    ClaimAcquireCode,
    ClaimReleaseCode,
    ClaimValidateCode,
)
from position_protection.claim_redis import (
    ACQUIRE_PROTECTION_CLAIM_LUA,
    RELEASE_PROTECTION_CLAIM_LUA,
    VALIDATE_PROTECTION_CLAIM_LUA,
    RedisProtectionMutationClaimAdapter,
)
from position_protection.task import AlgoProtectionTask

EPISODE_ID = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo',
        environment='DEMO',
        symbol='BTCUSDT',
    )


def _authority(*, revision=4, status=AuthorityStatus.ACTIVE):
    return SlotAuthority(
        exchange_position_key=_slot(),
        episode_id=EPISODE_ID,
        slot_generation=3,
        status=status,
        provenance=AuthorityProvenance.NATIVE,
        revision=revision,
        legacy_position_id_alias=None,
        created_at=1,
        updated_at=2,
    )


def _task(*, protection_generation=8):
    return AlgoProtectionTask(
        symbol='BTCUSDT', side='SELL', trigger_price=90, qty=2,
        exchange_position_key=_slot(), episode_id=EPISODE_ID,
        slot_generation=3, protection_generation=protection_generation,
    )


class FakeClaimRedis:
    """Atomic in-memory implementation of the three injected Lua seams."""

    def __init__(self):
        self.data = {_slot().to_storage_key(): _authority().to_json()}
        self.counters = {}
        self.lock = threading.Lock()
        self.raise_eval = False
        self.renewals = []

    def eval(self, script, numkeys, *values):
        if self.raise_eval:
            raise RuntimeError('redis unavailable')
        keys = values[:numkeys]
        args = values[numkeys:]
        with self.lock:
            if script == ACQUIRE_PROTECTION_CLAIM_LUA:
                return self._acquire(keys, args)
            if script == VALIDATE_PROTECTION_CLAIM_LUA:
                return self._validate(keys, args)
            if script == RELEASE_PROTECTION_CLAIM_LUA:
                return self._release(keys, args)
        raise AssertionError('unexpected script')

    def _read_authority(self, key):
        raw = self.data.get(key)
        if raw is None:
            return None, 'NOT_FOUND'
        try:
            return SlotAuthority.from_json(raw), None
        except (TypeError, ValueError):
            return None, 'MALFORMED'

    def _acquire(self, keys, args):
        authority_key, claim_key, fence_key = keys
        revision, episode, generation, protection, owner, lease = args
        authority, error = self._read_authority(authority_key)
        if error:
            return [error, '']
        if str(authority.revision) != revision \
                or authority.status is not AuthorityStatus.ACTIVE \
                or authority.episode_id != episode \
                or str(authority.slot_generation) != generation:
            return ['STALE', '']
        if claim_key in self.data:
            return ['BUSY', '']
        token = self.counters.get(fence_key, 0) + 1
        self.counters[fence_key] = token
        self.data[claim_key] = json.dumps({
            'schema_version': 1,
            'owner_token': owner,
            'fencing_token': token,
            'authority_revision': int(revision),
            'episode_id': episode,
            'slot_generation': int(generation),
            'protection_generation': int(protection),
            'lease_ms': int(lease),
        })
        return ['ACQUIRED', str(token)]

    def _validate(self, keys, args):
        authority_key, claim_key = keys
        revision, episode, generation, protection, owner, token, lease = args
        raw_claim = self.data.get(claim_key)
        if raw_claim is None:
            return ['LOST', '']
        authority, error = self._read_authority(authority_key)
        if error:
            return ['STALE' if error == 'NOT_FOUND' else error, '']
        claim = json.loads(raw_claim)
        if str(authority.revision) != revision \
                or authority.status is not AuthorityStatus.ACTIVE \
                or authority.episode_id != episode \
                or str(authority.slot_generation) != generation:
            return ['STALE', '']
        if claim != {
            **claim,
            'schema_version': 1,
            'owner_token': owner,
            'fencing_token': int(token),
            'authority_revision': int(revision),
            'episode_id': episode,
            'slot_generation': int(generation),
            'protection_generation': int(protection),
        }:
            return ['LOST', '']
        self.renewals.append((claim_key, int(lease)))
        return ['VALID', '']

    def _release(self, keys, args):
        claim_key = keys[0]
        owner, token = args
        raw_claim = self.data.get(claim_key)
        if raw_claim is None:
            return ['NOT_OWNER', '']
        claim = json.loads(raw_claim)
        if claim['owner_token'] != owner \
                or claim['fencing_token'] != int(token):
            return ['NOT_OWNER', '']
        del self.data[claim_key]
        return ['RELEASED', '']


def _adapter(fake, owner='worker-A'):
    return RedisProtectionMutationClaimAdapter(
        redis_eval=fake.eval,
        owner_token_factory=lambda: owner,
        lease_ms=12_000,
    )


def test_claim_acquire_validate_release_round_trip():
    fake = FakeClaimRedis()
    adapter = _adapter(fake)
    acquired = adapter.acquire(_task(), expected_authority_revision=4)
    assert acquired.code is ClaimAcquireCode.ACQUIRED
    assert acquired.claim.fencing_token == 1
    assert acquired.claim.protection_generation == 8
    assert adapter.validate(acquired.claim).code is ClaimValidateCode.VALID
    assert fake.renewals == [(
        adapter._claim_key(_slot()), 12_000,
    )]
    assert adapter.release(acquired.claim).code is ClaimReleaseCode.RELEASED


def test_claim_serializes_workers_and_fencing_token_never_reuses():
    fake = FakeClaimRedis()
    first_adapter = _adapter(fake, 'worker-A')
    second_adapter = _adapter(fake, 'worker-B')
    first = first_adapter.acquire(_task(), expected_authority_revision=4)
    busy = second_adapter.acquire(_task(), expected_authority_revision=4)
    assert busy.code is ClaimAcquireCode.BUSY
    first_adapter.release(first.claim)
    second = second_adapter.acquire(_task(), expected_authority_revision=4)
    assert second.code is ClaimAcquireCode.ACQUIRED
    assert second.claim.fencing_token == 2


def test_v2_rejects_authority_change_and_lost_claim():
    fake = FakeClaimRedis()
    adapter = _adapter(fake)
    acquired = adapter.acquire(_task(), expected_authority_revision=4)
    fake.data[_slot().to_storage_key()] = _authority(revision=5).to_json()
    assert adapter.validate(acquired.claim).code is ClaimValidateCode.STALE
    fake.data[_slot().to_storage_key()] = _authority().to_json()
    del fake.data[adapter._claim_key(_slot())]
    assert adapter.validate(acquired.claim).code is ClaimValidateCode.LOST


def test_backend_and_malformed_responses_fail_closed():
    fake = FakeClaimRedis()
    adapter = _adapter(fake)
    fake.raise_eval = True
    result = adapter.acquire(_task(), expected_authority_revision=4)
    assert result.code is ClaimAcquireCode.BACKEND_ERROR

    broken = RedisProtectionMutationClaimAdapter(
        redis_eval=lambda *args: ['UNKNOWN', ''],
    )
    result = broken.acquire(_task(), expected_authority_revision=4)
    assert result.code is ClaimAcquireCode.BACKEND_ERROR


def test_lua_claim_is_authority_bound_leased_and_owner_released():
    assert "authority['status'] ~= 'ACTIVE'" in ACQUIRE_PROTECTION_CLAIM_LUA
    assert "redis.call('INCR', KEYS[3])" in ACQUIRE_PROTECTION_CLAIM_LUA
    assert "'PX', ARGV[6]" in ACQUIRE_PROTECTION_CLAIM_LUA
    assert "claim['protection_generation']" in VALIDATE_PROTECTION_CLAIM_LUA
    assert "redis.call('PEXPIRE', KEYS[2], ARGV[7])" \
        in VALIDATE_PROTECTION_CLAIM_LUA
    assert "claim['owner_token'] ~= ARGV[1]" in RELEASE_PROTECTION_CLAIM_LUA
    assert "redis.call('DEL', KEYS[1])" in RELEASE_PROTECTION_CLAIM_LUA
