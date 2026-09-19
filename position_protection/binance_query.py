"""Injected signed-GET transport for bounded Binance verification snapshots."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from position_identity.slot import ExchangePositionKey
from position_protection.verification_coordinator import BinanceVerificationSnapshot


@dataclass(frozen=True)
class BinanceVerificationEndpoints:
    position_risk_path: str
    open_algo_orders_path: str

    def __post_init__(self) -> None:
        position = self._path(self.position_risk_path, "position_risk_path")
        algo = self._path(self.open_algo_orders_path, "open_algo_orders_path")
        if position == algo:
            raise ValueError("verification endpoints must be distinct")
        object.__setattr__(self, "position_risk_path", position)
        object.__setattr__(self, "open_algo_orders_path", algo)

    @staticmethod
    def _path(value, field):
        if not isinstance(value, str) or not value.startswith("/"):
            raise ValueError(f"{field} must be an absolute API path")
        if "?" in value or "#" in value or any(ord(char) <= 32 for char in value):
            raise ValueError(f"{field} must not contain query, fragment, or whitespace")
        return value


class BinanceQueryErrorCode(str, Enum):
    POSITION_QUERY_FAILED = "POSITION_QUERY_FAILED"
    ALGO_QUERY_FAILED = "ALGO_QUERY_FAILED"
    INVALID_CLOCK = "INVALID_CLOCK"


class BinanceVerificationQueryError(RuntimeError):
    def __init__(self, code: BinanceQueryErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)


class InjectedBinanceVerificationQueryAdapter:
    """Two symbol-scoped signed GETs; credentials and HTTP stay outside."""

    def __init__(
        self,
        *,
        signed_get: Callable[[str, dict], object],
        clock: Callable[[], float],
        endpoints: BinanceVerificationEndpoints,
    ) -> None:
        if not callable(signed_get):
            raise TypeError("signed_get must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(endpoints, BinanceVerificationEndpoints):
            raise TypeError("endpoints must be BinanceVerificationEndpoints")
        self._signed_get = signed_get
        self._clock = clock
        self._endpoints = endpoints

    def fetch(self, key: ExchangePositionKey) -> BinanceVerificationSnapshot:
        if not isinstance(key, ExchangePositionKey):
            raise TypeError("key must be ExchangePositionKey")
        position = self._query(
            self._endpoints.position_risk_path,
            key.symbol,
            BinanceQueryErrorCode.POSITION_QUERY_FAILED,
        )
        algo = self._query(
            self._endpoints.open_algo_orders_path,
            key.symbol,
            BinanceQueryErrorCode.ALGO_QUERY_FAILED,
        )
        try:
            observed_at = self._clock()
        except Exception as exc:
            raise BinanceVerificationQueryError(
                BinanceQueryErrorCode.INVALID_CLOCK,
                "query completion clock failed",
            ) from exc
        if (
            isinstance(observed_at, bool)
            or not isinstance(observed_at, (int, float))
            or not math.isfinite(float(observed_at))
            or observed_at < 0
        ):
            raise BinanceVerificationQueryError(
                BinanceQueryErrorCode.INVALID_CLOCK,
                "query completion clock must be finite and nonnegative",
            )
        return BinanceVerificationSnapshot(position, algo, float(observed_at))

    def _query(self, path, symbol, error_code):
        try:
            payload = self._signed_get(path, {"symbol": symbol})
        except Exception as exc:
            raise BinanceVerificationQueryError(
                error_code, "signed query transport failed"
            ) from exc
        if not isinstance(payload, list):
            message = self._safe_error(payload)
            raise BinanceVerificationQueryError(error_code, message)
        return payload

    @staticmethod
    def _safe_error(payload) -> str:
        if not isinstance(payload, dict):
            return "signed query did not return a list"
        code = payload.get("code")
        message = payload.get("msg")
        safe_code = code if isinstance(code, (int, str)) else "unknown"
        safe_message = message if isinstance(message, str) else "invalid response"
        return f"Binance error code={safe_code}: {safe_message}"
