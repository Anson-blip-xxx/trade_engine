"""Controller composition with fake ports; no DB, venue or actual subprocess."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import v2_managed_testnet as managed
from services.v2_entry_worker import EntryPorts
from services.v2_execution_controller import ExecutionController
from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_runtime import MODE, StrategyRuntimeFactory
from v2_core.account_risk import AccountScope


def job(stop, source):
    return "COMPLETED"


def settings(**changes):
    return {
        "V2_CONTROLLER_WORKER_MODE": MODE,
        "V2_TYPED_ENTRY_RETRY_MS": "10",
        "V2_TYPED_ENTRY_POLL_MS": "10",
        "V2_TYPED_ENTRY_STOP_JOIN_MS": "10",
        **changes,
    }


def target():
    return {
        "target_registry": str(uuid4()),
        "binding_version": 2,
        "epoch": 7,
        "worker_token": str(uuid4()),
    }


def ports(tenant, selected, _values):
    binding = ManagementBinding(
        AccountScope("BINANCE", "runtime-qa", "SANDBOX", "FUTURES"),
        tenant,
        selected["target_registry"],
        selected["binding_version"],
        selected["epoch"],
        selected["worker_token"],
    )
    return EntryPorts(binding, job, (), ("s3", "tv_bridge"))


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "bounded pure-test condition did not arrive"


class Member:
    entries_enabled = False
    startup_failure = None

    def __init__(self, name, events):
        self.name, self.events = name, events
        self.ready_result = self.alive_result = self.healthy_result = (
            self.stop_result
        ) = True
        self.release_error = None
        self.batch_status = "COMPLETED"

    def invoke(self, name, value=None):
        self.events.append((self.name, name))
        if isinstance(value, Exception):
            raise value
        return value

    def ready(self):
        return self.invoke("ready", self.ready_result)

    def release(self):
        return self.invoke("release", self.release_error)

    def alive(self):
        return self.alive_result

    def healthy(self):
        return self.healthy_result

    def request_stop(self):
        self.invoke("request_stop")

    def stop(self):
        return self.invoke("stop", self.stop_result)

    def snapshot(self):
        return {"status": self.batch_status}


@pytest.fixture
def case():
    value = SimpleNamespace(
        events=[], tenant=str(uuid4()), selected=target(), runtimes=[]
    )
    value.management, value.settlement = (
        Member("management", value.events),
        Member("settlement", value.events),
    )
    value.entry_workers, value.construction = [], []

    def management(tenant, selected, config):
        value.construction.append(("management", tenant, selected, config))
        return value.management

    def settlement(tenant, selected, config):
        value.construction.append(("settlement", tenant, selected, config))
        return value.settlement

    def entry(entry_ports, config):
        value.construction.append(("entry", entry_ports, config))
        member = Member("entry" + str(len(value.entry_workers)), value.events)
        value.entry_workers.append(member)
        return member

    def build(
        *,
        config=None,
        ports_for=ports,
        entry_factory=entry,
        management_factory=management,
        settlement_factory=settlement,
    ):
        factory = StrategyRuntimeFactory(
            value.tenant,
            settings() if config is None else config,
            entry_ports_for=ports_for,
            entry_factory=entry_factory,
            management_factory=management_factory,
            settlement_factory=settlement_factory,
        )
        runtime = factory.spawn(value.selected)
        value.runtimes.append(runtime)
        return runtime

    value.build = build
    yield value
    for runtime in value.runtimes:
        value.management.stop_result = value.settlement.stop_result = True
        for worker in value.entry_workers:
            worker.stop_result = True
        runtime.request_stop()
        wait_for(runtime.stop)


def test_paused_typed_composition_never_constructs_entry_before_critical_release(case):
    runtime = case.build()
    assert [item[0] for item in case.construction] == ["management", "settlement"]
    assert runtime.entries_enabled is False
    assert runtime.ready() is True
    assert case.events == [("management", "ready"), ("settlement", "ready")]
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "RUNNING")
    assert case.events.index(("settlement", "release")) < case.events.index(
        ("entry0", "ready")
    )
    assert runtime.alive() and runtime.healthy()
    assert runtime.snapshot()["entry"]["execution_authorized"] is False
    assert case.construction[0][3]["V2_CONTROLLER_WORKER_MODE"] == "MANAGEMENT_ONLY"
    assert all(
        item[-1]["V2_CONTROLLER_ENABLE_ENTRIES"] == "false"
        for item in case.construction
    )
    assert len(runtime.members) == 3


def test_snapshot_owns_detached_target_and_input_settings(case):
    config = settings()
    runtime = case.build(config=config)
    original = dict(case.selected)
    config["V2_CONTROLLER_ENABLE_ENTRIES"] = "true"
    case.selected["epoch"] += 1
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: len(case.entry_workers) == 1)
    entry_ports = next(item[1] for item in case.construction if item[0] == "entry")
    assert entry_ports.binding.epoch == original["epoch"]
    assert runtime.entries_enabled is False


def fixed_enabled_ports(tenant, selected, values):
    from services.v2_strategy_entry_job import EntryJobSpec
    from services.v2_strategy_runtime_job import (
        SPEC,
        StrategyJobConfig,
        typed_runtime_job,
    )
    from v2_core.strategy_plan import PLAN_SCHEMA

    binding = ports(tenant, selected, values).binding
    spec = EntryJobSpec(
        binding,
        "causal-trend-v1",
        (SPEC,),
        (SPEC.entry_handler,),
        (SPEC.exit_handler,),
        (PLAN_SCHEMA,),
    )
    return EntryPorts(
        binding,
        typed_runtime_job,
        (StrategyJobConfig(spec, entries_enabled=True),),
        ("quant", "tv_bridge", "s3"),
    )


def test_explicit_enabled_runtime_keeps_other_lanes_entry_disabled(case, monkeypatch):
    from services import v2_strategy_runtime as runtime_module
    from services import v2_strategy_runtime_job as job_module

    def owner(entry_ports, values):
        assert values["V2_CONTROLLER_ENABLE_ENTRIES"] == "true"
        assert values["V2_ENTRY_WORK_MS"] == "90000"
        assert values["V2_ENTRY_PROGRESS_TIMEOUT_MS"] == "180000"
        assert entry_ports.job_args[0].entries_enabled is True
        member = Member("entry-enabled", case.events)
        case.entry_workers.append(member)
        return member

    monkeypatch.setattr(runtime_module, "EntryWorker", owner)
    monkeypatch.setattr(job_module, "typed_entry_ports_for", fixed_enabled_ports)
    runtime = case.build(
        config=settings(V2_CONTROLLER_ENABLE_ENTRIES="true"),
        ports_for=fixed_enabled_ports,
        entry_factory=owner,
    )
    assert runtime.entries_enabled is True
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "RUNNING")
    assert all(
        item[-1]["V2_CONTROLLER_ENABLE_ENTRIES"] == "false"
        for item in case.construction
    )
    assert runtime.snapshot()["entries_enabled"] is True
    assert runtime.snapshot()["entry"]["entry_dispatch_enabled"] is True
    assert runtime.snapshot()["execution_authorized"] is False


@pytest.mark.parametrize(
    "bad", ["target", "args", "config", "disabled", "binding", "sources"]
)
def test_enabled_auxiliary_checks_fixed_job_contract_before_owner(
    case, monkeypatch, bad
):
    from services import v2_strategy_runtime as runtime_module
    from services import v2_strategy_runtime_job as job_module

    def selected_ports(tenant, selected, values):
        result = fixed_enabled_ports(tenant, selected, values)
        config = result.job_args[0]
        if bad == "target":
            return replace(result, job_target=job)
        if bad == "args":
            return replace(result, job_args=(config, config))
        if bad == "config":
            return replace(result, job_args=({},))
        if bad == "disabled":
            return replace(result, job_args=(replace(config, entries_enabled=False),))
        if bad == "binding":
            config = replace(
                config,
                entry=replace(config.entry, binding=replace(result.binding, epoch=99)),
            )
            return replace(result, job_args=(config,))
        return replace(result, sources=("s3",))

    def owner(*_):
        pytest.fail("an unverified job must not reach the entry owner")

    monkeypatch.setattr(runtime_module, "EntryWorker", owner)
    monkeypatch.setattr(job_module, "typed_entry_ports_for", selected_ports)
    runtime = case.build(
        config=settings(
            V2_CONTROLLER_ENABLE_ENTRIES="true", V2_TYPED_ENTRY_MAX_RESTARTS="0"
        ),
        ports_for=selected_ports,
        entry_factory=owner,
    )
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "EXHAUSTED")
    assert runtime.snapshot()["entry"]["blocker"] == "ENTRY_FIXED_TYPED_JOB_REQUIRED"
    assert runtime.alive() and runtime.healthy()
    assert not any(event[1] == "stop" for event in case.events)


def test_enabled_custom_owner_rejected_even_with_fixed_ports(case):
    from services.v2_strategy_runtime_job import typed_entry_ports_for

    with pytest.raises(ValueError, match="TYPED_RUNTIME_FIXED_FACTORY_REQUIRED"):
        case.build(
            config=settings(V2_CONTROLLER_ENABLE_ENTRIES="true"),
            ports_for=typed_entry_ports_for,
        )
    assert case.construction == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"V2_CONTROLLER_WORKER_MODE": "LEGACY"}, "EXPLICIT_TYPED_RUNTIME_REQUIRED"),
        ({"V2_ENVIRONMENT": "LIVE"}, "EXPLICIT_TYPED_RUNTIME_REQUIRED"),
        (
            {"V2_CONTROLLER_ENABLE_ENTRIES": "true"},
            "TYPED_RUNTIME_FIXED_FACTORY_REQUIRED",
        ),
        ({"V2_CONTROLLER_ENABLE_ENTRIES": True}, "TYPED_RUNTIME_ENTRY_FLAG_INVALID"),
        ({"V2_CONTROLLER_ENABLE_ENTRIES": "yes"}, "TYPED_RUNTIME_ENTRY_FLAG_INVALID"),
        ({"V2_TYPED_ENTRY_MAX_RESTARTS": "6"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_TYPED_ENTRY_RETRY_MS": "0"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_TYPED_ENTRY_POLL_MS": "251"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_TYPED_ENTRY_STOP_JOIN_MS": "1001"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_ENTRY_WORK_MS": "59999"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_ENTRY_WORK_MS": "300001"}, "TYPED_RUNTIME_CONFIG_INVALID"),
        ({"V2_ENTRY_PROGRESS_TIMEOUT_MS": "0"}, "TYPED_RUNTIME_CONFIG_INVALID"),
    ],
)
def test_unsupported_modes_fail_before_any_worker_construction(case, changes, code):
    with pytest.raises(ValueError, match=code):
        case.build(config=settings(**changes))
    assert case.construction == []


def test_partial_critical_construction_preserves_owned_management(case):
    def fail(*_):
        raise RuntimeError("private-key-is-not-a-diagnostic")

    case.management.stop_result = False
    runtime = case.build(settlement_factory=fail)
    assert runtime.members == (case.management,)
    assert runtime.startup_failure == "WORKER_START_FAILED"
    assert not runtime.ready() and not runtime.stop()
    assert runtime.stop_confirmations == (False,)
    assert case.entry_workers == []
    assert "private" not in str(runtime.snapshot())


def test_critical_readiness_failure_stops_all_without_entry_construction(case):
    case.settlement.ready_result = False
    runtime = case.build()
    assert not runtime.ready()
    assert runtime.startup_failure == "WORKER_START_FAILED"
    assert case.entry_workers == []
    assert ("management", "request_stop") in case.events
    with pytest.raises(RuntimeError, match="WORKER_GROUP_CLOSING"):
        runtime.release()


def test_critical_release_failure_prevents_entry_work(case):
    case.settlement.release_error = RuntimeError("private error")
    runtime = case.build()
    assert runtime.ready()
    with pytest.raises(RuntimeError, match="WORKER_GROUP_RELEASE_FAILED"):
        runtime.release()
    assert case.entry_workers == []
    assert not runtime.alive()


@pytest.mark.parametrize("failure", ["ports", "ready", "release", "alive", "healthy"])
def test_entry_failure_has_bounded_recovery_without_stopping_management(case, failure):
    def fail_ports(*args):
        if failure == "ports":
            raise ValueError("private account secret")
        return ports(*args)

    def entry(entry_ports, config):
        member = Member("entry" + str(len(case.entry_workers)), case.events)
        case.entry_workers.append(member)
        if failure == "ready":
            member.ready_result = False
        elif failure == "release":
            member.release_error = RuntimeError("private startup secret")
        elif failure == "alive":
            member.alive_result = False
        elif failure == "healthy":
            member.healthy_result = False
        return member

    runtime = case.build(
        config=settings(V2_TYPED_ENTRY_MAX_RESTARTS="1"),
        ports_for=fail_ports,
        entry_factory=entry,
    )
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "EXHAUSTED")
    report = runtime.snapshot()["entry"]
    assert report["attempts"] == 2
    assert report["blocker"].startswith("ENTRY_")
    assert "private" not in str(report)
    assert runtime.alive() and runtime.healthy()
    assert not any(
        action == "request_stop" and name in {"management", "settlement"}
        for name, action in case.events
    )
    assert report["stop_confirmations"] == ([True, True] if failure != "ports" else [])


def test_confirmed_failed_entry_can_restart_and_keep_old_handle(case):
    def entry(entry_ports, config):
        member = Member("entry" + str(len(case.entry_workers)), case.events)
        member.ready_result = bool(case.entry_workers)
        case.entry_workers.append(member)
        return member

    runtime = case.build(entry_factory=entry)
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "RUNNING")
    lane = runtime.members[-1]
    assert lane.owned_workers == tuple(case.entry_workers)
    assert lane.snapshot()["stop_confirmations"] == [True, None]
    assert len(case.entry_workers) == 2
    assert case.events.index(("entry0", "stop")) < case.events.index(
        ("entry1", "ready")
    )


def test_unknown_entry_stop_retains_handle_and_forbids_replacement(case):
    def entry(entry_ports, config):
        member = Member("entry0", case.events)
        member.ready_result = False
        member.stop_result = False
        case.entry_workers.append(member)
        return member

    runtime = case.build(entry_factory=entry)
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "STOP_UNCONFIRMED")
    assert runtime.alive() and runtime.healthy()
    assert len(case.entry_workers) == 1
    assert not runtime.stop()
    assert runtime.stop_confirmations == (True, True, False)
    assert runtime.members[-1].owned_workers == (case.entry_workers[0],)
    case.entry_workers[0].stop_result = True
    wait_for(runtime.stop)
    assert len(case.entry_workers) == 1


def test_stuck_port_builder_does_not_starve_critical_release_or_claim_stopped(case):
    entered, unblock = threading.Event(), threading.Event()

    def slow_ports(*args):
        entered.set()
        assert unblock.wait(3)
        return ports(*args)

    runtime = case.build(ports_for=slow_ports)
    try:
        assert runtime.ready()
        runtime.release()
        assert entered.wait(1)
        assert runtime.alive() and runtime.healthy()
        assert ("management", "release") in case.events and (
            "settlement",
            "release",
        ) in case.events
        assert not runtime.stop()
        assert runtime.members[-1].snapshot()["owner_alive"] is True
        assert case.entry_workers == []
    finally:
        unblock.set()
    wait_for(runtime.stop)
    assert case.entry_workers == []  # stop wins before construction after callback


def test_stuck_entry_ready_does_not_starve_core_and_stop_keeps_returned_handle(case):
    entered, unblock = threading.Event(), threading.Event()

    def entry(entry_ports, config):
        member = Member("entry0", case.events)
        case.entry_workers.append(member)

        def slow_ready():
            entered.set()
            assert unblock.wait(3)
            return True

        member.ready = slow_ready
        return member

    runtime = case.build(entry_factory=entry)
    try:
        assert runtime.ready()
        runtime.release()
        assert entered.wait(1)
        assert runtime.alive() and runtime.healthy()
        assert not runtime.stop()
        assert runtime.members[-1].owned_workers == tuple(case.entry_workers)
    finally:
        unblock.set()
    wait_for(runtime.stop)
    assert ("entry0", "release") not in case.events


def test_batch_failure_is_visible_but_live_entry_owner_is_not_restarted(case):
    runtime = case.build()
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: len(case.entry_workers) == 1)
    case.entry_workers[0].batch_status = "TIMEOUT"
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "DEGRADED")
    assert runtime.snapshot()["entry"]["blocker"] == "ENTRY_BATCH_UNAVAILABLE"
    assert runtime.alive() and runtime.healthy()
    assert len(case.entry_workers) == 1
    case.entry_workers[0].batch_status = "COMPLETED"
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "RUNNING")


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", str(uuid4())),
        ("registry_id", str(uuid4())),
        ("binding_version", 99),
        ("epoch", 99),
        ("worker_token", str(uuid4())),
    ],
)
def test_auxiliary_binding_mismatch_never_constructs_entry(case, field, value):
    def wrong(*args):
        result = ports(*args)
        return replace(result, binding=replace(result.binding, **{field: value}))

    runtime = case.build(
        config=settings(V2_TYPED_ENTRY_MAX_RESTARTS="0"), ports_for=wrong
    )
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "EXHAUSTED")
    assert runtime.snapshot()["entry"]["blocker"] == "ENTRY_BINDING_MISMATCH"
    assert case.entry_workers == []
    assert runtime.healthy()


def test_stop_before_release_prevents_all_entry_activity(case):
    runtime = case.build()
    runtime.request_stop()
    with pytest.raises(RuntimeError, match="WORKER_GROUP_CLOSING"):
        runtime.release()
    wait_for(runtime.stop)
    assert case.entry_workers == []


def test_actual_testnet_ports_selects_typed_factory_not_legacy(case, monkeypatch):
    calls = []
    sentinel = object()

    class Factory:
        def __init__(self, tenant, values, *, entry_ports_for):
            calls.append((tenant, values, entry_ports_for))

        def spawn(self, selected):
            calls.append(selected)
            return sentinel

    monkeypatch.setattr("services.v2_strategy_runtime.StrategyRuntimeFactory", Factory)
    monkeypatch.setattr(
        managed,
        "ChildWorker",
        lambda *_: pytest.fail("typed mode cannot fall back to legacy child"),
    )
    actual = managed.TestnetPorts(case.tenant, settings(), typed_entry_ports_for=ports)
    assert actual.spawn(case.selected) is sentinel
    assert calls == [(case.tenant, settings(), ports), case.selected]


def test_actual_testnet_ports_uses_fixed_runtime_job_module_by_default(
    case, monkeypatch
):
    import sys

    calls = []
    sentinel = object()

    class Factory:
        def __init__(self, tenant, values, *, entry_ports_for):
            assert entry_ports_for is ports
            calls.append(tenant)

        def spawn(self, selected):
            return sentinel

    monkeypatch.setitem(
        sys.modules,
        "services.v2_strategy_runtime_job",
        SimpleNamespace(typed_entry_ports_for=ports),
    )
    monkeypatch.setattr("services.v2_strategy_runtime.StrategyRuntimeFactory", Factory)
    assert (
        managed.TestnetPorts(case.tenant, settings()).spawn(case.selected) is sentinel
    )
    assert calls == [case.tenant]


def test_actual_typed_mode_refuses_direct_child_before_spawning(case):
    assert managed._worker_mode(settings()) == MODE
    with pytest.raises(ValueError, match="DIRECT_WORKER_GROUP_OWNER_REQUIRED"):
        managed.ChildWorker(case.tenant, case.selected, settings())


def test_actual_typed_mode_true_rejects_custom_ports_before_workers(case, monkeypatch):
    monkeypatch.setattr(
        managed,
        "ChildWorker",
        lambda *_: pytest.fail("cannot start a child for unsupported dispatch"),
    )
    actual = managed.TestnetPorts(
        case.tenant,
        settings(V2_CONTROLLER_ENABLE_ENTRIES="true"),
        typed_entry_ports_for=ports,
    )
    with pytest.raises(ValueError, match="TYPED_RUNTIME_FIXED_FACTORY_REQUIRED"):
        actual.spawn(case.selected)


def test_controller_cannot_switch_account_after_unconfirmed_entry_stop(case):
    def entry(entry_ports, config):
        member = Member("entry0", case.events)
        member.stop_result = False
        case.entry_workers.append(member)
        return member

    runtime = case.build(entry_factory=entry)
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "RUNNING")
    controller = ExecutionController(
        lambda: pytest.fail("pure controller test must not access DB"),
        tenant_id=case.tenant,
        preflight=lambda _: [],
        source_clear=lambda _: [],
        spawn=lambda _: pytest.fail("cannot replace an unconfirmed owned worker"),
    )
    controller.worker = runtime
    current = {**case.selected, "phase": "ACTIVE", "blockers": []}
    desired = {**target(), "epoch": case.selected["epoch"] + 1}
    records = []
    controller.actual = lambda: current
    controller.desired = lambda: desired
    controller.record = lambda target, phase, **kwargs: records.append((phase, kwargs))
    controller.step()
    assert controller.worker is runtime
    assert records[-1][0] == "DRAINING"
    assert records[-1][1]["blockers"] == ["SOURCE_PROCESS_NOT_STOPPED"]
    assert runtime.members[-1].owned_workers == tuple(case.entry_workers)
    case.entry_workers[0].stop_result = True


@pytest.mark.parametrize(
    "confirmation", [False, None, 1, RuntimeError("private stop error")]
)
def test_only_exact_true_confirms_entry_stop_and_no_new_handle_until_then(
    case, confirmation
):
    def entry(entry_ports, config):
        member = Member("entry0", case.events)
        member.ready_result = False
        member.stop_result = confirmation
        case.entry_workers.append(member)
        return member

    runtime = case.build(entry_factory=entry)
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "STOP_UNCONFIRMED")
    assert len(case.entry_workers) == 1
    assert runtime.snapshot()["entry"]["stop_confirmations"] == (
        [False] if confirmation is False else [None]
    )
    assert not runtime.stop()
    case.entry_workers[0].stop_result = True
    wait_for(runtime.stop)
    assert len(case.entry_workers) == 1


def test_invalid_returned_entry_handle_is_retained_until_its_stop_can_be_confirmed(
    case,
):
    incomplete = SimpleNamespace(entries_enabled=False)
    runtime = case.build(entry_factory=lambda *_: incomplete)
    assert runtime.ready()
    runtime.release()
    try:
        wait_for(lambda: runtime.snapshot()["entry"]["status"] == "STOP_UNCONFIRMED")
        assert runtime.members[-1].owned_workers == (incomplete,)
        assert not runtime.stop()
        assert runtime.alive() is False  # explicit group stop, not entry failure
    finally:
        incomplete.stop = lambda: True
    wait_for(runtime.stop)


def test_reused_handle_does_not_reuse_its_previous_stop_confirmation(case):
    member = Member("entry0", case.events)
    member.ready_result = False
    case.entry_workers.append(member)
    calls = []

    def recycled(*_):
        calls.append(1)
        if len(calls) == 2:
            member.stop_result = False  # object reused after prior stop confirmation
        return member

    runtime = case.build(entry_factory=recycled)
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "STOP_UNCONFIRMED")
    assert len(calls) == 2
    assert runtime.members[-1].owned_workers == (member,)
    assert runtime.snapshot()["entry"]["stop_confirmations"] == [False]
    assert not runtime.stop()
    member.stop_result = True
    wait_for(runtime.stop)


def test_fixed_transition_diagnostics_do_not_leak_callback_error(case, capsys):
    def broken(*_):
        raise RuntimeError("secret=private_token")

    runtime = case.build(
        config=settings(V2_TYPED_ENTRY_MAX_RESTARTS="0"), ports_for=broken
    )
    assert runtime.ready()
    runtime.release()
    wait_for(lambda: runtime.snapshot()["entry"]["status"] == "EXHAUSTED")
    output = capsys.readouterr().out
    assert "ENTRY_OWNER_START_FAILED" in output
    assert "private_token" not in output
