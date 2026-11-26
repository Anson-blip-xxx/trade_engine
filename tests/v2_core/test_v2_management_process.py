"""Offline composition and child lifecycle checks; no PostgreSQL fixture."""

import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_testnet as managed
from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_management_process import (
    ManagementProcessConfig,
    ManagementSignedRequest,
    create_management_process,
)
from services.v2_management_runtime import ManagementBinding, ManagementSupervisor
from v2_core.account_risk import AccountScope
from v2_core.errors import SubmissionNotSent
from v2_core.venue_readiness import GuardedOpeningSubmit


def settings(**changes):
    return {
        "V2_ACCOUNT_ID": "management-qa",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_INTERVAL_SECONDS": "1",
        "V2_ENABLE_ENTRIES": "false",
        "V2_ENABLE_PROTECTION_WRITES": "true",
        "V2_ENABLE_REDUCE_ONLY_EXITS": "true",
        "V2_TESTNET_WRITE_ACK": "SANDBOX:management-qa",
        "V2_EXIT_FEE_RATE": "0.0004",
        **changes,
    }


@pytest.mark.parametrize(
    "unused",
    [
        {},
        {"V2_SYMBOLS": "../broken, BTCUSDT"},
        {"V2_JEV_SHADOW_ACCOUNT_ID": ""},
        {"V2_ENABLE_TV_SIGNALS": "invalid", "V2_TV_ADMISSION_START_MS": "invalid"},
        {"V2_ENABLE_QUANT_SIGNALS": "invalid"},
    ],
)
def test_management_config_ignores_absent_or_invalid_entry_only_fields(unused):
    config = ManagementProcessConfig.from_mapping(settings(**unused))
    assert config == ManagementProcessConfig.from_mapping(settings())
    assert not hasattr(config, "symbols")


@pytest.mark.parametrize(
    "changes",
    [
        {"V2_ENABLE_ENTRIES": "true"},
        {"V2_ENABLE_PROTECTION_WRITES": "invalid"},
        {"V2_ENABLE_REDUCE_ONLY_EXITS": None},
        {"V2_TESTNET_WRITE_ACK": ""},
        {"V2_ENVIRONMENT": "LIVE"},
        {"V2_INTERVAL_SECONDS": None},
        {"V2_INTERVAL_SECONDS": "0"},
        {"V2_INTERVAL_SECONDS": "61"},
        {"V2_EXIT_FEE_RATE": None},
        {"V2_EXIT_FEE_RATE": "-0.1"},
        {"V2_EXIT_FEE_RATE": "0.0101"},
        {"V2_EXIT_FEE_RATE": "4e-4"},
        {"V2_EXIT_FEE_RATE": "NaN"},
        {"V2_SHARED_MARKET": "invalid"},
        {"V2_SHARED_MARKET": True},
    ],
)
def test_management_config_still_validates_management_requirements(changes):
    with pytest.raises((ValueError, TypeError)):
        ManagementProcessConfig.from_mapping(settings(**changes))


def test_management_config_preserves_explicit_disabled_writes_and_shared_budget():
    config = ManagementProcessConfig.from_mapping(
        settings(
            V2_ENABLE_PROTECTION_WRITES="false",
            V2_ENABLE_REDUCE_ONLY_EXITS="false",
            V2_TESTNET_WRITE_ACK="",
            V2_SHARED_MARKET="true",
        )
    )
    assert config.daemon.enable_protection_writes is False
    assert config.daemon.enable_reduce_only_exits is False
    assert config.shared_market is True
    with pytest.raises(ValueError, match="EXPLICIT_MANAGEMENT_CONFIG_REQUIRED"):
        replace(config, daemon=replace(config.daemon, enable_entries=True))


