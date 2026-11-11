"""Bounded read-only diagnostics and fake TG only; never venue/order network I/O."""

import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_v2_testnet_watchdog import Stop

from services.v2_testnet_watchdog import HealthWatchdog, WatchdogTelegramNotifier
from services.v2_trading_health import (
    TradingHealth,
    pipeline_position_details,
    rejected_residual_closes,
)
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import evidence as admission_evidence
from tests.support.v2_admission import intent
from v2_core.account_risk import AccountScope
from v2_core.evidence import EvidenceStore
from v2_core.intents import IntentStore
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.state import BusinessState, StateKey
from v2_core.telegram import (
    BUSINESS_HEALTH_MESSAGES,
    TelegramOperationalSink,
    health_notification_details,
)

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, host, timeout):
        assert (host, timeout) == ("api.telegram.org", 10)
        return self

    def request(self, method, target, *, body, headers):
        self.calls.append(json.loads(body))
        if self.fail:
            raise ConnectionError("SECRET_RAW_TRANSPORT_ERROR")

    def getresponse(self):
        return self

    status = 200

    def read(self, _):
        return json.dumps(
            {
                "ok": True,
                "result": {
                    "message_id": 7,
                    "chat": {"id": int(self.calls[-1]["chat_id"])},
                },
            }
        ).encode()

    def close(self):
        pass


def notifier(*, details_for=None, now=1000000):
    connection = FakeTelegram()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        alerts_chat_id="321",
        environment="SANDBOX",
        connection_factory=connection,
    )
    notify = WatchdogTelegramNotifier(
        sink, account_id=SCOPE.account_id, clock_ms=lambda: now, details_for=details_for
    )
    return notify, connection


def event(code="POSITION_SAFETY_BLOCKED", status="UNAVAILABLE"):
    return {"status": status, "error_code": code, "account_scope": asdict(SCOPE)}


def opened(database, *, account="test-account", symbol="BTCUSDT"):
    admitted = replace(intent(), account_id=account, symbol=symbol)
    if symbol != "BTCUSDT":
        proof = admission_evidence()
        proof = replace(
            proof,
            snapshot_json=json.dumps(
                {**json.loads(proof.snapshot_json), "symbol": symbol}
            ),
        )
        EvidenceStore(database).put(proof)
        admitted = replace(admitted, evidence_ref=proof.evidence_ref)
    IntentStore(database).admit(admitted)
    orders, ledger = Orders(database), Ledger(database)
    opening, _ = orders.prepare(admitted.intent_id)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=opening,
        exchange_fill_id=str(uuid4()),
        quantity="0.01",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={},
    )
    orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        exchange_order_id=str(uuid4()),
        evidence={"fills_complete": True},
    )
    return admitted.intent_id, orders, ledger


def reject_close(episode, orders, *, key="rejected-close"):
    order, _ = orders.prepare(episode, leg="CLOSE", quantity="0.01", request_key=key)
    orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(
        order,
        expected_version=2,
        status="REJECTED",
        evidence={"error_code": "SECRET_DO_NOT_RENDER"},
    )
    return order


def test_order_details_name_open_close_without_raw_fields():
    raw = {
        "status": "CURRENT",
        "observed_at_ms": 1000000,
        "orders": [
            {
                "symbol": "BTCUSDT",
                "leg": "OPEN",
                "status": "PREPARED",
                "idle_seconds": 120,
                "episode_id": str(uuid4()),
                "raw_response": "SECRET",
            },
            {
                "symbol": "ETHUSDT",
                "leg": "CLOSE",
                "status": "UNKNOWN",
                "idle_seconds": 95,
                "episode_id": str(uuid4()),
                "reason": "SECRET",
            },
        ],
        "secret": "SECRET",
    }
    notify, connection = notifier(details_for=lambda _: raw)
    assert notify(event("ORDER_PROGRESS_STALLED"))
    sent = connection.calls[-1]
    assert sent["chat_id"] == "321"  # alerts never reach the trade-only room
    assert "BTCUSDT｜开仓" in sent["text"] and "ETHUSDT｜平仓" in sent["text"]
    assert "95 秒" in sent["text"] and "SECRET" not in sent["text"]
    assert "当前交易所保护状态：未知" in sent["text"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "SECRET",
        {"status": []},
        {"status": "CURRENT", "observed_at_ms": True},
        {"status": "CURRENT", "observed_at_ms": "SECRET"},
    ],
)
def test_malformed_detail_never_suppresses_base_alert(bad):
    notify, connection = notifier(details_for=lambda _: bad)
    assert notify(event())
    assert "详情：暂时无法读取" in connection.calls[-1]["text"]
    assert "SECRET" not in connection.calls[-1]["text"]


