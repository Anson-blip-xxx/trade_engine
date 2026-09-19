"""Strict normalization of Binance verification query payloads."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from position_identity.slot import ExchangePositionKey, PositionMode, SlotSide
from position_protection.verification import (
    ExchangeExposureObservation,
    ExchangeProtectionObservation,
)


class BinanceObservationCode(str, Enum):
    NORMALIZED = "NORMALIZED"
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    AMBIGUOUS_POSITION = "AMBIGUOUS_POSITION"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class BinanceObservationResult:
    code: BinanceObservationCode
    exposure: ExchangeExposureObservation | None = None
    orders: tuple[ExchangeProtectionObservation, ...] = ()
    message: str = ""

    @property
    def normalized(self) -> bool:
        return self.code is BinanceObservationCode.NORMALIZED


def normalize_binance_verification_snapshot(
    *,
    exchange_position_key: ExchangePositionKey,
    position_risk_payload,
    open_algo_orders_payload,
    observed_at: float,
) -> BinanceObservationResult:
    """Normalize one query-completion snapshot; never infer legacy fields."""
    if not isinstance(exchange_position_key, ExchangePositionKey):
        raise TypeError("exchange_position_key must be ExchangePositionKey")
    observed_at = _timestamp(observed_at)
    if not isinstance(position_risk_payload, list):
        return BinanceObservationResult(
            BinanceObservationCode.MALFORMED,
            message="positionRisk payload must be a list",
        )
    if not isinstance(open_algo_orders_payload, list):
        return BinanceObservationResult(
            BinanceObservationCode.MALFORMED,
            message="openAlgoOrders payload must be a list",
        )

    expected_position_side = exchange_position_key.slot_side.value
    matching_positions = []
    for row in position_risk_payload:
        if not isinstance(row, dict):
            return BinanceObservationResult(
                BinanceObservationCode.MALFORMED,
                message="positionRisk row must be an object",
            )
        if row.get("symbol") == exchange_position_key.symbol:
            if row.get("positionSide") != expected_position_side:
                continue
            matching_positions.append(row)
    if not matching_positions:
        return BinanceObservationResult(BinanceObservationCode.POSITION_NOT_FOUND)
    if len(matching_positions) != 1:
        return BinanceObservationResult(BinanceObservationCode.AMBIGUOUS_POSITION)
    try:
        exposure = _normalize_exposure(
            exchange_position_key, matching_positions[0], observed_at
        )
    except (TypeError, ValueError) as exc:
        return BinanceObservationResult(
            BinanceObservationCode.MALFORMED, message=str(exc)
        )

    orders = []
    try:
        for row in open_algo_orders_payload:
            if not isinstance(row, dict):
                raise TypeError("openAlgoOrders row must be an object")
            if row.get("symbol") != exchange_position_key.symbol:
                continue
            if row.get("positionSide") != expected_position_side:
                continue
            orders.append(_normalize_order(exchange_position_key, row, observed_at))
    except (TypeError, ValueError) as exc:
        return BinanceObservationResult(
            BinanceObservationCode.MALFORMED, message=str(exc)
        )
    return BinanceObservationResult(
        BinanceObservationCode.NORMALIZED, exposure, tuple(orders)
    )


def _normalize_exposure(key, row, observed_at):
    if {"symbol", "positionSide", "positionAmt"}.difference(row):
        raise ValueError("positionRisk row lacks required fields")
    amount = _finite_number(row["positionAmt"], "positionAmt")
    if amount == 0:
        raise ValueError("positionAmt must be nonzero")
    if key.position_mode is PositionMode.ONE_WAY:
        side = "LONG" if amount > 0 else "SHORT"
    elif key.slot_side is SlotSide.LONG:
        if amount <= 0:
            raise ValueError("LONG hedge slot requires positive positionAmt")
        side = "LONG"
    else:
        if amount >= 0:
            raise ValueError("SHORT hedge slot requires negative positionAmt")
        side = "SHORT"
    return ExchangeExposureObservation(key, side, abs(amount), observed_at)


def _normalize_order(key, row, observed_at):
    required = {
        "algoId",
        "algoType",
        "orderType",
        "symbol",
        "side",
        "positionSide",
        "quantity",
        "algoStatus",
        "triggerPrice",
        "closePosition",
        "reduceOnly",
    }
    if required.difference(row):
        raise ValueError("openAlgoOrders row lacks required new-schema fields")
    if row["algoType"] != "CONDITIONAL":
        raise ValueError("algoType must be CONDITIONAL")
    if row["closePosition"] is not False:
        raise ValueError("closePosition orders are unsupported by quantity verifier")
    if not isinstance(row["reduceOnly"], bool):
        raise TypeError("reduceOnly must be boolean")
    alias = row["algoId"]
    if isinstance(alias, bool) or not isinstance(alias, (int, str)):
        raise TypeError("algoId must be integer or string")
    alias = str(alias).strip()
    if not alias:
        raise ValueError("algoId is required")
    return ExchangeProtectionObservation(
        exchange_position_key=key,
        algo_alias=alias,
        status=_required_text(row["algoStatus"], "algoStatus"),
        order_type=_required_text(row["orderType"], "orderType"),
        reduce_only=row["reduceOnly"],
        closing_side=_required_text(row["side"], "side"),
        trigger_price=_positive_number(row["triggerPrice"], "triggerPrice"),
        quantity=_positive_number(row["quantity"], "quantity"),
        observed_at=observed_at,
    )


def _required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _finite_number(value, field):
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def _positive_number(value, field):
    value = _finite_number(value, field)
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _timestamp(value):
    value = _finite_number(value, "observed_at")
    if value < 0:
        raise ValueError("observed_at must be nonnegative")
    return value
