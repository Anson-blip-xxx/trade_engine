"""Actual isolated financial work behind the new controller-owned job adapter."""

import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_settlement_execution_fence import (
    ScopedRead,
    assert_financially_pending,
    database,
    fenced,
    route_update,
)
from test_v2_strategy_settlement import SCOPE, closed_plan

from services import v2_managed_testnet as managed
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_management_runtime import ManagementBinding
from services.v2_settlement_worker import (
    _bootstrap,
    _settlement_job,
    run_settlement_work,
)
from v2_core.session_lock import session_advisory_lock

__all__ = ["database", "fenced"]


def binding_for(fenced):
    args = dict(fenced.args)
    args.pop("environment")
    return ManagementBinding(scope=SCOPE, **args)


def run(fenced, case, *, kind="FINANCIAL", **overrides):
    args = {
        "binding": binding_for(fenced),
        "guard": fenced.guard,
        "kind": kind,
        "episode": case.episode,
        "stop": threading.Event(),
        "clock_ms": case.stage.clock,
    }
    request = overrides.pop("request", case.venue)
    args.update(overrides)
    return run_settlement_work(fenced.admin, request, **args)


def test_real_job_settles_once_then_independent_outcome_records(fenced):
    case = closed_plan(fenced.admin)
    assert run(fenced, case, kind="OUTCOME") == "BLOCKED"
    assert_financially_pending(case)
    assert run(fenced, case) == "COMPLETED"
    before = case.data.trace(case.episode)
    assert len(before["settlements"]) == 1
    assert before["account_risk"]["status"] == "RELEASED"
    assert run(fenced, case) == "COMPLETED"
    assert run(fenced, case, kind="OUTCOME") == "COMPLETED"
    assert run(fenced, case, kind="OUTCOME") == "COMPLETED"
    after = case.data.trace(case.episode)
    for key in ("settlements", "cash", "income_allocations", "account_risk"):
        assert after[key] == before[key]


def test_bootstrap_selects_without_venue_and_uses_independent_cursors(
    fenced, monkeypatch
):
    case = closed_plan(fenced.admin)
    binding = binding_for(fenced)
    target = {
        "target_registry": binding.registry_id,
        "binding_version": binding.binding_version,
        "epoch": binding.epoch,
        "worker_token": binding.worker_token,
    }
    monkeypatch.setattr(managed, "database", lambda: fenced.admin)
    monkeypatch.setattr(managed, "scope_for", lambda *_: SCOPE)
    monkeypatch.setattr(
        managed, "readonly_request", lambda *_: pytest.fail("selector called venue")
    )
    ports = _bootstrap(binding.tenant_id, target, {})
    assert ports.binding == binding
    assert ports.job_args == (binding,)
    assert ports.select("FINANCIAL", "") == case.episode
    assert ports.select("FINANCIAL", case.episode) == case.episode  # wraps
    assert ports.select("OUTCOME", "") is None
    assert run(fenced, case) == "COMPLETED"
    assert ports.select("FINANCIAL", "") is None
    assert ports.select("OUTCOME", "") == case.episode
    assert run(fenced, case, kind="OUTCOME") == "COMPLETED"
    assert ports.select("OUTCOME", "") is None


def test_job_exclusion_never_reports_locked_obligation_completed(fenced):
    case = closed_plan(fenced.admin)
    with session_advisory_lock(
        fenced.admin, "directional-settlement-stage:" + SCOPE.key
    ) as acquired:
        assert acquired
        assert run(fenced, case) == "BUSY"
    assert_financially_pending(case)
    assert run(fenced, case) == "COMPLETED"


def test_stop_before_work_and_unknown_episode_do_not_release_money(fenced):
    case = closed_plan(fenced.admin)
    stop = threading.Event()
    stop.set()
    assert run(fenced, case, stop=stop) == "BLOCKED"
    assert run(fenced, case, episode=str(uuid4())) == "BLOCKED"
    assert_financially_pending(case)


@pytest.mark.parametrize("change", ["binding", "request", "kind", "episode"])
def test_job_rejects_wrong_identity_before_venue_work(fenced, change):
    case = closed_plan(fenced.admin)
    overrides = {
        "binding": {"binding": replace(binding_for(fenced), epoch=2)},
        "request": {"request": lambda *_: pytest.fail("unbound request")},
        "kind": {"kind": "OPEN"},
        "episode": {"episode": "not-an-episode"},
    }[change]
    with pytest.raises(ValueError):
        run(fenced, case, **overrides)
    assert_financially_pending(case)


def test_guard_is_rechecked_after_network_not_held_during_it(fenced, monkeypatch):
    case = closed_plan(fenced.admin)
    revoked, calls = [], []
    original = DirectionalSettlementStage._state

    def request(method, path, params):
        assert method == "GET"
        route_update(fenced)  # short guard must already have been released
        calls.append(path)
        return case.venue(method, path, params)

    def state(self, namespace, key):
        result = original(self, namespace, key)
        if namespace != "opening-readiness-v1" and not revoked:
            assert "/fapi/v1/income" in calls
            route_update(fenced, "SET epoch=epoch+1")
            revoked.append(True)
        return result

    monkeypatch.setattr(DirectionalSettlementStage, "_state", state)
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        run(fenced, case, request=ScopedRead(request))
    assert revoked
    assert_financially_pending(case)


def test_pure_outcome_job_does_not_decrypt_exchange_credentials(fenced, monkeypatch):
    case = closed_plan(fenced.admin)
    assert run(fenced, case) == "COMPLETED"
    monkeypatch.setattr(managed, "database", lambda: fenced.admin)
    monkeypatch.setattr(
        managed, "readonly_request", lambda *_: pytest.fail("unnecessary credentials")
    )
    assert (
        _settlement_job(threading.Event(), binding_for(fenced), "OUTCOME", case.episode)
        == "COMPLETED"
    )


def test_stop_after_last_network_read_prevents_starting_financial_commit(
    fenced, monkeypatch
):
    case = closed_plan(fenced.admin)
    stop = threading.Event()
    original = DirectionalSettlementStage._state

    def state(self, namespace, key):
        result = original(self, namespace, key)
        if namespace != "opening-readiness-v1":
            stop.set()
        return result

    monkeypatch.setattr(DirectionalSettlementStage, "_state", state)
    with pytest.raises(ValueError, match="SETTLEMENT_STOPPED_BEFORE_COMMIT"):
        run(fenced, case, stop=stop)
    assert_financially_pending(case)
