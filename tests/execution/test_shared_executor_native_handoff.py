"""R4 active shared-executor native-open producer routing."""

from types import SimpleNamespace

from position_protection.handoff import NativeOpenHandoffCode


def test_shared_executor_uses_native_handoff_when_principal_configured(
    exec_env, monkeypatch
):
    se = exec_env["se"]
    calls = []
    result = SimpleNamespace(
        applied=True,
        code=NativeOpenHandoffCode.APPLIED,
    )
    monkeypatch.setattr(se, "_ACCOUNT_PRINCIPAL_ID", "r4-native")
    monkeypatch.setattr(se, "_is_testnet", True)
    monkeypatch.setattr(
        se,
        "_algo_enqueue_native_open",
        lambda *args, **kwargs: calls.append((args, kwargs)) or result,
    )
    assert se.open_position(
        "S6",
        "TESTUSDT",
        "LONG",
        100,
        95,
        2,
        "CROSSED",
        3,
        "TREND",
        80,
    )
    assert exec_env["calls"]["algo"] == []
    args, kwargs = calls[0]
    assert args == ("TESTUSDT", "SELL", 95, 2)
    assert kwargs["account_principal_id"] == "r4-native"
    assert kwargs["environment"] == "DEMO"
    assert kwargs["position_side"] == "LONG"
    assert kwargs["system"] == "S6"


def test_failed_native_handoff_never_falls_back_to_legacy_enqueue(
    exec_env, monkeypatch
):
    se = exec_env["se"]
    monkeypatch.setattr(se, "_ACCOUNT_PRINCIPAL_ID", "r4-native")
    monkeypatch.setattr(
        se,
        "_algo_enqueue_native_open",
        lambda *_args, **_kwargs: SimpleNamespace(
            applied=False,
            code=NativeOpenHandoffCode.CONFLICT,
        ),
    )
    assert se.open_position(
        "S6",
        "TESTUSDT",
        "LONG",
        100,
        95,
        2,
        "CROSSED",
        3,
        "TREND",
        80,
    )
    assert exec_env["calls"]["algo"] == []
