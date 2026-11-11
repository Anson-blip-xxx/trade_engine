"""Explicit isolated-PG only: one diagnostic key, never trading/JEV request data."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_diagnostic_retention import invoke, seed, versions
from test_v2_diagnostic_retention import migrate as migrate_base

from tests.support.v2_admission import database as database_fixture
from v2_core.jev_health import MODEL_HEALTH_KEY, fold_model_health
from v2_core.state import BusinessState

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261106_jev_health_retention.sql"
REASON = "JEV durable advisory coordination"


def apply_migration(connect):
    with connect() as conn:
        conn.execute(MIGRATION.read_text())


def migrate(connect):
    migrate_base(connect)
    apply_migration(connect)


def seed_health(
    connect, *, key=MODEL_HEALTH_KEY, count=3, days=45, reason=REASON, invalid=None
):
    store = BusinessState(connect)
    payload = None
    for i in range(count):
        payload = fold_model_health(
            payload,
            status="FALLBACK",
            reason="MODEL_LOCAL_STARTUP_UNAVAILABLE",
            completed_at_ms=1793924160000 + i * 1000,
        )
        result = store.change(
            key,
            expected_version=i,
            request_key=f"qa-health:{i}",
            payload=payload if invalid is None else invalid,
            reason=reason,
        )
        assert result.code == "APPLIED"
    with connect() as conn:
        # The fixture rejects all but a dedicated temporary QA socket/schema.
        conn.execute(
            "ALTER TABLE v2_state_history DISABLE TRIGGER v2_state_history_immutable"
        )
        conn.execute(
            "UPDATE v2_state_history SET created_at=clock_timestamp()-make_interval(days=>%s) WHERE state_id=%s",
            (days, key.identity),
        )
        conn.execute(
            "ALTER TABLE v2_state_history ENABLE TRIGGER v2_state_history_immutable"
        )
    return key


def direct_owner_delete_is_denied(connect, key, version=1):
    with pytest.raises(psycopg.errors.RaiseException), connect() as conn:
        conn.execute("SET LOCAL ROLE trade_v2_retention_owner")
        conn.execute(
            "DELETE FROM v2_state_history WHERE state_id=%s AND version=%s",
            (key.identity, version),
        )


def corrupt_scope_for_qa(connect, key, scope):
    # The normal application trigger prevents this corruption already. The
    # isolated-socket fixture permits a deliberately corrupt row only to prove
    # the independent retention guard also rejects it. Restore in this same
    # transaction so an error cannot leave a disabled trigger behind.
    with connect() as conn:
        conn.execute("ALTER TABLE v2_business_state DISABLE TRIGGER v2_state_version")
        conn.execute(
            "UPDATE v2_business_state SET scope=%s::jsonb WHERE state_id=%s",
            (json.dumps(scope), key.identity),
        )
        conn.execute("ALTER TABLE v2_business_state ENABLE TRIGGER v2_state_version")


def test_dry_run_then_apply_only_prunes_old_intermediate_projection_history(database):
    key = seed_health(database)
    before = BusinessState(database).read(key)
    migrate(database)
    preview = invoke(database)
    assert preview["shared_jev_health_candidates"] == 2
    assert preview["shared_jev_health_removed"] == 0
    assert preview["shared_jev_health_scope"] == asdict(key)
    assert preview["shared_jev_health_cutoff"] == preview["diagnostic_cutoff"]
    assert versions(database, key) == [1, 2, 3]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_retention_runs").fetchone()[0] == 0
    result = invoke(database, True)
    assert result["shared_jev_health_removed"] == 2
    assert result["diagnostic_removed"] == result["expired_demands_removed"] == 0
    assert versions(database, key) == [3]
    assert BusinessState(database).read(key) == before
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_retention_runs").fetchone()[0] == 2
    direct_owner_delete_is_denied(database, key, 3)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "other-jev"),
        ("environment", "LIVE"),
        ("exchange", "OTHER"),
        ("key", "not-latest"),
        ("namespace", "jev-advisory-day-budget-v1"),
        ("namespace", "jev-advisory-request-v1"),
        ("product", "FUTURES"),
    ],
)
def test_other_global_keys_never_become_retirable_by_sharing_reason_or_schema(
    database, field, value
):
    key = seed_health(database, key=replace(MODEL_HEALTH_KEY, **{field: value}))
    migrate(database)
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    assert versions(database, key) == [1, 2, 3]
    direct_owner_delete_is_denied(database, key)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "other-jev"),
        ("environment", "SANDBOX"),
        ("exchange", "BINANCE"),
        ("key", "other"),
        ("namespace", "jev-advisory-request-v1"),
        ("product", "FUTURES"),
        ("extra", "not-a-canonical-scope"),
    ],
)
def test_fixed_id_with_wrong_or_extra_scope_fields_is_not_retirable(
    database, field, value
):
    key = seed_health(database)
    migrate(database)
    corrupt_scope_for_qa(database, key, {**asdict(key), field: value})
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)


def test_exact_scope_attached_to_wrong_physical_id_is_not_retirable(database):
    key = seed_health(database, key=replace(MODEL_HEALTH_KEY, key="forged"))
    migrate(database)
    corrupt_scope_for_qa(database, key, asdict(MODEL_HEALTH_KEY))
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"schema": "not-the-projection", "execution_authorized": False},
        {"schema": "jev-completed-model-health-v1", "execution_authorized": True},
        {"schema": "jev-completed-model-health-v1", "execution_authorized": "false"},
        {"schema": "jev-completed-model-health-v1"},
    ],
)
def test_wrong_payload_schema_or_authority_is_never_swept(database, invalid):
    key = seed_health(database, invalid=invalid)
    migrate(database)
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)


@pytest.mark.parametrize(
    "reason", ["JEV MODEL HEALTH", "TRADING_BUSINESS_HEALTH", "OTHER"]
)
def test_exact_key_and_schema_still_require_exact_write_reason(database, reason):
    key = seed_health(database, reason=reason)
    migrate(database)
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)


def test_tombstoned_projection_is_not_retired(database):
    key = seed_health(database)
    migrate(database)
    assert (
        BusinessState(database)
        .change(
            key,
            expected_version=3,
            request_key="qa-delete-projection",
            payload={},
            reason=REASON,
            deleted=True,
        )
        .code
        == "APPLIED"
    )
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)


def test_policy_duration_and_thirty_day_floor_apply_to_projection(database):
    key = seed_health(database, days=29)
    migrate(database)
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    direct_owner_delete_is_denied(database, key)
    with pytest.raises(psycopg.errors.CheckViolation), database() as conn:
        conn.execute("UPDATE v2_retention_policy SET diagnostic_days=29")


def test_longer_existing_diagnostic_policy_is_respected(database):
    key = seed_health(database, days=45)
    migrate(database)
    with database() as conn:
        conn.execute("UPDATE v2_retention_policy SET diagnostic_days=90")
    assert invoke(database, True)["shared_jev_health_removed"] == 0
    assert versions(database, key) == [1, 2, 3]
    with database() as conn:
        conn.execute("UPDATE v2_retention_policy SET diagnostic_days=30")
    assert invoke(database, True)["shared_jev_health_removed"] == 2


def test_referenced_projection_revision_and_latest_are_protected(database):
    key = seed_health(database)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_producer_batches(source,environment,frame_id,content_digest,observed_at_ms,signal_ids) VALUES('s3','SANDBOX','jev-health-ref','digest',1,'[]')"
        )
        conn.execute(
            "INSERT INTO v2_s3_frames(environment,frame_id,input_digest,config,emitted_events,state_id,state_version) VALUES('SANDBOX','jev-health-ref','digest','{}','[]',%s,1)",
            (key.identity,),
        )
    migrate(database)
    assert invoke(database, True)["shared_jev_health_removed"] == 1
    assert versions(database, key) == [1, 3]
    direct_owner_delete_is_denied(database, key, 1)
    direct_owner_delete_is_denied(database, key, 3)


def test_batch_bounds_and_disabled_policy_keep_existing_hourly_contract(database):
    key = seed_health(database, count=6)
    migrate(database)
    with database() as conn:
        conn.execute("UPDATE v2_retention_policy SET batch_rows=2")
    first = invoke(database, True)
    assert first["shared_jev_health_removed"] == 2 and first["batch_full"] is True
    second = invoke(database, True)
    assert second["shared_jev_health_removed"] == 2 and second["batch_full"] is True
    third = invoke(database, True)
    assert third["shared_jev_health_removed"] == 1 and third["batch_full"] is False
    assert versions(database, key) == [6]
    with database() as conn:
        conn.execute("UPDATE v2_retention_policy SET enabled=false")
    assert invoke(database, True) == {"status": "DISABLED"}


@pytest.mark.parametrize(
    "namespace,reason",
    [
        ("host-disk-health-v1", "HOST_DISK_ASSESSMENT"),
        ("testnet-trading-daemon-v1", "TESTNET_TRADING_DAEMON_CYCLE"),
        ("trading-health-v1", "TRADING_BUSINESS_HEALTH"),
        ("trading-pipeline-cycle-v1", "TRADING_PIPELINE_CYCLE"),
    ],
)
def test_previous_diagnostics_and_expired_demand_rules_are_unchanged(
    database, namespace, reason
):
    old = seed(database, namespace, reason)
    live = seed(database, namespace, reason, environment="LIVE")
    health_key = seed_health(database)
    migrate(database)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_market_demands VALUES ('SANDBOX','OLDUSDT',1),('SANDBOX','NEWUSDT',9223372036854775806),('LIVE','OLDUSDT',1)"
        )
    result = invoke(database, True)
    assert (
        result["diagnostic_removed"] == 2 and result["shared_jev_health_removed"] == 2
    )
    assert result["expired_demands_removed"] == 1
    assert versions(database, old) == versions(database, health_key) == [3]
    assert versions(database, live) == [1, 2, 3]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_market_demands").fetchone()[0] == 2


def test_failures_roll_back_health_old_diagnostics_demands_and_audit(database):
    key = seed_health(database)
    old = seed(database)
    migrate(database)
    with database() as conn:
        conn.execute("INSERT INTO v2_market_demands VALUES('SANDBOX','OLDUSDT',1)")
        conn.execute(
            "ALTER TABLE v2_retention_runs ADD CONSTRAINT qa_failure CHECK(false)"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        invoke(database, True)
    assert versions(database, key) == versions(database, old) == [1, 2, 3]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_market_demands").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v2_retention_runs").fetchone()[0] == 0


def test_existing_advisory_lock_and_skip_locked_apply_to_new_projection(database):
    key = seed_health(database)
    migrate(database)
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('v2-diagnostic-retention:' || %s,0))",
            (schema,),
        )
        assert invoke(database, True) == {"status": "BUSY"}
    with database() as conn:
        conn.execute(
            "SELECT event_id FROM v2_state_history WHERE state_id=%s AND version=1 FOR UPDATE",
            (key.identity,),
        )
        assert invoke(database, True)["shared_jev_health_removed"] == 1
    assert versions(database, key) == [1, 3]


def test_direct_application_delete_or_update_remains_forbidden(database):
    key = seed_health(database)
    migrate(database)
    with pytest.raises(psycopg.errors.RaiseException), database() as conn:
        conn.execute(
            "DELETE FROM v2_state_history WHERE state_id=%s AND version=1",
            (key.identity,),
        )
    with pytest.raises(psycopg.errors.RaiseException), database() as conn:
        conn.execute(
            "UPDATE v2_state_history SET reason=reason WHERE state_id=%s",
            (key.identity,),
        )
    assert versions(database, key) == [1, 2, 3]


def test_replace_is_idempotent_keeps_existing_owner_acl_oid_and_runner_permissions(
    database,
):
    key = seed_health(database)
    migrate_base(database)
    role = "jev_retention_client_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION v2_run_retention(boolean) TO {}").format(
                sql.Identifier(role)
            )
        )
        before = conn.execute(
            "SELECT oid,proowner,proacl,prosecdef,proconfig FROM pg_proc WHERE pronamespace=%s::regnamespace AND proname IN ('v2_run_retention','v2_retention_history_guard') ORDER BY proname",
            (schema,),
        ).fetchall()
    try:
        apply_migration(database)
        apply_migration(database)
        with database() as conn:
            after = conn.execute(
                "SELECT oid,proowner,proacl,prosecdef,proconfig FROM pg_proc WHERE pronamespace=%s::regnamespace AND proname IN ('v2_run_retention','v2_retention_history_guard') ORDER BY proname",
                (schema,),
            ).fetchall()
            assert after == before
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            assert (
                conn.execute("SELECT v2_run_retention(true)").fetchone()[0][
                    "shared_jev_health_removed"
                ]
                == 2
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            conn.execute(
                "DELETE FROM v2_state_history WHERE state_id=%s", (key.identity,)
            )
    finally:
        with database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_migration_requires_existing_controlled_retention_function(database):
    with pytest.raises(
        psycopg.errors.RaiseException, match="EXISTING_CONTROLLED_RETENTION_REQUIRED"
    ):
        apply_migration(database)


def test_fixed_scope_identity_and_schema_contract_in_migration():
    ddl = MIGRATION.read_text()
    assert MODEL_HEALTH_KEY.identity in ddl
    assert (
        json.dumps(asdict(MODEL_HEALTH_KEY), sort_keys=True, separators=(",", ":"))
        in ddl
    )
    assert (
        "CREATE ROLE" not in ddl and "GRANT " not in ddl and "CREATE INDEX" not in ddl
    )
    assert (
        "ORDER BY h.version LIMIT policy.batch_rows FOR UPDATE OF h SKIP LOCKED" in ddl
    )
