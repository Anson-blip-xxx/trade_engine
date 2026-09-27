import json
import threading
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_trading_health import opening

from services import v2_managed_health as managed
from services.v2_testnet_watchdog import HealthWatchdog, WatchdogDeliveryState
from v2_core.account_risk import AccountScope
from v2_core.state import BusinessState, StateKey
from v2_core.telegram import TelegramOperationalSink

database = database_fixture


def route(registry=None, version=1, phase="ACTIVE", **changes):
    return {
        "target_registry": registry or str(uuid4()),
        "binding_version": version,
        "phase": phase,
        **changes,
    }


def scope(account="test-account", environment="SANDBOX"):
    return AccountScope("BINANCE", account, environment, "FUTURES")


def test_account_binding_switch_rebuilds_observers_and_keeps_durable_dedup(database):
    tenant = str(uuid4())
    first, second = route(), route()
    accounts = {first["target_registry"]: "first", second["target_registry"]: "second"}
    events, built, resolutions, pins = [], [], [], []
    findings = {"first": {"OPENING_SUBMISSION_STALLED"}, "second": set()}

    def resolve(owner, actual):
        resolutions.append((owner, actual))
        return scope(accounts[actual["target_registry"]])

    def factory(account, lifecycle):
        built.append(account.account_id)

        class Notify:
            account_id = account.account_id

            def __call__(self, event):
                events.append(event)
                return True

        return HealthWatchdog(
            lambda: frozenset(findings[account.account_id]) | lifecycle(),
            notify=Notify(),
            stop=threading.Event(),
            interval_seconds=10,
            delivery_state=WatchdogDeliveryState(database, account.account_id),
            maintenance=lambda: pins.append(account.account_id),
        )

    monitor = managed.ManagedHealthMonitor(
        tenant, scope_for=resolve, factory=factory, stop=threading.Event()
    )
    assert monitor.step(first)["active"] == ["OPENING_SUBMISSION_STALLED"]
    monitor.step(first)
    assert len(resolutions) == 1 and len(events) == 1
    assert monitor.step(second)["active"] == []
    # Observing a healthy DIFFERENT account cannot recover the first account.
    assert len(events) == 1
    monitor.step(first)
    monitor.step({**first, "binding_version": 2})
    assert built == ["first", "second", "first", "first"]
    assert pins == ["first", "first", "second", "first", "first"]
    assert len(events) == 1  # Pg receipt survives rebinding and credential rotation.
    findings["first"].clear()
    monitor.step({**first, "binding_version": 2})
    assert [e["status"] for e in events] == ["UNAVAILABLE", "RECOVERED"]
    assert all(e["account_scope"]["account_id"] == "first" for e in events)


@pytest.mark.parametrize("phase", ["STARTING", "BLOCKED", "ACTIVE", "DRAINING"])
def test_controller_lifecycle_is_visible_without_raw_blocker_text(phase):
    class Watch:
        def __init__(self, lifecycle):
            self.lifecycle = lifecycle

        def run_once(self):
            return self.lifecycle() | {"DAEMON_HEARTBEAT_MISSING"}

    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=lambda *_: scope(),
        factory=lambda _, lifecycle: Watch(lifecycle),
        stop=threading.Event(),
    )
    result = monitor.step(route(phase=phase, blockers=["secret raw SQL password"]))
    assert result["status"] == "MONITORED"
    assert "DAEMON_HEARTBEAT_MISSING" in result["active"]
    assert ("EXECUTION_CONTROLLER_BLOCKED" in result["active"]) == (phase == "BLOCKED")
    assert "secret" not in json.dumps(result)


