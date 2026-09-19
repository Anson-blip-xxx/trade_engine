"""Atomic controlled legacy-authority and projection migration handoff."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from position_identity.adoption import (
    AdoptionClassification,
    QuarantineReason,
    prepare_legacy_adoption,
)
from position_identity.authority import (
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
)
from position_identity.projection_migration import (
    LegacyProjectionCode,
    LegacyProjectionReason,
    prepare_legacy_projection,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey

LEGACY_MIGRATION_HANDOFF_LUA = r"""
local authority = redis.call('GET', KEYS[1])
local projection = redis.call('GET', KEYS[2])
local absent = ARGV[1]

local function expected(current, value)
  if value == absent then return current == false end
  return current == value
end

if authority == ARGV[4] and projection == ARGV[5] then
  return {'ALREADY_APPLIED', authority, projection}
end
if not expected(authority, ARGV[2]) or not expected(projection, ARGV[3]) then
  return {'CONFLICT', authority or '', projection or ''}
end
redis.call('SET', KEYS[1], ARGV[4])
redis.call('SET', KEYS[2], ARGV[5])
return {'APPLIED', ARGV[4], ARGV[5]}
"""


class LegacyMigrationHandoffCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    QUARANTINED = "QUARANTINED"
    EXISTING_OWNER = "EXISTING_OWNER"
    CONFLICT = "CONFLICT"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class LegacyMigrationHandoffResult:
    code: LegacyMigrationHandoffCode
    authority: SlotAuthority | None = None
    projection: LivePositionProjection | None = None
    reason: QuarantineReason | LegacyProjectionReason | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            LegacyMigrationHandoffCode.APPLIED,
            LegacyMigrationHandoffCode.ALREADY_APPLIED,
        )


class RedisLegacyMigrationHandoffAdapter:
    """Strict two-record CAS for explicitly authorized legacy adoption."""

    _ABSENT = "__PM_ABSENT_V1__"

    def __init__(
        self,
        *,
        redis_get: Callable,
        redis_eval: Callable,
        redis_available: Callable[[], bool] | None = None,
    ) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval
        self._redis_available = redis_available

    def migrate(
        self,
        *,
        exchange_position_key: ExchangePositionKey,
        candidate_episode_id: str,
        legacy_row: dict,
        exchange_side: str,
        exchange_quantity: float,
        quantity_tolerance: float,
        mixed_version: bool,
        allow_authority_initialization: bool,
        operation_id: str,
        now: float,
    ) -> LegacyMigrationHandoffResult:
        if not isinstance(exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        if not isinstance(legacy_row, dict):
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.INVALID,
                reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            )
        if self._redis_available is not None:
            try:
                if self._redis_available() is not True:
                    return LegacyMigrationHandoffResult(
                        LegacyMigrationHandoffCode.UNAVAILABLE,
                        reason=QuarantineReason.BACKEND_UNAVAILABLE,
                    )
            except Exception as exc:  # noqa: BLE001 - backend boundary
                return LegacyMigrationHandoffResult(
                    LegacyMigrationHandoffCode.UNAVAILABLE,
                    reason=QuarantineReason.BACKEND_UNAVAILABLE,
                    message=str(exc),
                )

        authority_key = exchange_position_key.to_storage_key()
        projection_key = self._projection_key(exchange_position_key)
        try:
            raw_authority = self._redis_get(authority_key)
            raw_projection = self._redis_get(projection_key)
        except Exception as exc:  # noqa: BLE001 - pre-attempt read
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.UNAVAILABLE,
                reason=QuarantineReason.BACKEND_UNAVAILABLE,
                message=str(exc),
            )

        authority_read = self._authority_read(
            raw_authority, exchange_position_key
        )
        projection_read = self._projection_read(
            raw_projection, exchange_position_key
        )
        if authority_read.code is AuthorityReadCode.MALFORMED:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.MALFORMED,
                reason=QuarantineReason.MALFORMED_AUTHORITY,
                message=authority_read.message,
            )
        if projection_read.code is ProjectionReadCode.MALFORMED:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.MALFORMED,
                message=projection_read.message,
            )

        alias = legacy_row.get("position_id")
        plan = prepare_legacy_adoption(
            slot_key=exchange_position_key,
            candidate_episode_id=candidate_episode_id,
            legacy_position_id_alias=alias,
            local_symbol=legacy_row.get("symbol"),
            local_side=legacy_row.get("side"),
            local_quantity=legacy_row.get("qty"),
            exchange_side=exchange_side,
            exchange_quantity=exchange_quantity,
            quantity_tolerance=quantity_tolerance,
            authority_read=authority_read,
            mixed_version=mixed_version,
            allow_authority_initialization=allow_authority_initialization,
        )
        if plan.classification is AdoptionClassification.LEGACY_QUARANTINE:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.QUARANTINED,
                authority=plan.existing_authority,
                reason=plan.reason,
            )
        if plan.classification is AdoptionClassification.NATIVE_AUTHORIZED:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.EXISTING_OWNER,
                authority=plan.existing_authority,
            )
        if plan.classification is AdoptionClassification.CONFLICT:
            code = (
                LegacyMigrationHandoffCode.UNAVAILABLE
                if plan.reason is QuarantineReason.BACKEND_UNAVAILABLE
                else LegacyMigrationHandoffCode.CONFLICT
            )
            return LegacyMigrationHandoffResult(
                code, authority=plan.existing_authority, reason=plan.reason
            )
        if plan.classification is AdoptionClassification.INVALID:
            code = (
                LegacyMigrationHandoffCode.QUARANTINED
                if plan.reason is QuarantineReason.MISSING_LEGACY_ID
                else LegacyMigrationHandoffCode.INVALID
            )
            return LegacyMigrationHandoffResult(code, reason=plan.reason)

        proposed_authority = self._proposed_authority(plan, now=now)
        if proposed_authority is None:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.INVALID,
                reason=QuarantineReason.MALFORMED_AUTHORITY,
            )
        projection_result = prepare_legacy_projection(
            legacy_row=legacy_row,
            authority=proposed_authority,
            canonical_read=projection_read,
            operation_id=operation_id,
            now=now,
        )
        if projection_result.code not in (
            LegacyProjectionCode.READY,
            LegacyProjectionCode.ALREADY_PROJECTED,
        ):
            code = (
                LegacyMigrationHandoffCode.QUARANTINED
                if projection_result.code is LegacyProjectionCode.QUARANTINE
                else LegacyMigrationHandoffCode.INVALID
            )
            return LegacyMigrationHandoffResult(
                code,
                authority=proposed_authority,
                reason=projection_result.reason,
                message=projection_result.message,
            )
        proposed_projection = projection_result.projection
        assert proposed_projection is not None

        try:
            response = self._redis_eval(
                LEGACY_MIGRATION_HANDOFF_LUA,
                2,
                authority_key,
                projection_key,
                self._ABSENT,
                raw_authority if raw_authority is not None else self._ABSENT,
                raw_projection if raw_projection is not None else self._ABSENT,
                proposed_authority.to_json(),
                proposed_projection.to_json(),
            )
        except Exception as exc:  # noqa: BLE001 - ACK can be ambiguous
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.UNKNOWN, message=str(exc)
            )
        return self._parse_response(response, exchange_position_key)

    @staticmethod
    def _proposed_authority(plan, *, now: float) -> SlotAuthority | None:
        if plan.classification is AdoptionClassification.ALREADY_ADOPTED:
            return plan.existing_authority
        if plan.classification is not AdoptionClassification.LEGACY_ADOPTABLE:
            return None
        base = plan.existing_authority
        if base is None and plan.requires_authority_initialization:
            base = SlotAuthority.initial_flat(plan.slot_key, now=now)
        if not isinstance(base, SlotAuthority) or base.status is not AuthorityStatus.FLAT:
            return None
        return base.allocate_episode(
            episode_id=plan.candidate_episode_id,
            provenance=AuthorityProvenance.MIGRATED,
            status=AuthorityStatus.ACTIVE,
            legacy_position_id_alias=plan.legacy_position_id_alias,
            now=now,
        )

    @staticmethod
    def _authority_read(raw, key) -> AuthorityReadResult:
        if raw is None:
            return AuthorityReadResult(AuthorityReadCode.NOT_FOUND)
        try:
            authority = SlotAuthority.from_json(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return AuthorityReadResult(AuthorityReadCode.MALFORMED, message=str(exc))
        if authority.exchange_position_key != key:
            return AuthorityReadResult(
                AuthorityReadCode.MALFORMED,
                message="authority slot does not match storage key",
            )
        return AuthorityReadResult(AuthorityReadCode.FOUND, authority)

    @staticmethod
    def _projection_read(raw, key) -> ProjectionReadResult:
        if raw is None:
            return ProjectionReadResult(ProjectionReadCode.NOT_FOUND)
        try:
            projection = LivePositionProjection.from_json(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return ProjectionReadResult(ProjectionReadCode.MALFORMED, message=str(exc))
        if projection.exchange_position_key != key:
            return ProjectionReadResult(
                ProjectionReadCode.MALFORMED,
                message="projection slot does not match storage key",
            )
        return ProjectionReadResult(ProjectionReadCode.FOUND, projection)

    @classmethod
    def _parse_response(cls, response, key) -> LegacyMigrationHandoffResult:
        if not isinstance(response, (list, tuple)) or len(response) != 3:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.UNKNOWN,
                message="invalid Redis migration response",
            )
        raw_code, raw_authority, raw_projection = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return LegacyMigrationHandoffResult(
                    LegacyMigrationHandoffCode.UNKNOWN,
                    message="migration response code is not UTF-8",
                )
        try:
            code = LegacyMigrationHandoffCode(raw_code)
        except (TypeError, ValueError):
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.UNKNOWN,
                message=f"unknown migration response: {raw_code!r}",
            )
        if code is LegacyMigrationHandoffCode.CONFLICT:
            return LegacyMigrationHandoffResult(code)
        try:
            authority = SlotAuthority.from_json(raw_authority)
            projection = LivePositionProjection.from_json(raw_projection)
        except (TypeError, ValueError, UnicodeError) as exc:
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.MALFORMED, message=str(exc)
            )
        if (
            authority.exchange_position_key != key
            or projection.exchange_position_key != key
            or authority.episode_id != projection.episode_id
            or authority.slot_generation != projection.slot_generation
        ):
            return LegacyMigrationHandoffResult(
                LegacyMigrationHandoffCode.MALFORMED,
                message="migration acknowledgement identity mismatch",
            )
        return LegacyMigrationHandoffResult(code, authority, projection)

    @staticmethod
    def _projection_key(key: ExchangePositionKey) -> str:
        return (
            f"{RedisLivePositionProjectionAdapter.STORAGE_KEY_PREFIX}:"
            f"{key.canonical_digest()}"
        )
