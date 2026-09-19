"""Strict Binance payload normalization and bounded verification retry."""


import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.binance_observation import (
    BinanceObservationCode,
    normalize_binance_verification_snapshot,
)
from position_protection.verification import (
    ProtectionVerificationCode,
    ProtectionVerificationResult,
)
from position_protection.verification_retry import (
    VerificationRetryAction,
    VerificationRetryPolicy,
    decide_verification_retry,
)


def _key():
    return ExchangePositionKey.one_way(account_principal_id="binance-normalize", environment="SANDBOX", symbol="BTCUSDT")


def _position(**changes):
    row = {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "2.0"}
    row.update(changes)
    return row


def _order(**changes):
    row = {"algoId": 77, "algoType": "CONDITIONAL", "orderType": "STOP_MARKET", "symbol": "BTCUSDT", "side": "SELL", "positionSide": "BOTH", "quantity": "2.0", "algoStatus": "WORKING", "triggerPrice": "90.0", "closePosition": False, "reduceOnly": True, "updateTime": 1}
    row.update(changes)
    return row


def _normalize(position=None, orders=None):
    return normalize_binance_verification_snapshot(exchange_position_key=_key(), position_risk_payload=[_position()] if position is None else position, open_algo_orders_payload=[_order()] if orders is None else orders, observed_at=100)


def test_new_algo_schema_normalizes_at_query_completion_time():
    result = _normalize()
    assert result.code is BinanceObservationCode.NORMALIZED
    assert result.exposure.side == "LONG"
    assert result.exposure.quantity == 2
    assert result.orders[0].algo_alias == "77"
    assert result.orders[0].observed_at == 100


def test_negative_one_way_position_is_short():
    result = _normalize(position=[_position(positionAmt="-3")])
    assert result.exposure.side == "SHORT"
    assert result.exposure.quantity == 3


@pytest.mark.parametrize("payload", [None, {}, "bad"])
def test_top_level_payloads_must_be_lists(payload):
    result = normalize_binance_verification_snapshot(exchange_position_key=_key(), position_risk_payload=payload, open_algo_orders_payload=[], observed_at=1)
    assert result.code is BinanceObservationCode.MALFORMED


def test_missing_or_duplicate_position_is_typed():
    assert _normalize(position=[]).code is BinanceObservationCode.POSITION_NOT_FOUND
    assert _normalize(position=[_position(), _position()]).code is BinanceObservationCode.AMBIGUOUS_POSITION


@pytest.mark.parametrize("field", ["algoId", "algoType", "orderType", "quantity", "algoStatus", "triggerPrice", "closePosition", "reduceOnly"])
def test_required_new_algo_schema_fields_fail_closed(field):
    row = _order()
    row.pop(field)
    assert _normalize(orders=[row]).code is BinanceObservationCode.MALFORMED


@pytest.mark.parametrize("changes", [{"algoType": "LEGACY"}, {"closePosition": True}, {"reduceOnly": "true"}, {"quantity": "nan"}, {"triggerPrice": "0"}])
def test_unsupported_or_malformed_order_values_fail_closed(changes):
    assert _normalize(orders=[_order(**changes)]).code is BinanceObservationCode.MALFORMED


def test_other_symbol_and_slot_orders_are_ignored():
    result = _normalize(orders=[_order(symbol="ETHUSDT"), _order(positionSide="LONG")])
    assert result.normalized
    assert result.orders == ()


def _result(code):
    return ProtectionVerificationResult(code)


def _policy():
    return VerificationRetryPolicy(max_attempts=4, deadline_seconds=20, base_delay_seconds=2, max_delay_seconds=5)


def test_verified_commits_and_hard_failures_quarantine():
    verified = decide_verification_retry(result=_result(ProtectionVerificationCode.VERIFIED), attempts_completed=1, started_at=10, now=10, policy=_policy())
    hard = decide_verification_retry(result=_result(ProtectionVerificationCode.IDENTITY_MISMATCH), attempts_completed=1, started_at=10, now=10, policy=_policy())
    assert verified.action is VerificationRetryAction.COMMIT_VERIFIED
    assert hard.action is VerificationRetryAction.QUARANTINE


@pytest.mark.parametrize("code", [ProtectionVerificationCode.STALE_EVIDENCE, ProtectionVerificationCode.ORDER_NOT_FOUND, ProtectionVerificationCode.ORDER_NOT_ACTIVE])
def test_transient_observations_receive_bounded_backoff(code):
    decision = decide_verification_retry(result=_result(code), attempts_completed=2, started_at=10, now=12, policy=_policy())
    assert decision.action is VerificationRetryAction.QUERY_AGAIN
    assert decision.retry_at == 16


def test_attempt_and_deadline_exhaustion_never_query_again():
    by_attempt = decide_verification_retry(result=_result(ProtectionVerificationCode.ORDER_NOT_FOUND), attempts_completed=4, started_at=10, now=12, policy=_policy())
    by_deadline = decide_verification_retry(result=_result(ProtectionVerificationCode.ORDER_NOT_FOUND), attempts_completed=2, started_at=10, now=29, policy=_policy())
    assert by_attempt.action is VerificationRetryAction.EXHAUSTED
    assert by_deadline.action is VerificationRetryAction.EXHAUSTED
