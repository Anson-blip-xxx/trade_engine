"""Redis-backed protection mutation claim with authority-bound fencing.

The adapter is IO-free at import time. Callers inject an EVAL seam. A claim is
not exchange atomicity; it serializes cooperating workers and makes V2 reject
authority changes or lease loss before create.
"""
from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from position_protection.claim import (
    ClaimAcquireCode,
    ClaimAcquireResult,
    ClaimReleaseCode,
    ClaimReleaseResult,
    ClaimValidateCode,
    ClaimValidateResult,
    ProtectionMutationClaim,
)
from position_protection.task import AlgoProtectionTask

ACQUIRE_PROTECTION_CLAIM_LUA = r'''
local authority_raw = redis.call('GET', KEYS[1])
if not authority_raw then
  return {'NOT_FOUND', ''}
end
local authority_ok, authority = pcall(cjson.decode, authority_raw)
if not authority_ok or type(authority) ~= 'table'
    or authority['schema_version'] ~= 1
    or type(authority['revision']) ~= 'number'
    or type(authority['slot_generation']) ~= 'number'
    or type(authority['status']) ~= 'string' then
  return {'MALFORMED', ''}
end
if tostring(authority['revision']) ~= ARGV[1]
    or authority['status'] ~= 'ACTIVE'
    or authority['episode_id'] ~= ARGV[2]
    or tostring(authority['slot_generation']) ~= ARGV[3] then
  return {'STALE', ''}
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  return {'BUSY', ''}
end
local fencing_token = redis.call('INCR', KEYS[3])
local claim = cjson.encode({
  schema_version = 1,
  owner_token = ARGV[5],
  fencing_token = fencing_token,
  authority_revision = tonumber(ARGV[1]),
  episode_id = ARGV[2],
  slot_generation = tonumber(ARGV[3]),
  protection_generation = tonumber(ARGV[4])
})
redis.call('SET', KEYS[2], claim, 'PX', ARGV[6])
return {'ACQUIRED', tostring(fencing_token)}
'''


VALIDATE_PROTECTION_CLAIM_LUA = r'''
local authority_raw = redis.call('GET', KEYS[1])
local claim_raw = redis.call('GET', KEYS[2])
if not claim_raw then
  return {'LOST', ''}
end
if not authority_raw then
  return {'STALE', ''}
end
local authority_ok, authority = pcall(cjson.decode, authority_raw)
local claim_ok, claim = pcall(cjson.decode, claim_raw)
if not authority_ok or type(authority) ~= 'table'
    or authority['schema_version'] ~= 1
    or not claim_ok or type(claim) ~= 'table'
    or claim['schema_version'] ~= 1 then
  return {'MALFORMED', ''}
end
if tostring(authority['revision']) ~= ARGV[1]
    or authority['status'] ~= 'ACTIVE'
    or authority['episode_id'] ~= ARGV[2]
    or tostring(authority['slot_generation']) ~= ARGV[3] then
  return {'STALE', ''}
end
if claim['owner_token'] ~= ARGV[5]
    or tostring(claim['fencing_token']) ~= ARGV[6]
    or tostring(claim['authority_revision']) ~= ARGV[1]
    or claim['episode_id'] ~= ARGV[2]
    or tostring(claim['slot_generation']) ~= ARGV[3]
    or tostring(claim['protection_generation']) ~= ARGV[4] then
  return {'LOST', ''}
end
redis.call('PEXPIRE', KEYS[2], ARGV[7])
return {'VALID', ''}
'''


RELEASE_PROTECTION_CLAIM_LUA = r'''
local claim_raw = redis.call('GET', KEYS[1])
if not claim_raw then
  return {'NOT_OWNER', ''}
end
local claim_ok, claim = pcall(cjson.decode, claim_raw)
if not claim_ok or type(claim) ~= 'table'
    or claim['owner_token'] ~= ARGV[1]
    or tostring(claim['fencing_token']) ~= ARGV[2] then
  return {'NOT_OWNER', ''}
end
redis.call('DEL', KEYS[1])
return {'RELEASED', ''}
'''


RedisEval = Callable[..., object]
OwnerTokenFactory = Callable[[], str]


