import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.v2_host_disk_health import GIB, measure, monitor_once
from tests.support.v2_admission import database as database_fixture
from v2_core.shared_market import MarketPolicy
from v2_core.telegram import TelegramOperationalSink

database = database_fixture


def snapshot(free=20, previous=(), **changes):
    return measure(
        **{
            "free_bytes": free * GIB,
            "total_bytes": 118 * GIB,
            "minimum_gib": 8,
            "warning_gib": 16,
            "recovery_gib": 2,
            "now": 100000,
            "market_at": 99000,
            "max_age_ms": 90000,
            "previous": previous,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "free,previous,expected",
    [
        (20, [], []),
        (16, [], []),
        (15, [], ["HOST_DISK_LOW"]),
        (17, ["HOST_DISK_LOW"], ["HOST_DISK_LOW"]),
        (18, ["HOST_DISK_LOW"], []),
        (8, [], ["HOST_DISK_LOW"]),
        (7, [], ["HOST_DISK_CRITICAL", "HOST_DISK_LOW"]),
    ],
)
def test_disk_thresholds_and_recovery_hysteresis(free, previous, expected):
    result = snapshot(free, previous)
    assert result["active"] == expected
    assert (result["market_reason"] == "MARKET_DISK_RESERVE_REQUIRED") is (free < 8)


@pytest.mark.parametrize("observed", [None, 10000, 100001])
def test_stale_missing_future_heartbeat_not_reported_healthy(observed):
    assert "HOST_MARKET_STALE" in snapshot(market_at=observed)["active"]


@pytest.mark.parametrize(
    "changes",
    [
        {"free_bytes": -1},
        {"free_bytes": True},
        {"warning_gib": 8},
        {"recovery_gib": 0},
        {"market_at": "99000"},
    ],
)
def test_invalid_measurement_fails_closed(changes):
    with pytest.raises(ValueError):
        snapshot(**changes)


@pytest.fixture
def monitor(database):
    with database() as conn:
        conn.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260929_shared_market.sql"
            ).read_text()
        )
        conn.execute(
            "INSERT INTO v2_market_hub_status VALUES('SANDBOX',99000,'{}'::jsonb)"
        )
    state = {"free": 12, "success": True, "calls": []}

    def sink(event_id, scope, kind, payload):
        assert scope == "SANDBOX" and kind == "HOST_RESOURCE_HEALTH"
        state["calls"].append(payload)
        return state["success"]

    def run():
        return monitor_once(
            database,
            sink,
            disk_usage=lambda _: SimpleNamespace(
                free=state["free"] * GIB, total=118 * GIB
            ),
            clock_ms=lambda: 100000,
        )

    return state, run


def test_notification_ack_persists_across_monitor_restarts(database, monitor):
    state, run = monitor
    assert not run()["delivery_pending"]
    assert len(state["calls"]) == 1
    for _ in range(3):
        assert not run()["delivery_pending"]
    assert len(state["calls"]) == 1
    state["free"] = 18
    assert run()["active"] == []
    assert [p["status"] for p in state["calls"]] == ["UNAVAILABLE", "RECOVERED"]
    run()
    assert len(state["calls"]) == 2
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_failed_delivery_retried_without_losing_alarm(monitor):
    state, run = monitor
    state["success"] = False
    assert run()["delivery_pending"]
    state["success"] = True
    assert not run()["delivery_pending"]
    run()
    assert len(state["calls"]) == 2


def test_unknown_state_never_announces_recovery(monitor, monkeypatch):
    state, run = monitor
    run()
    state["free"] = 50
    monkeypatch.setattr(
        MarketPolicy,
        "read",
        lambda *_: (_ for _ in ()).throw(RuntimeError("DB unavailable")),
    )
    with pytest.raises(RuntimeError):
        run()
    assert len(state["calls"]) == 1


