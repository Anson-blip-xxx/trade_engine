from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState, StateKey

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]


def seed(
    connect,
    namespace="host-disk-health-v1",
    reason="HOST_DISK_ASSESSMENT",
    *,
    environment="SANDBOX",
    versions=3,
    days=45,
):
    key = StateKey(
        "BINANCE", "retention-test", environment, "FUTURES", namespace, str(uuid4())
    )
    store = BusinessState(connect)
    for i in range(versions):
        store.change(
            key,
            expected_version=i,
            request_key=str(i),
            payload={"revision": i},
            reason=reason,
        )
    with connect() as c:
        c.execute(
            "ALTER TABLE v2_state_history DISABLE TRIGGER v2_state_history_immutable"
        )
        c.execute(
            "UPDATE v2_state_history SET created_at=clock_timestamp()-make_interval(days=>%s) WHERE state_id=%s",
            (days, key.identity),
        )
        c.execute(
            "ALTER TABLE v2_state_history ENABLE TRIGGER v2_state_history_immutable"
        )
    return key


def migrate(connect):
    with connect() as c:
        c.execute((ROOT / "db/migrations/20260929_shared_market.sql").read_text())
        c.execute(
            (ROOT / "db/migrations/20261004_diagnostic_retention.sql").read_text()
        )


def invoke(connect, apply=False):
    with connect() as c:
        return c.execute("SELECT v2_run_retention(%s)", (apply,)).fetchone()[0]


def versions(connect, key):
    with connect() as c:
        return [
            r[0]
            for r in c.execute(
                "SELECT version FROM v2_state_history WHERE state_id=%s ORDER BY version",
                (key.identity,),
            ).fetchall()
        ]


def test_preview_and_apply_preserve_latest_and_business_evidence(database):
    old = seed(database)
    protected = seed(database, "account-coverage-evidence-v1", "ACCOUNT_COVERAGE_AUDIT")
    fresh = seed(database, days=1)
    live = seed(database, environment="LIVE")
    forged = seed(database, reason="TRADING_PIPELINE_CYCLE")
    before = BusinessState(database).read(old)
    migrate(database)
    report = invoke(database)
    assert report["diagnostic_candidates"] == 2 and report["diagnostic_removed"] == 0
    assert versions(database, old) == [1, 2, 3]
    report = invoke(database, True)
    assert report["diagnostic_removed"] == 2
    assert versions(database, old) == [3]
    assert BusinessState(database).read(old) == before
    for key in (protected, fresh, live, forged):
        assert versions(database, key) == [1, 2, 3]
    assert invoke(database, True)["diagnostic_removed"] == 0
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_retention_runs").fetchone()[0] == 2


def test_foreign_frame_reference_protects_old_diagnostic_revision(database):
    key = seed(database)
    with database() as c:
        c.execute(
            "INSERT INTO v2_producer_batches(source,environment,frame_id,content_digest,observed_at_ms,signal_ids) VALUES('s3','SANDBOX','ref-test','digest',1,'[]')"
        )
        c.execute(
            "INSERT INTO v2_s3_frames(environment,frame_id,input_digest,config,emitted_events,state_id,state_version) VALUES('SANDBOX','ref-test','digest','{}','[]',%s,1)",
            (key.identity,),
        )
    migrate(database)
    assert invoke(database, True)["diagnostic_removed"] == 1
    assert versions(database, key) == [1, 3]


@pytest.mark.parametrize(
    "namespace,reason",
    [
        ("host-disk-health-v1", "HOST_DISK_ASSESSMENT"),
        ("testnet-trading-daemon-v1", "TESTNET_TRADING_DAEMON_CYCLE"),
        ("trading-health-v1", "TRADING_BUSINESS_HEALTH"),
        ("trading-pipeline-cycle-v1", "TRADING_PIPELINE_CYCLE"),
    ],
)
def test_only_intermediate_versions_are_retired(database, namespace, reason):
    key = seed(database, namespace, reason)
    migrate(database)
    assert invoke(database, True)["diagnostic_removed"] == 2
    assert versions(database, key) == [3]


def test_bounded_batches_and_policy_disable(database):
    key = seed(database, versions=6)
    migrate(database)
    with database() as c:
        c.execute("UPDATE v2_retention_policy SET batch_rows=2")
    assert invoke(database, True)["diagnostic_removed"] == 2
    assert invoke(database, True)["diagnostic_removed"] == 2
    assert invoke(database, True)["diagnostic_removed"] == 1
    assert versions(database, key) == [6]
    with database() as c:
        c.execute("UPDATE v2_retention_policy SET enabled=false")
    assert invoke(database, True) == {"status": "DISABLED"}


