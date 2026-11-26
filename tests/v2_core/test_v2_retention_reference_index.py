"""Reference protection must remain safe and cheap as unrelated S3 frames grow.

PG cases require the isolated Unix-socket fixture. They do not prove production
latency; deployment must separately verify the real query plan and bounded job.
"""

import re
from pathlib import Path

import psycopg
import pytest
from test_v2_diagnostic_retention import invoke, seed, versions
from test_v2_diagnostic_retention import migrate as migrate_base

from tests.support.v2_admission import database as database_fixture

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126_retention_reference_lookup.sql"
ONLINE = ROOT / "deploy/v2-testnet/retention-reference-index.sql"
INDEX = "v2_s3_frames_state_reference"


def code(path):
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def migrate(connect):
    migrate_base(connect)
    with connect() as conn:
        conn.execute(
            (ROOT / "db/migrations/20261106_jev_health_retention.sql").read_text()
        )
        conn.execute(MIGRATION.read_text())


def online_checks(conn):
    # Only validators run in the isolated QA schema, never CONCURRENTLY here.
    # Substitute the verified fixture's schema/database names, not credentials.
    schema, database_name = conn.execute(
        "SELECT current_schema(),current_database()"
    ).fetchone()
    assert re.fullmatch(r"v2_core_test_[a-f0-9]+", schema)
    assert re.fullmatch(r"[A-Za-z0-9_]+", database_name)
    text = code(ONLINE).replace("trade_v2.", schema + ".")
    text = text.replace("'trade_v2_testnet'", "'" + database_name + "'")
    return re.findall(
        r"DO \$retention_reference_check\$.*?\$retention_reference_check\$;",
        text,
        flags=re.DOTALL,
    )


def test_offline_online_definition_and_fail_closed_checks_are_present():
    offline, online = code(MIGRATION), code(ONLINE)
    pattern = r"CREATE INDEX(?: CONCURRENTLY)? IF NOT EXISTS.*?;"
    left, right = (re.search(pattern, text, re.DOTALL)[0] for text in (offline, online))
    assert " ".join(left.split()) == " ".join(
        right.replace(" CONCURRENTLY", "").replace("trade_v2.", "").split()
    )
    assert online.lstrip().startswith("\\set ON_ERROR_STOP on")
    assert online.count("RETENTION_REFERENCE_INDEX_INVALID") == 2
    assert offline.count("RETENTION_REFERENCE_INDEX_INVALID") == 1
    for text in (offline, online):
        assert not re.search(
            r"\b(?:DELETE|DROP|TRUNCATE|REINDEX)\b", text, re.IGNORECASE
        )
        assert "indisvalid AND i.indisready AND i.indislive" in text
        assert "i.indexprs IS NULL AND i.indpred IS NULL" in text
        assert "i.indclass[0]=" in text and "opcname='uuid_ops'" in text
        assert "i.indclass[1]=" in text and "opcname='int8_ops'" in text
    assert "NOT production latency acceptance" in ONLINE.read_text()


def test_online_database_guard_and_resource_budgets_precede_build():
    online = code(ONLINE)
    guard = online.index("current_database()<>'trade_v2_testnet'")
    preflight = online.index("DO $retention_reference_check$")
    build = online.index("CREATE INDEX CONCURRENTLY")
    assert guard < preflight < build
    for bound in (
        "SET lock_timeout='5s';",
        "SET statement_timeout='15min';",
        "SET maintenance_work_mem='64MB';",
        "SET work_mem='4MB';",
        "SET max_parallel_maintenance_workers=0;",
        "SET max_parallel_workers_per_gather=0;",
    ):
        assert guard < online.index(bound) < preflight


def test_offline_migration_repeats_and_online_validators_accept_exact_index(database):
    with database() as conn:
        before, after = online_checks(conn)
        conn.execute(before)  # Missing is acceptable only BEFORE the build.
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())
        conn.execute(before)
        conn.execute(after)
        assert conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname=%s",
            (INDEX,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "definition",
    [
        "ON v2_s3_frames(state_version,state_id)",
        "ON v2_s3_frames(state_id)",
        "ON v2_s3_frames(state_id,state_version) INCLUDE(environment)",
        "ON v2_s3_frames(state_id,state_version) WHERE environment='SANDBOX'",
        "ON v2_s3_frames(state_id DESC,state_version)",
        "ON v2_s3_frames(state_id,state_version qa_nondefault_int8_ops)",
        "ON v2_state_history(state_id,version)",
    ],
)
@pytest.mark.parametrize("validation", ["offline", "online_before", "online_after"])
def test_wrong_existing_definition_is_rejected_not_replaced(
    database, definition, validation
):
    with database() as conn:
        if "qa_nondefault_int8_ops" in definition:
            conn.execute("""CREATE OPERATOR CLASS qa_nondefault_int8_ops
                FOR TYPE bigint USING btree AS
                OPERATOR 1 <(bigint,bigint), OPERATOR 2 <=(bigint,bigint),
                OPERATOR 3 =(bigint,bigint), OPERATOR 4 >=(bigint,bigint),
                OPERATOR 5 >(bigint,bigint), FUNCTION 1 btint8cmp(bigint,bigint)""")
        conn.execute("CREATE INDEX " + INDEX + " " + definition)
        oid = conn.execute("SELECT to_regclass(%s)::oid", (INDEX,)).fetchone()[0]
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="RETENTION_REFERENCE_INDEX_INVALID"
        ),
        database() as conn,
    ):
        checks = online_checks(conn)
        conn.execute(
            MIGRATION.read_text()
            if validation == "offline"
            else checks[validation == "online_after"]
        )
    with database() as conn:
        assert conn.execute("SELECT to_regclass(%s)::oid", (INDEX,)).fetchone() == (
            oid,
        )


