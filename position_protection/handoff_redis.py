"""Atomic R4 native-open authority/projection/desired handoff."""

from __future__ import annotations

import math
from collections.abc import Callable

from position_identity.authority import (
    AuthorityProvenance,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus
from position_protection.handoff import (
    NativeOpenHandoffCode,
    NativeOpenHandoffResult,
)

_MISSING = "__R4_MISSING__"

NATIVE_OPEN_HANDOFF_LUA = r"""
local MISSING = '__R4_MISSING__'
local current_authority = redis.call('GET', KEYS[1])
local current_projection = redis.call('GET', KEYS[2])
local current_desired = redis.call('GET', KEYS[3])

local function same(current, expected)
  if expected == MISSING then return current == false end
  return current == expected
end

if current_authority == ARGV[4]
    and current_projection == ARGV[5]
    and current_desired == ARGV[6] then
  return {'ALREADY_APPLIED', ARGV[4], ARGV[5], ARGV[6]}
end

if not same(current_authority, ARGV[1])
    or not same(current_projection, ARGV[2])
    or not same(current_desired, ARGV[3]) then
  return {'CONFLICT', '', '', ''}
end

redis.call('SET', KEYS[1], ARGV[4])
redis.call('SET', KEYS[2], ARGV[5])
redis.call('SET', KEYS[3], ARGV[6])
return {'APPLIED', ARGV[4], ARGV[5], ARGV[6]}
"""

RedisGet = Callable[[str], str | bytes | None]
RedisEval = Callable[..., object]


class RedisNativeOpenHandoffAdapter:
    """Atomically establishes the three ACKed records needed by a V3 task."""

    def __init__(self, *, redis_get: RedisGet, redis_eval: RedisEval) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval

    def open_native(
        self,
        *,
        exchange_position_key: ExchangePositionKey,
        candidate_episode_id: str,
        desired_intent_id: str,
        trigger_price: float,
        covered_quantity: float,
        closing_side: str,
        position_side: str,
        system: str,
        entry_price: float,
        opened_at: float,
        operation_id: str,
        now: float,
    ) -> NativeOpenHandoffResult:
        if not isinstance(exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        self._validate_time(now, "now")
        self._validate_time(opened_at, "opened_at")
        if now < opened_at:
            raise ValueError("now must be >= opened_at")

        authority_key = exchange_position_key.to_storage_key()
        digest = exchange_position_key.canonical_digest()
        projection_key = f"pm:position-projection:v1:{digest}"
        desired_key = f"pm:desired-protection:v1:{digest}"
        try:
            raw_authority = self._redis_get(authority_key)
            raw_projection = self._redis_get(projection_key)
            raw_desired = self._redis_get(desired_key)
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.UNAVAILABLE, message=str(exc)
            )

        prepared = self._prepare(
            exchange_position_key=exchange_position_key,
            raw_authority=raw_authority,
            raw_projection=raw_projection,
            raw_desired=raw_desired,
            candidate_episode_id=candidate_episode_id,
            desired_intent_id=desired_intent_id,
            trigger_price=trigger_price,
            covered_quantity=covered_quantity,
            closing_side=closing_side,
            position_side=position_side,
            system=system,
            entry_price=entry_price,
            opened_at=opened_at,
            operation_id=operation_id,
            now=now,
        )
        if isinstance(prepared, NativeOpenHandoffResult):
            return prepared
        authority, projection, desired = prepared
        try:
            expected = tuple(
                _MISSING if value is None else self._text_raw(value)
                for value in (raw_authority, raw_projection, raw_desired)
            )
        except (TypeError, UnicodeError) as exc:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.MALFORMED, message=str(exc)
            )

        proposed = (authority.to_json(), projection.to_json(), desired.to_json())
        try:
            response = self._redis_eval(
                NATIVE_OPEN_HANDOFF_LUA,
                3,
                authority_key,
                projection_key,
                desired_key,
                *expected,
                *proposed,
            )
        except Exception as exc:  # noqa: BLE001 - ACK may be ambiguous
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.UNKNOWN, message=str(exc)
            )
        return self._parse(response, exchange_position_key)

    def _prepare(
        self,
        *,
        exchange_position_key,
        raw_authority,
        raw_projection,
        raw_desired,
        candidate_episode_id,
        desired_intent_id,
        trigger_price,
        covered_quantity,
        closing_side,
        position_side,
        system,
        entry_price,
        opened_at,
        operation_id,
        now,
    ):
        try:
            if raw_authority is None:
                if raw_projection is not None or raw_desired is not None:
                    return NativeOpenHandoffResult(
                        NativeOpenHandoffCode.CONFLICT,
                        message="orphan canonical state without slot authority",
                    )
                base = SlotAuthority.initial_flat(exchange_position_key, now=now)
            else:
                base = SlotAuthority.from_json(raw_authority)
                if base.exchange_position_key != exchange_position_key:
                    raise ValueError("authority slot does not match storage key")
                if base.status is not AuthorityStatus.FLAT:
                    return self._existing_or_conflict(
                        base=base,
                        key=exchange_position_key,
                        raw_projection=raw_projection,
                        raw_desired=raw_desired,
                        desired_intent_id=desired_intent_id,
                        trigger_price=trigger_price,
                        covered_quantity=covered_quantity,
                        closing_side=closing_side,
                        position_side=position_side,
                        system=system,
                        entry_price=entry_price,
                        opened_at=opened_at,
                    )
                self._validate_stale_records(
                    exchange_position_key, base, raw_projection, raw_desired
                )

            authority = base.allocate_episode(
                episode_id=candidate_episode_id,
                provenance=AuthorityProvenance.NATIVE,
                status=AuthorityStatus.ACTIVE,
                legacy_position_id_alias=None,
                now=now,
            )
            projection = LivePositionProjection(
                exchange_position_key=exchange_position_key,
                episode_id=authority.episode_id,
                slot_generation=authority.slot_generation,
                identity_provenance=AuthorityProvenance.NATIVE,
                state_revision=1,
                last_operation_id=f"{operation_id}:projection",
                side=position_side,
                system=system,
                quantity=covered_quantity,
                entry_price=entry_price,
                opened_at=opened_at,
                updated_at=now,
            )
            pending = DesiredProtectionRecord.initial_pending(
                exchange_position_key=exchange_position_key,
                episode_id=authority.episode_id,
                slot_generation=authority.slot_generation,
                desired_intent_id=desired_intent_id,
                trigger_price=trigger_price,
                covered_quantity=covered_quantity,
                closing_side=closing_side,
                operation_id=f"{operation_id}:declare",
                now=now,
            )
            desired = pending.transition(
                status=ProtectionStatus.SUBMITTING,
                operation_id=f"{operation_id}:submit",
                now=now,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.MALFORMED, message=str(exc)
            )
        return authority, projection, desired

    @staticmethod
    def _validate_stale_records(key, authority, raw_projection, raw_desired):
        if raw_projection is not None:
            projection = LivePositionProjection.from_json(raw_projection)
            if (
                projection.exchange_position_key != key
                or projection.slot_generation > authority.slot_generation
            ):
                raise ValueError("stale projection conflicts with flat authority")
        if raw_desired is not None:
            desired = DesiredProtectionRecord.from_json(raw_desired)
            if (
                desired.exchange_position_key != key
                or desired.slot_generation > authority.slot_generation
            ):
                raise ValueError("stale desired state conflicts with flat authority")

    @staticmethod
    def _existing_or_conflict(
        *,
        base,
        key,
        raw_projection,
        raw_desired,
        desired_intent_id,
        trigger_price,
        covered_quantity,
        closing_side,
        position_side,
        system,
        entry_price,
        opened_at,
    ):
        try:
            projection = LivePositionProjection.from_json(raw_projection)
            desired = DesiredProtectionRecord.from_json(raw_desired)
        except (TypeError, ValueError, UnicodeError) as exc:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.MALFORMED, message=str(exc)
            )
        same = (
            base.status is AuthorityStatus.ACTIVE
            and base.exchange_position_key == key
            and projection.exchange_position_key == key
            and desired.exchange_position_key == key
            and base.slot_generation
            == projection.slot_generation
            == desired.slot_generation
            and base.provenance is AuthorityProvenance.NATIVE
            and projection.episode_id == base.episode_id
            and desired.episode_id == base.episode_id
            and projection.side == str(position_side).strip().upper()
            and projection.system == str(system).strip()
            and projection.quantity == float(covered_quantity)
            and projection.entry_price == float(entry_price)
            and projection.opened_at == float(opened_at)
            and desired.desired_intent_id == desired_intent_id
            and desired.trigger_price == float(trigger_price)
            and desired.covered_quantity == float(covered_quantity)
            and desired.closing_side == str(closing_side).strip().upper()
            and desired.status is ProtectionStatus.SUBMITTING
        )
        if same:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.ALREADY_APPLIED, base, projection, desired
            )
        return NativeOpenHandoffResult(
            NativeOpenHandoffCode.CONFLICT,
            message="slot authority is not flat for native open",
        )

    @staticmethod
    def _parse(response, key) -> NativeOpenHandoffResult:
        if not isinstance(response, (list, tuple)) or len(response) != 4:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.UNKNOWN,
                message="invalid native-open handoff response",
            )
        raw_code, raw_authority, raw_projection, raw_desired = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return NativeOpenHandoffResult(
                    NativeOpenHandoffCode.UNKNOWN, message="invalid response UTF-8"
                )
        try:
            code = NativeOpenHandoffCode(raw_code)
        except (TypeError, ValueError):
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.UNKNOWN,
                message=f"unknown native-open response: {raw_code!r}",
            )
        if code is NativeOpenHandoffCode.CONFLICT:
            return NativeOpenHandoffResult(code)
        try:
            authority = SlotAuthority.from_json(raw_authority)
            projection = LivePositionProjection.from_json(raw_projection)
            desired = DesiredProtectionRecord.from_json(raw_desired)
        except (TypeError, ValueError, UnicodeError) as exc:
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.MALFORMED, message=str(exc)
            )
        if not (
            authority.exchange_position_key == key
            and projection.exchange_position_key == key
            and desired.exchange_position_key == key
            and authority.episode_id == projection.episode_id == desired.episode_id
            and authority.slot_generation
            == projection.slot_generation
            == desired.slot_generation
        ):
            return NativeOpenHandoffResult(
                NativeOpenHandoffCode.MALFORMED,
                message="handoff acknowledgement identity mismatch",
            )
        return NativeOpenHandoffResult(code, authority, projection, desired)

    @staticmethod
    def _text_raw(value):
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if not isinstance(value, str):
            raise TypeError("Redis state must be text")
        return value

    @staticmethod
    def _validate_time(value, field):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"{field} must be finite and nonnegative")
