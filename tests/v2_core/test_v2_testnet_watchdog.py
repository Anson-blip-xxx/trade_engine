import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_v2_intent_admission import database as database_fixture

from services.v2_testnet_watchdog import (
    DaemonHeartbeat,
    DependencyAssessment,
    HealthWatchdog,
    WatchdogConfig,
    WatchdogTelegramNotifier,
    watchdog_credentials,
)
from v2_core.account_risk import AccountScope
from v2_core.state import StateKey
from v2_core.telegram import TelegramOperationalSink

database = database_fixture


class Stop:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return bool(self.waits)

    def wait(self, seconds):
        self.waits.append(seconds)
        return True


class Response:
    status = 200

    @staticmethod
    def read(_):
        return b'{"ok":true,"result":{"message_id":7,"chat":{"id":-123}}}'


class Connection:
    calls: ClassVar[list] = []

    def __init__(self, host, timeout):
        assert (host, timeout) == ("api.telegram.org", 10)

    def request(self, method, target, *, body, headers):
        self.calls.append((method, target, body, headers))

    @staticmethod
    def getresponse():
        return Response()

    @staticmethod
    def close():
        pass


def configuration(**changes):
    values = {
        "V2_ACCOUNT_ID": "testnet-primary",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_WATCHDOG_INTERVAL_SECONDS": "15",
        "V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS": "180",
    }
    values.update(changes)
    return values


def test_watchdog_configuration_is_explicit_and_bounded():
    assert WatchdogConfig.from_mapping(configuration()) == WatchdogConfig(
        "testnet-primary", 15, 180
    )
    for name, value in (
        ("V2_ENVIRONMENT", "LIVE"),
        ("V2_WATCHDOG_INTERVAL_SECONDS", "4"),
        ("V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS", "29"),
        ("V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS", "20"),
    ):
        with pytest.raises(ValueError):
            WatchdogConfig.from_mapping(configuration(**{name: value}))


def test_dependency_assessment_skips_heartbeat_when_postgres_is_down():
    calls = []
    assess = DependencyAssessment(
        postgres=lambda: False,
        redis=lambda: True,
        clickhouse=lambda: (_ for _ in ()).throw(ConnectionError("secret")),
        heartbeat=lambda: calls.append("heartbeat"),
    )
    assert assess() == frozenset(
        {"POSTGRES_UNAVAILABLE", "CLICKHOUSE_UNAVAILABLE"}
    )
    assert calls == []


def test_dependency_assessment_includes_fixed_heartbeat_diagnostic():
    assess = DependencyAssessment(
        postgres=lambda: True,
        redis=lambda: True,
        clickhouse=lambda: True,
        heartbeat=lambda: "DAEMON_HEARTBEAT_STALE",
    )
    assert assess() == frozenset({"DAEMON_HEARTBEAT_STALE"})


def test_transition_notifications_are_quiet_until_change_and_retry_failures():
    states = iter(
        (
            frozenset(),
            frozenset({"REDIS_UNAVAILABLE"}),
            frozenset({"REDIS_UNAVAILABLE"}),
            frozenset(),
        )
    )

    class Notify:
        account_id = "testnet-primary"

        def __init__(self):
            self.events = []
            self.fail_first = True

        def __call__(self, event):
            self.events.append(event)
            if self.fail_first:
                self.fail_first = False
                return False
            return True

    notify = Notify()
    watchdog = HealthWatchdog(
        lambda: next(states), notify=notify, stop=Stop(), interval_seconds=15
    )
    assert watchdog.run_once() == frozenset()
    watchdog.run_once()
    watchdog.run_once()
    watchdog.run_once()
    assert [(item["status"], item["error_code"]) for item in notify.events] == [
        ("UNAVAILABLE", "REDIS_UNAVAILABLE"),
        ("UNAVAILABLE", "REDIS_UNAVAILABLE"),
        ("RECOVERED", "REDIS_UNAVAILABLE"),
    ]


