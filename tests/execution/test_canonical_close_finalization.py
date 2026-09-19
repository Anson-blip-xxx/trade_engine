"""Canonical close finalization is gated by exchange-flat evidence."""

import time


def _pos(**changes):
    value = {
        "entry": 2.0,
        "qty": 10.0,
        "original_qty": 10.0,
        "side": "SHORT",
        "system": "S8",
        "open_time": time.time(),
        "leverage": 3,
        "sl": 2.2,
        "event_type": "TREND_DOWN",
        "score": 66,
    }
    value.update(changes)
    return value


def _spies(close_env, monkeypatch):
    calls = {"capture": [], "finalize": []}
    fence = object()
    monkeypatch.setattr(
        close_env["pm"],
        "_capture_canonical_close",
        lambda symbol, pos: calls["capture"].append((symbol, pos)) or fence,
    )
    monkeypatch.setattr(
        close_env["pm"],
        "_finalize_canonical_close",
        lambda value: calls["finalize"].append(value),
    )
    return calls, fence


def test_full_close_finalizes_captured_generation(close_env, monkeypatch):
    calls, fence = _spies(close_env, monkeypatch)
    pos = _pos()
    assert close_env["pm"]._close(
        "AUSDT", pos, 1.9, "硬止损", {"AUSDT": pos}
    ) is True
    assert calls == {"capture": [("AUSDT", pos)], "finalize": [fence]}


def test_already_flat_finalizes_captured_generation(close_env, monkeypatch):
    calls, fence = _spies(close_env, monkeypatch)
    close_env["set_risk_responses"]([[]])
    pos = _pos()
    assert close_env["pm"]._close(
        "AUSDT", pos, 1.9, "硬止损", {"AUSDT": pos}
    ) is True
    assert calls["finalize"] == [fence]


def test_partial_close_does_not_release_slot(close_env, monkeypatch):
    calls, _fence = _spies(close_env, monkeypatch)
    close_env["set_risk_responses"]([
        [{"symbol": "AUSDT", "positionAmt": "-10"}],
        [{"symbol": "AUSDT", "positionAmt": "-6"}],
    ])
    close_env["set_order_result"](
        {"orderId": 1, "status": "FILLED", "executedQty": "4"}
    )
    pos = _pos()
    assert close_env["pm"]._close(
        "AUSDT", pos, 1.9, "硬止损", {"AUSDT": pos}
    ) is False
    assert len(calls["capture"]) == 1
    assert calls["finalize"] == []


def test_rejected_close_does_not_release_slot(close_env, monkeypatch):
    calls, _fence = _spies(close_env, monkeypatch)
    close_env["set_order_result"]({"code": -2019, "msg": "rejected"})
    pos = _pos()
    assert close_env["pm"]._close(
        "AUSDT", pos, 1.9, "硬止损", {"AUSDT": pos}
    ) is False
    assert len(calls["capture"]) == 1
    assert calls["finalize"] == []
