"""Pure typed-mode monitoring contracts; no live DB, credentials or network."""

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_health as managed
from services import v2_managed_testnet as ports_module
from services import v2_trading_health as business_module
from services import v2_typed_health as typed
from services.v2_management_runtime import PHASES, REASON, SCHEMA, ManagementBinding
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import SCHEMA as POLICY
from v2_core.state import StateKey


def sample():
    scope = AccountScope("BINANCE", "typed-health-qa", "SANDBOX", "FUTURES")
    binding = ManagementBinding(scope, str(uuid4()), str(uuid4()), 2, 7, str(uuid4()))
    route = {
        "target_registry": binding.registry_id,
        "binding_version": binding.binding_version,
        "epoch": binding.epoch,
        "worker_token": binding.worker_token,
        "phase": "ACTIVE",
    }
    payload = {
        "schema": SCHEMA,
        "account_scope": asdict(scope),
        "binding_digest": binding.digest,
        "cycle_id": str(uuid4()),
        "started_at_ms": 1000,
        "finished_at_ms": 1900,
        "status": "CLEAR",
        "execution_authorized": False,
        "entry_dispatch_enabled": False,
        "phases": {
            name: {
                "status": "CLEAR",
                "started_at_ms": 1000 + index * 200,
                "finished_at_ms": 1200 + index * 200,
            }
            for index, name in enumerate(PHASES)
        },
    }
    key = StateKey(
        **asdict(scope), namespace="testnet-trading-daemon-v1", key="management-latest"
    )
    report = {
        "mode": "TYPED_STRATEGY",
        "binding": binding.guard_args,
        "execution_authorized": False,
        "entries_enabled": False,
        "entry": {
            "status": "RUNNING",
            "owner_alive": True,
            "execution_authorized": False,
            "entry_dispatch_enabled": False,
        },
        "settlement": {
            "status": "COMPLETED",
            "kind": "FINANCIAL",
            "execution_authorized": False,
            "progress_fresh": {"FINANCIAL": True, "OUTCOME": True},
        },
    }
    return SimpleNamespace(
        scope=scope,
        binding=binding,
        route=route,
        payload=payload,
        key=key,
        report=report,
        statements=[],
        row=None,
    )


def heartbeat(value):
    @contextmanager
    def connect():
        def execute(query, params=()):
            value.statements.append((query, params))
            return SimpleNamespace(fetchone=lambda: value.row)

        yield SimpleNamespace(execute=execute)

    return typed.ManagementHeartbeat(
        connect,
        tenant=value.binding.tenant_id,
        scope=value.scope,
        route_for=lambda: value.route,
        max_age_seconds=30,
        clock_ms=lambda: 2000,
    )


def committed_row(value):
    return [
        asdict(value.key),
        value.payload,
        False,
        deepcopy(value.payload),
        False,
        REASON,
        100,
    ]


def test_typed_heartbeat_binds_current_checkpoint_and_history():
    value = sample()
    value.row = committed_row(value)
    probe = heartbeat(value)
    assert probe() is None
    assert probe.completion_status == "CURRENT"
    assert value.statements[-1][1] == (value.key.identity,)
    assert "h.version=b.version" in value.statements[-1][0]
    assert "h.payload" in value.statements[-1][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "scope",
        "history",
        "reason",
        "deleted",
        "history_deleted",
        "schema",
        "binding",
        "future",
        "phase_missing",
        "phase_time",
        "authorized",
        "entries",
    ],
)
def test_typed_heartbeat_never_accepts_wrong_binding_or_incomplete_evidence(mutation):
    value = sample()
    if mutation == "scope":
        value.key = StateKey(
            **asdict(value.scope), namespace="testnet-trading-daemon-v1", key="latest"
        )
    elif mutation == "schema":
        value.payload["schema"] = "old"
    elif mutation == "binding":
        value.route["epoch"] += 1
    elif mutation == "future":
        value.payload["finished_at_ms"] = 2001
    elif mutation == "phase_missing":
        del value.payload["phases"]["exits"]
    elif mutation == "phase_time":
        value.payload["phases"]["exits"]["finished_at_ms"] = 1000
    elif mutation == "authorized":
        value.payload["execution_authorized"] = True
    elif mutation == "entries":
        value.payload["entry_dispatch_enabled"] = True
    value.row = committed_row(value)
    if mutation == "history":
        value.row[3]["cycle_id"] = str(uuid4())
    elif mutation == "reason":
        value.row[5] = "UNRELATED_WRITE"
    elif mutation == "deleted":
        value.row[2] = True
    elif mutation == "history_deleted":
        value.row[4] = True
    probe = heartbeat(value)
    assert probe() == "DAEMON_HEARTBEAT_INVALID"
    assert probe.completion_status == "UNKNOWN"