@pytest.mark.parametrize(
    "column,value",
    [
        ("diagnostic_days", 29),
        ("demand_days", 0),
        ("batch_rows", 201),
        ("audit_days", 364),
    ],
)
def test_policy_cannot_remove_safety_floors(database, column, value):
    migrate(database)
    with pytest.raises(psycopg.errors.CheckViolation), database() as c:
        c.execute(
            sql.SQL("UPDATE v2_retention_policy SET {}=%s").format(
                sql.Identifier(column)
            ),
            (value,),
        )


def test_expired_demand_cleanup_keeps_recent_live_and_refreshing_rows(database):
    migrate(database)
    with database() as c:
        c.execute(
            "INSERT INTO v2_market_demands VALUES ('SANDBOX','OLDUSDT',1),('SANDBOX','NEWUSDT',9223372036854775806),('LIVE','OLDUSDT',1)"
        )
    assert invoke(database)["expired_demand_candidates"] == 1
    assert invoke(database, True)["expired_demands_removed"] == 1
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_market_demands").fetchone()[0] == 2


def test_failure_rolls_back_all_deletions_and_audit(database):
    key = seed(database)
    migrate(database)
    with database() as c:
        c.execute(
            "ALTER TABLE v2_retention_runs ADD CONSTRAINT injected_failure CHECK(false)"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        invoke(database, True)
    assert versions(database, key) == [1, 2, 3]


def test_no_trigger_bypass_for_application_or_direct_update(database):
    key = seed(database)
    migrate(database)
    with pytest.raises(psycopg.errors.RaiseException), database() as c:
        c.execute(
            "DELETE FROM v2_state_history WHERE state_id=%s AND version=1",
            (key.identity,),
        )
    with pytest.raises(psycopg.errors.RaiseException), database() as c:
        c.execute(
            "UPDATE v2_state_history SET reason=reason WHERE state_id=%s",
            (key.identity,),
        )
    with database() as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        role = "retention_client_" + uuid4().hex
        c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        c.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database() as c:
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            c.execute("SELECT v2_run_retention(true)")
        with database() as c:
            c.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION v2_run_retention(boolean) TO {}"
                ).format(sql.Identifier(role))
            )
        with database() as c:
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            assert (
                c.execute("SELECT v2_run_retention(true)").fetchone()[0][
                    "diagnostic_removed"
                ]
                == 2
            )
    finally:
        with database() as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_concurrent_job_is_skipped_and_locked_rows_not_deleted(database):
    key = seed(database)
    migrate(database)
    with database() as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('v2-diagnostic-retention:' || %s,0))",
            (schema,),
        )
        assert invoke(database, True) == {"status": "BUSY"}
    with database() as c:
        c.execute(
            "SELECT event_id FROM v2_state_history WHERE state_id=%s AND version=1 FOR UPDATE",
            (key.identity,),
        )
        assert invoke(database, True)["diagnostic_removed"] == 1
    assert versions(database, key) == [1, 3]


@pytest.mark.parametrize(
    "namespace",
    [
        "runtime-policy-v1",
        "capital-snapshot-v1",
        "testnet-protection-v1",
        "directional-account-context-v1",
        "watchdog-delivery-v1",
    ],
)
def test_protected_namespaces_cannot_be_deleted_even_by_retention_role(
    database, namespace
):
    key = seed(database, namespace, "HOST_DISK_ASSESSMENT")
    migrate(database)
    assert invoke(database, True)["diagnostic_removed"] == 0
    with pytest.raises(psycopg.errors.RaiseException), database() as c:
        c.execute("SET LOCAL ROLE trade_v2_retention_owner")
        c.execute(
            "DELETE FROM v2_state_history WHERE state_id=%s AND version=1",
            (key.identity,),
        )
    assert versions(database, key) == [1, 2, 3]


def test_retention_role_cannot_delete_latest_or_recent_versions(database):
    key = seed(database, days=1)
    migrate(database)
    for version in (1, 3):
        with pytest.raises(psycopg.errors.RaiseException), database() as c:
            c.execute("SET LOCAL ROLE trade_v2_retention_owner")
            c.execute(
                "DELETE FROM v2_state_history WHERE state_id=%s AND version=%s",
                (key.identity, version),
            )
