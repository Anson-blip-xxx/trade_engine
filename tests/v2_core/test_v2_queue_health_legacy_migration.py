"""Legacy alarm migration is pure, scoped and never alarm-clear authority."""

from copy import deepcopy
from uuid import UUID

import pytest

from services.v2_queue_health_legacy_migration import migrate_legacy_queue_gap
from tests.support.v2_admission import database as database_fixture

database = database_fixture


def identifier(number):
    return str(UUID(int=number))


def inputs():
    scope = {
        "exchange": "BINANCE",
        "account_id": "fixture",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    current = {
        "observed_at_ms": 2000,
        "account_scope": scope,
        "active": ["SIGNAL_CONSUMPTION_LAG"],
        "first_seen_ms": {"SIGNAL_CONSUMPTION_LAG": 500},
        "clear_since_ms": {"OTHER": 600},
        "findings": {"SIGNAL_CONSUMPTION_LAG": {"s6:s3": {"count": 3}}},
        "signal_lag_state": {"status": "ACTIVE"},
        "queue_diagnostics": {
            "s6:s3": {
                "status": "UNKNOWN",
                "reason": "HISTORICAL_RECOVERY_NOT_PROVEN",
                "lag_signal_ids": [identifier(1)],
                "historical_recovery_unproven_since_ms": 1000,
                "historical_recovery_sweeps": {"discovery": {"first": None}},
                "recent_to_ms": 1500,
            },
            "s8:s3": {"status": "UNKNOWN", "lag_signal_ids": [identifier(9)]},
        },
    }
    legacy = {
        "observed_at_ms": 1000,
        "account_scope": deepcopy(scope),
        "findings": {
            "SIGNAL_CONSUMPTION_LAG": {
                "s6:s3": {"count": 3, "oldest_received_at_ms": 400}
            }
        },
    }
    proof = {
        "row": (1, 400, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, {}),
        "diagnostics": {
            "status": "COMPLETE",
            "reason": "BOUNDED_HOT_QUEUE_OBSERVED",
            "scope": "VALIDATED_READY_AND_RECENT_INBOX",
            "recent_from_ms": 300,
            "recent_to_ms": 1000,
            "limit_per_source": 1000,
            "ready_keys": 1,
            "recent_keys": 20,
            "budget_ms": 1000,
            "query_ms": 50,
            "lag_signal_ids": [identifier(2)],
        },
    }
    return {
        "current_payload": current,
        "legacy_report": legacy,
        "lane": "s6:s3",
        "proof_result": proof,
        "proof_from_ms": 300,
        "proof_cutoff_ms": 1000,
        "now_ms": 2500,
    }


def test_merges_exact_keys_only_lane_changes_and_no_input_mutation():
    data = inputs()
    original = deepcopy(data)
    result = migrate_legacy_queue_gap(**data)
    assert data == original
    before = data["current_payload"]
    for key in set(before) - {"queue_diagnostics"}:
        assert result[key] == before[key]
    assert result["queue_diagnostics"]["s8:s3"] == before["queue_diagnostics"]["s8:s3"]
    lane = result["queue_diagnostics"]["s6:s3"]
    assert lane["lag_signal_ids"] == [identifier(1), identifier(2)]
    assert "historical_recovery_unproven_since_ms" not in lane
    assert "historical_recovery_sweeps" not in lane
    assert lane["status"] == "UNKNOWN"
    assert lane["recent_to_ms"] == 1500
    assert lane["legacy_coverage_proof"]["remaining_lag_count"] == 1
    assert lane["legacy_coverage_proof"]["alarm_clearance_authorized"] is False
    result["findings"]["SIGNAL_CONSUMPTION_LAG"]["s6:s3"]["count"] = 99
    assert data == original  # All nested state, not just the changed lane, is copied.


def test_zero_remaining_still_retains_newer_ids_and_does_not_clear_alarm():
    data = inputs()
    data["proof_result"]["row"] = (0, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, {})
    data["proof_result"]["diagnostics"]["lag_signal_ids"] = []
    result = migrate_legacy_queue_gap(**data)
    assert result["active"] == ["SIGNAL_CONSUMPTION_LAG"]
    assert result["queue_diagnostics"]["s6:s3"]["lag_signal_ids"] == [identifier(1)]
    assert result["queue_diagnostics"]["s6:s3"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("concurrent_health_update", [False, True])
def test_pg_migration_cas_preserves_newer_monitor_or_audits_exact_proof(
    database, concurrent_health_update
):
    import json

    from v2_core.state import BusinessState, StateKey

    data = inputs()
    original = data["current_payload"]
    key = StateKey(
        **original["account_scope"], namespace="trading-health-v1", key="latest"
    )
    store = BusinessState(database)
    assert (
        store.change(
            key,
            expected_version=0,
            request_key="initial",
            payload=original,
            reason="QA",
        ).code
        == "APPLIED"
    )
    migrated = migrate_legacy_queue_gap(**data)
    if concurrent_health_update:
        newer = deepcopy(original)
        newer["observed_at_ms"] = 3000
        store.change(
            key, expected_version=1, request_key="monitor", payload=newer, reason="QA"
        )
    result = store.change(
        key,
        expected_version=1,
        request_key="migration",
        payload=migrated,
        reason="LEGACY_QUEUE_WINDOW_PROVEN",
    )
    saved = json.loads(store.read(key).payload_json)
    if concurrent_health_update:
        assert result.code == "STALE" and saved == newer
    else:
        assert result.code == "APPLIED" and saved == migrated
        assert saved["active"] == original["active"]
    with database() as conn:
        history = conn.execute(
            "SELECT count(*) FROM v2_state_history WHERE state_id=%s AND reason='LEGACY_QUEUE_WINDOW_PROVEN'",
            (key.identity,),
        ).fetchone()[0]
        assert history == int(not concurrent_health_update)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("now_ms", True),
        ("now_ms", 999),
        ("proof_from_ms", True),
        ("proof_from_ms", -1),
        ("proof_from_ms", 401),
        ("proof_cutoff_ms", True),
        ("proof_cutoff_ms", 1001),
        ("lane", "s8:s3"),
        ("lane", "s6:unknown"),
        ("lane", None),
    ],
)
def test_bad_explicit_bounds_or_lane_fail_without_mutation(field, value):
    data = inputs()
    data[field] = value
    before = deepcopy(data)
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)
    assert data == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "UNKNOWN"),
        ("reason", "QUEUE_OBSERVATION_LIMIT_REACHED"),
        ("ready_keys", 1001),
        ("recent_keys", 1001),
        ("ready_keys", True),
        ("recent_keys", -1),
        ("tracking_overflow", True),
        ("tracking_overflow", 0),
        ("tracking_gap_reason", ""),
        ("failure_type", "TimeoutError"),
        ("recent_from_ms", 301),
        ("recent_to_ms", 999),
        ("recent_from_ms", True),
        ("query_ms", True),
        ("query_ms", -1),
        ("query_ms", 1001),
        ("budget_ms", 5001),
        ("budget_ms", 0),
        ("scope", "ALL_HISTORY"),
        ("limit_per_source", 0),
        ("capped", True),
        ("truncated", True),
        ("limit_reached", True),
        ("lag_signal_ids", ["invalid"]),
        ("lag_signal_ids", [identifier(2), identifier(2)]),
        ("lag_signal_ids", [UUID(int=2)]),
        ("lag_signal_ids", None),
    ],
)
def test_incomplete_capped_malformed_proof_is_rejected(field, value):
    data = inputs()
    data["proof_result"]["diagnostics"][field] = value
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tracking_gap_reason", "QUEUE_TRACKING_KEYS_INVALID"),
        ("tracking_overflow", True),
        ("tracking_overflow", None),
        ("lag_signal_ids", ["bad-id"]),
        ("lag_signal_ids", [True]),
        ("lag_signal_ids", [identifier(1), identifier(1)]),
        ("historical_recovery_unproven_since_ms", 1001),
        ("historical_recovery_unproven_since_ms", True),
        ("status", "COMPLETE"),
        ("legacy_coverage_proof", {}),
    ],
)
def test_current_lane_new_information_loss_cannot_be_erased(field, value):
    data = inputs()
    data["current_payload"]["queue_diagnostics"]["s6:s3"][field] = value
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


