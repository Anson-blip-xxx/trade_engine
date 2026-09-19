"""R4 native-open producer wiring and V3 queue payload tests."""

from types import SimpleNamespace

from position_protection.handoff import NativeOpenHandoffCode
from position_protection.task import ConditionalWritebackProtectionTask
from shared import position_manager as pm


def test_position_manager_enqueues_only_after_handoff_ack(monkeypatch):
    authority = SimpleNamespace(
        episode_id="6a4717d8-7390-4386-9878-56ff2981f93d", slot_generation=3
    )
    projection = SimpleNamespace(state_revision=5)
    desired = SimpleNamespace(protection_generation=7, revision=11)
    result = SimpleNamespace(
        applied=True,
        code=NativeOpenHandoffCode.APPLIED,
        authority=authority,
        projection=projection,
        desired=desired,
    )

    class Adapter:
        def __init__(self, **_kwargs):
            pass

        def open_native(self, **_kwargs):
            return result

    monkeypatch.setattr(pm, "RedisNativeOpenHandoffAdapter", Adapter)
    monkeypatch.setattr(pm.uuid, "uuid4", lambda: "candidate")
    monkeypatch.setattr(pm, "_ALGO_QUEUE", [])
    monkeypatch.setattr(pm, "_pmlog", lambda _message: None)
    returned = pm._algo_enqueue_native_open(
        "BTCUSDT",
        "SELL",
        90,
        2,
        account_principal_id="r4-native",
        environment="SANDBOX",
        position_side="LONG",
        system="S6",
        entry_price=100,
        opened_at=1,
        open_order_alias="42",
    )
    assert returned is result
    task = pm._ALGO_QUEUE[0]
    assert isinstance(task, ConditionalWritebackProtectionTask)
    assert task.episode_id == "6a4717d8-7390-4386-9878-56ff2981f93d"
    assert task.slot_generation == 3
    assert task.protection_generation == 7
    assert task.desired_revision == 11
    assert task.projection_revision == 5


def test_position_manager_does_not_enqueue_failed_handoff(monkeypatch):
    result = SimpleNamespace(
        applied=False,
        code=NativeOpenHandoffCode.CONFLICT,
    )

    class Adapter:
        def __init__(self, **_kwargs):
            pass

        def open_native(self, **_kwargs):
            return result

    monkeypatch.setattr(pm, "RedisNativeOpenHandoffAdapter", Adapter)
    monkeypatch.setattr(pm, "_ALGO_QUEUE", [])
    monkeypatch.setattr(pm, "_pmlog", lambda _message: None)
    returned = pm._algo_enqueue_native_open(
        "BTCUSDT",
        "SELL",
        90,
        2,
        account_principal_id="r4-native",
        environment="SANDBOX",
        position_side="LONG",
        system="S6",
        entry_price=100,
        opened_at=1,
        open_order_alias="42",
    )
    assert returned is result
    assert pm._ALGO_QUEUE == []