@pytest.fixture
def composition():
    scope = AccountScope("BINANCE", "management-qa", "SANDBOX", "FUTURES")
    binding = ManagementBinding(scope, str(uuid4()), str(uuid4()), 1, 7, str(uuid4()))
    events = []

    @contextmanager
    def connect():
        pytest.fail("factory construction must perform no database I/O")
        yield

    class Guard:
        _args = binding.guard_args

        @contextmanager
        def __call__(self, *, operation):
            events.append(("enter", operation))
            try:
                yield
            finally:
                events.append(("exit", operation))

    def no_http(*args, **kwargs):
        pytest.fail("factory construction must perform no network I/O")

    return SimpleNamespace(
        scope=scope,
        binding=binding,
        events=events,
        config=ManagementProcessConfig.from_mapping(settings()),
        secrets={
            "BINANCE_TESTNET_API_KEY": "qa-key",
            "BINANCE_TESTNET_API_SECRET": "qa-secret",
        },
        kwargs={
            "connect": connect,
            "binding": binding,
            "execution_guard": Guard(),
            "clock_ms": lambda: 1790245000000,
            "signed_connection_factory": no_http,
            "public_connection_factory": no_http,
        },
    )


@pytest.mark.parametrize("shared", [False, True])
def test_real_management_factory_has_no_optional_service_io(composition, shared):
    case = composition
    process = create_management_process(
        replace(case.config, shared_market=shared), case.secrets, **case.kwargs
    )
    assert isinstance(process, ManagementSupervisor)
    assert isinstance(process.protection, DirectionalProtectionStage)
    assert isinstance(process.exits, DirectionalExitStage)
    assert process.runtime.scope == case.scope
    assert process.runtime.projectors == ()
    assert not hasattr(process, "schedulers")
    assert not hasattr(process, "settlement")
    assert process.exits.quant_market is None
    assert process.protection.request is process.exits.request
    assert isinstance(process.protection.request, ManagementSignedRequest)
    gate = process.runtime.execution.submit
    assert isinstance(gate, GuardedOpeningSubmit)
    assert gate.enabled is False and gate.reduce_only_enabled is True
    assert process.runtime.risk_check({"leg": "OPEN", "reduce_only": False}) is False
    assert process.runtime.risk_check({"leg": "CLOSE", "reduce_only": True}) is True
    with pytest.raises(SubmissionNotSent, match="ENDPOINT_OR_WRITE_DISABLED"):
        gate({**asdict(case.scope), "leg": "OPEN", "reduce_only": False})
    assert case.events == []


@pytest.mark.parametrize("change", ["type", "scope", "guard", "secrets"])
def test_management_factory_rejects_wrong_capability_or_binding(composition, change):
    case = composition
    config, secrets, kwargs = case.config, case.secrets, dict(case.kwargs)
    if change == "type":
        config = SimpleNamespace(daemon=config.daemon)
    elif change == "scope":
        config = replace(config, daemon=replace(config.daemon, account_id="other"))
    elif change == "guard":
        kwargs["execution_guard"]._args = {**case.binding.guard_args, "epoch": 8}
    else:
        secrets = {**secrets, "TG_NOTIFY_TOKEN": "not-needed"}
    with pytest.raises(ValueError, match="EXPLICIT_MANAGEMENT_PROCESS_REQUIRED"):
        create_management_process(config, secrets, **kwargs)


@pytest.mark.parametrize(
    "method,path,params,allowed",
    [
        ("POST", "/fapi/v1/order", {"reduceOnly": "false"}, False),
        ("POST", "/fapi/v1/order", {"reduceOnly": True}, False),
        ("POST", "/fapi/v1/leverage", {}, False),
        ("POST", "/fapi/v1/marginType", {}, False),
        ("POST", "/fapi/v1/algoOrder", {"closePosition": "false"}, False),
        ("POST", "/fapi/v1/order", {"reduceOnly": "true"}, True),
        ("POST", "/fapi/v1/algoOrder", {"closePosition": "true"}, True),
        ("DELETE", "/fapi/v1/order", {}, True),
        ("DELETE", "/fapi/v1/algoOrder", {}, True),
    ],
)
def test_management_transport_clamps_write_operation_before_io(
    composition, method, path, params, allowed
):
    case = composition

    class Request:
        account_id, environment = case.scope.account_id, case.scope.environment

        def __call__(self, method, path, params):
            case.events.append(("write", method, path))
            return "ACK"

    request = ManagementSignedRequest(
        Request(),
        account_id=case.scope.account_id,
        environment=case.scope.environment,
        guard=case.kwargs["execution_guard"],
    )
    if allowed:
        assert request(method, path, params) == "ACK"
        assert case.events == [
            ("enter", "MANAGE"),
            ("write", method, path),
            ("exit", "MANAGE"),
        ]
    else:
        with pytest.raises(SubmissionNotSent, match="ENDPOINT_OR_WRITE_DISABLED"):
            request(method, path, params)
        assert case.events == []