def test_watchdog_notifier_sends_only_allowlisted_transition():
    Connection.calls.clear()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    notifier = WatchdogTelegramNotifier(
        sink, account_id="testnet-primary", clock_ms=lambda: 123456
    )
    scope = {
        "exchange": "BINANCE",
        "account_id": "testnet-primary",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    assert notifier(
        {
            "status": "RECOVERED",
            "error_code": "POSTGRES_UNAVAILABLE",
            "account_scope": scope,
        }
    )
    sent = json.loads(Connection.calls[0][2])
    assert "RECOVERED" in sent["text"]
    assert "POSTGRES_UNAVAILABLE" in sent["text"]
    assert notifier(
        {
            "status": "RECOVERED",
            "error_code": "secret endpoint",
            "account_scope": scope,
        }
    ) is False


def test_watchdog_credentials_load_only_telegram_fields(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text(
        "BINANCE_TESTNET_API_KEY=must-not-load\n"
        "TG_NOTIFY_TOKEN=123:abcdefghijklmnop\n"
        "TG_NOTIFY_CHAT_ID=-123\n"
    )
    path.chmod(0o600)
    assert watchdog_credentials(path) == {
        "TG_NOTIFY_TOKEN": "123:abcdefghijklmnop",
        "TG_NOTIFY_CHAT_ID": "-123",
    }


def test_watchdog_systemd_unit_is_independent_and_hardened():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/v2-testnet/trade-v2-testnet-watchdog.service.in").read_text()
    assert "Requires=postgresql" not in unit
    assert "Requires=trade-v2" not in unit
    assert "--runtime-only" in unit
    assert "Restart=always" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadOnlyPaths=/var/run/postgresql /var/lib/trade-engine-v2" in unit
    assert "[Install]\nWantedBy=multi-user.target" in unit


def insert_heartbeat(database, *, status="RUNNING", age_seconds=0):
    scope = AccountScope("BINANCE", "testnet-primary", "SANDBOX", "FUTURES")
    key = StateKey(
        **asdict(scope), namespace="testnet-trading-daemon-v1", key="latest"
    )
    payload = {
        "status": status,
        "account_scope": asdict(scope),
        "entry_dispatch_enabled": False,
        "protection_writes_enabled": False,
        "reduce_only_exits_enabled": False,
    }
    with database() as conn:
        conn.execute(
            """INSERT INTO v2_business_state(state_id,scope,version,payload,deleted)
            VALUES (%s,%s,1,%s,FALSE)""",
            (key.identity, Jsonb(asdict(key)), Jsonb(payload)),
        )
        conn.execute(
            """INSERT INTO v2_state_history(event_id,state_id,request_key,
            expected_version,version,payload,deleted,reason,created_at)
            VALUES (%s,%s,'watchdog-test',0,1,%s,FALSE,
                'TESTNET_TRADING_DAEMON_CYCLE',%s)""",
            (
                str(uuid4()),
                key.identity,
                Jsonb(payload),
                datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
            ),
        )


def test_database_heartbeat_accepts_fresh_running_checkpoint(database):
    insert_heartbeat(database)
    heartbeat = DaemonHeartbeat(
        database, account_id="testnet-primary", max_age_seconds=180
    )
    assert heartbeat() is None


def test_database_heartbeat_reports_degraded_checkpoint(database):
    insert_heartbeat(database, status="DEGRADED")
    heartbeat = DaemonHeartbeat(
        database, account_id="testnet-primary", max_age_seconds=180
    )
    assert heartbeat() == "DAEMON_DEGRADED"


def test_database_heartbeat_reports_stale_and_missing_checkpoint(database):
    heartbeat = DaemonHeartbeat(
        database, account_id="testnet-primary", max_age_seconds=180
    )
    assert heartbeat() == "DAEMON_HEARTBEAT_MISSING"
    insert_heartbeat(database, age_seconds=181)
    assert heartbeat() == "DAEMON_HEARTBEAT_STALE"