@pytest.mark.parametrize("age", [-1, 30001, "NaN", "Infinity"])
def test_typed_heartbeat_rejects_stale_or_nonfinite_commit_time(age):
    value = sample()
    value.row = committed_row(value)
    value.row[-1] = age
    assert heartbeat(value)() == "DAEMON_HEARTBEAT_STALE"


def test_started_heartbeat_only_proves_activity_not_completion():
    value = sample()
    value.payload.update(status="STARTED", phases={})
    del value.payload["finished_at_ms"]
    value.row = committed_row(value)
    probe = heartbeat(value)
    assert probe() is None
    assert probe.completion_status == "IN_PROGRESS"
    value.row = None
    assert probe() == "DAEMON_HEARTBEAT_MISSING"
    assert probe.completion_status == "UNKNOWN"


@pytest.mark.parametrize(
    "phase,code",
    [
        ("protection", "POSITION_SAFETY_BLOCKED"),
        ("exits", "POSITION_SAFETY_BLOCKED"),
        ("recovery", "DAEMON_DEGRADED"),
    ],
)
def test_management_blocked_phase_is_not_hidden(phase, code):
    value = sample()
    value.payload["status"] = "BLOCKED"
    value.payload["phases"][phase]["status"] = "BLOCKED"
    value.row = committed_row(value)
    assert heartbeat(value)() == code


def lanes(value):
    return typed.typed_lane_failures(
        lambda: value.report,
        tenant=value.binding.tenant_id,
        scope=value.scope,
        route=value.route,
    )


def test_typed_lane_observations_cannot_authorize_execution():
    value = sample()
    assert lanes(value) == frozenset()
    value.report["entries_enabled"] = True
    value.report["entry"]["entry_dispatch_enabled"] = True
    assert lanes(value) == frozenset()
    value.report["binding"]["epoch"] += 1
    assert lanes(value) == {"BUSINESS_HEALTH_UNAVAILABLE"}


@pytest.mark.parametrize(
    "state",
    ["DEGRADED", "RETRY_WAIT", "EXHAUSTED", "STOP_UNCONFIRMED", "STOPPED", "invented"],
)
def test_typed_entry_fault_uses_separate_fixed_alert(state):
    value = sample()
    value.report["entry"]["status"] = state
    assert lanes(value) == {"TYPED_ENTRY_DEGRADED"}


def test_settlement_analysis_cannot_hide_financial_failure():
    value = sample()
    value.report["settlement"].update(status="FAILED", kind="OUTCOME")
    assert lanes(value) == {"TYPED_SETTLEMENT_ANALYSIS_DEGRADED"}
    value.report["settlement"]["progress_fresh"]["FINANCIAL"] = False
    assert lanes(value) == {
        "TYPED_SETTLEMENT_ANALYSIS_DEGRADED",
        "TYPED_FINANCIAL_SETTLEMENT_UNAVAILABLE",
    }
    value.report["settlement"].update(
        status="FAILED", kind="FINANCIAL", progress_fresh={"FINANCIAL": True}
    )
    assert lanes(value) == {"TYPED_FINANCIAL_SETTLEMENT_UNAVAILABLE"}


