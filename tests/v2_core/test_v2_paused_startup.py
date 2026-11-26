"""Explicit paused startup selection only; no DB, account requests or services."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.v2_execution_controller import ExecutionController
from services.v2_execution_supervisor import recover_paused_typed


def values(**changes):
    return {
        "V2_TYPED_RECOVER_PAUSED": "true",
        "V2_CONTROLLER_WORKER_MODE": "TYPED_STRATEGY",
        "V2_CONTROLLER_ENABLE_ENTRIES": "false",
        "V2_ENVIRONMENT": "SANDBOX",
        **changes,
    }


def case():
    actual = {
        "target_registry": str(uuid4()),
        "binding_version": 2,
        "epoch": 7,
        "worker_token": None,
        "phase": "STOPPED",
    }
    state = SimpleNamespace(actual=actual, desired=dict(actual), calls=[])

    def start(target, *, recovering):
        assert target is actual and recovering is True
        state.calls.append((dict(target), recovering))
        return True

    state.controller = SimpleNamespace(
        worker=None,
        actual=lambda: state.actual,
        desired=lambda: state.desired,
        start=start,
    )
    return state


def test_explicit_same_binding_starts_only_recovery_without_new_epoch():
    value = case()
    before = dict(value.actual)
    assert recover_paused_typed(value.controller, values()) == "RECOVERING"
    assert value.calls == [(before, True)]
    assert value.actual == before and value.desired == before


def test_older_same_binding_request_also_keeps_current_source_epoch():
    value = case()
    value.desired["epoch"] -= 1
    assert recover_paused_typed(value.controller, values()) == "RECOVERING"
    assert value.calls[0][0]["epoch"] == 7


def test_default_does_not_read_or_start():
    def forbidden():
        pytest.fail("default disabled recovery must not read DB")

    controller = SimpleNamespace(actual=forbidden, desired=forbidden)
    assert recover_paused_typed(controller, {}) == "DISABLED"


@pytest.mark.parametrize(
    "change",
    [
        {"V2_TYPED_RECOVER_PAUSED": True},
        {"V2_TYPED_RECOVER_PAUSED": "yes"},
        {"V2_CONTROLLER_ENABLE_ENTRIES": "true"},
        {"V2_CONTROLLER_ENABLE_ENTRIES": False},
        {"V2_ENVIRONMENT": "LIVE"},
        {"V2_CONTROLLER_WORKER_MODE": "LEGACY"},
        {"V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY"},
    ],
)
def test_invalid_or_enabled_configuration_refused_before_controller(change):
    with pytest.raises(ValueError, match="TYPED_PAUSED_RECOVERY_CONFIG_INVALID"):
        recover_paused_typed(object(), values(**change))


@pytest.mark.parametrize("phase", ["STARTING", "ACTIVE", "DRAINING", "BLOCKED"])
def test_does_not_recover_existing_route_phase(phase):
    value = case()
    value.actual["phase"] = phase
    assert recover_paused_typed(value.controller, values()) == "NOT_STOPPED"
    assert value.calls == []


def test_existing_unconfirmed_handle_is_retained_without_db_reads():
    value = case()
    owned = object()
    value.controller.worker = owned
    value.controller.actual = lambda: pytest.fail(
        "must not read before ownership decision"
    )
    assert recover_paused_typed(value.controller, values()) == "EXISTING_OWNER_RETAINED"
    assert value.controller.worker is owned and value.calls == []


@pytest.mark.parametrize(
    "key,new", [("target_registry", str(uuid4())), ("binding_version", 3), ("epoch", 8)]
)
def test_new_requests_cannot_trigger_paused_source_recovery(key, new):
    value = case()
    value.desired[key] = new
    code = "NEW_REQUEST_PENDING" if key == "epoch" else "DIFFERENT_BINDING_PENDING"
    with pytest.raises(ValueError, match="^TYPED_PAUSED_RECOVERY_" + code + "$"):
        recover_paused_typed(value.controller, values())
    assert value.calls == []


@pytest.mark.parametrize("key", ["target_registry", "binding_version", "epoch"])
def test_pending_startup_aborts_main_before_step_and_keeps_shutdown_boundary(
    monkeypatch, key
):
    import services.v2_execution_supervisor as module

    value = case()
    value.desired[key] = str(uuid4()) if key == "target_registry" else 8
    events = []
    value.controller.step = lambda: pytest.fail("paused startup must not activate")
    monkeypatch.setattr(
        module, "ExecutionController", lambda *_args, **_kwargs: value.controller
    )
    monkeypatch.setattr(
        module,
        "TestnetPorts",
        lambda *_args: SimpleNamespace(
            preflight=None, source_clear=None, spawn=None, resume_preflight=None
        ),
    )
    monkeypatch.setattr(module, "build_managed_health", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kw: SimpleNamespace(stdout="inactive"),
    )
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(environ={**values(), "V2_CONTROLLER_TENANT": str(uuid4())}),
    )

    @contextmanager
    def lease():
        events.append("lease_enter")
        try:
            yield SimpleNamespace(
                execute=lambda *_args: SimpleNamespace(fetchone=lambda: (True,)),
                commit=lambda: events.append("lease_commit"),
            )
        finally:
            events.append("lease_exit")

    monkeypatch.setattr(module, "database", lambda: lease)

    def shutdown(controller):
        assert controller is value.controller and events[-1] == "lease_commit"
        events.append("shutdown")

    monkeypatch.setattr(module, "shutdown_controller", shutdown)
    with pytest.raises(ValueError, match="^TYPED_PAUSED_RECOVERY_.*_PENDING$"):
        module.main()
    assert events == ["lease_enter", "lease_commit", "shutdown", "lease_exit"]
    assert value.calls == []


def test_missing_request_has_no_implicit_activation():
    value = case()
    value.desired = None
    assert recover_paused_typed(value.controller, values()) == "NO_MATCHING_REQUEST"
    assert value.calls == []


@pytest.mark.parametrize(
    "key,new",
    [("binding_version", True), ("epoch", 0), ("epoch", "7"), ("environment", "LIVE")],
)
def test_invalid_route_evidence_cannot_start(key, new):
    value = case()
    value.actual[key] = new
    with pytest.raises(ValueError, match="TYPED_PAUSED_RECOVERY_ROUTE_UNAVAILABLE"):
        recover_paused_typed(value.controller, values())
    assert value.calls == []


def test_failed_start_retains_original_owned_handle():
    value = case()
    handle = object()

    def fail(_target, *, recovering):
        value.controller.worker = handle
        return False

    value.controller.start = fail
    assert recover_paused_typed(value.controller, values()) == "RECOVERY_NOT_STARTED"
    assert value.controller.worker is handle


def test_real_controller_start_records_draining_not_active():
    value = case()
    records = []
    owned = SimpleNamespace(
        entries_enabled=False,
        ready=lambda: True,
        release=lambda: records.append("released"),
        stop=lambda: True,
    )
    controller = ExecutionController(
        lambda: None,
        tenant_id=str(uuid4()),
        preflight=lambda _: pytest.fail("paused recovery must not activate"),
        source_clear=lambda _: pytest.fail("must not switch account"),
        spawn=lambda _: owned,
    )
    controller.actual = lambda: value.actual
    controller.desired = lambda: value.desired
    controller.record = lambda target, phase, **kwargs: records.append(
        (dict(target), phase, kwargs)
    )
    assert recover_paused_typed(controller, values()) == "RECOVERING"
    assert [item[1] for item in records if isinstance(item, tuple)] == [
        "STARTING",
        "DRAINING",
    ]
    assert all(item[0]["epoch"] == 7 for item in records if isinstance(item, tuple))
    assert records[-1] == "released"
    assert controller.worker is owned and controller.worker.entries_enabled is False
