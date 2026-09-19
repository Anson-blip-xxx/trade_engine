"""Atomic R3 ACK alias writeback across claim, desired, and projection state."""

from __future__ import annotations

import math
from collections.abc import Callable

from position_protection.claim import ProtectionMutationClaim
from position_protection.desired import DesiredProtectionRecord
from position_protection.writeback import WritebackCode, WritebackResult

CONDITIONAL_ALIAS_WRITEBACK_LUA = r"""
local authority_raw = redis.call('GET', KEYS[1])
local claim_raw = redis.call('GET', KEYS[2])
local desired_raw = redis.call('GET', KEYS[3])
local projection_raw = redis.call('GET', KEYS[4])
if not authority_raw or not desired_raw or not projection_raw then
  return {'NOT_FOUND', ''}
end
if not claim_raw then return {'CLAIM_LOST', ''} end
local authority_ok, authority = pcall(cjson.decode, authority_raw)
local claim_ok, claim = pcall(cjson.decode, claim_raw)
local desired_ok, desired = pcall(cjson.decode, desired_raw)
local projection_ok, projection = pcall(cjson.decode, projection_raw)
if not authority_ok or type(authority) ~= 'table'
    or authority['schema_version'] ~= 1
    or not claim_ok or type(claim) ~= 'table'
    or claim['schema_version'] ~= 1
    or not desired_ok or type(desired) ~= 'table'
    or desired['schema_version'] ~= 1
    or type(desired['exchange_algo_aliases']) ~= 'table'
    or type(desired['revision']) ~= 'number'
    or type(desired['updated_at']) ~= 'number'
    or not projection_ok or type(projection) ~= 'table'
    or projection['schema_version'] ~= 1
    or type(projection['state_revision']) ~= 'number' then
  return {'MALFORMED', ''}
end
if authority['status'] ~= 'ACTIVE'
    or authority['episode_id'] ~= ARGV[1]
    or tostring(authority['slot_generation']) ~= ARGV[2]
    or tostring(authority['revision']) ~= ARGV[3]
    or claim['owner_token'] ~= ARGV[4]
    or tostring(claim['fencing_token']) ~= ARGV[5]
    or tostring(claim['authority_revision']) ~= ARGV[3]
    or claim['episode_id'] ~= ARGV[1]
    or tostring(claim['slot_generation']) ~= ARGV[2]
    or tostring(claim['protection_generation']) ~= ARGV[6] then
  return {'CLAIM_LOST', ''}
end
if desired["episode_id"] ~= ARGV[1]
    or tostring(desired["slot_generation"]) ~= ARGV[2]
    or tostring(desired["protection_generation"]) ~= ARGV[6]
    or projection["episode_id"] ~= ARGV[1]
    or tostring(projection["slot_generation"]) ~= ARGV[2]
    or tostring(projection["state_revision"]) ~= ARGV[8] then
  return {"STALE", desired_raw}
end
local alias = ARGV[9]
local operation_id = ARGV[10]
local found = false
for _, existing in ipairs(desired["exchange_algo_aliases"]) do
  if tostring(existing) == alias then found = true end
end
if tostring(desired["revision"]) ~= ARGV[7] then
  if desired["revision"] == tonumber(ARGV[7]) + 1
      and desired["last_operation_id"] == operation_id and found then
    return {"ALREADY_APPLIED", desired_raw}
  end
  return {"STALE", desired_raw}
end
local updated_at = tonumber(ARGV[11])
if not updated_at or updated_at < desired["updated_at"] then
  return {"INVALID", desired_raw}
end
if desired["last_operation_id"] == operation_id then
  return {"CONFLICT", desired_raw}
end
if found then return {"ALREADY_APPLIED", desired_raw} end
if desired["status"] ~= "SUBMITTING" then
  return {"INVALID", desired_raw}
end
table.insert(desired['exchange_algo_aliases'], alias)
desired['revision'] = desired['revision'] + 1
desired['last_operation_id'] = operation_id
desired['updated_at'] = updated_at
local updated = cjson.encode(desired)
redis.call('SET', KEYS[3], updated)
return {'APPLIED', updated}
"""

RedisEval = Callable[..., object]


class RedisConditionalAliasWritebackAdapter:
    """No-fallback atomic writeback for one still-owned V3 task."""

    def __init__(self, *, redis_eval: RedisEval) -> None:
        self._redis_eval = redis_eval

    def writeback(
        self,
        *,
        claim: ProtectionMutationClaim,
        expected_desired_revision: int,
        expected_projection_revision: int,
        exchange_algo_alias: str,
        operation_id: str,
        now: float,
    ) -> WritebackResult:
        if not isinstance(claim, ProtectionMutationClaim):
            raise TypeError("claim must be ProtectionMutationClaim")
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or now < 0
        ):
            raise ValueError("now must be finite and nonnegative")
        for value, field in (
            (expected_desired_revision, "expected_desired_revision"),
            (expected_projection_revision, "expected_projection_revision"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        alias = self._text(exchange_algo_alias, "exchange_algo_alias")
        operation = self._text(operation_id, "operation_id")
        key = claim.exchange_position_key
        digest = key.canonical_digest()
        try:
            response = self._redis_eval(
                CONDITIONAL_ALIAS_WRITEBACK_LUA,
                4,
                key.to_storage_key(),
                f"pm:protection-claim:v1:{digest}",
                f"pm:desired-protection:v1:{digest}",
                f"pm:position-projection:v1:{digest}",
                claim.episode_id,
                str(claim.slot_generation),
                str(claim.authority_revision),
                claim.owner_token,
                str(claim.fencing_token),
                str(claim.protection_generation),
                str(expected_desired_revision),
                str(expected_projection_revision),
                alias,
                operation,
                str(now),
            )
        except Exception as exc:  # noqa: BLE001 - ACK may be ambiguous
            return WritebackResult(WritebackCode.UNKNOWN, message=str(exc))
        return self._parse(response)

    @staticmethod
    def _text(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{field} contains control characters")
        return value

    @staticmethod
    def _parse(response) -> WritebackResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return WritebackResult(
                WritebackCode.UNKNOWN, message="invalid writeback response"
            )
        raw_code, raw_desired = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return WritebackResult(
                    WritebackCode.UNKNOWN, message="invalid response UTF-8"
                )
        try:
            code = WritebackCode(raw_code)
        except (TypeError, ValueError):
            return WritebackResult(
                WritebackCode.UNKNOWN,
                message=f"unknown writeback response: {raw_code!r}",
            )
        desired = None
        if raw_desired:
            try:
                desired = DesiredProtectionRecord.from_json(raw_desired)
            except (TypeError, ValueError, UnicodeError) as exc:
                return WritebackResult(WritebackCode.MALFORMED, message=str(exc))
        return WritebackResult(code, desired)
