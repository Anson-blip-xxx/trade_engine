"""Cold housekeeping is visible but never attributed as an entry safety gate."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from services.v2_trading_health import TradingHealth, entry_blocking_stages
from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState, StateKey

database = database_fixture


def maintenance():
    return {
        "s6:s3": {
            "status": "UNAVAILABLE",
            "failure_type": "TimeoutError",
            "component_failures": {"cold_expiry": "TimeoutError"},
        }
    }


@pytest.mark.parametrize("stage", ["settlement", "protection", "exits", "market"])
def test_pure_maintenance_does_not_hide_or_join_a_real_blocker(stage):
    phases = {
        "signal_maintenance": maintenance(),
        stage: {"status": "BLOCKED", "error_code": "QA_TRUE_BLOCKER"},
    }
    original = deepcopy(phases)
    assert entry_blocking_stages(phases) == [stage]
    assert phases == original


def test_pure_only_exact_maintenance_name_is_excluded():
    phases = {
        "signal_maintenance": maintenance(),
        "signal_maintenance_unknown": {"status": "BLOCKED"},
        "unknown_safety_stage": {"status": "BLOCKED"},
    }
    assert entry_blocking_stages(phases) == [
        "signal_maintenance_unknown",
        "unknown_safety_stage",
    ]


def test_pure_verified_settlement_projection_remains_independent():
    phases = {
        "signal_maintenance": maintenance(),
        "settlement": {"status": "BLOCKED"},
        "protection": {"status": "BLOCKED"},
    }
    assert entry_blocking_stages(phases, settlement_clear=True) == ["protection"]
    assert entry_blocking_stages(phases, settlement_clear=False) == [
        "settlement",
        "protection",
    ]
    assert entry_blocking_stages({"signal_maintenance": maintenance()}) == []


@pytest.mark.parametrize("stage", ["settlement", "protection", "exits"])
def test_health_keeps_independent_maintenance_and_true_blocker(database, stage):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope),
        namespace="trading-pipeline-cycle-v1",
        key="mixed-maintenance-blocker",
    )
    payload = {
        "cycle_id": "mixed-maintenance-blocker",
        "started_at_ms": now[0],
        "finished_at_ms": now[0],
        "status": "ENTRY_BLOCKED",
        "phases": {
            "signal_maintenance": maintenance(),
            stage: {"status": "BLOCKED", "error_code": "QA_TRUE_BLOCKER"},
        },
    }
    store.change(key, expected_version=0, request_key="1", payload=payload, reason="QA")
    health()
    now[0] += 121000
    codes = health()
    report = json.loads(store.read(health.key).payload_json)
    assert "SIGNAL_MAINTENANCE_DEGRADED" in codes
    assert "PIPELINE_ENTRY_BLOCKED" in codes
    assert report["findings"]["SIGNAL_MAINTENANCE_DEGRADED"]["consumers"] == ["s6:s3"]
    assert report["findings"]["PIPELINE_ENTRY_BLOCKED"]["stages"] == [stage]
    if stage in {"protection", "exits"}:
        assert "POSITION_SAFETY_BLOCKED" in codes
        assert report["findings"]["POSITION_SAFETY_BLOCKED"]["stages"] == [stage]
    else:
        assert "POSITION_SAFETY_BLOCKED" not in codes

    # Cold recovery must not clear the still-failed real safety phase.
    payload["phases"]["signal_maintenance"] = {"s6:s3": {"status": "CURRENT"}}
    payload["finished_at_ms"] = now[0]
    store.change(key, expected_version=1, request_key="2", payload=payload, reason="QA")
    codes = health()
    assert "SIGNAL_MAINTENANCE_DEGRADED" not in codes
    assert "PIPELINE_ENTRY_BLOCKED" in codes


def test_completed_cycle_with_cold_failure_is_only_maintenance_degraded(database):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="cold-only"
    )
    store.change(
        key,
        expected_version=0,
        request_key="1",
        payload={
            "cycle_id": "cold-only",
            "started_at_ms": now[0],
            "finished_at_ms": now[0],
            "status": "CYCLE_COMPLETE",
            "phases": {"signal_maintenance": maintenance()},
        },
        reason="QA",
    )
    health()
    now[0] += 121000
    assert health() == frozenset({"SIGNAL_MAINTENANCE_DEGRADED"})
    report = json.loads(store.read(health.key).payload_json)
    assert "PIPELINE_ENTRY_BLOCKED" not in report["findings"]
    assert "POSITION_SAFETY_BLOCKED" not in report["findings"]
