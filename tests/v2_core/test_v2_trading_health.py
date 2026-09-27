import json
import time
from dataclasses import asdict

import pytest
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


@pytest.mark.parametrize(
    "code", ["ACCOUNT_RISK_CAPACITY_UNAVAILABLE", "EXISTING_SYMBOL_POSITION"]
)
def test_expected_risk_wait_is_visible_not_a_fault_and_expires(database, code):
    from v2_core.scheduling import StrategyScheduler

    now = [40000]
    signal_id = scheduler_signal(database, "waiting", expires=100000)
    worker, _ = strategy_worker(database, now=lambda: now[0])
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: (_ for _ in ()).throw(ValueError(code))
    )
    assert scheduler.run_once(1) == {signal_id: "DEFERRED"}
    # Finish the other consumer, so only the intentional S6 wait remains.
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) VALUES (%s,%s,'IGNORED','QA')",
            (worker.scope.consumer.replace('"s6"', '"s8"'), signal_id),
        )
    health = TradingHealth(
        database,
        account_id="test-account",
        tv_enabled=True,
        clock_ms=lambda: time.time_ns() // 1000000 + 31000,
    )
    assert health() == frozenset()
    state = json.loads(health.store.read(health.key).payload_json)
    assert state["expected_waits"]["s6:tv_bridge"]["count"] == 1
    now[0] = 100001
    assert scheduler.expire_pending() == 1
    assert health() == frozenset()
    assert (
        json.loads(health.store.read(health.key).payload_json)["expected_waits"] == {}
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("ACCOUNT_RISK_POLICY_MISSING"),
        RuntimeError("ACCOUNT_RISK_CAPACITY_UNAVAILABLE"),
    ],
)
def test_missing_risk_evidence_or_wrong_exception_remains_a_failure(database, exc):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "risk-error", expires=100000)
    worker, _ = strategy_worker(database, now=lambda: 40000)
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: (_ for _ in ()).throw(exc)
    )
    assert scheduler.run_once(1) == {signal_id: "UNAVAILABLE"}
    health = TradingHealth(
        database,
        account_id="test-account",
        tv_enabled=True,
        clock_ms=lambda: time.time_ns() // 1000000 + 31000,
    )
    assert "SIGNAL_CONSUMPTION_LAG" in health()


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
        database,
        account_id="test-account",
        tv_enabled=True,
        clock_ms=lambda: time.time_ns() // 1000000 + 31000,
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


def test_old_event_just_received_is_not_a_queue_delay(database):
    scheduler_signal(database, "old-candle-just-arrived")
    health = TradingHealth(database, account_id="test-account", tv_enabled=True)
    assert health() == frozenset()
    assert json.loads(health.store.read(health.key).payload_json)["findings"] == {}


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


def opening(
    database,
    *,
    status="REJECTED",
    account_id="test-account",
    reason=None,
    symbol="BTCUSDT",
):
    """Create durable QA outcomes, not a transport or simulated health response."""
    from dataclasses import replace

    from test_v2_intent_admission import evidence, intent

    from v2_core.evidence import EvidenceStore
    from v2_core.intents import AdmissionCode, IntentStore
    from v2_core.ledger import Ledger
    from v2_core.orders import Orders

    original = replace(intent(), account_id=account_id)
    if symbol != original.symbol:
        proof = evidence()
        proof = replace(
            proof,
            snapshot_json=json.dumps(
                {**json.loads(proof.snapshot_json), "symbol": symbol}
            ),
        )
        EvidenceStore(database).put(proof)
        original = replace(original, symbol=symbol, evidence_ref=proof.evidence_ref)
    assert IntentStore(database).admit(original).code is AdmissionCode.ACCEPTED
    orders = Orders(database)
    order_id, _ = orders.prepare(original.intent_id)
    assert orders.transition(
        order_id, expected_version=1, status="SUBMITTING", evidence={"source": "QA"}
    )
    if status == "FILLED":
        Ledger(database).record_fill(
            order_id=order_id,
            exchange_fill_id="qa-fill",
            quantity=original.quantity,
            price="100",
            fee="0",
            fee_currency="USDT",
            occurred_at_ms=time.time_ns() // 1000000,
            evidence={"source": "QA"},
        )
    assert orders.transition(
        order_id,
        expected_version=2,
        status=status,
        exchange_order_id="qa-venue-order"
        if status in {"FILLED", "ACKNOWLEDGED"}
        else None,
        evidence={
            "reason": reason or "VENUE_READINESS_BLOCKED",
            "submission_sent": status != "REJECTED",
        },
    )
    return order_id


