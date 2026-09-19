"""Injected Redis CAS adapter for canonical live-position projections."""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
    ProjectionWriteCode,
    ProjectionWriteResult,
)
from position_identity.slot import ExchangePositionKey

COMPARE_AND_SET_PROJECTION_LUA = r'''
local raw = redis.call('GET', KEYS[1])
local operation = ARGV[1]
local expected_episode = ARGV[2]
local expected_generation = ARGV[3]
local expected_revision = ARGV[4]
local operation_id = ARGV[5]
local new_json = ARGV[6]

local function field_count(record)
  local count = 0
  for _ in pairs(record) do count = count + 1 end
  return count
end

local function nonempty(value)
  return type(value) == 'string' and #value > 0
end

local function canonical_uuid(value)
  return nonempty(value) and #value == 36
      and string.match(value, '^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$') ~= nil
end

local function complete_slot(slot)
  return type(slot) == 'table'
      and field_count(slot) == 7
      and nonempty(slot['account_principal_id'])
      and nonempty(slot['symbol'])
      and slot['exchange'] == 'BINANCE'
      and slot['product'] == 'FUTURES'
      and (slot['environment'] == 'PROD'
           or slot['environment'] == 'DEMO'
           or slot['environment'] == 'SANDBOX')
      and (slot['position_mode'] == 'ONE_WAY'
           or slot['position_mode'] == 'HEDGE')
      and (slot['slot_side'] == 'BOTH'
           or slot['slot_side'] == 'LONG'
           or slot['slot_side'] == 'SHORT')
      and ((slot['position_mode'] == 'ONE_WAY'
            and slot['slot_side'] == 'BOTH')
           or (slot['position_mode'] == 'HEDGE'
               and slot['slot_side'] ~= 'BOTH'))
      and type(slot['account_principal_id']) == 'string'
      and type(slot['environment']) == 'string'
      and type(slot['exchange']) == 'string'
      and type(slot['position_mode']) == 'string'
      and type(slot['product']) == 'string'
      and type(slot['slot_side']) == 'string'
      and type(slot['symbol']) == 'string'
end

local function decode(payload)
  local ok, record = pcall(cjson.decode, payload)
  if not ok or type(record) ~= 'table'
      or field_count(record) ~= 14
      or record['schema_version'] ~= 1
      or type(record['state_revision']) ~= 'number'
      or record['state_revision'] < 1
      or record['state_revision'] % 1 ~= 0
      or type(record['slot_generation']) ~= 'number'
      or record['slot_generation'] < 1
      or record['slot_generation'] % 1 ~= 0
      or type(record['episode_id']) ~= 'string'
      or not canonical_uuid(record['episode_id'])
      or type(record['identity_provenance']) ~= 'string'
      or (record['identity_provenance'] ~= 'NATIVE'
          and record['identity_provenance'] ~= 'MIGRATED'
          and record['identity_provenance'] ~= 'RECONSTRUCTED')
      or type(record['last_operation_id']) ~= 'string'
      or not nonempty(record['last_operation_id'])
      or type(record['side']) ~= 'string'
      or (record['side'] ~= 'LONG' and record['side'] ~= 'SHORT')
      or type(record['system']) ~= 'string'
      or not nonempty(record['system'])
      or type(record['quantity']) ~= 'number'
      or record['quantity'] <= 0
      or type(record['entry_price']) ~= 'number'
      or record['entry_price'] <= 0
      or type(record['opened_at']) ~= 'number'
      or record['opened_at'] < 0
      or type(record['updated_at']) ~= 'number'
      or record['updated_at'] < record['opened_at']
      or not complete_slot(record['exchange_position_key'])
      or (record['legacy_position_id_alias'] ~= cjson.null
          and not nonempty(record['legacy_position_id_alias']))
      or (record['legacy_position_id_alias'] ~= cjson.null
          and type(record['legacy_position_id_alias']) ~= 'string') then
    return nil
  end
  return record
end

local function same_slot(left, right)
  return left['account_principal_id'] == right['account_principal_id']
      and left['environment'] == right['environment']
      and left['exchange'] == right['exchange']
      and left['position_mode'] == right['position_mode']
      and left['product'] == right['product']
      and left['slot_side'] == right['slot_side']
      and left['symbol'] == right['symbol']
end

local proposed = decode(new_json)
if not proposed or proposed['last_operation_id'] ~= operation_id then
  return {'INVALID', ''}
end

if operation == 'CREATE' then
  if raw then
    local current = decode(raw)
    if not current then
      return {'MALFORMED', raw}
    end
    if current['last_operation_id'] == operation_id and raw == new_json then
      return {'ALREADY_APPLIED', raw}
    end
    return {'CONFLICT', raw}
  end
  if proposed['state_revision'] ~= 1 then
    return {'INVALID', ''}
  end
  redis.call('SET', KEYS[1], new_json)
  return {'APPLIED', new_json}
end

if not raw then
  return {'NOT_FOUND', ''}
end
local current = decode(raw)
if not current then
  return {'MALFORMED', raw}
end
if current['last_operation_id'] == operation_id then
  if raw == new_json then
    return {'ALREADY_APPLIED', raw}
  end
  return {'CONFLICT', raw}
end
if current['episode_id'] ~= expected_episode
    or tostring(current['slot_generation']) ~= expected_generation
    or tostring(current['state_revision']) ~= expected_revision then
  return {'STALE', raw}
end
if proposed['episode_id'] ~= expected_episode
    or tostring(proposed['slot_generation']) ~= expected_generation
    or proposed['state_revision'] ~= current['state_revision'] + 1
    or not same_slot(proposed['exchange_position_key'],
                     current['exchange_position_key'])
    or proposed['identity_provenance'] ~= current['identity_provenance']
    or proposed['opened_at'] ~= current['opened_at']
    or proposed['side'] ~= current['side']
    or proposed['system'] ~= current['system']
    or proposed['legacy_position_id_alias'] ~=
       current['legacy_position_id_alias'] then
  return {'INVALID', ''}
end
redis.call('SET', KEYS[1], new_json)
return {'APPLIED', new_json}
'''


