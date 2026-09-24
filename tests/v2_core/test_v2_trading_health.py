import json
from dataclasses import asdict

from test_v2_directional_cash import closed as closed_fixture
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal, strategy_worker
from test_v2_testnet_watchdog import Stop

from services.v2_testnet_watchdog import (
    DependencyAssessment,
    HealthWatchdog,
    WatchdogDeliveryState,
)
from services.v2_trading_health import TradingHealth
from v2_core.state import BusinessState, StateKey

database = database_fixture
closed = closed_fixture


def test_health_detects_flat_unsettled_episode_and_dashboard_exposes_details(
    database, closed
):
    from services.v2_dashboard import DashboardData

    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: 200000)
    assert "SETTLEMENT_OVERDUE" in health()
    overview = DashboardData(database).overview()
    record = overview["health"][0]
    assert record["account_id"] == "test-account"
    assert len(record["payload"]["findings"]["SETTLEMENT_OVERDUE"]["episodes"]) == 1
    assert record["payload"]["observed_at_ms"] == 200000


def test_business_telegram_is_utc8_and_contains_no_trace():
    from test_v2_testnet_watchdog import Connection

    from services.v2_testnet_watchdog import WatchdogTelegramNotifier
    from v2_core.telegram import TelegramOperationalSink

    Connection.calls.clear()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    notify = WatchdogTelegramNotifier(
        sink, account_id="test-account", clock_ms=lambda: 100000
    )
    scope = {
        "exchange": "BINANCE",
        "account_id": "test-account",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    assert (
        notify(
            {
                "status": "UNAVAILABLE",
                "error_code": "ORDER_PROGRESS_STALLED",
                "account_scope": scope,
            }
        )
        is True
    )
    text = json.loads(Connection.calls[-1][2])["text"]
    assert "订单" in text and "UTC+8" in text
    assert "event_id" not in text and "watchdog:" not in text


def test_health_signal_lag_receipt_clears_both_consumers(database):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "old")
    health = TradingHealth(
        database, account_id="test-account", tv_enabled=True, clock_ms=lambda: 100000
    )
    assert health() == frozenset({"SIGNAL_CONSUMPTION_LAG"})
    for producer in ("s6", "s8"):
        worker, _ = strategy_worker(database, producer=producer, now=lambda: 100000)
        assert (
            StrategyScheduler(worker, context_provider=lambda _: {}).expire_pending()
            == 1
        )
    assert health() == frozenset()
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s", (signal_id,)
        ).fetchone() == (2,)


def test_health_order_age_and_account_isolation(database):
    worker, _ = strategy_worker(database)
    signal_id = scheduler_signal(database, "order")
    assert worker.consume(signal_id, context={"price": "100"})["status"] == "PREPARED"
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: 3)
    assert health() == frozenset()
    with database() as conn:
        conn.execute(
            "UPDATE v2_orders SET version=version+1,updated_at=clock_timestamp()-interval '100 seconds'"
        )
    assert health() == frozenset({"ORDER_PROGRESS_STALLED"})
    assert (
        TradingHealth(database, account_id="another-account", clock_ms=lambda: 3)()
        == frozenset()
    )


def test_health_blockage_debounce_survives_restart_and_recovers(database):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    key = StateKey(
        **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="test"
    )
    store = BusinessState(database)
    payload = {
        "started_at_ms": now[0],
        "status": "ENTRY_BLOCKED",
        "phases": {"protection": {"status": "BLOCKED"}},
    }
    store.change(
        key, expected_version=0, request_key="1", payload=payload, reason="TEST"
    )
    assert health() == frozenset()
    now[0] += 31000
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    assert health() == frozenset({"POSITION_SAFETY_BLOCKED"})
    now[0] += 90000
    assert health() == frozenset({"POSITION_SAFETY_BLOCKED", "PIPELINE_ENTRY_BLOCKED"})
    payload.update(status="CYCLE_COMPLETE", phases={})
    store.change(
        key, expected_version=1, request_key="2", payload=payload, reason="TEST"
    )
    assert health() == frozenset()
    assert json.loads(store.read(health.key).payload_json)["first_seen_ms"] == {}


def test_business_probe_failures_are_not_healthy():
    assess = DependencyAssessment(
        postgres=lambda: True,
        redis=lambda: True,
        clickhouse=lambda: True,
        heartbeat=lambda: None,
        business=lambda: (_ for _ in ()).throw(ValueError("secret")),
    )
    assert assess() == frozenset({"BUSINESS_HEALTH_UNAVAILABLE"})


def test_watchdog_persists_delivery_and_does_not_claim_recovery_without_evidence(
    database,
):
    events = []
    current = [frozenset({"ORDER_PROGRESS_STALLED"})]

    def notify(event):
        events.append(event)
        return True

    def watchdog():
        return HealthWatchdog(
            lambda: current[0],
            notify=notify,
            stop=Stop(),
            interval_seconds=15,
            delivery_state=WatchdogDeliveryState(database, "test-account"),
        )

    watchdog().run_once()
    restarted = watchdog()
    restarted.run_once()
    assert len(events) == 1
    current[0] = frozenset({"POSTGRES_UNAVAILABLE"})
    restarted.run_once()
    assert not any(e["status"] == "RECOVERED" for e in events)
    current[0] = frozenset()
    restarted.run_once()
    assert {(e["error_code"], e["status"]) for e in events} == {
        ("ORDER_PROGRESS_STALLED", "UNAVAILABLE"),
        ("ORDER_PROGRESS_STALLED", "RECOVERED"),
        ("POSTGRES_UNAVAILABLE", "UNAVAILABLE"),
        ("POSTGRES_UNAVAILABLE", "RECOVERED"),
    }