def test_opening_rejection_threshold_scope_and_no_raw_error_disclosure(database):
    health = TradingHealth(database, account_id="test-account")
    opening(database)
    opening(database, account_id="different-account")
    opening(database)
    assert health() == frozenset()
    opening(database, reason="permission denied SELECT secret-password")
    assert health() == frozenset({"OPENING_SUBMISSION_STALLED"})
    state = json.loads(health.store.read(health.key).payload_json)
    assert state["opening"]["rejected"] == 3
    assert state["opening"]["accepted"] == 0
    assert state["opening"]["rejection_codes"] == {
        "VENUE_READINESS_BLOCKED": 2,
        "UNCLASSIFIED_REJECTION": 1,
    }
    assert "secret-password" not in json.dumps(state)
    assert TradingHealth(database, account_id="different-account")() == frozenset()
    assert TradingHealth(database, account_id="untouched-account")() == frozenset()


def test_permission_failure_is_immediate_and_silence_is_not_recovery(database):
    opening(database, reason="EXECUTION_GUARD_PERMISSION_DENIED")
    now = [time.time_ns() // 1000000 + 1]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
    now[0] += 901000
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
    saved = json.loads(health.store.read(health.key).payload_json)
    assert saved["opening"]["rejected"] == 0
    assert saved["findings"]["EXECUTION_GUARD_PERMISSION_DENIED"][
        "awaiting_successful_submission"
    ]
    opening(database, status="FILLED")
    # A newer durable venue outcome proves recovery even after the window aged
    # out; no clock sleeps or business-record mutations are necessary for QA.
    restarted = TradingHealth(
        database, account_id="test-account", clock_ms=lambda: now[0]
    )
    assert restarted() == frozenset()


def test_successful_fill_is_not_a_zero_submission_window(database):
    health = TradingHealth(database, account_id="test-account")
    for _ in range(3):
        opening(database)
    opening(database, status="FILLED")
    # Three failures out of four attempts are below the default 80% threshold.
    assert health() == frozenset()
    state = json.loads(health.store.read(health.key).payload_json)
    assert state["opening"]["accepted"] == 1
    opening(database, symbol="ETHUSDT")
    assert health() == frozenset({"OPENING_REJECTION_RATE_HIGH"})


def test_opening_health_policy_is_account_scoped_and_bounded(database):
    from v2_core.runtime_policy import PolicyStore, resolve

    health = TradingHealth(database, account_id="test-account")
    for _ in range(3):
        opening(database)
    policy = PolicyStore(database, health.scope)
    policy.patch(
        {"health.opening_min_rejections": 4, "health.opening_window_seconds": 60},
        expected_version=0,
        reason="QA bounded rejection policy",
    )
    assert health() == frozenset()
    opening(database)
    assert health() == frozenset({"OPENING_SUBMISSION_STALLED"})
    for changes in (
        {"health.opening_window_seconds": 59},
        {"health.opening_window_seconds": 86401},
        {"health.opening_min_rejections": 1},
        {"health.opening_min_rejections": 1001},
        {"health.opening_reject_fraction": "0"},
        {"health.opening_reject_fraction": "1.1"},
    ):
        with pytest.raises(ValueError):
            resolve(changes)


def test_repeated_opening_health_notification_is_deduplicated(database):
    for _ in range(3):
        opening(database)
    health = TradingHealth(database, account_id="test-account")
    events = []
    for _ in range(2):
        HealthWatchdog(
            health,
            notify=lambda event: events.append(event) or True,
            stop=Stop(),
            interval_seconds=15,
            delivery_state=WatchdogDeliveryState(database, "test-account"),
        ).run_once()
    assert len(events) == 1
    assert events[0]["error_code"] == "OPENING_SUBMISSION_STALLED"


def test_acknowledged_then_cancelled_is_successful_submission_not_rejection(database):
    from v2_core.orders import Orders

    order_id = opening(database, status="ACKNOWLEDGED")
    assert Orders(database).transition(
        order_id,
        expected_version=3,
        status="CANCELLED",
        evidence={"fills_complete": True, "reason": "QA venue cancel"},
    )
    for _ in range(3):
        opening(database)
    health = TradingHealth(database, account_id="test-account")
    assert health() == frozenset()
    state = json.loads(health.store.read(health.key).payload_json)
    assert state["opening"]["accepted"] == 1


def test_close_permission_rejections_are_urgent_not_an_opening_failure(database):
    from v2_core.orders import Orders

    order_id = opening(database, status="FILLED")
    with database() as conn:
        intent_id, quantity = conn.execute(
            "SELECT episode_id::text,quantity::text FROM v2_orders WHERE order_id=%s",
            (order_id,),
        ).fetchone()
    orders = Orders(database)
    for index in range(3):
        closing, _ = orders.prepare(
            intent_id,
            leg="CLOSE",
            request_key="qa-close-" + str(index),
            quantity=quantity,
        )
        assert orders.transition(
            closing, expected_version=1, status="SUBMITTING", evidence={"source": "QA"}
        )
        assert orders.transition(
            closing,
            expected_version=2,
            status="REJECTED",
            evidence={
                "reason": "EXECUTION_GUARD_PERMISSION_DENIED",
                "submission_sent": False,
            },
        )
    health = TradingHealth(database, account_id="test-account")
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
    state = json.loads(health.store.read(health.key).payload_json)
    assert state["opening"]["rejected"] == 0


def test_empty_window_and_other_account_success_do_not_clear_opening_incident(database):
    for _ in range(3):
        opening(database)
    now = [time.time_ns() // 1000000 + 1]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    assert health() == frozenset({"OPENING_SUBMISSION_STALLED"})
    now[0] += 901000
    opening(database, status="FILLED", account_id="another-account")
    assert health() == frozenset({"OPENING_SUBMISSION_STALLED"})
    saved = json.loads(health.store.read(health.key).payload_json)
    assert saved["opening"]["rejected"] == saved["opening"]["accepted"] == 0
    assert saved["findings"]["OPENING_SUBMISSION_STALLED"][
        "awaiting_successful_submission"
    ]
    opening(database, status="FILLED")
    assert health() == frozenset()
    assert (
        json.loads(health.store.read(health.key).payload_json)["opening_incidents"]
        == {}
    )


def test_old_acceptance_recent_cancel_does_not_prove_permission_recovery(database):
    from v2_core.orders import Orders

    accepted = opening(database, status="ACKNOWLEDGED")
    opening(database, symbol="ETHUSDT", reason="EXECUTION_GUARD_PERMISSION_DENIED")
    assert Orders(database).transition(
        accepted,
        expected_version=3,
        status="CANCELLED",
        evidence={"fills_complete": True, "reason": "QA later cancellation"},
    )
    health = TradingHealth(database, account_id="test-account")
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
    opening(database, status="ACKNOWLEDGED")
    assert health() == frozenset()


def test_late_recovery_of_old_submission_is_not_guard_recovery(database):
    from v2_core.orders import Orders

    old_order = opening(database, status="UNKNOWN")
    opening(database, symbol="ETHUSDT", reason="EXECUTION_GUARD_PERMISSION_DENIED")
    assert Orders(database).transition(
        old_order,
        expected_version=3,
        status="ACKNOWLEDGED",
        exchange_order_id="qa-late-recovery",
        evidence={"source": "read-only recovery"},
    )
    now = [time.time_ns() // 1000000 + 1]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
    now[0] += 901000
    assert health() == frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
