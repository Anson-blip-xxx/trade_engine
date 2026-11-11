"""Signal health distinguishes causes without hiding lag or granting execution."""

import json
import time

import pytest

from services.v2_trading_health import TradingHealth, signal_lag_state
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal, strategy_worker
from v2_core.runtime_policy import PolicyStore
from v2_core.strategy import StrategyScope

database = database_fixture
LAG = "SIGNAL_CONSUMPTION_LAG"


def report(database, now):
    health = TradingHealth(
        database, account_id="test-account", tv_enabled=True, clock_ms=lambda: now
    )
    health()
    return health, json.loads(health.store.read(health.key).payload_json)


def pending(database, name, *, expiry, error=None):
    signal = scheduler_signal(database, name, expires=expiry)
    worker, _ = strategy_worker(database)
    if error is not None:
        with database() as conn:
            conn.execute(
                "INSERT INTO v2_strategy_tasks(consumer,signal_id,error_code) "
                "VALUES (%s,%s,%s)",
                (worker.scope.consumer, signal, error),
            )
    return signal


@pytest.mark.parametrize("expired", [False, True])
def test_valid_and_expired_queue_both_alert_with_distinct_cause(database, expired):
    now = time.time_ns() // 1000000 + 31000
    pending(database, "aged", expiry=now - 1 if expired else now + 60000)
    _, saved = report(database, now)
    assert LAG in saved["active"]
    detail = saved["findings"][LAG]["s6:tv_bridge"]
    assert detail["count"] == 1
    assert detail["valid_pending"] == int(not expired)
    assert detail["expired_pending"] == int(expired)
    assert detail["classification"] == (
        "EXPIRED_SIGNAL_BOOKKEEPING_LAG" if expired else "VALID_SIGNAL_LAG"
    )
    assert saved["signal_lag_state"]["status"] == "CURRENT_LAG"
    assert saved["signal_lag_state"]["current_lag_observed"] is True
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize(
    "code", ["SHORT_RATIO_STALE", "SHORT_RATIO_MISSING", "ENTRY_RESEARCH_CONTEXT_EXPIRED"]
)
def test_candidate_dependency_remains_lag_with_precise_safe_reason(database, code):
    now = time.time_ns() // 1000000 + 31000
    pending(database, "dependency", expiry=now + 60000, error=code)
    _, saved = report(database, now)
    assert LAG in saved["active"]
    detail = saved["queue_diagnostics"]["s6:tv_bridge"]
    assert detail["candidate_dependency_wait"] == 1
    assert detail["reason_codes"] == {code: 1}
    assert detail["valid_lag"] == 1
    assert detail["account_capacity_wait"] == 0


def test_admission_read_quota_is_not_mislabeled_as_capital(database):
    now = time.time_ns() // 1000000 + 31000
    pending(database, "quota", expiry=now + 60000, error="ADMISSION_BUDGET_UNAVAILABLE")
    _, saved = report(database, now)
    assert LAG not in saved["active"]  # Preserve established expected-wait contract.
    detail = saved["expected_waits"]["s6:tv_bridge"]
    assert detail["reason"] == "ADMISSION_READ_QUOTA_WAIT"
    assert detail["admission_read_quota_wait"] == 1
    assert detail["account_capacity_wait"] == 0
    assert saved["queue_diagnostics"]["s6:tv_bridge"]["reason_codes"] == {
        "ADMISSION_BUDGET_UNAVAILABLE": 1
    }


def test_mixed_quota_capacity_and_dependency_do_not_hide_real_lag(database):
    now = time.time_ns() // 1000000 + 31000
    for code in (
        "ADMISSION_BUDGET_UNAVAILABLE",
        "EXISTING_SYMBOL_POSITION",
        "SHORT_RATIO_STALE",
    ):
        pending(database, code, expiry=now + 60000, error=code)
    _, saved = report(database, now)
    assert LAG in saved["active"]
    assert saved["findings"][LAG]["s6:tv_bridge"]["count"] == 1
    assert saved["findings"][LAG]["s6:tv_bridge"]["reason_codes"] == {
        "SHORT_RATIO_STALE": 1
    }
    assert saved["expected_waits"]["s6:tv_bridge"] == {
        "count": 2,
        "reason": "MIXED_CAPACITY_AND_ADMISSION_READ_QUOTA_WAIT",
        "admission_read_quota_wait": 1,
        "account_capacity_wait": 1,
    }


def test_unknown_task_error_stays_alert_without_publishing_raw_message(database):
    now = time.time_ns() // 1000000 + 31000
    private = "transport failed with api_key=qa-secret-never-publish"
    pending(database, "unknown", expiry=now + 60000, error=private)
    _, saved = report(database, now)
    assert LAG in saved["active"]
    assert private not in json.dumps(saved)
    assert saved["queue_diagnostics"]["s6:tv_bridge"]["reason_codes"] == {
        "UNCLASSIFIED_TASK_ERROR": 1
    }


def test_recovery_latch_is_explicit_and_persists_across_health_instances(database):
    now = time.time_ns() // 1000000 + 31000
    signal = pending(database, "recovery", expiry=now + 60000)
    health = TradingHealth(database, account_id="test-account", tv_enabled=True)
    PolicyStore(database, health.scope).patch(
        {"health.signal_recovery_seconds": 120}, expected_version=0, reason="QA"
    )
    _, saved = report(database, now)
    assert saved["signal_lag_state"]["status"] == "CURRENT_LAG"
    with database() as conn:
        for producer in ("s6", "s8"):
            consumer = StrategyScope(
                "BINANCE", "test-account", "SANDBOX", "FUTURES", producer
            ).consumer
            conn.execute(
                "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
                "VALUES (%s,%s,'IGNORED','QA')",
                (consumer, signal),
            )
    _, saved = report(database, now + 1)
    assert LAG in saved["active"]
    assert LAG not in saved["findings"]
    assert saved["signal_lag_state"] == {
        "status": "RECOVERY_PENDING",
        "current_lag_observed": False,
        "alarm_active": True,
        "recovery_started_at_ms": now + 1,
        "recovery_remaining_ms": 120000,
    }
    _, saved = report(database, now + 60001)
    assert saved["signal_lag_state"]["recovery_remaining_ms"] == 60000
    _, saved = report(database, now + 120001)
    assert LAG not in saved["active"]
    assert saved["signal_lag_state"]["status"] == "CLEAR"


@pytest.mark.parametrize(
    "findings,active,waits,expected",
    [
        ({LAG: {}}, [], {}, "CONFIRMATION_PENDING"),
        ({LAG: {}}, [LAG], {}, "CURRENT_LAG"),
        ({}, [], {"signals_waiting_for_safety": {}}, "UPSTREAM_SAFETY_WAIT"),
        ({}, [LAG], {"signals_waiting_for_safety": {}}, "UPSTREAM_SAFETY_WAIT"),
        ({}, [], {}, "CLEAR"),
    ],
)
def test_alarm_state_classification_does_not_change_alarm_authority(
    findings, active, waits, expected
):
    result = signal_lag_state(
        findings, active, {}, waits, 100, {"health.signal_recovery_seconds": 120}
    )
    assert result["status"] == expected
    assert result["alarm_active"] is (LAG in active)
