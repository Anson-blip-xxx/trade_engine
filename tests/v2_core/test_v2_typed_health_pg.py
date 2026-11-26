"""Isolated-QA-only typed health persistence and current-history checks."""

import json
import time
from dataclasses import asdict
from uuid import uuid4

from test_v2_trading_health import opening
from test_v2_typed_health import sample

from services.v2_management_runtime import REASON
from services.v2_trading_health import TradingHealth
from services.v2_typed_health import ManagementHeartbeat
from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState, StateKey

database = database_fixture


def test_typed_heartbeat_requires_current_committed_management_identity(database):
    value = sample()
    clock = time.time_ns() // 1000000
    for name in ("started_at_ms", "finished_at_ms"):
        value.payload[name] += clock - 2000
    for phase in value.payload["phases"].values():
        for name in ("started_at_ms", "finished_at_ms"):
            phase[name] += clock - 2000
    store = BusinessState(database)
    store.change(
        StateKey(
            **asdict(value.scope), namespace="testnet-trading-daemon-v1", key="latest"
        ),
        expected_version=0,
        request_key="legacy",
        reason=REASON,
        payload={"status": "RUNNING", "account_scope": asdict(value.scope)},
    )
    probe = ManagementHeartbeat(
        database,
        tenant=value.binding.tenant_id,
        scope=value.scope,
        route_for=lambda: value.route,
        max_age_seconds=30,
    )
    assert probe() == "DAEMON_HEARTBEAT_MISSING"
    result = store.change(
        value.key,
        expected_version=0,
        request_key="typed",
        reason=REASON,
        payload=value.payload,
    )
    assert result.code == "APPLIED" and probe() is None
    assert probe.completion_status == "CURRENT"
    value.route["epoch"] += 1
    assert probe() == "DAEMON_HEARTBEAT_INVALID"
    value.route["epoch"] -= 1
    # A new current checkpoint cannot borrow an older valid history row.
    store.change(
        value.key,
        expected_version=1,
        request_key="wrong-origin",
        reason="UNVERIFIED_ORIGIN",
        payload=value.payload,
    )
    assert probe() == "DAEMON_HEARTBEAT_INVALID"


def test_typed_health_keeps_real_rejections_without_legacy_pipeline_or_jev(database):
    health = TradingHealth(
        database,
        account_id="test-account",
        runtime_mode="TYPED_STRATEGY",
        tv_enabled=True,
        quant_enabled=True,
    )
    store = BusinessState(database)
    old = StateKey(**asdict(health.scope), namespace="trading-health-v1", key="latest")
    store.change(
        old,
        expected_version=0,
        request_key="legacy-health",
        reason="QA",
        payload={
            "account_scope": asdict(health.scope),
            "active": ["SIGNAL_CONSUMPTION_LAG", "JEV_MODEL_DEGRADED"],
            "findings": {"SIGNAL_CONSUMPTION_LAG": {"s6:s3": {"count": 9}}},
            "first_seen_ms": {"SIGNAL_CONSUMPTION_LAG": 0},
        },
    )
    store.change(
        StateKey(
            **asdict(health.scope),
            namespace="trading-pipeline-cycle-v1",
            key=str(uuid4()),
        ),
        expected_version=0,
        request_key="legacy-pipeline",
        reason="QA",
        payload={
            "status": "ENTRY_BLOCKED",
            "started_at_ms": 1,
            "finished_at_ms": 2,
            "phases": {"protection": {"status": "BLOCKED"}},
        },
    )
    for _ in range(3):
        opening(database)
    assert health() == {"OPENING_SUBMISSION_STALLED"}
    saved = json.loads(store.read(health.key).payload_json)
    assert saved["execution_health"] == "UNKNOWN"
    assert saved["execution_authorized"] is False
    assert saved["typed_queue_observation"] == "NOT_EVALUATED"
    assert saved["opening"]["rejected"] == 3
    assert saved["queue_diagnostics"] == {} and saved["jev_budget"] is None
    assert (
        "SIGNAL_CONSUMPTION_LAG" in json.loads(store.read(old).payload_json)["active"]
    )
