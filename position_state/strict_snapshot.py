"""Strict typed CAS boundary for the legacy pm:positions snapshot."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

PM_POSITIONS_KEY = "pm:positions"

STRICT_SNAPSHOT_CAS_LUA = r"""
local current = redis.call('GET', KEYS[1])
local absent = ARGV[1]
local expected = ARGV[2]
local proposed = ARGV[3]
if current == proposed then
  return {'ALREADY_APPLIED', current}
end
if expected == absent then
  if current ~= false then return {'STALE', current} end
elseif current ~= expected then
  return {'STALE', current or ''}
end
redis.call('SET', KEYS[1], proposed)
return {'APPLIED', proposed}
"""


class StrictSnapshotReadCode(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"


class StrictSnapshotWriteCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    STALE = "STALE"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class StrictSnapshotRead:
    code: StrictSnapshotReadCode
    positions: dict | None = None
    raw_token: str | bytes | None = None
    message: str = ""


@dataclass(frozen=True)
class StrictSnapshotWriteResult:
    code: StrictSnapshotWriteCode
    positions: dict | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            StrictSnapshotWriteCode.APPLIED,
            StrictSnapshotWriteCode.ALREADY_APPLIED,
        )


class StrictRedisPositionSnapshotAdapter:
    """No-fallback read and compare-and-set for the whole legacy snapshot."""

    _ABSENT = "__PM_POSITIONS_ABSENT_V1__"

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

    def read(self) -> StrictSnapshotRead:
        unavailable = self._availability()
        if unavailable:
            return StrictSnapshotRead(
                StrictSnapshotReadCode.UNAVAILABLE, message=unavailable
            )
        try:
            raw = self._redis_get(PM_POSITIONS_KEY)
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return StrictSnapshotRead(
                StrictSnapshotReadCode.UNAVAILABLE, message=str(exc)
            )
        if raw is None:
            return StrictSnapshotRead(StrictSnapshotReadCode.NOT_FOUND)
        try:
            positions = self._decode(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            return StrictSnapshotRead(
                StrictSnapshotReadCode.MALFORMED,
                raw_token=raw,
                message=str(exc),
            )
        return StrictSnapshotRead(
            StrictSnapshotReadCode.FOUND,
            positions=positions,
            raw_token=raw,
        )

    def compare_and_set(
        self, expected: StrictSnapshotRead, proposed: dict
    ) -> StrictSnapshotWriteResult:
        if not isinstance(expected, StrictSnapshotRead):
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.INVALID,
                message="expected must be StrictSnapshotRead",
            )
        if expected.code not in (
            StrictSnapshotReadCode.FOUND,
            StrictSnapshotReadCode.NOT_FOUND,
        ):
            code = (
                StrictSnapshotWriteCode.UNAVAILABLE
                if expected.code is StrictSnapshotReadCode.UNAVAILABLE
                else StrictSnapshotWriteCode.MALFORMED
            )
            return StrictSnapshotWriteResult(code, message=expected.message)
        try:
            encoded = self._encode(proposed)
        except (TypeError, ValueError) as exc:
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.INVALID, message=str(exc)
            )
        if expected.code is StrictSnapshotReadCode.FOUND:
            if expected.raw_token is None or expected.positions is None:
                return StrictSnapshotWriteResult(
                    StrictSnapshotWriteCode.INVALID,
                    message="FOUND read requires positions and raw token",
                )
            try:
                if self._decode(expected.raw_token) != expected.positions:
                    return StrictSnapshotWriteResult(
                        StrictSnapshotWriteCode.INVALID,
                        message="read token does not match positions",
                    )
            except (TypeError, ValueError, UnicodeError) as exc:
                return StrictSnapshotWriteResult(
                    StrictSnapshotWriteCode.INVALID, message=str(exc)
                )
        elif expected.raw_token is not None or expected.positions is not None:
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.INVALID,
                message="NOT_FOUND read cannot carry state",
            )

        unavailable = self._availability()
        if unavailable:
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.UNAVAILABLE, message=unavailable
            )
        expected_token = (
            expected.raw_token
            if expected.code is StrictSnapshotReadCode.FOUND
            else self._ABSENT
        )
        try:
            response = self._redis_eval(
                STRICT_SNAPSHOT_CAS_LUA,
                1,
                PM_POSITIONS_KEY,
                self._ABSENT,
                expected_token,
                encoded,
            )
        except Exception as exc:  # noqa: BLE001 - ACK can be ambiguous
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.UNKNOWN, message=str(exc)
            )
        return self._parse_response(response)

    def _availability(self) -> str:
        if self._redis_available is None:
            return ""
        try:
            if self._redis_available() is True:
                return ""
            return "Redis unavailable before strict snapshot operation"
        except Exception as exc:  # noqa: BLE001 - pre-attempt boundary
            return str(exc)

    @classmethod
    def _parse_response(cls, response) -> StrictSnapshotWriteResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.UNKNOWN,
                message="invalid strict snapshot response",
            )
        raw_code, raw_snapshot = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return StrictSnapshotWriteResult(
                    StrictSnapshotWriteCode.UNKNOWN,
                    message="strict snapshot response code is not UTF-8",
                )
        try:
            code = StrictSnapshotWriteCode(raw_code)
        except (TypeError, ValueError):
            return StrictSnapshotWriteResult(
                StrictSnapshotWriteCode.UNKNOWN,
                message=f"unknown strict snapshot response: {raw_code!r}",
            )
        positions = None
        if raw_snapshot:
            try:
                positions = cls._decode(raw_snapshot)
            except (TypeError, ValueError, UnicodeError) as exc:
                return StrictSnapshotWriteResult(
                    StrictSnapshotWriteCode.MALFORMED, message=str(exc)
                )
        return StrictSnapshotWriteResult(code, positions)

    @staticmethod
    def _decode(raw) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise TypeError("snapshot payload must be text")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("snapshot payload is not valid JSON") from exc
        if not isinstance(value, dict) or not all(
            isinstance(symbol, str)
            and bool(symbol.strip())
            and symbol == symbol.strip().upper()
            and isinstance(row, dict)
            for symbol, row in value.items()
        ):
            raise ValueError("snapshot must map canonical symbols to objects")
        return value

    @classmethod
    def _encode(cls, positions) -> str:
        if not isinstance(positions, dict):
            raise TypeError("positions must be a dict")
        encoded = json.dumps(
            positions,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        cls._decode(encoded)
        return encoded
