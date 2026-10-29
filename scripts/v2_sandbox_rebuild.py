"""Build a fenced configuration-only SANDBOX candidate; never switch databases.

Default is read-only planning. The source must be the fixed local deployment.
This deliberately refuses LIVE business data: preserving an arbitrary live
dependency closure is not an operation this one-off maintenance tool supports.
No source writes, trading requests, service changes, or DROP statements exist.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SOURCE = "trade_v2_testnet"
SCHEMA = "trade_v2"
HOST = "/var/run/postgresql"
PORT = 55432
PG_USER = "postgres"
PG_BIN = Path("/usr/lib/postgresql/16/bin")
TARGET_PATTERN = re.compile(r"trade_v2_rebuild_20\d{6}_[a-f0-9]{8}\Z")
CONFIG_NAMESPACES = frozenset(
    {
        "runtime-policy-v1",
        "entry-controls-v1",
        "capital-drawdown-control-v1",
        "jev-advisory-control-v1",
        "jev-entry-gate-policy-v1",
        "shared-market-policy",
        "liquidity-controls-v1",
        "symbol-opening-quarantine-v1",
    }
)
GLOBAL_NAMESPACES = frozenset(
    {
        "jev-advisory-day-budget-v1",
        "jev-advisory-minute-budget-v1",
    }
)
RUNTIME_NAMESPACES = frozenset(
    {
        "account-coverage-evidence-v1",
        "account-coverage-v1",
        "account-drawdown",
        "account-inventory-v1",
        "capital-snapshot-v1",
        "directional-account-context-v1",
        "directional-cash-audit-v1",
        "directional-exit-v1",
        "directional-protection-stage-v1",
        "episode-opening-halt-v1",
        "external-close-resolution-v1",
        "host-disk-health-v1",
        "jev-advisory-request-v1",
        "jev-entry-check-v1",
        "maintenance-adoption-v1",
        "maintenance-resolution-v1",
        "native-child-owner-v1",
        "opening-readiness-probe-v1",
        "opening-readiness-v1",
        "order-recovery-diagnostic-v1",
        "protection-recovery-result-v1",
        "protection-supervisor-v1",
        "range-volume-study-v1",
        "s3-lifecycle",
        "sandbox-lifecycle-acceptance-v1",
        "telegram-pin-v1",
        "testnet-acceptance-v1",
        "testnet-maintenance-v1",
        "testnet-protection-v1",
        "testnet-roundtrip-v1",
        "testnet-roundtrip-verification-v1",
        "testnet-scoped-cleanup-v1",
        "testnet-trading-daemon-v1",
        "trading-health-v1",
        "trading-pipeline-cycle-v1",
        "venue-symbol-settings-v1",
        "watchdog-delivery-v1",
    }
)
SEED_TABLES = (
    "v2_tenants",
    "v2_credential_vault",
    "v2_tenant_accounts",
    "v2_account_aliases",
    "v2_account_alias_events",
    "v2_account_retirements",
    "v2_registry_events",
    "v2_credential_checks",
    "v2_policy_console_events",
    "v2_risk_recovery_events",
    "v2_risk_accounts",
    "v2_risk_policies",
    "v2_retention_policy",
    "v2_egress_budgets",
    "v2_public_market_budgets",
)
EMPTY_TABLES = frozenset(
    {
        "v2_capital_entries",
        "v2_capital_journals",
        "v2_cash_adjustments",
        "v2_consumer_receipts",
        "v2_coverage_blobs",
        "v2_decision_evidence",
        "v2_delivery_attempts",
        "v2_directional_followups",
        "v2_directional_outcomes",
        "v2_domain_outbox",
        "v2_episodes",
        "v2_exchange_income",
        "v2_execution_controller_events",
        "v2_execution_route_events",
        "v2_execution_routes",
        "v2_fill_observations",
        "v2_fills",
        "v2_fx_valuations",
        "v2_inbound_signals",
        "v2_income_allocations",
        "v2_income_imports",
        "v2_market_catalog",
        "v2_market_demands",
        "v2_market_hub_status",
        "v2_market_symbol_heads",
        "v2_operational_attempts",
        "v2_operational_outbox",
        "v2_operational_receipts",
        "v2_order_events",
        "v2_order_recovery",
        "v2_orders",
        "v2_producer_batches",
        "v2_reentry_observations",
        "v2_reentry_observer_budget",
        "v2_reentry_observer_claims",
        "v2_reentry_observer_state",
        "v2_retention_runs",
        "v2_risk_reservations",
        "v2_s3_frames",
        "v2_settlements",
        "v2_signal_receipts",
        "v2_strategy_decisions",
        "v2_strategy_tasks",
        "v2_trade_intents",
        "v2_candle_deliveries",
        "v2_candle_delivery_events",
    }
)
EXPECTED_TABLES = (
    frozenset(SEED_TABLES)
    | EMPTY_TABLES
    | {
        "v2_business_state",
        "v2_state_history",
    }
)


class RebuildRejected(ValueError):
    """Fixed error codes only: never leak account or credential records."""


def validate_target(value):
    if not isinstance(value, str) or not TARGET_PATTERN.fullmatch(value):
        raise RebuildRejected("TARGET_NAME_NOT_ALLOWED")
    return value


def validate_namespaces(rows):
    """Return only explicitly supported configuration state identities."""
    for environment, namespace in rows:
        if environment not in {"SANDBOX", "LIVE", "GLOBAL"}:
            raise RebuildRejected("UNKNOWN_STATE_ENVIRONMENT")
        if environment == "GLOBAL" and namespace not in GLOBAL_NAMESPACES:
            raise RebuildRejected("UNKNOWN_GLOBAL_STATE")
        if environment == "LIVE" and namespace not in CONFIG_NAMESPACES:
            raise RebuildRejected("LIVE_BUSINESS_STATE_PRESENT")
        if (
            environment == "SANDBOX"
            and namespace not in CONFIG_NAMESPACES | RUNTIME_NAMESPACES
        ):
            raise RebuildRejected("UNKNOWN_SANDBOX_STATE_NAMESPACE")


def seed_queries():
    """Static SQL only; neither user names nor account values become SQL."""
    quotes = lambda items: ",".join("'" + item + "'" for item in sorted(items))
    predicate = (
        "(scope->>'environment' IN ('SANDBOX','LIVE') AND "
        f"scope->>'namespace' IN ({quotes(CONFIG_NAMESPACES)})) OR "
        "(scope->>'environment'='GLOBAL' AND "
        f"scope->>'namespace' IN ({quotes(GLOBAL_NAMESPACES)}))"
    )
    selected = f"SELECT * FROM trade_v2.v2_business_state WHERE {predicate}"
    queries = {table: f"SELECT * FROM trade_v2.{table}" for table in SEED_TABLES}
    queries["v2_business_state"] = selected
    queries["v2_state_history"] = (
        "SELECT h.* FROM trade_v2.v2_state_history h JOIN ("
        + selected
        + ") s USING(state_id)"
    )
    return queries


def inspect_source(conn, target):
    validate_target(target)
    identity = conn.execute(
        "SELECT current_database(),current_setting('cluster_name'),"
        "current_setting('listen_addresses'),current_setting('TimeZone'),"
        "current_setting('transaction_read_only'),current_user"
    ).fetchone()
    if identity != (SOURCE, "16/tradev2", "", "Asia/Shanghai", "on", "postgres"):
        raise RebuildRejected("SOURCE_IDENTITY_MISMATCH")
    if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,)).fetchone():
        raise RebuildRejected("TARGET_ALREADY_EXISTS")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='trade_v2'"
        )
    }
    if tables != EXPECTED_TABLES:
        raise RebuildRejected("SCHEMA_TABLE_ALLOWLIST_MISMATCH")
    if conn.execute(
        "SELECT 1 FROM pg_constraint WHERE connamespace='trade_v2'::regnamespace "
        "AND contype IN ('f','c') AND NOT convalidated LIMIT 1"
    ).fetchone():
        raise RebuildRejected("UNVALIDATED_SOURCE_CONSTRAINT")
    # One bounded-result violation probe per table, not DISTINCT/GROUP BY or
    # two full scans for LIVE + unknown values. Absence may still require a
    # table scan without a suitable index; do not substitute sampling.
    for (table,) in conn.execute(
        "SELECT table_name FROM information_schema.columns WHERE "
        "table_schema='trade_v2' AND column_name='environment' "
        "AND table_name IN (SELECT tablename FROM pg_tables WHERE schemaname='trade_v2')"
    ):
        if table not in EXPECTED_TABLES:
            raise RebuildRejected("UNRECOGNIZED_ENVIRONMENT_TABLE")
        predicate = (
            "environment IS NULL OR environment NOT IN ('SANDBOX','LIVE')"
            if table in SEED_TABLES
            else "environment IS DISTINCT FROM 'SANDBOX'"
        )
        violation = conn.execute(
            f"SELECT environment FROM trade_v2.{table} WHERE {predicate} LIMIT 1"
        ).fetchone()
        if violation:
            if violation[0] == "LIVE":
                raise RebuildRejected("LIVE_BUSINESS_ROWS_PRESENT")
            raise RebuildRejected("UNKNOWN_TABLE_ENVIRONMENT")
    validate_namespaces(
        conn.execute(
            "SELECT DISTINCT scope->>'environment',scope->>'namespace' "
            "FROM trade_v2.v2_business_state"
        )
    )
    for table in ("v2_income_imports", "v2_reentry_observations"):
        if conn.execute(
            f"SELECT 1 FROM trade_v2.{table} WHERE "
            "scope->>'environment' IS DISTINCT FROM 'SANDBOX' LIMIT 1"
        ).fetchone():
            raise RebuildRejected("NON_SANDBOX_SCOPED_RUNTIME")
    for table in ("v2_risk_accounts", "v2_risk_policies", "v2_risk_reservations"):
        if conn.execute(
            f"SELECT 1 FROM trade_v2.{table} WHERE "
            "scope::jsonb->>'environment' IS NULL OR "
            "scope::jsonb->>'environment' NOT IN ('SANDBOX','LIVE') LIMIT 1"
        ).fetchone():
            raise RebuildRejected("UNKNOWN_RISK_ENVIRONMENT")
        if (
            table == "v2_risk_reservations"
            and conn.execute(
                f"SELECT 1 FROM trade_v2.{table} WHERE "
                "scope::jsonb->>'environment'='LIVE' LIMIT 1"
            ).fetchone()
        ):
            raise RebuildRejected("LIVE_RISK_RESERVATION_PRESENT")
    if conn.execute(
        "SELECT 1 FROM trade_v2.v2_operational_outbox "
        "WHERE scope_id IS DISTINCT FROM 'SANDBOX' LIMIT 1"
    ).fetchone():
        raise RebuildRejected("NON_SANDBOX_OPERATIONAL_ROWS")
    # Capital journals lack an environment column. Do not erase LIVE cash history.
    if conn.execute(
        "SELECT 1 FROM trade_v2.v2_capital_journals j JOIN "
        "trade_v2.v2_tenant_accounts a USING(tenant_id,registry_id) "
        "WHERE a.environment IS DISTINCT FROM 'SANDBOX' LIMIT 1"
    ).fetchone():
        raise RebuildRejected("LIVE_CAPITAL_ROWS_PRESENT")
    counts = {
        table: conn.execute("SELECT count(*) FROM (" + query + ") seed").fetchone()[0]
        for table, query in seed_queries().items()
    }
    return {
        "status": "PLAN_ONLY",
        "source": SOURCE,
        "target": target,
        "seed_counts": counts,
        "empty_tables": sorted(EMPTY_TABLES),
        "source_mutated": False,
        "cutover_authorized": False,
        "live_business_migration_supported": False,
        "venue_flat_verified": False,
        "backup_restore_verified": False,
        "database_properties": database_properties(conn),
    }


def database_properties(conn):
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba),pg_encoding_to_char(encoding),"
        "datcollate,datctype,datlocprovider,daticulocale FROM pg_database "
        "WHERE datname=current_database()"
    ).fetchone()
    if row is None or row[4] != "c" or row[5] is not None:
        raise RebuildRejected("DATABASE_LOCALE_PROVIDER_NOT_SUPPORTED")
    acl = [
        list(r)
        for r in conn.execute(
            "SELECT CASE WHEN a.grantee=0 THEN NULL ELSE pg_get_userbyid(a.grantee) END,"
            "a.privilege_type,a.is_grantable,pg_get_userbyid(a.grantor) "
            "FROM pg_database d CROSS JOIN LATERAL "
            "aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a "
            "WHERE d.datname=current_database() "
            "ORDER BY a.grantee,a.privilege_type,a.grantor"
        )
    ]
    if any(
        r[1] not in {"CREATE", "CONNECT", "TEMPORARY"} or r[3] != row[0] for r in acl
    ):
        raise RebuildRejected("DATABASE_ACL_NOT_SUPPORTED")
    settings = []
    for role, values in conn.execute(
        "SELECT CASE WHEN setrole=0 THEN NULL ELSE pg_get_userbyid(setrole) END,"
        "setconfig FROM pg_db_role_setting WHERE setdatabase="
        "(SELECT oid FROM pg_database WHERE datname=current_database())"
    ):
        # Unexpected connection behavior must be reviewed, not silently lost.
        if role is not None or values != ["TimeZone=Asia/Shanghai"]:
            raise RebuildRejected("DATABASE_SETTINGS_NOT_SUPPORTED")
        settings.extend(values)
    return {
        "owner": row[0],
        "encoding": row[1],
        "collate": row[2],
        "ctype": row[3],
        "locale_provider": row[4],
        "acl": acl,
        "settings": sorted(settings),
    }


def apply_database_acl(admin, target, properties):
    from psycopg import sql

    admin.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(target))
    )
    for grantee, privilege, grantable, grantor in properties["acl"]:
        if (
            privilege not in {"CREATE", "CONNECT", "TEMPORARY"}
            or grantor != properties["owner"]
        ):
            raise RebuildRejected("DATABASE_ACL_NOT_SUPPORTED")
        role = sql.SQL("PUBLIC") if grantee is None else sql.Identifier(grantee)
        # The owner is also the original ACL grantor; explicit SET ROLE retains
        # the grantor identity instead of creating postgres-owned grants.
        admin.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(grantor)))
        try:
            admin.execute(
                sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                    sql.SQL(privilege),
                    sql.Identifier(target),
                    role,
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )
        finally:
            admin.execute("RESET ROLE")


def run_pg(arguments):
    # Do not inherit libpq passwords, service files, search_path, or preload hooks.
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    result = subprocess.run(
        arguments, env=env, capture_output=True, timeout=300, check=False
    )
    if result.returncode:
        raise RebuildRejected("POSTGRES_TOOL_FAILED")


def copy_seed(source, destination, table, query):
    if table not in seed_queries() or query != seed_queries()[table]:
        raise RebuildRejected("COPY_NOT_ALLOWLISTED")
    # Binary COPY streams encrypted vault bytes directly; no record files/logging.
    checksum = hashlib.sha256()
    with (
        source.cursor().copy(copy_out_sql(query)) as reader,
        destination.cursor().copy(
            f"COPY trade_v2.{table} FROM STDIN (FORMAT BINARY)"
        ) as writer,
    ):
        for block in reader:
            checksum.update(block)
            writer.write(block)
    return checksum.hexdigest()


def copy_out_sql(query):
    return (
        "COPY (SELECT * FROM (" + query + ") seeded "
        'ORDER BY row_to_json(seeded)::text COLLATE "C") TO STDOUT (FORMAT BINARY)'
    )


def table_checksum(conn, table):
    if table not in seed_queries():
        raise RebuildRejected("CHECKSUM_NOT_ALLOWLISTED")
    checksum = hashlib.sha256()
    with conn.cursor().copy(copy_out_sql(f"SELECT * FROM trade_v2.{table}")) as rows:
        for block in rows:
            checksum.update(block)
    return checksum.hexdigest()


def build_candidate(source, *, target, plan, connect):
    """Source is already in a fixed read-only repeatable-read transaction."""
    from psycopg import sql

    validate_target(target)
    if plan != inspect_source(source, target):
        raise RebuildRejected("PLAN_CHANGED")
    snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
    common = ["--host", HOST, "--port", str(PORT), "--username", PG_USER]
    with tempfile.TemporaryDirectory(prefix="v2-sandbox-schema-") as directory:
        os.chmod(directory, 0o700)
        archive = Path(directory) / "schema.dump"
        run_pg(
            [
                str(PG_BIN / "pg_dump"),
                *common,
                "--dbname",
                SOURCE,
                "--schema",
                SCHEMA,
                "--schema-only",
                "--format=custom",
                "--snapshot",
                snapshot,
                "--file",
                str(archive),
            ]
        )
        os.chmod(archive, 0o600)
        schema_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        # A CREATE DATABASE race fails closed. Existing targets are never reused.
        with connect("postgres", autocommit=True) as admin:
            properties = plan["database_properties"]
            admin.execute(
                sql.SQL(
                    "CREATE DATABASE {} WITH TEMPLATE template0 OWNER {} "
                    "ENCODING {} LOCALE_PROVIDER libc LC_COLLATE {} LC_CTYPE {} CONNECTION LIMIT 0"
                ).format(
                    sql.Identifier(target),
                    sql.Identifier(properties["owner"]),
                    sql.Literal(properties["encoding"]),
                    sql.Literal(properties["collate"]),
                    sql.Literal(properties["ctype"]),
                )
            )
        # On failure retain only this incomplete new database for inspection.
        # It never has the deployment name, and services are never reconfigured.
        try:
            with connect("postgres", autocommit=True) as admin:
                apply_database_acl(admin, target, properties)
            run_pg(
                [
                    str(PG_BIN / "pg_restore"),
                    *common,
                    "--dbname",
                    target,
                    "--exit-on-error",
                    "--section=pre-data",
                    str(archive),
                ]
            )
            with connect(target) as candidate:
                checksums = {}
                for table, query in seed_queries().items():
                    checksums[table] = copy_seed(source, candidate, table, query)
            run_pg(
                [
                    str(PG_BIN / "pg_restore"),
                    *common,
                    "--dbname",
                    target,
                    "--exit-on-error",
                    "--section=post-data",
                    str(archive),
                ]
            )
            with connect(target) as candidate:
                for table, expected in plan["seed_counts"].items():
                    actual = candidate.execute(
                        f"SELECT count(*) FROM trade_v2.{table}"
                    ).fetchone()[0]
                    if actual != expected:
                        raise RebuildRejected("SEED_COUNT_MISMATCH")
                    if table_checksum(candidate, table) != checksums[table]:
                        raise RebuildRejected("SEED_CHECKSUM_MISMATCH")
                for table in EMPTY_TABLES:
                    if candidate.execute(
                        f"SELECT 1 FROM trade_v2.{table} LIMIT 1"
                    ).fetchone():
                        raise RebuildRejected("CANDIDATE_RUNTIME_NOT_EMPTY")
        finally:
            # Fence success AND partial failure. No application may use it until
            # a separate archived/venue-verified cutover explicitly enables it.
            with connect("postgres", autocommit=True) as admin:
                admin.execute(
                    sql.SQL("ALTER DATABASE {} SET TimeZone='Asia/Shanghai'").format(
                        sql.Identifier(target)
                    )
                )
                admin.execute(
                    sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                        sql.Identifier(target)
                    )
                )
        return {
            **plan,
            "status": "CANDIDATE_BUILT_FENCED",
            "schema_sha256": schema_digest,
            "seed_checksums": checksums,
            "target_connections_allowed": False,
        }


def main(argv=None):
    import psycopg

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--build-candidate", action="store_true")
    args = parser.parse_args(argv)
    validate_target(args.target)

    def connect(database, **kwargs):
        return psycopg.connect(
            host=HOST,
            port=PORT,
            dbname=database,
            user=PG_USER,
            connect_timeout=5,
            **kwargs,
        )

    with connect(SOURCE) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        source.execute("SET LOCAL statement_timeout='30s'")
        source.execute("SET LOCAL lock_timeout='2s'")
        plan = inspect_source(source, args.target)
        result = (
            build_candidate(source, target=args.target, plan=plan, connect=connect)
            if args.build_candidate
            else plan
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RebuildRejected as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}))
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 - database exceptions may contain credential rows
        print(json.dumps({"status": "FAILED", "reason": "REBUILD_FAILED_SAFE"}))
        raise SystemExit(2) from None
