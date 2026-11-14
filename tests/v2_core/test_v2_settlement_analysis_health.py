"""Analysis degradation remains visible without becoming a cash safety fault."""

import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_settlement_financial_projection import EPISODE, financial_result

from services.v2_testnet_watchdog import HealthWatchdog
from services.v2_trading_health import TradingHealth, settlement_health_projection
from tests.support.v2_admission import database as database_fixture
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.telegram import BUSINESS_HEALTH_MESSAGES

database = database_fixture
CODE = "SETTLEMENT_ANALYSIS_DEGRADED"


def cycle():
    return {
        "cycle_id": "analysis-health",
        "started_at_ms": 1000000,
        "finished_at_ms": 1000000,
        "status": "CYCLE_COMPLETE",
        "phases": {"settlement": financial_result()},
    }


def test_projection_keeps_safe_analysis_details_and_financial_clearance_distinct():
    payload = cycle()
    original = deepcopy(payload)
    clear, finding = settlement_health_projection(payload)
    assert clear
    assert finding["episodes"] == [
        {"episode": EPISODE, "error_code": "DIRECTIONAL_OUTCOME_CONFLICT"}
    ]
    assert finding["financial_status"] == "CLEAR"
    assert finding["execution_authorized"] is False
    assert payload == original


@pytest.mark.parametrize("defect", ["wallet", "top_error", "outcome_error", "shape"])
def test_unverified_or_financial_error_never_becomes_analysis_only(defect):
    payload = cycle()
    result = payload["phases"]["settlement"]
    if defect == "wallet":
        result["status"] = "BLOCKED"
        result["settlements"][EPISODE] = {
            "status": "BLOCKED", "error_code": "WALLET_CASH_MISMATCH"
        }
    elif defect == "top_error":
        result["error_code"] = "UNKNOWN"
    elif defect == "outcome_error":
        result["outcomes"][EPISODE]["error_code"] = "raw credential-bearing message"
    else:
        result["outcomes"] = []
    assert settlement_health_projection(payload) == (False, None)


def test_successful_or_empty_analysis_has_no_degraded_finding():
    payload = cycle()
    payload["phases"]["settlement"]["outcomes"] = {}
    assert settlement_health_projection(payload) == (True, None)
    assert settlement_health_projection({"phases": {}}) == (False, None)


def test_analysis_uses_existing_watchdog_dedup_and_unknown_health_is_not_recovery():
    assert CODE in BUSINESS_HEALTH_MESSAGES
    assert "不授予开仓权限" in BUSINESS_HEALTH_MESSAGES[CODE]
    state = {"current": frozenset({CODE}), "saved": frozenset()}
    events = []

    def save(codes):
        state["saved"] = frozenset(codes)

    def watchdog():
        return HealthWatchdog(
            lambda: state["current"],
            notify=lambda event: events.append(event) or True,
            stop=SimpleNamespace(is_set=lambda: False, wait=lambda _: None),
            interval_seconds=15,
            delivery_state=SimpleNamespace(load=lambda: state["saved"], save=save),
        )

    watchdog().run_once()
    restarted = watchdog()
    restarted.run_once()
    assert len(events) == 1
    state["current"] = frozenset({"BUSINESS_HEALTH_UNAVAILABLE"})
    restarted.run_once()
    assert not any(event["status"] == "RECOVERED" for event in events)
    state["current"] = frozenset()
    restarted.run_once()
    assert [event["status"] for event in events if event["error_code"] == CODE] == [
        "UNAVAILABLE", "RECOVERED"
    ]


def test_integration_analysis_debounce_survives_restart_and_recovers(database):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="analysis"
    )
    payload = cycle()
    store.change(key, expected_version=0, request_key="1", payload=payload, reason="QA")
    assert health() == frozenset()
    grace = PolicyStore(database, health.scope).read().values["health.pipeline_seconds"] * 1000
    now[0] += grace + 1
    restarted = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    assert restarted() == frozenset({CODE})
    report = json.loads(store.read(health.key).payload_json)
    assert report["findings"][CODE]["financial_status"] == "CLEAR"
    assert "PIPELINE_ENTRY_BLOCKED" not in report["findings"]
    payload["phases"]["settlement"]["outcomes"] = {}
    store.change(key, expected_version=1, request_key="2", payload=payload, reason="QA")
    assert restarted() == frozenset()


def test_integration_other_blocker_keeps_its_identity_and_wallet_errors_stay_blocking(database):
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: 1000000)
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="mixed"
    )
    payload = cycle()
    payload["status"] = "ENTRY_BLOCKED"
    payload["phases"]["market"] = {"status": "BLOCKED", "error_code": "MARKET_NOT_CURRENT"}
    store.change(key, expected_version=0, request_key="1", payload=payload, reason="QA")
    health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["findings"]["PIPELINE_ENTRY_BLOCKED"]["stages"] == ["market"]
    assert CODE in report["findings"]

    result = payload["phases"]["settlement"]
    result["status"] = "BLOCKED"
    result["settlements"][EPISODE] = {
        "status": "BLOCKED", "error_code": "WALLET_CASH_MISMATCH"
    }
    store.change(key, expected_version=1, request_key="2", payload=payload, reason="QA")
    health()
    report = json.loads(store.read(health.key).payload_json)
    assert set(report["findings"]["PIPELINE_ENTRY_BLOCKED"]["stages"]) == {"market", "settlement"}
    assert CODE not in report["findings"]
