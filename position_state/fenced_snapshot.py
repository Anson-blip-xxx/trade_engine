"""Atomic composition of canonical fences and the legacy snapshot CAS."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

from position_identity.authority import (
    AuthorityReadCode,
    AuthorityReadResult,
    SlotAuthority,
)
from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey
from position_identity.snapshot_fence import (
    CanonicalSnapshotRead,
    LegacySnapshotWritePlan,
    SnapshotFenceCode,
    plan_legacy_snapshot_write,
)
from position_state.strict_snapshot import (
    PM_POSITIONS_KEY,
    StrictRedisPositionSnapshotAdapter,
)

FENCED_SNAPSHOT_COMMIT_LUA = r"""
local absent = ARGV[1]
for index = 2, #KEYS do
  local current = redis.call('GET', KEYS[index])
  local expected = ARGV[index + 2]
  if expected == absent then
    if current ~= false then return {'STALE', redis.call('GET', KEYS[1]) or ''} end
  elseif current ~= expected then
    return {'STALE', redis.call('GET', KEYS[1]) or ''}
  end
end

local current_snapshot = redis.call('GET', KEYS[1])
local expected_snapshot = ARGV[2]
local proposed_snapshot = ARGV[3]
if current_snapshot == proposed_snapshot then
  return {'ALREADY_APPLIED', current_snapshot}
end
if expected_snapshot == absent then
  if current_snapshot ~= false then return {'STALE', current_snapshot} end
elseif current_snapshot ~= expected_snapshot then
  return {'STALE', current_snapshot or ''}
