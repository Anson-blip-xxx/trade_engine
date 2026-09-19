"""Injected Redis CAS adapter for dormant desired-protection state."""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    DesiredReadCode,
    DesiredReadResult,
    DesiredWriteCode,
    DesiredWriteResult,
)

_NONE = '__R2_NONE__'

COMPARE_AND_SET_DESIRED_PROTECTION_LUA = r'''
local raw = redis.call('GET', KEYS[1])
local operation = ARGV[1]
local expected_episode = ARGV[2]
local expected_slot_generation = ARGV[3]
local expected_protection_generation = ARGV[4]
local expected_revision = ARGV[5]
local operation_id = ARGV[6]
local new_json = ARGV[7]
local NONE = '__R2_NONE__'

local function count_fields(value)
  local count = 0
  for _ in pairs(value) do count = count + 1 end
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
  return type(slot) == 'table' and count_fields(slot) == 7
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
end

local function valid_status(status)
  return status == 'NONE' or status == 'PENDING'
      or status == 'SUBMITTING' or status == 'UNKNOWN'
      or status == 'ACTIVE' or status == 'REPLACING'
      or status == 'FAILED' or status == 'CANCELLED'
      or status == 'SUPERSEDED'
end

local function valid_aliases(aliases)
  if type(aliases) ~= 'table' then return false end
  local seen = {}
  for index, alias in ipairs(aliases) do
    if index < 1 or not nonempty(alias) or seen[alias] then return false end
    seen[alias] = true
  end
  return true
end

local function decode(payload)
  local ok, record = pcall(cjson.decode, payload)
  if not ok or type(record) ~= 'table' or count_fields(record) ~= 15
      or record['schema_version'] ~= 1
      or not complete_slot(record['exchange_position_key'])
      or not canonical_uuid(record['episode_id'])
      or type(record['slot_generation']) ~= 'number'
      or record['slot_generation'] < 1
      or record['slot_generation'] % 1 ~= 0
      or type(record['protection_generation']) ~= 'number'
      or record['protection_generation'] < 1
      or record['protection_generation'] % 1 ~= 0
      or not nonempty(record['desired_intent_id'])
      or type(record['trigger_price']) ~= 'number'
      or record['trigger_price'] <= 0
      or type(record['covered_quantity']) ~= 'number'
      or record['covered_quantity'] <= 0
      or (record['closing_side'] ~= 'BUY'
          and record['closing_side'] ~= 'SELL')
      or not valid_status(record['status'])
      or not valid_aliases(record['exchange_algo_aliases'])
      or type(record['revision']) ~= 'number'
      or record['revision'] < 1 or record['revision'] % 1 ~= 0
      or not nonempty(record['last_operation_id'])
      or type(record['created_at']) ~= 'number'
      or record['created_at'] < 0
      or type(record['updated_at']) ~= 'number'
      or record['updated_at'] < record['created_at'] then
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

local function same_spec(left, right)
  return same_slot(left['exchange_position_key'],
                   right['exchange_position_key'])
      and left['episode_id'] == right['episode_id']
      and left['slot_generation'] == right['slot_generation']
      and left['trigger_price'] == right['trigger_price']
      and left['covered_quantity'] == right['covered_quantity']
      and left['closing_side'] == right['closing_side']
end

local function same_aliases(left, right)
  if #left ~= #right then return false end
  for index, alias in ipairs(left) do
    if right[index] ~= alias then return false end
  end
  return true
end

local function legal_transition(old, new, old_aliases, new_aliases)
  if old == new then return not same_aliases(old_aliases, new_aliases) end
  if old == 'NONE' then return new == 'PENDING' end
  if old == 'PENDING' then
    return new == 'SUBMITTING' or new == 'CANCELLED'
        or new == 'SUPERSEDED' or new == 'FAILED'
  end
  if old == 'SUBMITTING' then
    return new == 'PENDING' or new == 'UNKNOWN' or new == 'ACTIVE'
        or new == 'CANCELLED' or new == 'SUPERSEDED' or new == 'FAILED'
  end
  if old == 'UNKNOWN' then
    return new == 'PENDING' or new == 'ACTIVE' or new == 'CANCELLED'
        or new == 'SUPERSEDED' or new == 'FAILED'
  end
  if old == 'ACTIVE' then
    return new == 'REPLACING' or new == 'UNKNOWN'
        or new == 'CANCELLED' or new == 'SUPERSEDED'
  end
  if old == 'REPLACING' then
    return new == 'ACTIVE' or new == 'UNKNOWN' or new == 'FAILED'
        or new == 'CANCELLED' or new == 'SUPERSEDED'
  end
  return false
end

local proposed = decode(new_json)
if not proposed or proposed['last_operation_id'] ~= operation_id then
  return {'INVALID', ''}
end

if not raw then
  if operation ~= 'DECLARE'
      or expected_episode ~= NONE
      or proposed['protection_generation'] ~= 1
      or proposed['revision'] ~= 1
      or proposed['status'] ~= 'PENDING'
      or #proposed['exchange_algo_aliases'] ~= 0 then
    return {'NOT_FOUND', ''}
  end
  redis.call('SET', KEYS[1], new_json)
  return {'APPLIED', new_json}
end

local current = decode(raw)
if not current then return {'MALFORMED', raw} end
if current['last_operation_id'] == operation_id then
  if raw == new_json then return {'ALREADY_APPLIED', raw} end
  return {'CONFLICT', raw}
end
if expected_episode == NONE
    or current['episode_id'] ~= expected_episode
    or tostring(current['slot_generation']) ~= expected_slot_generation
    or tostring(current['protection_generation']) ~=
       expected_protection_generation
    or tostring(current['revision']) ~= expected_revision then
  return {'STALE', raw}
end

if operation == 'DECLARE' then
  if same_spec(current, proposed) then
    return {'ALREADY_APPLIED', raw}
  end
  if current['episode_id'] == proposed['episode_id'] then
    if proposed['desired_intent_id'] == current['desired_intent_id'] then
      return {'CONFLICT', raw}
    end
    if proposed['slot_generation'] ~= current['slot_generation']
        or proposed['protection_generation'] ~=
           current['protection_generation'] + 1
        or proposed['revision'] ~= current['revision'] + 1
        or proposed['created_at'] ~= current['created_at'] then
      return {'INVALID', ''}
    end
  else
    if proposed['slot_generation'] <= current['slot_generation']
        or proposed['protection_generation'] ~= 1
        or proposed['revision'] ~= 1 then
      return {'INVALID', ''}
    end
  end
  if not same_slot(current['exchange_position_key'],
                   proposed['exchange_position_key'])
      or proposed['status'] ~= 'PENDING'
      or #proposed['exchange_algo_aliases'] ~= 0 then
    return {'INVALID', ''}
  end
  redis.call('SET', KEYS[1], new_json)
  return {'APPLIED', new_json}
end

if operation ~= 'CAS'
    or not same_spec(current, proposed)
    or proposed['protection_generation'] ~=
       current['protection_generation']
    or proposed['desired_intent_id'] ~= current['desired_intent_id']
    or proposed['revision'] ~= current['revision'] + 1
    or proposed['created_at'] ~= current['created_at']
    or not legal_transition(current['status'], proposed['status'],
                            current['exchange_algo_aliases'],
                            proposed['exchange_algo_aliases']) then
  return {'INVALID', ''}
end
redis.call('SET', KEYS[1], new_json)
return {'APPLIED', new_json}
'''