@pytest.mark.parametrize(
    "code,description",
    [
        ("HOST_DISK_CRITICAL", "新开仓"),
        ("RETENTION_TIMER_DISABLED", "计时器"),
        ("RETENTION_JOB_FAILED", "执行失败"),
        ("RETENTION_JOB_STALE", "长时间没有完成"),
        ("QUEUE_STORAGE_UNAVAILABLE", "不会把未知当恢复"),
        ("QUEUE_READY_PROJECTION_UNAVAILABLE", "原领取路径"),
        ("QUEUE_READY_INDEX_PRESSURE", "索引"),
        ("QUEUE_VACUUM_PRESSURE", "死元组"),
        ("QUEUE_PROJECTION_MISMATCH", "不一致"),
    ],
)
def test_host_notification_contains_capacity_without_extra_payload_or_group(
    code, description
):
    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            calls.append(json.loads(kwargs["body"]))

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                read=lambda _: (
                    b'{"ok":true,"result":{"message_id":7,"chat":{"id":123}}}'
                ),
            )

        def close(self):
            pass

    sink = TelegramOperationalSink(
        token="123:qa-disabled-token-only",
        chat_id="123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    payload = {
        **snapshot(7),
        "status": "UNAVAILABLE",
        "error_code": code,
        "private": "must-not-leak",
    }
    assert sink("qa", "SANDBOX", "HOST_RESOURCE_HEALTH", payload)
    assert calls[0]["chat_id"] == "123"
    for expected in ("7.00 GiB", "8.00 GiB", "16.00 GiB", "UTC+8", description):
        assert expected in calls[0]["text"]
    assert "must-not-leak" not in calls[0]["text"]
    assert not sink("qa", "LIVE", "HOST_RESOURCE_HEALTH", payload)
    assert not sink(
        "qa", "SANDBOX", "HOST_RESOURCE_HEALTH", {**payload, "free_bytes": "7"}
    )
    assert len(calls) == 1


def test_maintenance_units_do_not_load_trading_or_master_credentials():
    root = Path(__file__).resolve().parents[2] / "deploy/v2-testnet"
    disk = (root / "trade-v2-host-disk-health.service").read_text()
    gc = (root / "trade-v2-qa-gc.service").read_text()
    assert "LoadCredentialEncrypted=v2-notifications:" in disk
    assert "v2-master" not in disk and "binance.env" not in disk
    assert "PrivateTmp=no" in gc and "--min-age-hours 24" in gc


def test_retention_failure_and_recovery_use_persisted_dm_dedup(database, monitor):
    state, _ = monitor
    state["free"] = 20
    failures = ["RETENTION_JOB_FAILED"]

    def sink(_event, _scope, _kind, payload):
        state["calls"].append(payload)
        return True

    def run():
        return monitor_once(
            database,
            sink,
            disk_usage=lambda _: SimpleNamespace(free=20 * GIB, total=118 * GIB),
            clock_ms=lambda: 100000,
            maintenance_status=lambda: {"active": list(failures), "jobs": {}},
        )

    assert run()["active"] == ["RETENTION_JOB_FAILED"]
    run()
    assert len(state["calls"]) == 1
    failures.clear()
    assert run()["active"] == []
    assert [p["status"] for p in state["calls"]] == ["UNAVAILABLE", "RECOVERED"]


def test_queue_telemetry_failure_preserves_alarms_and_disk_checks(database, monitor):
    state, _ = monitor
    storage = {"status": "PRESSURE", "active": ["QUEUE_READY_INDEX_PRESSURE"]}
    previous_seen = []

    def assess(_connect, *, now_ms, previous):
        assert now_ms == 100000
        previous_seen.append(previous)
        if storage is None:
            raise RuntimeError("private exception must not leak")
        return dict(storage)

    def sink(_event, _scope, _kind, payload):
        state["calls"].append(payload)
        return True

    def run():
        return monitor_once(
            database,
            sink,
            disk_usage=lambda _: SimpleNamespace(
                free=state["free"] * GIB, total=118 * GIB
            ),
            clock_ms=lambda: 100000,
            queue_storage_assess=assess,
        )

    first = run()
    assert first["active"] == ["HOST_DISK_LOW", "QUEUE_READY_INDEX_PRESSURE"]
    run()
    assert len(state["calls"]) == 2  # stable alarms survive separate monitor instances
    assert previous_seen[0] is None and previous_seen[1] == storage
    storage = None
    unknown = run()
    assert unknown["active"] == [
        "HOST_DISK_LOW",
        "QUEUE_READY_INDEX_PRESSURE",
        "QUEUE_STORAGE_UNAVAILABLE",
    ]
    assert "private exception" not in json.dumps(unknown)
    assert all(p["status"] == "UNAVAILABLE" for p in state["calls"])
    state["free"] = 20
    storage = {"status": "HEALTHY", "active": []}
    assert run()["active"] == []
    assert len([p for p in state["calls"] if p["status"] == "RECOVERED"]) == 3
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
