"""Authority-fenced canonical projection quantity reduction."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from position_identity.authority import AuthorityStatus, SlotAuthority
from position_identity.projection import LivePositionProjection
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey

REDUCE_PROJECTION_QUANTITY_LUA = r"""
local authority = redis.call('GET', KEYS[1])
local projection = redis.call('GET', KEYS[2])
if authority ~= ARGV[1] then
  return {'STALE', projection or ''}
end
if projection == ARGV[3] then
  return {'ALREADY_APPLIED', projection}
end
if projection ~= ARGV[2] then
  return {'STALE', projection or ''}
end
redis.call('SET', KEYS[2], ARGV[3])
return {'APPLIED', ARGV[3]}
"""


class ProjectionMutationCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    STALE = "STALE"
    NOT_FOUND = "NOT_FOUND"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ProjectionMutationResult:
    code: ProjectionMutationCode
    projection: LivePositionProjection | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            ProjectionMutationCode.APPLIED,
            ProjectionMutationCode.ALREADY_APPLIED,
        )


class RedisProjectionMutationAdapter:
    """Reduce quantity only while exact ACTIVE authority remains current."""

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

    def reduce_quantity(
        self,
        exchange_position_key: ExchangePositionKey,
        *,
        expected_episode_id: str,
        expected_slot_generation: int,
        expected_authority_revision: int,
        expected_projection_revision: int,
        new_quantity: float,
        operation_id: str,
        now: float,
    ) -> ProjectionMutationResult:
        invalid = self._validate_request(
            exchange_position_key=exchange_position_key,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            expected_authority_revision=expected_authority_revision,
            expected_projection_revision=expected_projection_revision,
            new_quantity=new_quantity,
            operation_id=operation_id,
            now=now,
        )
        if invalid:
            return ProjectionMutationResult(
                ProjectionMutationCode.INVALID, message=invalid
            )
        if self._redis_available is not None:
            try:
                if self._redis_available() is not True:
                    return ProjectionMutationResult(
                        ProjectionMutationCode.UNAVAILABLE,
                        message="Redis unavailable before projection mutation",
                    )
            except Exception as exc:  # noqa: BLE001 - backend boundary
                return ProjectionMutationResult(
                    ProjectionMutationCode.UNAVAILABLE, message=str(exc)
                )

        authority_key = exchange_position_key.to_storage_key()
        projection_key = self._projection_key(exchange_position_key)
        try:
            raw_authority = self._redis_get(authority_key)
            raw_projection = self._redis_get(projection_key)
        except Exception as exc:  # noqa: BLE001 - pre-attempt read
            return ProjectionMutationResult(
                ProjectionMutationCode.UNAVAILABLE, message=str(exc)
            )
        if raw_authority is None or raw_projection is None:
            return ProjectionMutationResult(ProjectionMutationCode.NOT_FOUND)
        try:
            authority = SlotAuthority.from_json(raw_authority)
            projection = LivePositionProjection.from_json(raw_projection)
        except (TypeError, ValueError, UnicodeError) as exc:
            return ProjectionMutationResult(
                ProjectionMutationCode.MALFORMED, message=str(exc)
            )
        if (
            authority.exchange_position_key != exchange_position_key
            or projection.exchange_position_key != exchange_position_key
        ):
            return ProjectionMutationResult(
                ProjectionMutationCode.MALFORMED,
                message="canonical record slot does not match storage key",
            )

        if self._is_applied_retry(
            authority=authority,
            projection=projection,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            expected_authority_revision=expected_authority_revision,
            expected_projection_revision=expected_projection_revision,
            new_quantity=new_quantity,
            operation_id=operation_id,
        ):
            return ProjectionMutationResult(
                ProjectionMutationCode.ALREADY_APPLIED, projection
            )
        if not self._matches_expected(
            authority=authority,
            projection=projection,
            expected_episode_id=expected_episode_id,
            expected_slot_generation=expected_slot_generation,
            expected_authority_revision=expected_authority_revision,
            expected_projection_revision=expected_projection_revision,
        ):
            return ProjectionMutationResult(
                ProjectionMutationCode.STALE, projection
            )
        if new_quantity >= projection.quantity:
            return ProjectionMutationResult(
                ProjectionMutationCode.INVALID,
                projection,
                "new_quantity must strictly reduce current quantity",
            )
        try:
            proposed = projection.revise(
                quantity=new_quantity, operation_id=operation_id, now=now
            )
        except (TypeError, ValueError) as exc:
            return ProjectionMutationResult(
                ProjectionMutationCode.INVALID, projection, str(exc)
            )

        try:
            response = self._redis_eval(
                REDUCE_PROJECTION_QUANTITY_LUA,
                2,
                authority_key,
                projection_key,
                raw_authority,
                raw_projection,
                proposed.to_json(),
            )
        except Exception as exc:  # noqa: BLE001 - ACK can be ambiguous
            return ProjectionMutationResult(
                ProjectionMutationCode.UNKNOWN, message=str(exc)
            )
        return self._parse_response(response, exchange_position_key)

    @staticmethod
    def _matches_expected(
        *,
        authority,
        projection,
        expected_episode_id,
        expected_slot_generation,
        expected_authority_revision,
        expected_projection_revision,
    ) -> bool:
        return (
            authority.status is AuthorityStatus.ACTIVE
            and authority.episode_id == expected_episode_id
            and authority.slot_generation == expected_slot_generation
            and authority.revision == expected_authority_revision
            and projection.episode_id == expected_episode_id
            and projection.slot_generation == expected_slot_generation
            and projection.state_revision == expected_projection_revision
            and authority.provenance is projection.identity_provenance
            and authority.legacy_position_id_alias
            == projection.legacy_position_id_alias
        )

    @classmethod
    def _is_applied_retry(cls, *, projection, operation_id, new_quantity, **state):
        return (
            projection.last_operation_id == operation_id
            and projection.quantity == new_quantity
            and projection.state_revision
            == state["expected_projection_revision"] + 1
            and cls._matches_expected(
                projection=projection,
                expected_projection_revision=projection.state_revision,
                **{key: value for key, value in state.items() if key != "expected_projection_revision"},
            )
        )

    @staticmethod
    def _validate_request(**values) -> str:
        if not isinstance(values["exchange_position_key"], ExchangePositionKey):
            return "exchange_position_key must be ExchangePositionKey"
        try:
            if str(UUID(values["expected_episode_id"])) != values[
                "expected_episode_id"
            ]:
                return "expected_episode_id must be canonical UUID"
        except (TypeError, ValueError, AttributeError):
            return "expected_episode_id must be canonical UUID"
        for field in (
            "expected_slot_generation",
            "expected_authority_revision",
            "expected_projection_revision",
        ):
            value = values[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return f"{field} must be a positive integer"
        quantity = values["new_quantity"]
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(float(quantity))
            or quantity <= 0
        ):
            return "new_quantity must be finite and positive"
        operation_id = values["operation_id"]
        if not isinstance(operation_id, str) or not operation_id.strip():
            return "operation_id is required"
        now = values["now"]
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or now < 0
        ):
            return "now must be finite and nonnegative"
        return ""

    @staticmethod
    def _parse_response(response, key) -> ProjectionMutationResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return ProjectionMutationResult(
                ProjectionMutationCode.UNKNOWN,
                message="invalid Redis projection-mutation response",
            )
        raw_code, raw_projection = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return ProjectionMutationResult(
                    ProjectionMutationCode.UNKNOWN,
                    message="projection-mutation response code is not UTF-8",
                )
        try:
            code = ProjectionMutationCode(raw_code)
        except (TypeError, ValueError):
            return ProjectionMutationResult(
                ProjectionMutationCode.UNKNOWN,
                message=f"unknown projection-mutation response: {raw_code!r}",
            )
        projection = None
        if raw_projection:
            try:
                projection = LivePositionProjection.from_json(raw_projection)
            except (TypeError, ValueError, UnicodeError) as exc:
                return ProjectionMutationResult(
                    ProjectionMutationCode.MALFORMED, message=str(exc)
                )
            if projection.exchange_position_key != key:
                return ProjectionMutationResult(
                    ProjectionMutationCode.MALFORMED,
                    message="projection slot does not match mutation key",
                )
        return ProjectionMutationResult(code, projection)

    @staticmethod
    def _projection_key(key: ExchangePositionKey) -> str:
        return (
            f"{RedisLivePositionProjectionAdapter.STORAGE_KEY_PREFIX}:"
            f"{key.canonical_digest()}"
        )
