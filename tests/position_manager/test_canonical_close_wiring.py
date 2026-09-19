"""Ghost and reconcile flat evidence drive canonical close finalization."""

from shared import position_manager as pm


def _canonical_spies(monkeypatch):
    calls = {"capture": [], "finalize": []}
    fence = object()
    monkeypatch.setattr(
        pm,
        "_capture_canonical_close",
        lambda symbol, pos: calls["capture"].append((symbol, pos)) or fence,
    )
    monkeypatch.setattr(
        pm,
        "_finalize_canonical_close",
        lambda value: calls["finalize"].append(value),
    )
    return calls, fence


def _base(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, "_rget", fake_redis.get)
    monkeypatch.setattr(pm, "_rset", fake_redis.set)
    monkeypatch.setattr("shared.redis_store.delete", fake_redis.delete)
    monkeypatch.setattr(pm, "_sandbox_active", lambda: False)
    monkeypatch.setattr(pm, "_pmlog", lambda *_args, **_kwargs: None)


def test_ghost_cleanup_finalizes_inside_acquired_lock(monkeypatch, fake_redis):
    _base(monkeypatch, fake_redis)
    calls, fence = _canonical_spies(monkeypatch)
    order = []
    monkeypatch.setattr(pm, "_light_fapi_get", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pm, "_light_get_price", lambda _symbol: 100)
    monkeypatch.setattr(pm, "_was_closed_recently", lambda _symbol: False)
    monkeypatch.setattr(
        pm, "_lock_acquire", lambda *_args, **_kwargs: order.append("lock") or True
    )
    monkeypatch.setattr(
        pm, "_lock_release", lambda *_args, **_kwargs: order.append("unlock")
    )
    monkeypatch.setattr(pm, "_mark_closed", lambda _symbol: None)
    monkeypatch.setattr(
        pm,
        "_s6api",
        lambda: (None, None, None, None, None, None, None, lambda *_a, **_k: None),
    )
    pos = {"entry": 100, "qty": 2, "side": "LONG", "system": "S6",
           "open_time": 1}
    positions = {"BTCUSDT": pos}

    assert pm._ghost_cleanup(positions) == [
        ("BTCUSDT", "手动平仓", 100, 100, 2, "LONG")
    ]
    assert order == ["lock", "unlock"]
    assert calls == {"capture": [("BTCUSDT", pos)], "finalize": [fence]}


def test_reconcile_local_only_finalizes_before_silent_pop(monkeypatch, fake_redis):
    _base(monkeypatch, fake_redis)
    calls, fence = _canonical_spies(monkeypatch)
    monkeypatch.setattr(
        pm,
        "_s6api",
        lambda: (lambda *_a, **_k: [], None, None, None, None, None, None, None),
    )
    pos = {"entry": 100, "qty": 2, "side": "LONG", "system": "S6",
           "open_time": 1}
    pm._save({"BTCUSDT": pos})

    assert pm.reconcile_all() == (["BTCUSDT"], [])
    assert calls == {"capture": [("BTCUSDT", pos)], "finalize": [fence]}
    assert pm._load() == {}


def test_exchange_position_never_finalizes(monkeypatch, fake_redis):
    _base(monkeypatch, fake_redis)
    calls, _fence = _canonical_spies(monkeypatch)
    monkeypatch.setattr(pm, "_light_fapi_get", lambda *_args, **_kwargs: [
        {"symbol": "BTCUSDT", "positionAmt": "2"}
    ])
    monkeypatch.setattr(pm, "_was_closed_recently", lambda _symbol: False)
    pos = {"entry": 100, "qty": 2, "side": "LONG", "system": "S6",
           "open_time": 1}

    assert pm._ghost_cleanup({"BTCUSDT": pos}) == []
    assert calls == {"capture": [], "finalize": []}
