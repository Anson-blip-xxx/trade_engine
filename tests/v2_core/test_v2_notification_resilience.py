import json
import time

from test_v2_account_inventory import TelegramConnection, sink
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal

from services.v2_trading_health import TradingHealth
from v2_core.runtime_policy import PolicyStore
from v2_core.strategy import StrategyScope
from v2_core.trade_notifications import TradeLifecycleNotifications

database = database_fixture


def test_safety_block_is_primary_alarm_not_duplicate_signal_lag(database):
    from dataclasses import asdict

    from v2_core.state import BusinessState, StateKey

    scheduler_signal(database, "safety-wait", expires=9999999999999)
    now = [time.time_ns() // 1000000 + 31000]
    health = TradingHealth(
        database, account_id=SCOPE.account_id, tv_enabled=True, clock_ms=lambda: now[0]
    )
    BusinessState(database).change(
        StateKey(**asdict(SCOPE), namespace="trading-pipeline-cycle-v1", key="qa"),
        expected_version=0,
        request_key="blocked",
        reason="QA",
        payload={
            "started_at_ms": now[0],
            "status": "ENTRY_BLOCKED",
            "phases": {"protection": {"status": "BLOCKED"}},
        },
    )
    assert "SIGNAL_CONSUMPTION_LAG" not in health()
    now[0] += 121000
    assert "POSITION_SAFETY_BLOCKED" in health()
    saved = json.loads(health.store.read(health.key).payload_json)
    assert "signals_waiting_for_safety" in saved["expected_waits"]
    assert "SIGNAL_CONSUMPTION_LAG" not in saved["active"]


def test_signal_alarm_requires_persistence_and_quiet_recovery_survives_restart(
    database,
):
    PolicyStore(database, SCOPE).patch(
        {"health.signal_confirm_seconds": 60, "health.signal_recovery_seconds": 120},
        expected_version=0,
        reason="QA debounce",
    )
    signal = scheduler_signal(database, "debounce", expires=9999999999999)
    now = [time.time_ns() // 1000000 + 31000]

    def observe():
        return TradingHealth(
            database,
            account_id=SCOPE.account_id,
            tv_enabled=True,
            clock_ms=lambda: now[0],
        )()

    assert "SIGNAL_CONSUMPTION_LAG" not in observe()
    now[0] += 60000
    assert "SIGNAL_CONSUMPTION_LAG" in observe()
    with database() as c:
        for producer in ("s6", "s8"):
            consumer = StrategyScope(
                SCOPE.exchange,
                SCOPE.account_id,
                SCOPE.environment,
                SCOPE.product,
                producer,
            ).consumer
            c.execute(
                "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) VALUES (%s,%s,'IGNORED','QA')",
                (consumer, signal),
            )
    assert "SIGNAL_CONSUMPTION_LAG" in observe()
    now[0] += 60000
    assert "SIGNAL_CONSUMPTION_LAG" in observe()
    now[0] += 60000
    assert "SIGNAL_CONSUMPTION_LAG" not in observe()


def test_pin_failure_retries_pin_only_and_survives_reconstruction(
    database, monkeypatch
):
    now = [1000000]
    monkeypatch.setattr(
        "v2_core.trade_notifications.time.time_ns", lambda: now[0] * 1000000
    )
    connection = TelegramConnection()
    notifications = TradeLifecycleNotifications(
        database, sink(connection), scope=SCOPE, pin_messages=True
    )
    notifications._queue_pin("qa-message", 123)
    notifications._retry_pins(10)
    saved = json.loads(
        notifications.store.read(notifications._pin_key("qa-message")).payload_json
    )
    assert saved["status"] == "PENDING" and saved["attempts"] == 1
    now[0] += 60000
    connection.raw = b'{"ok":true,"result":true}'
    restarted = TradeLifecycleNotifications(
        database, sink(connection), scope=SCOPE, pin_messages=True
    )
    restarted._retry_pins(10)
    assert (
        json.loads(restarted.store.read(restarted._pin_key("qa-message")).payload_json)[
            "status"
        ]
        == "PINNED"
    )
    assert len(connection.calls) == 2
    assert all(call[0][1].endswith("/pinChatMessage") for call in connection.calls)
    assert all(
        json.loads(call[1]["body"])["disable_notification"] is True
        for call in connection.calls
    )
    assert restarted._deliver("qa-message", None, "SETTLED:1", {}) is True
    assert len(connection.calls) == 2
