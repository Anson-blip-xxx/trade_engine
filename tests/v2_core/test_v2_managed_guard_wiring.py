"""Composition regression: a correct helper must actually guard the child.

No network or credential files: dependencies are replaced at the process
boundary, while child_main itself runs unchanged through READY and one cycle.
Actual PostgreSQL ACL behavior is covered by test_v2_execution_guard_acl.
"""

import json
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from services import v2_managed_testnet as managed
from v2_core.account_risk import AccountScope


@pytest.fixture
def child_case(monkeypatch):
    events, sent = [], []
    stop = threading.Event()
    tenant = str(uuid4())
    target = {
        "target_registry": str(uuid4()),
        "binding_version": 7,
        "epoch": 11,
        "worker_token": str(uuid4()),
    }
    state = {"phase": "ACTIVE", "preflight_error": None}
    scope = AccountScope("BINANCE", "qa-registered-account", "SANDBOX", "FUTURES")
    config = SimpleNamespace(daemon=SimpleNamespace(interval_seconds=0))
    pipeline = SimpleNamespace(enable_entries=None)
    notifications = {"TG_NOTIFY_TOKEN": "qa-token", "TG_NOTIFY_CHAT_ID": "qa-chat"}
    private = {"api_key": "qa-key", "api_secret": "qa-secret"}

    def scope_for(actual_tenant, actual_target):
        assert (actual_tenant, actual_target) == (tenant, target)
        return scope

    def private_for(actual_tenant, actual_target, actual_scope):
        assert (actual_tenant, actual_target, actual_scope) == (tenant, target, scope)
        return private

    class CredentialPath:
        def __init__(self, value):
            assert value == "/qa-no-real-credentials"

        def __truediv__(self, name):
            assert name == "v2-notifications"
            return self

        def read_bytes(self):
            return json.dumps(notifications).encode()

    class Resource:
        def __init__(self, name):
            self.name = name

        def ping(self):
            events.append("cache-ping")

        def command(self, statement):
            assert statement == "SELECT 1"
            events.append("archive-check")

        def close(self):
            events.append(self.name + "-closed")

    @contextmanager
    def unguarded_connection():
        pytest.fail(
            "managed child routing must not use the ordinary database connector"
        )
        yield

    class Guard:
        def preflight(self):
            events.append("guard-preflight")
            if state["preflight_error"] is not None:
                raise state["preflight_error"]

        @contextmanager
        def connect(self):
            events.append("guard-connect")

            def execute(statement, params):
                assert (
                    "SELECT phase,worker_token::text FROM v2_execution_routes"
                    in statement
                )
                assert params == (tenant,)
                row = (
                    state["phase"],
                    state.get("worker_token", target["worker_token"]),
                )
                return SimpleNamespace(fetchone=lambda: row)

            yield SimpleNamespace(execute=execute)

    guard = Guard()

    def create_guard(connect, **binding):
        assert connect is unguarded_connection
        assert binding == {
            "tenant_id": tenant,
            "environment": "SANDBOX",
            "registry_id": target["target_registry"],
            "binding_version": target["binding_version"],
            "epoch": target["epoch"],
            "worker_token": target["worker_token"],
        }
        events.append("guard-created")
        return guard

    def parse_config(values):
        assert values["V2_ACCOUNT_ID"] == scope.account_id
        assert values["V2_ENVIRONMENT"] == "SANDBOX"
        assert values["V2_TESTNET_WRITE_ACK"] == "SANDBOX:" + scope.account_id
        return config

    def run_once():
        events.append("pipeline-cycle")
        stop.set()

    def create_process(actual_config, actual_private, **kwargs):
        assert actual_config is config
        assert actual_private == {
            **notifications,
            "BINANCE_TESTNET_API_KEY": private["api_key"],
            "BINANCE_TESTNET_API_SECRET": private["api_secret"],
        }
        assert kwargs["connect"] is unguarded_connection
        assert kwargs["execution_guard"] is guard
        assert kwargs["stop"] is stop
        assert "guard-preflight" in events
        assert "READY" not in sent
        events.append("process-created")
        return SimpleNamespace(
            daemon=SimpleNamespace(pipeline=pipeline, run_once=run_once)
        )

    def send(value):
        sent.append(value)
        events.append("channel-" + value)

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/qa-no-real-credentials")
    monkeypatch.setattr(managed, "scope_for", scope_for)
    monkeypatch.setattr(managed, "private_for", private_for)
    monkeypatch.setattr(managed, "Path", CredentialPath)
    monkeypatch.setattr(managed, "SystemdMasterKeys", lambda _: lambda _key: b"qa-only")
    monkeypatch.setattr(
        managed, "TestnetProcessConfig", SimpleNamespace(from_mapping=parse_config)
    )
    monkeypatch.setattr(managed, "database", lambda: unguarded_connection)
    monkeypatch.setattr(managed, "create_execution_guard", create_guard)
    monkeypatch.setattr(managed, "create_testnet_process", create_process)
    monkeypatch.setattr(managed, "TelegramOperationalSink", lambda **_: object())
    monkeypatch.setitem(
        sys.modules, "redis", SimpleNamespace(Redis=lambda **_: Resource("cache"))
    )
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect",
        SimpleNamespace(get_client=lambda **_: Resource("archive")),
    )

    def run(*, entries="true"):
        managed.child_main(
            tenant,
            target,
            {"V2_CONTROLLER_ENABLE_ENTRIES": entries},
            stop,
            SimpleNamespace(wait=lambda _: True),
            SimpleNamespace(send=send, close=lambda: events.append("channel-closed")),
        )

    return SimpleNamespace(
        run=run, state=state, events=events, sent=sent, pipeline=pipeline
    )


