"""Generation-fenced canonical ACTIVE-to-FLAT close finalization."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from position_identity.authority import (
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityStatus,
)
from position_identity.projection import ProjectionReadCode
from position_identity.slot import ExchangePositionKey


class CloseCaptureCode(str, Enum):
    CAPTURED = "CAPTURED"
    STALE = "STALE"
    NOT_FOUND = "NOT_FOUND"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"


class CloseFinalizeCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    STALE = "STALE"
    MALFORMED = "MALFORMED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class CanonicalCloseFence:
    exchange_position_key: ExchangePositionKey
    episode_id: str
    slot_generation: int
    authority_revision: int
    projection_revision: int


@dataclass(frozen=True)
class CloseCaptureResult:
    code: CloseCaptureCode
    fence: CanonicalCloseFence | None = None
    message: str = ""

    @property
    def captured(self) -> bool:
        return self.code is CloseCaptureCode.CAPTURED


@dataclass(frozen=True)
class CloseFinalizeResult:
    code: CloseFinalizeCode
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            CloseFinalizeCode.APPLIED,
            CloseFinalizeCode.ALREADY_APPLIED,
        )


class CanonicalCloseFinalizer:
    """Capture local ownership before close and release only that generation."""

    def __init__(self, *, authority_store, projection_store) -> None:
        self._authority_store = authority_store
        self._projection_store = projection_store

    def capture(
        self, key: ExchangePositionKey, *, local_position: dict
    ) -> CloseCaptureResult:
        if not isinstance(key, ExchangePositionKey):
            raise TypeError("key must be ExchangePositionKey")
        if not isinstance(local_position, dict):
            raise TypeError("local_position must be a dict")
        authority_read = self._authority_store.get_slot_authority(key)
        if authority_read.code is AuthorityReadCode.NOT_FOUND:
            return CloseCaptureResult(CloseCaptureCode.NOT_FOUND)
        if authority_read.code is AuthorityReadCode.MALFORMED:
            return CloseCaptureResult(
                CloseCaptureCode.MALFORMED, message=authority_read.message
            )
        if authority_read.code is not AuthorityReadCode.FOUND:
            return CloseCaptureResult(
                CloseCaptureCode.UNAVAILABLE, message=authority_read.message
            )
        projection_read = self._projection_store.get(key)
        if projection_read.code is ProjectionReadCode.NOT_FOUND:
            return CloseCaptureResult(CloseCaptureCode.NOT_FOUND)
        if projection_read.code is ProjectionReadCode.MALFORMED:
            return CloseCaptureResult(
                CloseCaptureCode.MALFORMED, message=projection_read.message
            )
        if projection_read.code is not ProjectionReadCode.FOUND:
            return CloseCaptureResult(
                CloseCaptureCode.UNAVAILABLE, message=projection_read.message
            )

        authority = authority_read.authority
        projection = projection_read.projection
        assert authority is not None and projection is not None
        if not self._matches(authority, projection, local_position):
            return CloseCaptureResult(CloseCaptureCode.STALE)
        return CloseCaptureResult(
            CloseCaptureCode.CAPTURED,
            CanonicalCloseFence(
                exchange_position_key=key,
                episode_id=authority.episode_id,
                slot_generation=authority.slot_generation,
                authority_revision=authority.revision,
                projection_revision=projection.state_revision,
            ),
        )

    def finalize(
        self, fence: CanonicalCloseFence, *, now: float
    ) -> CloseFinalizeResult:
        if not isinstance(fence, CanonicalCloseFence):
            raise TypeError("fence must be CanonicalCloseFence")
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or now < 0
        ):
            raise ValueError("now must be finite and nonnegative")
        current = self._authority_store.get_slot_authority(
            fence.exchange_position_key
        )
        if current.code is AuthorityReadCode.MALFORMED:
            return CloseFinalizeResult(
                CloseFinalizeCode.MALFORMED, current.message
            )
        if current.code is not AuthorityReadCode.FOUND:
            code = (
                CloseFinalizeCode.STALE
                if current.code is AuthorityReadCode.NOT_FOUND
                else CloseFinalizeCode.UNAVAILABLE
            )
            return CloseFinalizeResult(code, current.message)
        authority = current.authority
        assert authority is not None
        same_identity = (
            authority.episode_id == fence.episode_id
            and authority.slot_generation == fence.slot_generation
        )
        if (
            same_identity
            and authority.status is AuthorityStatus.FLAT
            and authority.revision == fence.authority_revision + 1
        ):
            return CloseFinalizeResult(CloseFinalizeCode.ALREADY_APPLIED)
        if not (
            same_identity
            and authority.status is AuthorityStatus.ACTIVE
            and authority.revision == fence.authority_revision
        ):
            return CloseFinalizeResult(CloseFinalizeCode.STALE)
        ack = self._authority_store.transition_active_to_flat(
            fence.exchange_position_key,
            expected_episode_id=fence.episode_id,
            expected_slot_generation=fence.slot_generation,
            expected_revision=fence.authority_revision,
            now=now,
        )
        if ack.code is AuthorityAckCode.APPLIED:
            return CloseFinalizeResult(CloseFinalizeCode.APPLIED)
        if ack.code is AuthorityAckCode.MALFORMED:
            return CloseFinalizeResult(
                CloseFinalizeCode.MALFORMED, ack.message
            )
        if ack.code is AuthorityAckCode.BACKEND_ERROR:
            return CloseFinalizeResult(
                CloseFinalizeCode.UNAVAILABLE, ack.message
            )
        return CloseFinalizeResult(CloseFinalizeCode.STALE, ack.message)

    @staticmethod
    def _matches(authority, projection, local_position) -> bool:
        try:
            local_side = str(local_position["side"]).strip().upper()
            local_system = str(local_position["system"]).strip()
            local_entry = float(local_position["entry"])
            local_opened = float(local_position["open_time"])
            local_quantity = float(local_position["qty"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            authority.status is AuthorityStatus.ACTIVE
            and authority.provenance is AuthorityProvenance.NATIVE
            and authority.episode_id == projection.episode_id
            and authority.slot_generation == projection.slot_generation
            and projection.side == local_side
            and projection.system == local_system
            and projection.entry_price == local_entry
            and projection.opened_at == local_opened
            and 0 < local_quantity <= projection.quantity
        )