RedisGet = Callable[[str], str | bytes | None]
RedisEval = Callable[..., object]


class RedisDesiredProtectionAdapter:
    """Required/no-fallback current desired state for one physical slot."""

    STORAGE_KEY_PREFIX = 'pm:desired-protection:v1'

    def __init__(
            self, *, redis_get: RedisGet, redis_eval: RedisEval,
            redis_available: Callable[[], bool] | None = None) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval
        self._redis_available = redis_available

    def get(self, key: ExchangePositionKey) -> DesiredReadResult:
        try:
            raw = self._redis_get(self._storage_key(key))
        except Exception as exc:  # noqa: BLE001
            return DesiredReadResult(
                DesiredReadCode.UNAVAILABLE, message=str(exc))
        if raw is None:
            return DesiredReadResult(DesiredReadCode.NOT_FOUND)
        try:
            record = DesiredProtectionRecord.from_json(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return DesiredReadResult(
                DesiredReadCode.MALFORMED, message=str(exc))
        if record.exchange_position_key != key:
            return DesiredReadResult(
                DesiredReadCode.MALFORMED,
                message='desired-protection slot does not match storage key')
        return DesiredReadResult(DesiredReadCode.FOUND, record)

    def declare(
            self, record: DesiredProtectionRecord, *,
            expected_episode_id: str | None = None,
            expected_slot_generation: int | None = None,
            expected_protection_generation: int | None = None,
            expected_revision: int | None = None) -> DesiredWriteResult:
        expected = self._expectations(
            expected_episode_id, expected_slot_generation,
            expected_protection_generation, expected_revision)
        if isinstance(expected, DesiredWriteResult):
            return expected
        return self._write('DECLARE', record, *expected)

    def compare_and_set(
            self, record: DesiredProtectionRecord, *,
            expected_episode_id: str, expected_slot_generation: int,
            expected_protection_generation: int,
            expected_revision: int) -> DesiredWriteResult:
        expected = self._expectations(
            expected_episode_id, expected_slot_generation,
            expected_protection_generation, expected_revision,
            require_all=True)
        if isinstance(expected, DesiredWriteResult):
            return expected
        return self._write('CAS', record, *expected)

    @staticmethod
    def _expectations(
            episode_id, slot_generation, protection_generation, revision,
            *, require_all: bool = False):
        values = (episode_id, slot_generation, protection_generation, revision)
        if not require_all and all(value is None for value in values):
            return (_NONE, _NONE, _NONE, _NONE)
        if any(value is None for value in values):
            return DesiredWriteResult(
                DesiredWriteCode.INVALID,
                message='all expected identity fields are required together')
        try:
            episode_id = str(UUID(episode_id))
        except (TypeError, ValueError, AttributeError):
            return DesiredWriteResult(
                DesiredWriteCode.INVALID,
                message='expected_episode_id must be an opaque UUID')
        for value, field in (
            (slot_generation, 'expected_slot_generation'),
            (protection_generation, 'expected_protection_generation'),
            (revision, 'expected_revision'),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return DesiredWriteResult(
                    DesiredWriteCode.INVALID,
                    message=f'{field} must be a positive integer')
        return (episode_id, str(slot_generation),
                str(protection_generation), str(revision))

    def _write(
            self, operation: str, record: DesiredProtectionRecord,
            expected_episode: str, expected_slot_generation: str,
            expected_protection_generation: str,
            expected_revision: str) -> DesiredWriteResult:
        if not isinstance(record, DesiredProtectionRecord):
            raise TypeError('record must be DesiredProtectionRecord')
        if self._redis_available is not None:
            try:
                available = self._redis_available()
            except Exception as exc:  # noqa: BLE001
                return DesiredWriteResult(
                    DesiredWriteCode.UNAVAILABLE, message=str(exc))
            if available is not True:
                return DesiredWriteResult(
                    DesiredWriteCode.UNAVAILABLE,
                    message='Redis unavailable before desired-state attempt')
        try:
            response = self._redis_eval(
                COMPARE_AND_SET_DESIRED_PROTECTION_LUA, 1,
                self._storage_key(record.exchange_position_key), operation,
                expected_episode, expected_slot_generation,
                expected_protection_generation, expected_revision,
                record.last_operation_id, record.to_json())
        except Exception as exc:  # noqa: BLE001 - ACK may be ambiguous
            return DesiredWriteResult(
                DesiredWriteCode.UNKNOWN, message=str(exc))
        return self._parse_write(response, record.exchange_position_key)

    @staticmethod
    def _parse_write(response, key) -> DesiredWriteResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return DesiredWriteResult(
                DesiredWriteCode.UNKNOWN,
                message='invalid Redis desired-state response')
        raw_code, raw_record = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode('utf-8')
            except UnicodeError:
                return DesiredWriteResult(
                    DesiredWriteCode.UNKNOWN,
                    message='desired-state response code is not valid UTF-8')
        try:
            code = DesiredWriteCode(raw_code)
        except (TypeError, ValueError):
            return DesiredWriteResult(
                DesiredWriteCode.UNKNOWN,
                message=f'unknown desired-state response: {raw_code!r}')
        record = None
        if raw_record:
            try:
                record = DesiredProtectionRecord.from_json(raw_record)
            except (TypeError, ValueError, UnicodeError) as exc:
                return DesiredWriteResult(
                    DesiredWriteCode.MALFORMED, message=str(exc))
            if record.exchange_position_key != key:
                return DesiredWriteResult(
                    DesiredWriteCode.MALFORMED,
                    message='desired-protection slot does not match storage key')
        return DesiredWriteResult(code, record)

    @classmethod
    def _storage_key(cls, key: ExchangePositionKey) -> str:
        if not isinstance(key, ExchangePositionKey):
            raise TypeError('key must be ExchangePositionKey')
        return f'{cls.STORAGE_KEY_PREFIX}:{key.canonical_digest()}'