def test_optional_detail_failure_keeps_alert_and_delivery_retry():
    def unavailable(_):
        raise ValueError("SECRET_DB_DSN")

    notify, connection = notifier(details_for=unavailable)
    watchdog = HealthWatchdog(
        lambda: frozenset({"POSITION_SAFETY_BLOCKED"}),
        notify=notify,
        stop=Stop(),
        interval_seconds=10,
    )
    connection.fail = True
    watchdog.run_once()
    assert not watchdog.reported
    connection.fail = False
    watchdog.run_once()
    assert watchdog.reported == {"POSITION_SAFETY_BLOCKED"}
    assert len(connection.calls) == 2
    watchdog.run_once()
    assert len(connection.calls) == 2  # unchanged code remains deduplicated
    assert "SECRET" not in connection.calls[-1]["text"]


def test_old_constructor_compatibility_and_recovery_does_not_claim_all_healthy():
    notify, connection = notifier()
    assert notify(event())
    assert "详情：" not in connection.calls[-1]["text"]
    assert notify(event(status="RECOVERED"))
    assert "不代表所有环节均已恢复" in connection.calls[-1]["text"]


@pytest.mark.parametrize("observed", [879999, 1000001])
def test_stale_or_future_details_not_shown_as_current(observed):
    notify, connection = notifier(
        details_for=lambda _: {
            "status": "CURRENT",
            "observed_at_ms": observed,
            "orders": [{"symbol": "BTCUSDT", "status": "UNKNOWN"}],
        }
    )
    assert notify(event("ORDER_PROGRESS_STALLED"))
    assert "诊断已过期" in connection.calls[-1]["text"]
    assert "BTCUSDT" not in connection.calls[-1]["text"]


def test_typed_projection_bounds_and_rejects_unknown_strings():
    item = {
        "symbol": "BTCUSDT",
        "status": "BLOCKED",
        "reason": "SECRET",
        "outcome": "SECRET",
        "idle_seconds": True,
        "remaining_quantity": "NaN",
    }
    details = health_notification_details(
        "POSITION_SAFETY_BLOCKED",
        {
            "status": "CURRENT",
            "observed_at_ms": 10,
            "episodes": [item] * 21 + [{"status": []}],
            "coverage_codes": ["MISSING_CONFIRMED_STOP", "SECRET", {}],
            "protection": "ABSENT",
        },
    )
    assert len(details["episodes"]) == 3 and details["truncated"] is True
    assert details["protection"] == "UNKNOWN" and details["unknown_coverage"] is True
    assert "SECRET" not in json.dumps(details)
    assert all(set(row) == {"symbol", "status"} for row in details["episodes"])
    assert (
        health_notification_details("POSITION_SAFETY_BLOCKED", details)[
            "unknown_coverage"
        ]
        is True
    )


def test_configurable_thresholds_are_not_hardcoded_in_messages():
    assert "60 秒" not in BUSINESS_HEALTH_MESSAGES["ORDER_PROGRESS_STALLED"]
    assert "90 秒" not in BUSINESS_HEALTH_MESSAGES["ORDER_PROGRESS_STALLED"]
    assert "2 分钟" not in BUSINESS_HEALTH_MESSAGES["PIPELINE_ENTRY_BLOCKED"]


@pytest.mark.parametrize("complete,remaining", [(False, 1), (True, 0)])
def test_scan_detail_explains_bounded_progress_without_claiming_close_failure(
    complete, remaining
):
    raw = {
        "status": "CURRENT",
        "observed_at_ms": 1000000,
        "exit_reason": "EXIT_SCAN_INCOMPLETE" if not complete else None,
        "scan": {
            "selected": 10,
            "remaining_count_lower_bound": remaining,
            "complete": complete,
            "raw_error": "SECRET",
        },
    }
    notify, connection = notifier(details_for=lambda _: raw)
    assert notify(event())
    text = connection.calls[-1]["text"]
    assert "SECRET" not in text
    if complete:
        assert "已覆盖全部 10 笔" in text and "待轮转" not in text
    else:
        assert "已扫描 10 笔，至少 1 笔待轮转" in text
        assert "扫描未完成不等于平仓失败" in text


