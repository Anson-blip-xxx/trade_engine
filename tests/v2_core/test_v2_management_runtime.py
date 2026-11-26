"""Opt-in management-only orchestration on disposable PG and fake venue ports.

These tests do not claim concurrent entry/settlement scheduling or progress
through a venue/account lock owned by another process. All order writes below
go to an explicit fake exchange; no deployment or running account is touched.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_protection import SCOPE

from services.v2_management_runtime import ManagementBinding, ManagementSupervisor
from tests.support.v2_admission import database as database_fixture
from v2_core.runtime import DataRuntime
from v2_core.state import BusinessState
from v2_core.venue_readiness import GuardedOpeningSubmit

database = database_fixture


def binding(**changes):
    return ManagementBinding(
        **{
            "scope": SCOPE,
            "tenant_id": str(uuid4()),
            "registry_id": str(uuid4()),
            "binding_version": 1,
            "epoch": 1,
            "worker_token": str(uuid4()),
            **changes,
        }
    )


def forbidden(*args, **kwargs):
    pytest.fail("management-only orchestration touched an excluded dependency")


def disabled_open_gate(database, runtime):
    runtime.execution.submit = GuardedOpeningSubmit(
        database,
        readiness=SimpleNamespace(scope=runtime.scope),
        reference=forbidden,
        plan=forbidden,
        submit=runtime.execution.submit,
        enabled=False,
        reduce_only_enabled=True,
    )


def test_binding_contract_is_immutable_and_covers_exact_account_and_worker():
    selected = binding()
    assert len(selected.digest) == 64
    assert selected.guard_args == {
        "tenant_id": selected.tenant_id,
        "environment": "SANDBOX",
        "registry_id": selected.registry_id,
        "binding_version": 1,
        "epoch": 1,
        "worker_token": selected.worker_token,
    }
    for change in (
        {"scope": replace(SCOPE, account_id="other-account")},
        {"tenant_id": str(uuid4())},
        {"registry_id": str(uuid4())},
        {"binding_version": 2},
        {"epoch": 2},
        {"worker_token": str(uuid4())},
    ):
        assert replace(selected, **change).digest != selected.digest
    with pytest.raises(FrozenInstanceError):
        selected.epoch = 2


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": asdict(SCOPE)},
        {"scope": replace(SCOPE, environment="LIVE")},
        {"tenant_id": "not-a-uuid"},
        {"registry_id": "not-a-uuid"},
        {"worker_token": "not-a-uuid"},
        {"binding_version": 0},
        {"binding_version": True},
        {"epoch": 0},
        {"epoch": True},
    ],
)
def test_binding_contract_rejects_noncanonical_or_non_sandbox_authority(changes):
    with pytest.raises(ValueError):
        binding(**changes)


class Guard:
    def __init__(self, selected):
        self._args = selected.guard_args
        self.calls = 0
        self.active = False
        self.fail_from = None

    @contextmanager
    def __call__(self, *, operation):
        assert operation == "MANAGE"
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise ValueError("EXECUTION_FENCED")
        self.active = True
        try:
            yield
        finally:
            self.active = False


@pytest.fixture
def case(database, monkeypatch):
    clock = [time.time_ns() // 1000000]
    calls, observed_coverage = [], []
    selected = binding()
    guard = Guard(selected)
    runtime = DataRuntime(
        database,
        scope=SCOPE,
        submit=forbidden,
        query=lambda _: None,
        risk_check=lambda _: False,
        clock_ms=lambda: clock[0],
    )
    disabled_open_gate(database, runtime)
    # A management pass is recovery only, not the full runtime's expiry,
    # releases, attention, projection or any pipeline/admission work.
    for name in ("tick", "expire_once", "project_once", "accept_open"):
        monkeypatch.setattr(runtime, name, forbidden)
    monkeypatch.setattr(runtime.account_risk, "sweep_releases", forbidden)
    monkeypatch.setattr(runtime.attention, "scan", forbidden)

    def recovery(limit):
        assert limit == 3
        assert not guard.active, "route row locks must end before external waits"
        calls.append("recovery")
        return {}

    monkeypatch.setattr(runtime, "recover_once", recovery)

    class Stage:
        scope = SCOPE

        def __init__(self, name):
            self.name = name
            self.result = {"status": "CLEAR"}

        def run_once(self):
            assert not guard.active
            calls.append(self.name)
            return self.result

    protection, exits = Stage("protection"), Stage("exits")
    protection.result["coverage"] = {"observation_id": "current-protection"}

    def after_protection(coverage):
        observed_coverage.append(coverage)
        return exits.run_once()

    exits.run_after_protection = after_protection
    supervisor = ManagementSupervisor(
        runtime,
        protection,
        exits,
        binding=selected,
        guard=guard,
        clock_ms=lambda: clock[0],
        limit=3,
    )
    return SimpleNamespace(
        supervisor=supervisor,
        runtime=runtime,
        protection=protection,
        exits=exits,
        binding=selected,
        guard=guard,
        clock=clock,
        calls=calls,
        coverage=observed_coverage,
    )


def test_only_recovery_protection_and_exits_run_with_separate_manage_checks(case):
    result = case.supervisor.run_once()
    assert result["status"] == "CLEAR", result
    assert set(result["phases"]) == {"recovery", "protection", "exits"}
    assert case.calls == ["recovery", "protection", "exits"]
    assert case.guard.calls >= 3
    assert not case.guard.active
    assert case.coverage == [case.protection.result["coverage"]]
    assert case.runtime.execution.submit.enabled is False
    assert result.get("execution_authorized") is False


@pytest.mark.parametrize(
    "defect",
    [
        "opening_enabled",
        "unguarded_submit",
        "projectors",
        "runtime_scope",
        "execution_scope",
        "readiness_scope",
        "protection_scope",
        "exits_scope",
        "guard_binding",
        "clock",
        "zero_limit",
        "large_limit",
        "boolean_limit",
    ],
)
def test_constructor_rejects_unsafe_or_unbounded_composition(case, defect):
    other = replace(SCOPE, account_id="other-account")
    clock, limit = lambda: case.clock[0], 3
    if defect == "opening_enabled":
        case.runtime.execution.submit.enabled = True
    elif defect == "unguarded_submit":
        case.runtime.execution.submit = forbidden
    elif defect == "projectors":
        case.runtime.projectors = (SimpleNamespace(scope=SCOPE),)
    elif defect == "runtime_scope":
        case.runtime.scope = other
    elif defect == "execution_scope":
        case.runtime.execution.scope = other
    elif defect == "readiness_scope":
        case.runtime.execution.submit.readiness.scope = other
    elif defect == "protection_scope":
        case.protection.scope = other
    elif defect == "exits_scope":
        case.exits.scope = other
    elif defect == "guard_binding":
        case.guard._args = {**case.guard._args, "worker_token": str(uuid4())}
    elif defect == "clock":
        clock = None
    else:
        limit = {"zero_limit": 0, "large_limit": 21, "boolean_limit": True}[defect]
    with pytest.raises(ValueError):
        ManagementSupervisor(
            case.runtime,
            case.protection,
            case.exits,
            binding=case.binding,
            guard=case.guard,
            clock_ms=clock,
            limit=limit,
        )
    assert case.calls == [] and case.guard.calls == 0


@pytest.mark.parametrize("phase", ["recovery", "protection", "exits"])
def test_phase_exception_is_redacted_and_does_not_skip_later_management(
    case, monkeypatch, phase
):
    def fail(*args, **kwargs):
        assert not case.guard.active
        case.calls.append(phase)
        raise TimeoutError("QA-SECRET private-request-url")

    if phase == "recovery":
        monkeypatch.setattr(case.runtime, "recover_once", fail)
    else:
        monkeypatch.setattr(getattr(case, phase), "run_once", fail)
    result = case.supervisor.run_once()
    assert result["status"] == "BLOCKED"
    assert case.calls == ["recovery", "protection", "exits"]
    assert "QA-SECRET" not in json.dumps(result)
    assert "private-request-url" not in json.dumps(result)


def test_failed_protection_never_reuses_previous_cycle_coverage(case):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    previous = case.coverage[-1]
    case.protection.result = {"status": "BLOCKED"}
    result = case.supervisor.run_once()
    assert result["status"] == "BLOCKED"
    assert case.calls[-3:] == ["recovery", "protection", "exits"]
    assert case.coverage[-1] is None
    assert previous is not None


def test_already_stopped_launches_no_phase_or_guard(case):
    assert case.supervisor.run_once(stop_requested=lambda: True)["status"] == "STOPPED"
    assert case.calls == [] and case.guard.calls == 0


def test_stop_after_recovery_persists_incomplete_pass_and_skips_later_stages(
    case, monkeypatch
):
    stopped = Event()
    original = case.runtime.recover_once

    def recovery(limit):
        result = original(limit)
        stopped.set()
        return result

    monkeypatch.setattr(case.runtime, "recover_once", recovery)
    result = case.supervisor.run_once(stop_requested=stopped.is_set)
    assert result["status"] == "STOPPED"
    assert case.calls == ["recovery"]
    assert_unavailable(case)


@pytest.mark.parametrize("revoke_at", [1, 2, 3])
def test_revoked_route_prevents_every_later_phase(case, revoke_at):
    case.guard.fail_from = revoke_at
    result = case.supervisor.run_once()
    assert result["status"] == "BLOCKED"
    assert case.calls == ["recovery", "protection", "exits"][: revoke_at - 1]
    assert result.get("execution_authorized") is False


def test_legacy_pipeline_owner_excludes_management_cycle(case, database):
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("v2-pipeline:" + SCOPE.key,),
        )
        conn.commit()
        assert case.supervisor.run_once()["status"] == "BUSY"
    assert case.calls == []


def test_duplicate_manager_is_excluded_then_next_owner_can_run(case, monkeypatch):
    entered, release = Event(), Event()
    original = case.runtime.recover_once

    def held_recovery(limit):
        entered.set()
        assert release.wait(5)
        return original(limit)

    monkeypatch.setattr(case.runtime, "recover_once", held_recovery)
    other = ManagementSupervisor(
        case.runtime,
        case.protection,
        case.exits,
        binding=case.binding,
        guard=Guard(case.binding),
        clock_ms=lambda: case.clock[0],
        limit=3,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(case.supervisor.run_once)
        try:
            assert entered.wait(5)
            assert other.run_once()["status"] == "BUSY"
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "CLEAR"
    monkeypatch.setattr(case.runtime, "recover_once", original)
    assert other.run_once()["status"] == "CLEAR"


def test_checkpoint_failure_prevents_any_stage(case, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ConnectionError("private database details")

    monkeypatch.setattr(BusinessState, "change", unavailable)
    with pytest.raises(ConnectionError):
        case.supervisor.run_once()
    assert case.calls == []


def heartbeat_payload(case, database):
    row = BusinessState(database).read(case.supervisor.key)
    return row, json.loads(row.payload_json)


def replace_heartbeat(case, database, payload, *, deleted=False):
    row, _ = heartbeat_payload(case, database)
    result = BusinessState(database).change(
        case.supervisor.key,
        expected_version=row.version,
        request_key=str(uuid4()),
        payload={} if deleted else payload,
        deleted=deleted,
        reason="TESTNET_TRADING_DAEMON_CYCLE",
    )
    assert result.code == "APPLIED"


def assert_unavailable(case):
    result = case.supervisor.health(max_age_ms=30000)
    assert result["status"] == "UNAVAILABLE", result
    assert result["reason"].startswith("MANAGEMENT_")
    assert result["execution_authorized"] is False
    return result


def test_health_requires_current_completed_pass_and_grants_no_entry_authority(
    case, database
):
    assert_unavailable(case)
    result = case.supervisor.run_once()
    health = case.supervisor.health(max_age_ms=30000)
    assert health["status"] == "CURRENT", health
    assert health["execution_authorized"] is False
    row, payload = heartbeat_payload(case, database)
    assert row.version == 2  # Start and completion only; no large phase journals.
    assert payload["schema"] == "management-supervision-v1"
    assert payload["binding_digest"] == case.binding.digest
    assert payload["entry_dispatch_enabled"] is False
    assert payload["execution_authorized"] is False
    assert payload == result
    assert case.binding.worker_token not in json.dumps(payload)
    assert "current-protection" not in json.dumps(payload)


@pytest.mark.parametrize("status", ["STARTED", "BLOCKED", "STOPPED"])
def test_incomplete_current_pass_never_falls_back_to_previous_clear(
    case, database, status
):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    _, payload = heartbeat_payload(case, database)
    payload["status"] = status
    replace_heartbeat(case, database, payload)
    assert_unavailable(case)


@pytest.mark.parametrize("offset", [30001, -1])
def test_health_rejects_stale_or_future_completed_pass(case, offset):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    case.clock[0] += offset
    assert_unavailable(case)


def test_slow_completed_cycle_cannot_refresh_old_management_observations(
    case, monkeypatch
):
    original = case.exits.run_once

    def slow_exit():
        case.clock[0] += 30001
        return original()

    monkeypatch.setattr(case.exits, "run_once", slow_exit)
    result = case.supervisor.run_once()
    assert result["status"] == "CLEAR"
    assert result["finished_at_ms"] == case.clock[0]
    assert_unavailable(case)


@pytest.mark.parametrize("value", [0, 999, 300001, True, "30000"])
def test_health_age_is_explicit_and_bounded(case, value):
    with pytest.raises(ValueError):
        case.supervisor.health(max_age_ms=value)


def test_health_rechecks_route_and_opening_disabled_flag(case):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    case.guard.fail_from = case.guard.calls + 1
    assert_unavailable(case)
    case.guard.fail_from = None
    case.runtime.execution.submit.enabled = True
    assert_unavailable(case)


@pytest.mark.parametrize(
    "defect",
    [
        "schema",
        "binding_digest",
        "scope",
        "missing_phase",
        "blocked_phase",
        "started_phase",
        "missing_finished",
        "reversed_times",
        "boolean_time",
        "entry_permission",
        "execution_permission",
        "deleted",
    ],
)
def test_health_rejects_malformed_current_snapshot(case, database, defect):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    _, payload = heartbeat_payload(case, database)
    if defect == "schema":
        payload["schema"] = "unknown"
    elif defect == "binding_digest":
        payload["binding_digest"] = "0" * 64
    elif defect == "scope":
        payload["account_scope"] = {**asdict(SCOPE), "account_id": "other-account"}
    elif defect == "missing_phase":
        del payload["phases"]["protection"]
    elif defect in {"blocked_phase", "started_phase"}:
        payload["phases"]["protection"]["status"] = (
            "BLOCKED" if defect == "blocked_phase" else "STARTED"
        )
    elif defect == "missing_finished":
        del payload["finished_at_ms"]
    elif defect == "reversed_times":
        payload["started_at_ms"] = payload["finished_at_ms"] + 1
    elif defect == "boolean_time":
        payload["finished_at_ms"] = True
    elif defect == "entry_permission":
        payload["entry_dispatch_enabled"] = True
    elif defect == "execution_permission":
        payload["execution_authorized"] = True
    replace_heartbeat(case, database, payload, deleted=defect == "deleted")
    assert_unavailable(case)


def test_health_rejects_head_that_disagrees_with_its_immutable_history(case, database):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    with database() as conn:
        # The application version guard already prevents this corruption.
        # Deliberately bypass it only in this disposable QA schema to prove
        # health independently verifies history. Restore within the same
        # transaction; any error rolls back the trigger change as well.
        conn.execute("ALTER TABLE v2_business_state DISABLE TRIGGER v2_state_version")
        conn.execute(
            "UPDATE v2_business_state SET payload=payload || %s::jsonb WHERE state_id=%s",
            (json.dumps({"cycle_id": str(uuid4())}), case.supervisor.key.identity),
        )
        conn.execute("ALTER TABLE v2_business_state ENABLE TRIGGER v2_state_version")
    assert_unavailable(case)


def test_new_worker_cannot_inherit_previous_binding_health(case):
    assert case.supervisor.run_once()["status"] == "CLEAR"
    next_binding = replace(
        case.binding, epoch=case.binding.epoch + 1, worker_token=str(uuid4())
    )
    restarted = ManagementSupervisor(
        case.runtime,
        case.protection,
        case.exits,
        binding=next_binding,
        guard=Guard(next_binding),
        clock_ms=lambda: case.clock[0],
        limit=3,
    )
    assert restarted.health(max_age_ms=30000)["status"] == "UNAVAILABLE"
    assert restarted.run_once()["status"] == "CLEAR"
    assert restarted.health(max_age_ms=30000)["status"] == "CURRENT"
    assert_unavailable(case)


def test_management_pass_installs_frozen_stop_and_dispatches_only_typed_exit(
    database, monkeypatch
):
    from test_v2_strategy_exit import REGISTRY, setup
    from test_v2_strategy_protection import service as protection_stage

    from v2_core.strategy_deployment import StrategyDeployments

    selected, plan, episode, opening, data, venue, exits = setup(
        database, mark="123", protected=False
    )
    StrategyDeployments(database, REGISTRY).publish(
        replace(selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id=str(uuid4()),
    )
    runtime = exits.runtime
    disabled_open_gate(database, runtime)
    for name in ("tick", "project_once", "accept_open"):
        monkeypatch.setattr(runtime, name, forbidden)
    protection = protection_stage(
        database, venue, tick=runtime._now(), allow_writes=True
    )
    selected_binding = binding()
    supervisor = ManagementSupervisor(
        runtime,
        protection,
        exits,
        binding=selected_binding,
        guard=Guard(selected_binding),
        clock_ms=runtime.clock_ms,
    )
    result = supervisor.run_once()
    assert result["phases"]["protection"]["status"] == "CLEAR", result
    assert len(venue.writes) == 1
    assert venue.writes[0]["triggerPrice"] == plan.stop_price
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == plan.exit_side
    trace = data.trace(episode)
    assert [row["order_id"] for row in trace["orders"] if row["leg"] == "OPEN"] == [
        opening
    ]
    closing = next(row for row in trace["orders"] if row["leg"] == "CLOSE")
    assert closing["request_evidence"]["source"] == "strategy-exit-v1"
    assert (
        closing["request_evidence"]["strategy_plan"]["plan_digest"] == plan.plan_digest
    )
    assert not runtime.execution.submit.enabled