def test_unknown_snapshot_and_callback_failure_stay_unknown():
    value = sample()
    value.report = None
    assert lanes(value) == {"BUSINESS_HEALTH_UNAVAILABLE"}

    def fail():
        raise RuntimeError("secret db error")

    assert typed.typed_lane_failures(
        fail, tenant=value.binding.tenant_id, scope=value.scope, route=value.route
    ) == {"BUSINESS_HEALTH_UNAVAILABLE"}


def test_typed_business_health_keeps_real_ledger_checks_not_legacy_queue(monkeypatch):
    value = sample()
    saved = []
    queries = []
    settings = {key: definition[0] for key, definition in POLICY.items()}
    settings["health.safety_seconds"] = 1

    @contextmanager
    def connect():
        def execute(query, params=()):
            queries.append(query)
            if "FROM v2_orders o JOIN v2_trade_intents" in query:
                rows = [
                    (str(uuid4()), "BTCUSDT", "UNKNOWN", 999, 1, "OPEN", str(uuid4()))
                ]
            elif "FROM v2_trade_intents i JOIN v2_episodes" in query:
                rows = [(str(uuid4()), "BTCUSDT", 0)]
            else:
                rows = []
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: None)

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(
        business_module,
        "PolicyStore",
        lambda *_: SimpleNamespace(
            read=lambda: SimpleNamespace(values=settings, version=1, digest="qa")
        ),
    )
    monkeypatch.setattr(
        business_module,
        "opening_health",
        lambda *_: ({}, {"OPENING_SUBMISSION_STALLED": {}}),
    )
    monkeypatch.setattr(business_module, "recovery_summaries", lambda *_: {})
    monkeypatch.setattr(
        business_module, "rejected_residual_closes", lambda *_: [{"rejected_at_ms": 0}]
    )
    monkeypatch.setattr(
        business_module, "health_notification_details", lambda _code, detail: detail
    )
    monkeypatch.setattr(business_module, "retain_opening_incidents", lambda *_: {})

    def forbidden(*_, **__):
        pytest.fail("typed health must not observe legacy S6/S8 or JEV")

    monkeypatch.setattr(business_module, "read_signal_queue", forbidden)
    monkeypatch.setattr(business_module, "read_budget_health", forbidden)
    health = business_module.TradingHealth(
        connect,
        account_id=value.scope.account_id,
        tv_enabled=True,
        quant_enabled=True,
        runtime_mode="TYPED_STRATEGY",
        clock_ms=lambda: 2000000,
    )
    health.store = SimpleNamespace(
        read=lambda *_: None,
        change=lambda *_args, **kwargs: (
            saved.append(kwargs["payload"]) or SimpleNamespace(code="APPLIED")
        ),
    )
    result = health()
    assert result == {
        "OPENING_SUBMISSION_STALLED",
        "ORDER_PROGRESS_STALLED",
        "POSITION_SAFETY_BLOCKED",
        "SETTLEMENT_OVERDUE",
    }
    assert health.key.key == "typed-latest" and health.sources == ()
    assert all("trading-pipeline-cycle-v1" not in query for query in queries)
    assert saved[0]["execution_health"] == "UNKNOWN"
    assert saved[0]["typed_queue_observation"] == "NOT_EVALUATED"
    assert saved[0]["execution_authorized"] is False
    assert saved[0]["jev_budget"] is None