@pytest.mark.parametrize(
    "scan",
    [
        None,
        [],
        {"selected": True, "remaining_count_lower_bound": 1, "complete": False},
        {"selected": 1, "remaining_count_lower_bound": "SECRET", "complete": False},
        {"selected": 1, "remaining_count_lower_bound": -1, "complete": False},
        {"selected": 1, "remaining_count_lower_bound": 1, "complete": True},
        {"selected": 1, "remaining_count_lower_bound": 0, "complete": False},
    ],
)
def test_invalid_scan_and_unknown_exit_reason_do_not_leak(scan):
    details = health_notification_details(
        "POSITION_SAFETY_BLOCKED",
        {
            "status": "CURRENT",
            "observed_at_ms": 1000000,
            "exit_reason": "SECRET",
            "scan": scan,
        },
    )
    assert "scan" not in details and "exit_reason" not in details
    assert "SECRET" not in json.dumps(details)


def test_persistent_rejected_close_detected_without_pipeline_and_restarts(database):
    episode, orders, _ = opened(database)
    close = reject_close(episode, orders)
    now = time.time_ns() // 1000000 + 31000
    health = TradingHealth(database, account_id=SCOPE.account_id, clock_ms=lambda: now)
    assert "POSITION_SAFETY_BLOCKED" in health()
    details = health.notification_details("POSITION_SAFETY_BLOCKED")
    assert details["episodes"][0]["episode_id"] == episode
    assert details["episodes"][0]["order_id"] == close
    assert details["episodes"][0]["status"] == "REJECTED_CLOSE_RESIDUAL"
    assert "SECRET" not in json.dumps(details)
    assert (
        "POSITION_SAFETY_BLOCKED"
        in TradingHealth(database, account_id=SCOPE.account_id, clock_ms=lambda: now)()
    )
    other = TradingHealth(database, account_id="other-account", clock_ms=lambda: now)
    assert "POSITION_SAFETY_BLOCKED" not in other()
    notify, connection = notifier(details_for=health.notification_details, now=now)
    assert notify(event())
    assert "本地残余待核对" in connection.calls[-1]["text"]
    assert "无法平仓" not in connection.calls[-1]["text"]


def test_new_rejection_waits_existing_safety_threshold(database):
    episode, orders, _ = opened(database)
    reject_close(episode, orders)
    health = TradingHealth(database, account_id=SCOPE.account_id)
    assert "POSITION_SAFETY_BLOCKED" not in health()


@pytest.mark.parametrize(
    "new_status", ["PREPARED", "SUBMITTING", "ACKNOWLEDGED", "FILLED"]
)
def test_later_close_supersedes_old_rejection(database, new_status):
    episode, orders, ledger = opened(database)
    reject_close(episode, orders)
    fresh, _ = orders.prepare(
        episode, leg="CLOSE", quantity="0.01", request_key="new-close"
    )
    if new_status != "PREPARED":
        orders.transition(fresh, expected_version=1, status="SUBMITTING", evidence={})
    if new_status in {"ACKNOWLEDGED", "FILLED"}:
        if new_status == "FILLED":
            ledger.record_fill(
                order_id=fresh,
                exchange_fill_id="close-filled",
                quantity="0.01",
                price="100",
                fee="0",
                fee_currency="USDT",
                occurred_at_ms=4,
                evidence={},
            )
        orders.transition(
            fresh,
            expected_version=2,
            status=new_status,
            exchange_order_id="12",
            evidence={"source": "QA_EXCHANGE_OUTCOME", "status": new_status},
        )
    with database() as conn:
        assert (
            rejected_residual_closes(
                conn,
                tuple(asdict(SCOPE).values()),
                time.time_ns() // 1000000 + 31000,
                30,
            )
            == []
        )