def test_controller_startup_acl_failure_keeps_specific_safe_diagnostic():
    class Watch:
        def __init__(self, lifecycle):
            self.lifecycle = lifecycle

        def run_once(self):
            return self.lifecycle()

    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=lambda *_: scope(),
        factory=lambda _, lifecycle: Watch(lifecycle),
        stop=threading.Event(),
    )
    actual = route(
        phase="BLOCKED",
        blockers=["EXECUTION_GUARD_PERMISSION_DENIED", "secret raw exception"],
    )
    result = monitor.step(actual)
    assert result["active"] == [
        "EXECUTION_CONTROLLER_BLOCKED",
        "EXECUTION_GUARD_PERMISSION_DENIED",
    ]
    assert "secret" not in json.dumps(result)
    assert monitor.step({**actual, "blockers": None})["active"] == [
        "EXECUTION_CONTROLLER_BLOCKED"
    ]


@pytest.mark.parametrize(
    "actual",
    [route(environment="LIVE"), route(version=True), route(version=0), {}, "secret"],
)
def test_invalid_or_live_route_never_constructs_observer(actual):
    calls = []
    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=lambda *_: calls.append("scope"),
        factory=lambda *_: calls.append("factory"),
        stop=threading.Event(),
    )
    assert monitor.step(actual) == {
        "status": "UNAVAILABLE",
        "error_code": "MANAGED_HEALTH_ROUTE_INVALID",
    }
    assert calls == []


def test_missing_and_stopped_route_do_not_resolve_credentials():
    calls = []
    stop = threading.Event()
    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=lambda *_: calls.append("scope"),
        factory=lambda *_: calls.append("factory"),
        stop=stop,
    )
    assert monitor.step(None) == {"status": "IDLE"}
    assert monitor.step(route(phase="STOPPED")) == {"status": "IDLE"}
    stop.set()
    assert monitor.step(route()) == {"status": "IDLE"}
    assert calls == []


def test_binding_failure_forgets_old_account_and_retries_safely(capsys):
    bad = [False]
    calls = []

    def resolve(*_):
        if bad[0]:
            raise RuntimeError("secret key SELECT password")
        return scope()

    class Watch:
        def run_once(self):
            calls.append("run")
            return frozenset()

    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=resolve,
        factory=lambda *_: Watch(),
        stop=threading.Event(),
    )
    monitor.step(route())
    bad[0] = True
    actual = route()
    expected = {
        "status": "UNAVAILABLE",
        "error_code": "MANAGED_HEALTH_BINDING_UNAVAILABLE",
    }
    assert monitor.step(actual) == expected
    assert monitor.step(actual) == expected
    assert monitor.watchdog is None and calls == ["run"]
    assert capsys.readouterr().out.count("MANAGED_HEALTH_BINDING_UNAVAILABLE") == 1
    bad[0] = False
    assert monitor.step(actual)["status"] == "MONITORED"
    assert "secret" not in capsys.readouterr().out


def test_failed_check_emits_safe_diagnostic_and_continues(capsys):
    class Watch:
        def run_once(self):
            raise RuntimeError("token=sensitive")

    monitor = managed.ManagedHealthMonitor(
        str(uuid4()),
        scope_for=lambda *_: scope(),
        factory=lambda *_: Watch(),
        stop=threading.Event(),
    )
    assert monitor.step(route())["error_code"] == "MANAGED_HEALTH_CHECK_UNAVAILABLE"
    assert "sensitive" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    [
        {"BINANCE_TESTNET_API_KEY": "forbidden"},
        [],
        {"TG_NOTIFY_TOKEN": "t", "TG_NOTIFY_CHAT_ID": 1},
    ],
)
def test_notification_credentials_reject_extra_fields_and_invalid_types(tmp_path, bad):
    path = tmp_path / "v2-notifications"
    path.write_text(json.dumps(bad))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        managed._notification_credentials(str(tmp_path))


def test_notification_credentials_reject_symlink_and_public_permissions(tmp_path):
    actual = tmp_path / "actual"
    actual.write_text('{"TG_NOTIFY_TOKEN":"t","TG_NOTIFY_CHAT_ID":"1"}')
    actual.chmod(0o600)
    path = tmp_path / "v2-notifications"
    path.symlink_to(actual)
    with pytest.raises(ValueError):
        managed._notification_credentials(str(tmp_path))
    path.unlink()
    path.write_text(actual.read_text())
    path.chmod(0o644)
    with pytest.raises(ValueError):
        managed._notification_credentials(str(tmp_path))