class RedisProtectionMutationClaimAdapter:
    """Atomic acquire/validate/release for one physical slot."""

    CLAIM_KEY_PREFIX = 'pm:protection-claim:v1'
    FENCE_KEY_PREFIX = 'pm:protection-fence:v1'

    def __init__(
            self, *, redis_eval: RedisEval,
            owner_token_factory: OwnerTokenFactory | None = None,
            lease_ms: int = 30_000) -> None:
        if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) \
                or lease_ms < 1:
            raise ValueError('lease_ms must be a positive integer')
        self._redis_eval = redis_eval
        self._owner_token_factory = owner_token_factory or (
            lambda: str(uuid4()))
        self._lease_ms = lease_ms

    def acquire(
            self, task: AlgoProtectionTask, *,
            expected_authority_revision: int) -> ClaimAcquireResult:
        if not isinstance(task, AlgoProtectionTask):
            raise TypeError('task must be AlgoProtectionTask')
        if isinstance(expected_authority_revision, bool) \
                or not isinstance(expected_authority_revision, int) \
                or expected_authority_revision < 1:
            raise ValueError('expected_authority_revision must be positive')
        owner_token = self._owner_token_factory()
        if not isinstance(owner_token, str) or not owner_token.strip():
            raise ValueError('owner token factory returned an invalid token')
        owner_token = owner_token.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in owner_token):
            raise ValueError('owner token contains control characters')
        try:
            response = self._redis_eval(
                ACQUIRE_PROTECTION_CLAIM_LUA, 3,
                task.exchange_position_key.to_storage_key(),
                self._claim_key(task.exchange_position_key),
                self._fence_key(task.exchange_position_key),
                str(expected_authority_revision), task.episode_id,
                str(task.slot_generation), str(task.protection_generation),
                owner_token, str(self._lease_ms),
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return ClaimAcquireResult(
                ClaimAcquireCode.BACKEND_ERROR, message=str(exc))
        code, raw_token, error = self._response(response, ClaimAcquireCode)
        if error:
            return ClaimAcquireResult(
                ClaimAcquireCode.BACKEND_ERROR, message=error)
        if code is not ClaimAcquireCode.ACQUIRED:
            return ClaimAcquireResult(code)
        try:
            fencing_token = int(raw_token)
            if fencing_token < 1:
                raise ValueError
        except (TypeError, ValueError):
            return ClaimAcquireResult(
                ClaimAcquireCode.BACKEND_ERROR,
                message='invalid claim fencing token',
            )
        return ClaimAcquireResult(
            code,
            ProtectionMutationClaim(
                exchange_position_key=task.exchange_position_key,
                episode_id=task.episode_id,
                slot_generation=task.slot_generation,
                protection_generation=task.protection_generation,
                authority_revision=expected_authority_revision,
                owner_token=owner_token,
                fencing_token=fencing_token,
            ),
        )

    def validate(
            self, claim: ProtectionMutationClaim) -> ClaimValidateResult:
        if not isinstance(claim, ProtectionMutationClaim):
            raise TypeError('claim must be ProtectionMutationClaim')
        try:
            response = self._redis_eval(
                VALIDATE_PROTECTION_CLAIM_LUA, 2,
                claim.exchange_position_key.to_storage_key(),
                self._claim_key(claim.exchange_position_key),
                str(claim.authority_revision), claim.episode_id,
                str(claim.slot_generation),
                str(claim.protection_generation), claim.owner_token,
                str(claim.fencing_token), str(self._lease_ms),
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return ClaimValidateResult(
                ClaimValidateCode.BACKEND_ERROR, message=str(exc))
        code, _value, error = self._response(response, ClaimValidateCode)
        if error:
            return ClaimValidateResult(
                ClaimValidateCode.BACKEND_ERROR, message=error)
        return ClaimValidateResult(code)

    def release(
            self, claim: ProtectionMutationClaim) -> ClaimReleaseResult:
        if not isinstance(claim, ProtectionMutationClaim):
            raise TypeError('claim must be ProtectionMutationClaim')
        try:
            response = self._redis_eval(
                RELEASE_PROTECTION_CLAIM_LUA, 1,
                self._claim_key(claim.exchange_position_key),
                claim.owner_token, str(claim.fencing_token),
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return ClaimReleaseResult(
                ClaimReleaseCode.BACKEND_ERROR, message=str(exc))
        code, _value, error = self._response(response, ClaimReleaseCode)
        if error:
            return ClaimReleaseResult(
                ClaimReleaseCode.BACKEND_ERROR, message=error)
        return ClaimReleaseResult(code)

    @staticmethod
    def _response(response, enum_type):
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return None, None, 'invalid Redis claim response'
        raw_code, value = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode('utf-8')
            except UnicodeError:
                return None, None, 'claim response code is not valid UTF-8'
        if isinstance(value, bytes):
            try:
                value = value.decode('utf-8')
            except UnicodeError:
                return None, None, 'claim response value is not valid UTF-8'
        try:
            return enum_type(raw_code), value, ''
        except (TypeError, ValueError):
            return None, None, f'unknown Redis claim response: {raw_code!r}'

    @classmethod
    def _claim_key(cls, key):
        return f'{cls.CLAIM_KEY_PREFIX}:{key.canonical_digest()}'

    @classmethod
    def _fence_key(cls, key):
        return f'{cls.FENCE_KEY_PREFIX}:{key.canonical_digest()}'