@pytest.mark.parametrize(
    "legacy_lane",
    [
        {"lag_signal_ids": []},
        {"lag_signal_ids": [identifier(2)]},
        {"tracking_gap_reason": "QUEUE_TRACKING_KEYS_INVALID"},
        {"tracking_overflow": True},
        None,
    ],
)
def test_report_with_exact_keys_or_tracking_loss_is_not_legacy(legacy_lane):
    data = inputs()
    data["legacy_report"]["queue_diagnostics"] = {"s6:s3": legacy_lane}
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("count", True),
        ("count", 0),
        ("count", "3"),
        ("count", -1),
        ("oldest_received_at_ms", True),
        ("oldest_received_at_ms", 299),
        ("oldest_received_at_ms", 1001),
        ("oldest_received_at_ms", None),
    ],
)
def test_exact_original_lag_must_be_covered(field, value):
    data = inputs()
    data["legacy_report"]["findings"]["SIGNAL_CONSUMPTION_LAG"]["s6:s3"][field] = value
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


@pytest.mark.parametrize("target", ["current_payload", "legacy_report"])
@pytest.mark.parametrize("scope", [{}, None, {"account_id": "another-fixture"}])
def test_scope_must_be_present_nonempty_and_exact(target, scope):
    data = inputs()
    data[target]["account_scope"] = scope
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


@pytest.mark.parametrize(
    "index,value",
    [
        (0, True),
        (0, 2),
        (1, None),
        (1, True),
        (1, 3000),
        (2, -1),
        (3, 1),
        (4, True),
        (13, []),
        (13, {"bad private text": 1}),
        (13, {"SAFE_CODE": True}),
        (13, {"SAFE_CODE": 0}),
    ],
)
def test_proof_row_cannot_contradict_exact_ids(index, value):
    data = inputs()
    row = list(data["proof_result"]["row"])
    row[index] = value
    data["proof_result"]["row"] = row
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


def test_union_capacity_is_not_silently_truncated():
    data = inputs()
    data["current_payload"]["queue_diagnostics"]["s6:s3"]["lag_signal_ids"] = [
        identifier(n) for n in range(10, 2012)
    ]
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)
    data["proof_result"]["diagnostics"]["lag_signal_ids"] = [identifier(10)]
    assert (
        len(
            migrate_legacy_queue_gap(**data)["queue_diagnostics"]["s6:s3"][
                "lag_signal_ids"
            ]
        )
        == 2002
    )


def test_repeated_migration_fails_instead_of_reauthorizing_recovery():
    data = inputs()
    data["current_payload"] = migrate_legacy_queue_gap(**data)
    with pytest.raises(ValueError, match="LEGACY_QUEUE_COVERAGE_PROOF_INVALID"):
        migrate_legacy_queue_gap(**data)


def test_function_has_no_database_or_cas_side_effect_interface():
    # The only output is an ordinary dict. Its caller must CAS the exact state
    # version and must never replace a newer payload with this derived copy.
    data = inputs()
    result = migrate_legacy_queue_gap(**data)
    assert type(result) is dict
    assert set(result) == set(data["current_payload"])
