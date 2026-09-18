"""Injected Redis adapter for atomic slot-authority transitions.

Importing this module performs no IO and imports no Redis client. Callers inject
plain GET and EVAL command seams.
"""
from __future__ import annotations

from collections.abc import Callable

from position_identity.authority import (
    AuthorityAck,
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey

_NULL_EPISODE = '__P10_NULL_EPISODE__'


COMPARE_AND_TRANSITION_SLOT_LUA = r'''
local raw = redis.call('GET', KEYS[1])
local operation = ARGV[1]
local expected_revision = ARGV[2]
local expected_status = ARGV[3]
local expected_episode = ARGV[4]
local expected_generation = ARGV[5]
local new_json = ARGV[6]

local function decode_record(payload)
  local ok, record = pcall(cjson.decode, payload)
  if not ok or type(record) ~= 'table' then
    return nil
  end
  if record['schema_version'] ~= 1 then
    return nil
  end
  if type(record['revision']) ~= 'number'
      or type(record['slot_generation']) ~= 'number'
      or type(record['status']) ~= 'string' then
    return nil
  end
  return record
end

local new_record = decode_record(new_json)
if not new_record then
  return {'MALFORMED', ''}
end

if operation == 'INIT' then
  if raw then
    local current = decode_record(raw)
    if not current then
      return {'MALFORMED', raw}
    end
    if current['status'] == 'ACTIVE'
        or current['status'] == 'QUARANTINED' then
      return {'ALREADY_ACTIVE', raw}
    end
    return {'ALREADY_INITIALIZED', raw}
  end
  redis.call('SET', KEYS[1], new_json)
  return {'APPLIED', new_json}
end

if not raw then
  return {'NOT_FOUND', ''}
end
local current = decode_record(raw)
if not current then
  return {'MALFORMED', raw}
end

if tostring(current['revision']) ~= expected_revision
    or tostring(current['slot_generation']) ~= expected_generation then
  return {'CONFLICT', raw}
end

local current_episode = current['episode_id']
if expected_episode == '__P10_NULL_EPISODE__' then
  if current_episode ~= cjson.null then
    return {'CONFLICT', raw}
  end
elseif current_episode ~= expected_episode then
  return {'CONFLICT', raw}
end

if current['status'] ~= expected_status then
  return {'INVALID_STATE', raw}
end

redis.call('SET', KEYS[1], new_json)
return {'APPLIED', new_json}
'''


RedisGet = Callable[[str], str | bytes | None]
RedisEval = Callable[..., object]


class RedisSlotAuthorityAdapter:
    """Dedicated no-fallback authority adapter using one Lua CAS write seam."""

    def __init__(self, *, redis_get: RedisGet, redis_eval: RedisEval) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval

    def get_slot_authority(
            self, key: ExchangePositionKey) -> AuthorityReadResult:
        storage_key = self._storage_key(key)
        try:
            raw = self._redis_get(storage_key)
        except Exception as exc:  # noqa: BLE001 - injected backend boundary
            return AuthorityReadResult(
                AuthorityReadCode.BACKEND_ERROR, message=str(exc))
        if raw is None:
            return AuthorityReadResult(AuthorityReadCode.NOT_FOUND)
        try:
            authority = SlotAuthority.from_json(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return AuthorityReadResult(
                AuthorityReadCode.MALFORMED, message=str(exc))
        if authority.exchange_position_key != key:
            return AuthorityReadResult(
                AuthorityReadCode.MALFORMED,
                message='authority slot key does not match storage key',
            )
        return AuthorityReadResult(AuthorityReadCode.FOUND, authority)

    def initialize_flat(self, key: ExchangePositionKey, *, now: float) \
            -> AuthorityAck:
        authority = SlotAuthority.initial_flat(key, now=now)
        return self._transition(
            key=key,
            operation='INIT',
            expected_revision=None,
            expected_status=None,
            expected_episode_id=None,
            expected_slot_generation=None,
            new_authority=authority,
        )

    def allocate_new_episode(
            self, key: ExchangePositionKey, *, candidate_episode_id: str,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck:
        return self._allocate(
            key=key,
            candidate_episode_id=candidate_episode_id,
            provenance=AuthorityProvenance.NATIVE,
            status=AuthorityStatus.ACTIVE,
            legacy_position_id_alias=None,
            expected_revision=expected_revision,
            expected_slot_generation=expected_slot_generation,
            expected_episode_id=expected_episode_id,
            now=now,
        )

    def adopt_if_unowned(
            self, key: ExchangePositionKey, *, candidate_episode_id: str,
            legacy_position_id_alias: str,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck:
        return self._allocate(
            key=key,
            candidate_episode_id=candidate_episode_id,
            provenance=AuthorityProvenance.MIGRATED,
            status=AuthorityStatus.ACTIVE,
            legacy_position_id_alias=legacy_position_id_alias,
            expected_revision=expected_revision,
            expected_slot_generation=expected_slot_generation,
            expected_episode_id=expected_episode_id,
            now=now,
        )

    def create_reconstructed_quarantined(
            self, key: ExchangePositionKey, *, candidate_episode_id: str,
            legacy_position_id_alias: str | None,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck:
        return self._allocate(
            key=key,
            candidate_episode_id=candidate_episode_id,
            provenance=AuthorityProvenance.RECONSTRUCTED,
            status=AuthorityStatus.QUARANTINED,
            legacy_position_id_alias=legacy_position_id_alias,
            expected_revision=expected_revision,
            expected_slot_generation=expected_slot_generation,
            expected_episode_id=expected_episode_id,
            now=now,
        )

    def transition_active_to_flat(
            self, key: ExchangePositionKey, *, expected_episode_id: str,
            expected_slot_generation: int, expected_revision: int,
            now: float) -> AuthorityAck:
        current = self.get_slot_authority(key)
        if current.code is AuthorityReadCode.NOT_FOUND:
            return AuthorityAck(AuthorityAckCode.NOT_FOUND)
        if current.code is AuthorityReadCode.MALFORMED:
            return AuthorityAck(
                AuthorityAckCode.MALFORMED, message=current.message)
        if current.code is AuthorityReadCode.BACKEND_ERROR:
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR, message=current.message)
        authority = current.authority
        assert authority is not None
        if authority.revision != expected_revision \
                or authority.slot_generation != expected_slot_generation \
                or authority.episode_id != expected_episode_id:
            return AuthorityAck(AuthorityAckCode.CONFLICT, authority)
        if authority.status is not AuthorityStatus.ACTIVE:
            return AuthorityAck(AuthorityAckCode.INVALID_STATE, authority)
        new_authority = authority.transition_to_flat(now=now)
        return self._transition(
            key=key,
            operation='FLAT',
            expected_revision=expected_revision,
            expected_status=AuthorityStatus.ACTIVE,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            new_authority=new_authority,
        )

    def _allocate(
            self, *, key: ExchangePositionKey, candidate_episode_id: str,
            provenance: AuthorityProvenance, status: AuthorityStatus,
            legacy_position_id_alias: str | None,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck:
        current = self.get_slot_authority(key)
        if current.code is AuthorityReadCode.NOT_FOUND:
            return AuthorityAck(AuthorityAckCode.NOT_FOUND)
        if current.code is AuthorityReadCode.MALFORMED:
            return AuthorityAck(
                AuthorityAckCode.MALFORMED, message=current.message)
        if current.code is AuthorityReadCode.BACKEND_ERROR:
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR, message=current.message)
        authority = current.authority
        assert authority is not None
        if authority.revision != expected_revision \
                or authority.slot_generation != expected_slot_generation \
                or authority.episode_id != expected_episode_id:
            return AuthorityAck(AuthorityAckCode.CONFLICT, authority)
        if authority.status is not AuthorityStatus.FLAT:
            code = (AuthorityAckCode.ALREADY_ACTIVE
                    if authority.status in (
                        AuthorityStatus.ACTIVE, AuthorityStatus.QUARANTINED)
                    else AuthorityAckCode.INVALID_STATE)
            return AuthorityAck(code, authority)
        try:
            new_authority = authority.allocate_episode(
                episode_id=candidate_episode_id,
                provenance=provenance,
                status=status,
                legacy_position_id_alias=legacy_position_id_alias,
                now=now,
            )
        except (TypeError, ValueError) as exc:
            return AuthorityAck(AuthorityAckCode.INVALID_STATE, message=str(exc))
        return self._transition(
            key=key,
            operation='ALLOCATE',
            expected_revision=expected_revision,
            expected_status=AuthorityStatus.FLAT,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            new_authority=new_authority,
        )

    def _transition(
            self, *, key: ExchangePositionKey, operation: str,
            expected_revision: int | None,
            expected_status: AuthorityStatus | None,
            expected_episode_id: str | None,
            expected_slot_generation: int | None,
            new_authority: SlotAuthority) -> AuthorityAck:
        args = (
            operation,
            '' if expected_revision is None else str(expected_revision),
            '' if expected_status is None else expected_status.value,
            (_NULL_EPISODE if expected_episode_id is None
             else expected_episode_id),
            ('' if expected_slot_generation is None
             else str(expected_slot_generation)),
            new_authority.to_json(),
        )
        try:
            response = self._redis_eval(
                COMPARE_AND_TRANSITION_SLOT_LUA, 1,
                self._storage_key(key), *args,
            )
        except Exception as exc:  # noqa: BLE001 - injected backend boundary
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR, message=str(exc))
        return self._parse_ack(response, key)

    @staticmethod
    def _parse_ack(response, key: ExchangePositionKey) -> AuthorityAck:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR,
                message='invalid Redis CAS response',
            )
        raw_code, raw_authority = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode('utf-8')
            except UnicodeError:
                return AuthorityAck(
                    AuthorityAckCode.BACKEND_ERROR,
                    message='Redis CAS response code is not valid UTF-8',
                )
        try:
            code = AuthorityAckCode(raw_code)
        except (TypeError, ValueError, UnicodeError):
            return AuthorityAck(
                AuthorityAckCode.BACKEND_ERROR,
                message=f'unknown Redis CAS response: {raw_code!r}',
            )

        authority = None
        if raw_authority:
            try:
                authority = SlotAuthority.from_json(raw_authority)
            except (TypeError, ValueError, UnicodeError) as exc:
                return AuthorityAck(
                    AuthorityAckCode.MALFORMED, message=str(exc))
            if authority.exchange_position_key != key:
                return AuthorityAck(
                    AuthorityAckCode.MALFORMED,
                    message='authority slot key does not match storage key',
                )
        return AuthorityAck(code, authority)

    @staticmethod
    def _storage_key(key: ExchangePositionKey) -> str:
        if not isinstance(key, ExchangePositionKey):
            raise TypeError('key must be ExchangePositionKey')
        return key.to_storage_key()