@pytest.mark.parametrize("validation", ["offline", "online_before", "online_after"])
def test_invalid_existing_index_is_rejected_not_rebuilt(database, validation):
    with database() as conn:
        conn.execute(MIGRATION.read_text())
        # Deliberately corrupt catalog state only in the isolated superuser QA
        # fixture to model an interrupted CONCURRENTLY build, without killing DB.
        conn.execute(
            "UPDATE pg_index SET indisvalid=false WHERE indexrelid=%s::regclass",
            (INDEX,),
        )
        oid = conn.execute("SELECT to_regclass(%s)::oid", (INDEX,)).fetchone()[0]
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="RETENTION_REFERENCE_INDEX_INVALID"
        ),
        database() as conn,
    ):
        checks = online_checks(conn)
        conn.execute(
            MIGRATION.read_text()
            if validation == "offline"
            else checks[validation == "online_after"]
        )
    with database() as conn:
        assert conn.execute(
            "SELECT indexrelid,indisvalid FROM pg_index WHERE indexrelid=%s", (oid,)
        ).fetchone() == (oid, False)


def test_online_postcondition_rejects_missing_index(database):
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="RETENTION_REFERENCE_INDEX_INVALID"
        ),
        database() as conn,
    ):
        conn.execute(online_checks(conn)[1])


def add_frames(conn, key, *, environment="SANDBOX", count=1, prefix="ref", version=1):
    conn.execute(
        "INSERT INTO v2_producer_batches(source,environment,frame_id,content_digest,observed_at_ms,signal_ids) "
        "SELECT 's3',%s,%s||n::text,'digest',1,'[]' FROM generate_series(1,%s) n",
        (environment, prefix, count),
    )
    conn.execute(
        "INSERT INTO v2_s3_frames(environment,frame_id,input_digest,config,emitted_events,state_id,state_version) "
        "SELECT %s,%s||n::text,'digest',jsonb_build_object('padding',repeat('x',768)),'[]',%s,%s "
        "FROM generate_series(1,%s) n",
        (environment, prefix, key.identity, version, count),
    )


@pytest.mark.parametrize("frame_environment", ["SANDBOX", "LIVE"])
def test_reference_protection_is_version_exact_and_not_environment_filtered(
    database, frame_environment
):
    key = seed(database)
    with database() as conn:
        add_frames(conn, key, environment=frame_environment, version=1)
    migrate(database)
    assert invoke(database, True)["diagnostic_removed"] == 1
    assert versions(database, key) == [1, 3]


CANDIDATE_QUERY = """SELECT h.event_id FROM v2_state_history h
JOIN v2_business_state b ON b.state_id=h.state_id
WHERE h.created_at<transaction_timestamp()-interval '30 days' AND h.version<b.version
  AND b.scope->>'environment'='SANDBOX'
  AND h.reason IN ('HOST_DISK_ASSESSMENT','TESTNET_TRADING_DAEMON_CYCLE','TRADING_BUSINESS_HEALTH','TRADING_PIPELINE_CYCLE')
  AND (b.scope->>'namespace',h.reason) IN (
    ('host-disk-health-v1','HOST_DISK_ASSESSMENT'),
    ('testnet-trading-daemon-v1','TESTNET_TRADING_DAEMON_CYCLE'),
    ('trading-health-v1','TRADING_BUSINESS_HEALTH'),
    ('trading-pipeline-cycle-v1','TRADING_PIPELINE_CYCLE'))
  AND NOT EXISTS(SELECT FROM v2_s3_frames f WHERE (f.state_id,f.state_version)=(h.state_id,h.version))
ORDER BY h.created_at,h.event_id LIMIT 200 FOR UPDATE OF h SKIP LOCKED"""


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def test_representative_frame_history_uses_index_without_forcing_planner_and_fits_budget(
    database,
):
    candidate = seed(database)
    protected = seed(database)
    unrelated = seed(
        database,
        namespace="account-coverage-evidence-v1",
        reason="ACCOUNT_COVERAGE_AUDIT",
    )
    with database() as conn:
        add_frames(conn, protected, prefix="protected-")
        add_frames(conn, unrelated, count=24000, prefix="unrelated-")
    migrate(database)
    with database() as conn:
        for table in ("v2_s3_frames", "v2_state_history", "v2_business_state"):
            conn.execute("ANALYZE " + table)
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='500ms'")
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + CANDIDATE_QUERY).fetchone()[0][
            0
        ]["Plan"]
        scans = list(nodes(plan))
        assert any(node.get("Index Name") == INDEX for node in scans), plan
        assert not any(
            node.get("Relation Name") == "v2_s3_frames"
            and node["Node Type"] == "Seq Scan"
            for node in scans
        ), plan
        # Exercise the full deployed security-definer function, including each
        # immutable-history trigger's reference lookup, within its real budget.
        preview = conn.execute("SELECT v2_run_retention(false)").fetchone()[0]
        applied = conn.execute("SELECT v2_run_retention(true)").fetchone()[0]
        assert preview["diagnostic_candidates"] == applied["diagnostic_removed"] == 3
    assert versions(database, candidate) == [3]
    assert versions(database, protected) == [1, 3]
    assert versions(database, unrelated) == [1, 2, 3]