end
redis.call('SET', KEYS[1], proposed_snapshot)
return {'APPLIED', proposed_snapshot}
"""


class FencedSnapshotCommitCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    FILTERED_APPLIED = "FILTERED_APPLIED"
    FILTERED_ALREADY_APPLIED = "FILTERED_ALREADY_APPLIED"
    STALE = "STALE"
    FENCE_REJECTED = "FENCE_REJECTED"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class FencedSnapshotCommitResult:
    code: FencedSnapshotCommitCode
    positions: dict | None = None
    plan: LegacySnapshotWritePlan | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            FencedSnapshotCommitCode.APPLIED,
            FencedSnapshotCommitCode.ALREADY_APPLIED,
            FencedSnapshotCommitCode.FILTERED_APPLIED,
            FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED,
        )


class RedisFencedSnapshotCommitAdapter:
    """Plan and commit one snapshot while all canonical tokens stay exact."""

    _ABSENT = "__PM_FENCED_ABSENT_V1__"

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

    def commit(
        self,
        *,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
    ) -> FencedSnapshotCommitResult:
        try:
            StrictRedisPositionSnapshotAdapter._encode(proposed_snapshot)
        except (TypeError, ValueError) as exc:
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.INVALID, message=str(exc)
            )
        unavailable = self._availability()
        if unavailable:
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNAVAILABLE, message=unavailable
            )
        try:
            raw_snapshot = self._redis_get(PM_POSITIONS_KEY)
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNAVAILABLE, message=str(exc)
            )
        if raw_snapshot is None:
            current_snapshot = {}
        else:
            try:
                current_snapshot = StrictRedisPositionSnapshotAdapter._decode(
                    raw_snapshot
                )
            except (TypeError, ValueError, UnicodeError) as exc:
                return FencedSnapshotCommitResult(
                    FencedSnapshotCommitCode.MALFORMED, message=str(exc)
                )

        symbols = set(current_snapshot) | set(proposed_snapshot)
        if not isinstance(slots, Mapping) or set(slots) != symbols:
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.INVALID,
                message="slots must exactly cover current/proposed symbols",
            )
        for symbol, key in slots.items():
            if (
                not isinstance(key, ExchangePositionKey)
                or key.symbol != symbol
            ):
                return FencedSnapshotCommitResult(
                    FencedSnapshotCommitCode.INVALID,
                    message=f"invalid slot mapping for {symbol}",
                )

        keys = [PM_POSITIONS_KEY]
        canonical_tokens = []
        reads = {}
        try:
            for symbol in sorted(symbols):
                key = slots[symbol]
                authority_key = key.to_storage_key()
                projection_key = self._projection_key(key)
                raw_authority = self._redis_get(authority_key)
                raw_projection = self._redis_get(projection_key)
                keys.extend((authority_key, projection_key))
                canonical_tokens.extend(
                    (
                        raw_authority
                        if raw_authority is not None
                        else self._ABSENT,
                        raw_projection
                        if raw_projection is not None
                        else self._ABSENT,
                    )
                )
                reads[symbol] = CanonicalSnapshotRead(
                    self._authority_read(raw_authority, key),
                    self._projection_read(raw_projection, key),
                )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNAVAILABLE, message=str(exc)
            )

        plan = plan_legacy_snapshot_write(
            current_snapshot=current_snapshot,
            proposed_snapshot=proposed_snapshot,
            canonical_reads=reads,
        )
        if not plan.writable or plan.snapshot is None:
            code = (
                FencedSnapshotCommitCode.INVALID
                if plan.code is SnapshotFenceCode.INVALID
                else FencedSnapshotCommitCode.FENCE_REJECTED
            )
            return FencedSnapshotCommitResult(
                code, plan=plan, message=plan.message
            )
        planned = dict(plan.snapshot)
        try:
            encoded = StrictRedisPositionSnapshotAdapter._encode(planned)
        except (TypeError, ValueError) as exc:
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.INVALID, plan=plan, message=str(exc)
            )
        expected_snapshot = (
            raw_snapshot if raw_snapshot is not None else self._ABSENT
        )
        try:
            response = self._redis_eval(
                FENCED_SNAPSHOT_COMMIT_LUA,
                len(keys),
                *keys,
                self._ABSENT,
                expected_snapshot,
                encoded,
                *canonical_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - ACK can be ambiguous
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNKNOWN,
                plan=plan,
                message=str(exc),
            )
        return self._parse_response(response, plan)

    def _availability(self) -> str:
        if self._redis_available is None:
            return ""
        try:
            if self._redis_available() is True:
                return ""
            return "Redis unavailable before fenced snapshot commit"
        except Exception as exc:  # noqa: BLE001 - pre-attempt boundary
            return str(exc)

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
    def _parse_response(cls, response, plan) -> FencedSnapshotCommitResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNKNOWN,
                plan=plan,
                message="invalid fenced snapshot response",
            )
        raw_code, raw_snapshot = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return FencedSnapshotCommitResult(
                    FencedSnapshotCommitCode.UNKNOWN,
                    plan=plan,
                    message="fenced snapshot response code is not UTF-8",
                )
        if raw_code not in ("APPLIED", "ALREADY_APPLIED", "STALE"):
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.UNKNOWN,
                plan=plan,
                message=f"unknown fenced snapshot response: {raw_code!r}",
            )
        positions = None
        if raw_snapshot:
            try:
                positions = StrictRedisPositionSnapshotAdapter._decode(raw_snapshot)
            except (TypeError, ValueError, UnicodeError) as exc:
                return FencedSnapshotCommitResult(
                    FencedSnapshotCommitCode.MALFORMED,
                    plan=plan,
                    message=str(exc),
                )
        if raw_code == "STALE":
            return FencedSnapshotCommitResult(
                FencedSnapshotCommitCode.STALE, positions, plan
            )
        filtered = plan.code is SnapshotFenceCode.FILTERED
        if raw_code == "APPLIED":
            code = (
                FencedSnapshotCommitCode.FILTERED_APPLIED
                if filtered
                else FencedSnapshotCommitCode.APPLIED
            )
        else:
            code = (
                FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED
                if filtered
                else FencedSnapshotCommitCode.ALREADY_APPLIED
            )
        return FencedSnapshotCommitResult(code, positions, plan)

    @staticmethod
    def _projection_key(key: ExchangePositionKey) -> str:
        return (
            f"{RedisLivePositionProjectionAdapter.STORAGE_KEY_PREFIX}:"
            f"{key.canonical_digest()}"
        )