@pytest.fixture
def monitor_case(monkeypatch):
    value = sample()
    value.pins = []
    value.fail_delivery = value.fail_pin = False
    value.business_modes = []
    value.assessment = None

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/not-read-in-test")
    monkeypatch.setattr(
        managed,
        "_notification_credentials",
        lambda _: {"TG_NOTIFY_TOKEN": "qa", "TG_NOTIFY_CHAT_ID": "qa-room"},
    )
    monkeypatch.setattr(ports_module, "scope_for", lambda *_: value.scope)
    monkeypatch.setattr(ports_module, "database", lambda: lambda: None)
    monkeypatch.setattr(managed, "TelegramOperationalSink", lambda **_: object())
    monkeypatch.setattr(
        managed, "WatchdogTelegramNotifier", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(managed, "WatchdogDeliveryState", lambda *_: object())

    def business(*_args, **kwargs):
        value.business_modes.append(kwargs["runtime_mode"])
        return SimpleNamespace(notification_details=lambda _: None)

    def assessment(**kwargs):
        value.assessment = kwargs
        return lambda: frozenset({"ORDER_PROGRESS_STALLED"})

    class Pins:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_once(self, *, limit):
            value.pins.append(("deliver", limit))
            if value.fail_delivery:
                raise ValueError("secret notification payload")

        def _retry_pins(self, limit):
            value.pins.append(("pin", limit))
            if value.fail_pin:
                raise ValueError("secret pin payload")

    class Watchdog:
        def __init__(self, observe, **kwargs):
            self.observe, self.maintain = observe, kwargs["maintenance"]

        def run_once(self):
            result = self.observe()
            self.maintain()
            return result

    monkeypatch.setattr(managed, "TradingHealth", business)
    monkeypatch.setattr(managed, "DependencyAssessment", assessment)
    monkeypatch.setattr(managed, "TradeLifecycleNotifications", Pins)
    monkeypatch.setattr(managed, "HealthWatchdog", Watchdog)
    return value


def test_factory_selects_typed_heartbeat_and_preserves_actual_alerts(monitor_case):
    value = monitor_case
    values = {
        "V2_CONTROLLER_WORKER_MODE": "TYPED_STRATEGY",
        "V2_TG_ALERT_CHAT_ID": "qa-dm",
    }
    monitor = managed.build_managed_health(
        value.binding.tenant_id,
        values,
        threading.Event(),
        runtime_snapshot=lambda: value.report,
    )
    result = monitor.step(value.route)
    assert type(value.assessment["heartbeat"]) is typed.ManagementHeartbeat
    assert value.business_modes == ["TYPED_STRATEGY"]
    assert result["active"] == ["ORDER_PROGRESS_STALLED"]
    assert result["execution_health"] == "UNKNOWN"
    assert result["execution_authorized"] is False
    assert value.pins == [("deliver", 1), ("pin", 1)]
    value.report["entry"]["status"] = "DEGRADED"
    assert monitor.step(value.route)["active"] == [
        "ORDER_PROGRESS_STALLED",
        "TYPED_ENTRY_DEGRADED",
    ]
    newer = {**value.route, "epoch": 8}
    assert "BUSINESS_HEALTH_UNAVAILABLE" in monitor.step(newer)["active"]
    assert monitor.route["epoch"] == 8


@pytest.mark.parametrize("failure", ["delivery", "pin", "both"])
def test_typed_notifications_attempt_delivery_and_pin_independently(
    monitor_case, capsys, failure
):
    value = monitor_case
    value.fail_delivery = failure in {"delivery", "both"}
    value.fail_pin = failure in {"pin", "both"}
    monitor = managed.build_managed_health(
        value.binding.tenant_id,
        {"V2_CONTROLLER_WORKER_MODE": "TYPED_STRATEGY", "V2_TG_ALERT_CHAT_ID": "qa-dm"},
        threading.Event(),
        runtime_snapshot=lambda: value.report,
    )
    assert monitor.step(value.route)["status"] == "MONITORED"
    assert value.pins == [("deliver", 1), ("pin", 1)]
    assert "secret" not in capsys.readouterr().out


def test_legacy_factory_keeps_old_heartbeat_and_pin_only(monitor_case):
    value = monitor_case
    monitor = managed.build_managed_health(
        value.binding.tenant_id, {"V2_TG_ALERT_CHAT_ID": "qa-dm"}, threading.Event()
    )
    result = monitor.step(value.route)
    assert type(value.assessment["heartbeat"]) is managed.DaemonHeartbeat
    assert value.business_modes == ["LEGACY"]
    assert "execution_health" not in result
    assert value.pins == [("pin", 1)]


def test_typed_without_runtime_observation_cannot_report_recovered(monitor_case):
    value = monitor_case
    monitor = managed.build_managed_health(
        value.binding.tenant_id,
        {"V2_CONTROLLER_WORKER_MODE": "TYPED_STRATEGY", "V2_TG_ALERT_CHAT_ID": "qa-dm"},
        threading.Event(),
    )
    assert "BUSINESS_HEALTH_UNAVAILABLE" in monitor.step(value.route)["active"]


def test_typed_runtime_snapshot_contains_binding_and_settlement():
    # A pure source contract assertion is intentionally not a worker/process test.
    from services.v2_strategy_runtime import TypedStrategyRuntime

    value = sample()
    group = SimpleNamespace(
        entries_enabled=False,
        members=(
            object(),
            SimpleNamespace(snapshot=lambda: value.report["settlement"]),
        ),
        startup_failure=None,
        stop_confirmations=(None, None),
    )
    runtime = TypedStrategyRuntime(group, binding=value.binding.guard_args)
    report = runtime.snapshot()
    assert report["binding"] == value.binding.guard_args
    assert report["settlement"] == value.report["settlement"]
    report["binding"]["epoch"] += 1
    assert runtime.snapshot()["binding"] == value.binding.guard_args


def test_typed_codes_use_existing_business_notification_channel():
    from v2_core.telegram import BUSINESS_HEALTH_MESSAGES

    assert "TYPED_ENTRY_DEGRADED" in BUSINESS_HEALTH_MESSAGES
    assert "TYPED_FINANCIAL_SETTLEMENT_UNAVAILABLE" in BUSINESS_HEALTH_MESSAGES
    assert "TYPED_SETTLEMENT_ANALYSIS_DEGRADED" in BUSINESS_HEALTH_MESSAGES
    assert "secret" not in json.dumps(BUSINESS_HEALTH_MESSAGES)


def test_typed_delivery_isolated_from_legacy_and_preserves_transition_dedup():
    from services.v2_testnet_watchdog import HealthWatchdog, WatchdogDeliveryState

    value = sample()
    typed_state = typed.TypedDeliveryState(lambda: None, value.scope.account_id)
    legacy_state = WatchdogDeliveryState(lambda: None, value.scope.account_id)
    assert typed_state.key.identity != legacy_state.key.identity
    assert typed_state.key.namespace == legacy_state.key.namespace
    saved, events = set(), []

    def save(items):
        saved.clear()
        saved.update(items)

    typed_state.load = lambda: set(saved)
    typed_state.save = save

    def notify(event):
        events.append(event)
        return True

    notify.account_id = value.scope.account_id

    def observer():
        return HealthWatchdog(
            lambda: lanes(value),
            notify=notify,
            stop=threading.Event(),
            interval_seconds=10,
            delivery_state=typed_state,
        )

    value.report["entry"]["status"] = "DEGRADED"
    watchdog = observer()
    watchdog.run_once()
    watchdog.run_once()
    assert len(events) == 1
    observer().run_once()  # Durable dedup also works after observer reconstruction.
    assert len(events) == 1
    value.report = None
    watchdog.run_once()
    assert all(item["status"] == "UNAVAILABLE" for item in events)
    assert "TYPED_ENTRY_DEGRADED" in saved
    value.report = sample().report
    value.report["binding"] = value.binding.guard_args
    watchdog.run_once()
    assert (
        sum(
            item["error_code"] == "TYPED_ENTRY_DEGRADED"
            and item["status"] == "RECOVERED"
            for item in events
        )
        == 1
    )