def test_flat_local_ledger_does_not_raise_rejected_residual(database):
    episode, orders, ledger = opened(database)
    closing, _ = orders.prepare(
        episode, leg="CLOSE", quantity="0.01", request_key="partial-before-reject"
    )
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    # Native close-all children may overlap an already-reserved reduce-only
    # exit. Persist the newer rejected child before the older exit fills.
    child, _ = orders.prepare(
        episode, leg="CLOSE", quantity="0.01", request_key="native-algo:1",
        evidence={
            "origin": "BINANCE_ALGO_CHILD", "parent_algo_id": 1,
            "parent_client_algo_id": "v2p-qa", "exchange_order_id": "22",
            "venue_client_order_id": "qa",
        },
    )
    orders.transition(child, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(
        child, expected_version=2, status="REJECTED",
        evidence={"source": "QA_EXCHANGE_OUTCOME", "status": "REJECTED"},
    )
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id="reconciled-close",
        quantity="0.01",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=4,
        evidence={},
    )
    orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        exchange_order_id="12",
        evidence={"source": "QA_EXCHANGE_OUTCOME", "fills_complete": True},
    )
    with database() as conn:
        assert (
            rejected_residual_closes(
                conn,
                tuple(asdict(SCOPE).values()),
                time.time_ns() // 1000000 + 31000,
                30,
            )
            == []
        )


def test_current_cycle_details_only_owned_episodes_and_no_normal_wait_failure(database):
    episode, _, _ = opened(database)
    other, _, _ = opened(database, account="other-account")
    raw = {
        "finished_at_ms": 1000,
        "phases": {
            "exits": {
                "exits": {
                    episode: {
                        "status": "RECOVERING_CLOSE",
                        "reason": "CLOSE_RECOVERY_PENDING",
                        "secret": "SECRET",
                    },
                    other: {"status": "BLOCKED"},
                },
                "emergency_exits": {
                    episode: {"status": "DISPATCHED", "outcome": "ACKNOWLEDGED"},
                    other: {"status": "DISPATCHED", "outcome": "ACKNOWLEDGED"},
                },
                "coverage": {"blockers": ["MISSING_CONFIRMED_STOP", "SECRET"]},
            }
        },
    }
    details = pipeline_position_details(
        database, tuple(asdict(SCOPE).values()), raw, 1001
    )
    assert [row["episode_id"] for row in details["episodes"]] == [episode]
    assert details["in_progress"] == 1 and details["unknown_coverage"] is True
    assert "SECRET" not in json.dumps(details) and "ETHUSDT" not in json.dumps(details)
    raw["phases"]["exits"]["exits"] = {episode: {"status": "WAIT"}}
    assert (
        pipeline_position_details(database, tuple(asdict(SCOPE).values()), raw, 1001)[
            "episodes"
        ]
        == []
    )
    assert (
        pipeline_position_details(database, tuple(asdict(SCOPE).values()), raw, 121001)[
            "status"
        ]
        == "STALE"
    )


@pytest.mark.parametrize("outcome", ["FILLED", "CANCELLED", "REJECTED", "RECONCILED"])
def test_terminal_recovery_does_not_mask_another_blocked_episode(database, outcome):
    finished, orders, _ = opened(database)
    completed_order = reject_close(finished, orders)
    blocked, _, _ = opened(database, symbol="ETHUSDT")
    cycle = {
        "finished_at_ms": 1000,
        "phases": {
            "exits": {
                "status": "BLOCKED",
                "recovery": {completed_order: outcome},
                "exits": {
                    blocked: {"status": "BLOCKED", "reason": "EXIT_PREFLIGHT_CHANGED"}
                },
            }
        },
    }
    details = pipeline_position_details(
        database, tuple(asdict(SCOPE).values()), cycle, 1001
    )
    assert [item["episode_id"] for item in details["episodes"]] == [blocked]
    assert details["episodes"][0]["symbol"] == "ETHUSDT"