def test_management_write_cannot_change_capability_while_waiting_for_guard(composition):
    case = composition
    params = {"reduceOnly": "true"}

    @contextmanager
    def guard(*, operation):
        assert operation == "MANAGE"
        params["reduceOnly"] = "false"
        yield

    class Request:
        account_id, environment = case.scope.account_id, case.scope.environment

        def __call__(self, method, path, actual):
            assert actual == {"reduceOnly": "true"}
            return "ACK"

    request = ManagementSignedRequest(
        Request(),
        account_id=case.scope.account_id,
        environment=case.scope.environment,
        guard=guard,
    )
    assert request("POST", "/fapi/v1/order", params) == "ACK"


@pytest.fixture
def child_case(monkeypatch, composition):
    from services import v2_management_process as factory

    case = composition
    events, sent = [], []
    stop = threading.Event()
    target = {
        "target_registry": case.binding.registry_id,
        "binding_version": case.binding.binding_version,
        "epoch": case.binding.epoch,
        "worker_token": case.binding.worker_token,
    }
    state = {
        "phase": "DRAINING",
        "token": target["worker_token"],
        "preflight_error": None,
        "result": {"status": "CLEAR"},
    }

    class Guard:
        _args = case.binding.guard_args

        def preflight(self):
            events.append("preflight")
            if state["preflight_error"] is not None:
                raise state["preflight_error"]

        @contextmanager
        def connect(self):
            events.append("route-read")
            yield SimpleNamespace(
                execute=lambda *_: SimpleNamespace(
                    fetchone=lambda: (state["phase"], state["token"])
                )
            )

    guard = Guard()

    def forbidden(*args, **kwargs):
        pytest.fail("management child must not initialize a legacy dependency")

    def build_guard(connect, **binding):
        assert connect is case.kwargs["connect"]
        assert binding == case.binding.guard_args
        return guard

    def run_once(*, stop_requested):
        assert stop_requested == stop.is_set
        assert not stop_requested()
        events.append("cycle")
        stop.set()
        return state["result"]

    def create(config, secrets, **kwargs):
        assert isinstance(config, ManagementProcessConfig)
        assert config.daemon.enable_entries is False
        assert not hasattr(config, "symbols")
        assert not hasattr(config, "enable_tv_signals")
        assert not hasattr(config, "enable_quant_signals")
        assert secrets == case.secrets
        assert kwargs["binding"] == case.binding
        assert kwargs["execution_guard"] is guard
        assert kwargs["connect"] is case.kwargs["connect"]
        assert events == ["preflight"]
        events.append("constructed")
        return SimpleNamespace(run_once=run_once)

    def release(_timeout):
        assert sent == ["READY"]
        assert "cycle" not in events
        events.append("released")
        return True

    monkeypatch.setitem(sys.modules, "redis", None)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", None)
    monkeypatch.setattr(managed, "scope_for", lambda *_: case.scope)
    monkeypatch.setattr(
        managed,
        "private_for",
        lambda *_: {"api_key": "qa-key", "api_secret": "qa-secret"},
    )
    monkeypatch.setattr(managed, "database", lambda: case.kwargs["connect"])
    monkeypatch.setattr(managed, "create_execution_guard", build_guard)
    monkeypatch.setattr(managed, "create_testnet_process", forbidden)
    monkeypatch.setattr(managed, "TelegramOperationalSink", forbidden)
    monkeypatch.setattr(managed, "Path", forbidden)
    monkeypatch.setattr(managed, "SystemdMasterKeys", forbidden)
    monkeypatch.setattr(factory, "create_management_process", create)

    def run(**changes):
        managed.child_main(
            case.binding.tenant_id,
            target,
            settings(
                **{
                    "V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY",
                    "V2_CONTROLLER_ENABLE_ENTRIES": "false",
                    **changes,
                }
            ),
            stop,
            SimpleNamespace(wait=release),
            SimpleNamespace(send=sent.append, close=lambda: events.append("closed")),
        )

    return SimpleNamespace(run=run, events=events, sent=sent, state=state)