def test_factory_real_health_detects_rejected_orders_routes_dm_and_retries_pin(
    database, tmp_path, monkeypatch
):
    from services import v2_managed_testnet

    account = scope()
    credentials = tmp_path / "v2-notifications"
    credentials.write_text(
        '{"TG_NOTIFY_TOKEN":"123:abcdefghijklmnop","TG_NOTIFY_CHAT_ID":"-123"}'
    )
    credentials.chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(v2_managed_testnet, "database", lambda: database)
    monkeypatch.setattr(v2_managed_testnet, "scope_for", lambda *_: account)
    monkeypatch.setattr(managed, "redis_ready", lambda: True)
    monkeypatch.setattr(managed, "clickhouse_ready", lambda: True)
    deliveries = []

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, path, *, body, headers):
            self.path, self.payload = path, json.loads(body)
            deliveries.append((path.rsplit("/", 1)[-1], self.payload))

        def getresponse(self):
            payload = self.payload

            class Response:
                status = 200

                def read(self, _size):
                    result = (
                        True
                        if "message_id" in payload
                        else {"message_id": 9, "chat": {"id": int(payload["chat_id"])}}
                    )
                    return json.dumps({"ok": True, "result": result}).encode()

            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        managed,
        "TelegramOperationalSink",
        lambda **kwargs: TelegramOperationalSink(
            **kwargs, connection_factory=Connection
        ),
    )
    store = BusinessState(database)
    heartbeat = StateKey(
        **asdict(account), namespace="testnet-trading-daemon-v1", key="latest"
    )
    store.change(
        heartbeat,
        expected_version=0,
        request_key="QA",
        payload={"status": "DEGRADED", "account_scope": asdict(account)},
        reason="QA",
    )
    pin = StateKey(**asdict(account), namespace="telegram-pin-v1", key=str(uuid4()))
    store.change(
        pin,
        expected_version=0,
        request_key="QA",
        payload={
            "message_id": 7,
            "destination": "123:-123",
            "status": "PENDING",
            "attempts": 0,
            "next_attempt_ms": 0,
        },
        reason="QA",
    )
    for _ in range(3):
        opening(database)
    values = {"V2_TG_ALERT_CHAT_ID": "456", "V2_ENABLE_TV_SIGNALS": "true"}
    actual, tenant = route(), str(uuid4())
    monitor = managed.build_managed_health(tenant, values, threading.Event())
    result = monitor.step(actual)
    assert result["status"] == "MONITORED"
    assert result["active"] == ["DAEMON_DEGRADED", "OPENING_SUBMISSION_STALLED"]
    monitor.step(actual)
    managed.build_managed_health(tenant, values, threading.Event()).step(actual)
    messages = [payload for method, payload in deliveries if method == "sendMessage"]
    assert len(messages) == 2
    assert all(message["chat_id"] == "456" for message in messages)
    pin_calls = [
        payload for method, payload in deliveries if method == "pinChatMessage"
    ]
    assert len(pin_calls) == 1 and pin_calls[0]["chat_id"] == "-123"
    assert json.loads(store.read(pin).payload_json)["status"] == "PINNED"


def test_factory_missing_notification_configuration_is_visible(
    tmp_path, monkeypatch, capsys
):
    from services import v2_managed_testnet

    monkeypatch.setattr(v2_managed_testnet, "scope_for", lambda *_: scope())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monitor = managed.build_managed_health(str(uuid4()), {}, threading.Event())
    assert monitor.step(route())["error_code"] == "MANAGED_HEALTH_BINDING_UNAVAILABLE"
    assert str(tmp_path) not in capsys.readouterr().out
