"""Unified Signal 校验测试辅助与 SignalValidation 层（validate_signal_payload）。"""
import pytest

from signals import SignalValidationError, signal_from_event
from signals.validation import validate_signal_payload


def test_validate_signal_payload_missing_symbol():
    errors = validate_signal_payload({'side': 'LONG', 'signal_type': 'T',
                                      'source': 'S3', 'strategy': 'S6'})
    assert any('symbol' in e for e in errors)


def test_validate_signal_payload_ok():
    errors = validate_signal_payload({'symbol': 'BTCUSDT', 'side': 'LONG',
                                      'signal_type': 'TREND_UP',
                                      'source': 'S3', 'strategy': 'S6'})
    assert errors == []


def test_validate_signal_payload_unknown_side():
    errors = validate_signal_payload({'symbol': 'X', 'side': 'UP',
                                      'signal_type': 'T', 'source': 'S3',
                                      'strategy': 'S6'})
    assert any('unknown side' in e for e in errors)


def test_adapter_raises_for_missing_symbol():
    with pytest.raises(SignalValidationError):
        signal_from_event({'type': 'T', 'strength': 70}, side='LONG', strategy='S6')