def test_managed_only_child_preflights_and_waits_release_without_legacy_services(
    child_case,
):
    child_case.run()
    assert child_case.sent == ["READY", "CYCLE"]
    assert child_case.events == [
        "preflight",
        "constructed",
        "released",
        "route-read",
        "cycle",
        "closed",
    ]


@pytest.mark.parametrize(
    "unused",
    [
        {"V2_SYMBOLS": "not a valid symbol"},
        {"V2_JEV_SHADOW_ACCOUNT_ID": ""},
        {"V2_ENABLE_TV_SIGNALS": "invalid", "V2_TV_ADMISSION_START_MS": "invalid"},
        {"V2_ENABLE_QUANT_SIGNALS": "invalid"},
    ],
)
def test_management_child_starts_despite_invalid_entry_only_config(child_case, unused):
    child_case.run(**unused)
    assert child_case.sent == ["READY", "CYCLE"]


def test_managed_only_child_stops_after_route_token_revocation(child_case):
    child_case.state["token"] = str(uuid4())
    child_case.run()
    assert child_case.sent == ["READY"]
    assert "cycle" not in child_case.events
    assert child_case.events[-1] == "closed"


@pytest.mark.parametrize("status", ["BUSY", "BLOCKED", "STARTED", "STOPPED"])
def test_nonclear_management_result_never_reports_success(child_case, status):
    child_case.state["result"] = {"status": status}
    child_case.run()
    assert child_case.sent == ["READY", "DEGRADED"]


def test_managed_only_child_permission_failure_never_ready(child_case):
    from psycopg.errors import InsufficientPrivilege

    child_case.state["preflight_error"] = InsufficientPrivilege("not-for-logs")
    child_case.run()
    assert child_case.sent == ["FAILED:EXECUTION_GUARD_PERMISSION_DENIED"]
    assert child_case.events == ["preflight", "closed"]


@pytest.mark.parametrize(
    "values",
    [
        {"V2_CONTROLLER_WORKER_MODE": "other"},
        {
            "V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY",
            "V2_CONTROLLER_ENABLE_ENTRIES": "true",
        },
    ],
)
def test_invalid_management_mode_is_rejected_before_spawning(values, monkeypatch):
    monkeypatch.setattr(
        managed.multiprocessing,
        "get_context",
        lambda *_: pytest.fail("invalid worker mode must not spawn"),
    )
    with pytest.raises(ValueError):
        managed.ChildWorker("unused", {}, values)


def test_default_worker_mode_remains_legacy():
    assert managed._worker_mode({}) == "LEGACY"


def worker_with_contact(monkeypatch, *, mode="MANAGEMENT_ONLY", messages=()):
    pending = list(messages)
    worker = managed.ChildWorker.__new__(managed.ChildWorker)
    worker.mode = mode
    worker.last_contact = worker.last_progress = 1.0
    worker.progress_timeout = 30
    worker.channel = SimpleNamespace(
        poll=lambda: bool(pending), recv=lambda: pending.pop(0)
    )
    worker.process = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(managed.time, "monotonic", lambda: 100.0)
    return worker


def test_unresponsive_management_child_requires_controller_recovery(monkeypatch):
    worker = worker_with_contact(monkeypatch)
    assert worker.alive() is False
    assert worker.healthy() is False


def test_responsive_blocked_management_child_keeps_management_but_drains_entries(
    monkeypatch,
):
    worker = worker_with_contact(monkeypatch, messages=["DEGRADED"])
    assert worker.alive() is True
    assert worker.last_contact == 100.0 and worker.last_progress == 1.0
    assert worker.healthy() is False


def test_ready_message_does_not_refresh_management_contact_or_progress(monkeypatch):
    worker = worker_with_contact(monkeypatch, messages=["READY"])
    assert worker.alive() is False
    assert worker.last_contact == worker.last_progress == 1.0


def test_completed_management_cycle_refreshes_contact_and_progress(monkeypatch):
    worker = worker_with_contact(monkeypatch, messages=["CYCLE"])
    assert worker.alive() is True
    assert worker.healthy() is True
    assert worker.last_contact == worker.last_progress == 100.0


def test_legacy_child_liveness_remains_os_liveness(monkeypatch):
    worker = worker_with_contact(monkeypatch, mode="LEGACY")
    assert worker.alive() is True
    del worker.mode
    assert worker.alive() is True