RedisGet = Callable[[str], str | bytes | None]
RedisEval = Callable[..., object]


class RedisLivePositionProjectionAdapter:
    """No-fallback per-slot projection read/create/CAS."""

    STORAGE_KEY_PREFIX = 'pm:position-projection:v1'

    def __init__(
            self, *, redis_get: RedisGet, redis_eval: RedisEval,
            redis_available: Callable[[], bool] | None = None) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval
        self._redis_available = redis_available

    def get(self, key: ExchangePositionKey) -> ProjectionReadResult:
        storage_key = self._storage_key(key)
        try:
            raw = self._redis_get(storage_key)
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return ProjectionReadResult(
                ProjectionReadCode.UNAVAILABLE, message=str(exc))
        if raw is None:
            return ProjectionReadResult(ProjectionReadCode.NOT_FOUND)
        try:
            projection = LivePositionProjection.from_json(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return ProjectionReadResult(
                ProjectionReadCode.MALFORMED, message=str(exc))
        if projection.exchange_position_key != key:
            return ProjectionReadResult(
                ProjectionReadCode.MALFORMED,
                message='projection slot does not match storage key')
        return ProjectionReadResult(ProjectionReadCode.FOUND, projection)

    def create(
            self, projection: LivePositionProjection) -> ProjectionWriteResult:
        if not isinstance(projection, LivePositionProjection):
            raise TypeError('projection must be LivePositionProjection')
        return self._write(
            operation='CREATE', projection=projection,
            expected_episode_id='', expected_slot_generation=0,
            expected_revision=0,
        )

    def compare_and_set(
            self, projection: LivePositionProjection, *,
            expected_episode_id: str, expected_slot_generation: int,
            expected_revision: int) -> ProjectionWriteResult:
        if not isinstance(projection, LivePositionProjection):
            raise TypeError('projection must be LivePositionProjection')
        try:
            expected_episode_id = str(UUID(expected_episode_id))
        except (TypeError, ValueError, AttributeError):
            return ProjectionWriteResult(
                ProjectionWriteCode.INVALID,
                message='expected_episode_id must be an opaque UUID')
        if (isinstance(expected_slot_generation, bool)
                or not isinstance(expected_slot_generation, int)
                or expected_slot_generation < 1):
            return ProjectionWriteResult(
                ProjectionWriteCode.INVALID,
                message='expected_slot_generation must be a positive integer')
        if (isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 1):
            return ProjectionWriteResult(
                ProjectionWriteCode.INVALID,
                message='expected_revision must be a positive integer')
        return self._write(
            operation='CAS', projection=projection,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            expected_revision=expected_revision,
        )

    def _write(
            self, *, operation: str,
            projection: LivePositionProjection,
            expected_episode_id: str,
            expected_slot_generation: int,
            expected_revision: int) -> ProjectionWriteResult:
        if self._redis_available is not None:
            try:
                available = self._redis_available()
            except Exception as exc:  # noqa: BLE001 - pre-attempt check
                return ProjectionWriteResult(
                    ProjectionWriteCode.UNAVAILABLE, message=str(exc))
            if available is not True:
                return ProjectionWriteResult(
                    ProjectionWriteCode.UNAVAILABLE,
                    message='Redis unavailable before projection attempt')
        try:
            response = self._redis_eval(
                COMPARE_AND_SET_PROJECTION_LUA, 1,
                self._storage_key(projection.exchange_position_key),
                operation, expected_episode_id,
                str(expected_slot_generation), str(expected_revision),
                projection.last_operation_id, projection.to_json(),
            )
        except Exception as exc:  # noqa: BLE001 - ACK may be ambiguous
            return ProjectionWriteResult(
                ProjectionWriteCode.UNKNOWN, message=str(exc))
        return self._parse_write(
            response, projection.exchange_position_key)

    @staticmethod
    def _parse_write(response, key) -> ProjectionWriteResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return ProjectionWriteResult(
                ProjectionWriteCode.UNKNOWN,
                message='invalid Redis projection response')
        raw_code, raw_projection = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode('utf-8')
            except UnicodeError:
                return ProjectionWriteResult(
                    ProjectionWriteCode.UNKNOWN,
                    message='projection response code is not valid UTF-8')
        try:
            code = ProjectionWriteCode(raw_code)
        except (TypeError, ValueError):
            return ProjectionWriteResult(
                ProjectionWriteCode.UNKNOWN,
                message=f'unknown projection response: {raw_code!r}')
        projection = None
        if raw_projection:
            try:
                projection = LivePositionProjection.from_json(raw_projection)
            except (TypeError, ValueError, UnicodeError) as exc:
                return ProjectionWriteResult(
                    ProjectionWriteCode.MALFORMED, message=str(exc))
            if projection.exchange_position_key != key:
                return ProjectionWriteResult(
                    ProjectionWriteCode.MALFORMED,
                    message='projection slot does not match storage key')
        return ProjectionWriteResult(code, projection)

    @classmethod
    def _storage_key(cls, key: ExchangePositionKey) -> str:
        if not isinstance(key, ExchangePositionKey):
            raise TypeError('key must be ExchangePositionKey')
        return f'{cls.STORAGE_KEY_PREFIX}:{key.canonical_digest()}'