def test_scan_only_wait_cycle_retains_explanation(database):
    episode, _, _ = opened(database)
    cycle = {
        "finished_at_ms": 1000,
        "phases": {
            "exits": {
                "status": "BLOCKED",
                "reason": "EXIT_SCAN_INCOMPLETE",
                "scan": {
                    "selected": 10,
                    "remaining_count_lower_bound": 1,
                    "complete": False,
                },
                "exits": {episode: {"status": "WAIT"}},
            }
        },
    }
    details = pipeline_position_details(
        database, tuple(asdict(SCOPE).values()), cycle, 1001
    )
    assert details["episodes"] == []
    assert details["exit_reason"] == "EXIT_SCAN_INCOMPLETE"
    assert details["scan"]["complete"] is False


def test_notification_provider_checks_saved_scope_age_and_deleted(database):
    now = [1000000]
    health = TradingHealth(
        database, account_id=SCOPE.account_id, clock_ms=lambda: now[0]
    )
    store = BusinessState(database)
    payload = {
        "account_scope": asdict(SCOPE),
        "observed_at_ms": now[0],
        "findings": {
            "ORDER_PROGRESS_STALLED": {
                "orders": [{"symbol": "BTCUSDT", "status": "UNKNOWN", "leg": "CLOSE"}]
            }
        },
    }
    store.change(
        health.key,
        expected_version=0,
        request_key="first",
        reason="QA",
        payload=payload,
    )
    assert health.notification_details("ORDER_PROGRESS_STALLED")["status"] == "CURRENT"
    now[0] += 120001
    assert health.notification_details("ORDER_PROGRESS_STALLED") == {"status": "STALE"}
    payload["observed_at_ms"] = now[0]
    payload["account_scope"]["account_id"] = "other-account"
    store.change(
        health.key,
        expected_version=1,
        request_key="foreign",
        reason="QA",
        payload=payload,
    )
    assert health.notification_details("ORDER_PROGRESS_STALLED") == {
        "status": "UNAVAILABLE"
    }
    store.change(
        health.key,
        expected_version=2,
        request_key="deleted",
        reason="QA",
        payload={},
        deleted=True,
    )
    assert health.notification_details("ORDER_PROGRESS_STALLED") == {
        "status": "UNAVAILABLE"
    }


def test_stale_cycle_is_not_current_safety_detail(database):
    health = TradingHealth(
        database, account_id=SCOPE.account_id, clock_ms=lambda: 1000000
    )
    key = StateKey(**asdict(SCOPE), namespace="trading-pipeline-cycle-v1", key="old")
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="old",
        reason="QA",
        payload={
            "started_at_ms": 1,
            "status": "ENTRY_BLOCKED",
            "phases": {"exits": {"status": "BLOCKED"}},
        },
    )
    health()
    assert health.notification_details("POSITION_SAFETY_BLOCKED") == {"status": "STALE"}


def test_enrichment_database_error_keeps_base_position_finding(database, monkeypatch):
    health = TradingHealth(
        database, account_id=SCOPE.account_id, clock_ms=lambda: 1000000
    )
    key = StateKey(
        **asdict(SCOPE), namespace="trading-pipeline-cycle-v1", key="current"
    )
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="current",
        reason="QA",
        payload={
            "started_at_ms": 1000000,
            "status": "ENTRY_BLOCKED",
            "phases": {"exits": {"status": "BLOCKED"}},
        },
    )

    def broken(*args):
        raise ValueError("SECRET_DB")

    monkeypatch.setattr("services.v2_trading_health.pipeline_position_details", broken)
    health()
    saved = json.loads(health.store.read(health.key).payload_json)
    assert "POSITION_SAFETY_BLOCKED" in saved["findings"]
    assert health.notification_details("POSITION_SAFETY_BLOCKED") == {
        "status": "UNAVAILABLE"
    }


def test_order_health_journal_includes_leg_and_episode(database):
    episode, orders, _ = opened(database)
    closing, _ = orders.prepare(
        episode, leg="CLOSE", quantity="0.01", request_key="stalled"
    )
    with database() as conn:
        conn.execute(
            "UPDATE v2_orders SET version=version+1,updated_at=clock_timestamp()-interval '100 seconds' WHERE order_id=%s",
            (closing,),
        )
    health = TradingHealth(database, account_id=SCOPE.account_id)
    assert "ORDER_PROGRESS_STALLED" in health()
    record = health.notification_details("ORDER_PROGRESS_STALLED")["orders"][0]
    assert record["leg"] == "CLOSE" and record["episode_id"] == episode