@pytest.mark.parametrize(
    "phase,entries,enabled",
    [("ACTIVE", "true", True), ("DRAINING", "true", False), ("ACTIVE", "false", False)],
)
def test_exact_guard_preflights_before_ready_and_owns_cycle_reads(
    child_case, phase, entries, enabled
):
    child_case.state["phase"] = phase
    child_case.run(entries=entries)
    assert child_case.sent == ["READY", "CYCLE"]
    assert child_case.pipeline.enable_entries is enabled
    assert [
        event
        for event in child_case.events
        if event.startswith(("guard-", "process-", "channel-READY", "pipeline-"))
    ] == [
        "guard-created",
        "guard-preflight",
        "process-created",
        "channel-READY",
        "guard-connect",
        "pipeline-cycle",
    ]
    assert child_case.events[-3:] == [
        "archive-closed",
        "cache-closed",
        "channel-closed",
    ]


def test_permission_preflight_failure_never_reports_ready_or_builds_process(child_case):
    child_case.state["preflight_error"] = psycopg.errors.InsufficientPrivilege(
        "qa-denied"
    )
    child_case.run()
    assert child_case.sent == ["FAILED:EXECUTION_GUARD_PERMISSION_DENIED"]
    assert "guard-preflight" in child_case.events
    assert "process-created" not in child_case.events
    assert "guard-connect" not in child_case.events
    assert "pipeline-cycle" not in child_case.events
    assert child_case.events[-3:] == [
        "archive-closed",
        "cache-closed",
        "channel-closed",
    ]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("qa-private-token"), psycopg.errors.UndefinedTable("qa-private-sql")],
)
def test_unclassified_startup_failure_remains_generic_and_redacted(child_case, error):
    child_case.state["preflight_error"] = error
    child_case.run()
    assert child_case.sent == ["FAILED"]
    assert "process-created" not in child_case.events


@pytest.mark.parametrize(
    "message,alive,ready,failure",
    [
        ("READY", True, True, None),
        ("READY", False, False, None),
        ("FAILED", True, False, None),
        (
            "FAILED:EXECUTION_GUARD_PERMISSION_DENIED",
            True,
            False,
            "EXECUTION_GUARD_PERMISSION_DENIED",
        ),
        ("FAILED:qa-private-token", True, False, None),
        ({"error_code": "qa-private-token"}, True, False, None),
    ],
)
def test_child_ready_accepts_only_allowlisted_startup_diagnostic(
    message, alive, ready, failure
):
    worker = managed.ChildWorker.__new__(managed.ChildWorker)
    worker.channel = SimpleNamespace(poll=lambda _: True, recv=lambda: message)
    worker.process = SimpleNamespace(is_alive=lambda: alive)
    assert worker.ready() is ready
    assert worker.startup_failure == failure


@pytest.mark.parametrize("timeout", [False, True])
def test_child_ready_closed_or_silent_channel_is_generic_failure(timeout):
    def closed():
        raise EOFError

    worker = managed.ChildWorker.__new__(managed.ChildWorker)
    worker.channel = SimpleNamespace(poll=lambda _: not timeout, recv=closed)
    worker.process = SimpleNamespace(is_alive=lambda: True)
    assert worker.ready() is False
    assert worker.startup_failure is None


def test_route_revocation_after_ready_prevents_a_cycle(child_case):
    child_case.state["worker_token"] = str(uuid4())
    child_case.run()
    assert child_case.sent == ["READY"]
    assert "guard-connect" in child_case.events
    assert "pipeline-cycle" not in child_case.events
