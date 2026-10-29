"""Offline maintenance-boundary tests; never touch the configured deployment."""

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import v2_sandbox_rebuild as rebuild

TARGET = "trade_v2_rebuild_20261021_0123abcd"


class Result(list):
    def fetchone(self):
        return self[0] if self else None


class Source:
    def __init__(self, *, fault=None):
        self.fault = fault
        self.commands = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.commands.append((text, params))
        if text.startswith("SELECT current_database()"):
            identity = (
                rebuild.SOURCE,
                "16/tradev2",
                "",
                "Asia/Shanghai",
                "on",
                "postgres",
            )
            return Result([identity if self.fault != "identity" else ("wrong",) * 6])
        if "aclexplode" in text:
            return Result([("ubuntu", "CONNECT", False, "ubuntu")])
        if "FROM pg_db_role_setting" in text:
            return Result([(None, ["TimeZone=Asia/Shanghai"])])
        if "FROM pg_database" in text:
            if "pg_encoding_to_char" in text:
                return Result([("ubuntu", "UTF8", "C.UTF-8", "C.UTF-8", "c", None)])
            return Result([(1,)] if self.fault == "existing" else [])
        if text.startswith("SELECT tablename"):
            tables = set(rebuild.EXPECTED_TABLES)
            if self.fault == "schema":
                tables.add("v2_unclassified_data")
            return Result([(table,) for table in tables])
        if "FROM information_schema.columns" in text:
            return Result([("v2_tenant_accounts",), ("v2_trade_intents",)])
        if text.startswith("SELECT environment FROM"):
            failures = {"live": "LIVE", "unknown": "UNKNOWN", "null": None}
            return Result([(failures[self.fault],)] if self.fault in failures else [])
        if text.startswith("SELECT DISTINCT scope"):
            return Result(
                [
                    ("SANDBOX", "runtime-policy-v1"),
                    ("GLOBAL", "jev-advisory-day-budget-v1"),
                ]
            )
        if text.startswith("SELECT count(*)"):
            return Result([(2,)])
        if "pg_export_snapshot()" in text:
            return Result([("001-002-3",)])
        fault_queries = {
            "constraint": "FROM pg_constraint",
            "scoped": "FROM trade_v2.v2_income_imports",
            "risk": "FROM trade_v2.v2_risk_accounts",
            "live_reserve": "scope::jsonb->>'environment'='LIVE'",
            "operational": "FROM trade_v2.v2_operational_outbox",
            "capital": "FROM trade_v2.v2_capital_journals",
        }
        if self.fault in fault_queries and fault_queries[self.fault] in text:
            return Result([(1,)])
        return Result()


@pytest.mark.parametrize(
    "name",
    [
        rebuild.SOURCE,
        "postgres",
        "trade_v2_rebuild_20261021_0123ABCD",
        "trade_v2_rebuild_20261021_0123abcd;DROP DATABASE x",
        "",
        None,
        "trade_v2_rebuild_20261021_0123abcd\n",
        "trade_v2_rebuild_20261021_deadbeef.extra",
    ],
)
def test_target_cannot_name_source_or_inject_sql(name):
    with pytest.raises(rebuild.RebuildRejected, match="TARGET_NAME_NOT_ALLOWED"):
        rebuild.validate_target(name)


def test_inventory_is_exhaustive_and_disjoint():
    assert len(rebuild.EXPECTED_TABLES) == 63
    assert not set(rebuild.SEED_TABLES) & rebuild.EMPTY_TABLES
    assert "v2_credential_vault" in rebuild.SEED_TABLES
    assert "v2_execution_routes" in rebuild.EMPTY_TABLES
    assert "v2_egress_budgets" in rebuild.SEED_TABLES
    assert "v2_public_market_budgets" in rebuild.SEED_TABLES


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("identity", "SOURCE_IDENTITY_MISMATCH"),
        ("existing", "TARGET_ALREADY_EXISTS"),
        ("schema", "SCHEMA_TABLE_ALLOWLIST_MISMATCH"),
        ("constraint", "UNVALIDATED_SOURCE_CONSTRAINT"),
        ("live", "LIVE_BUSINESS_ROWS_PRESENT"),
        ("unknown", "UNKNOWN_TABLE_ENVIRONMENT"),
        ("null", "UNKNOWN_TABLE_ENVIRONMENT"),
        ("scoped", "NON_SANDBOX_SCOPED_RUNTIME"),
        ("risk", "UNKNOWN_RISK_ENVIRONMENT"),
        ("live_reserve", "LIVE_RISK_RESERVATION_PRESENT"),
        ("operational", "NON_SANDBOX_OPERATIONAL_ROWS"),
        ("capital", "LIVE_CAPITAL_ROWS_PRESENT"),
    ],
)
def test_preflight_rejects_unhandled_data(fault, reason):
    with pytest.raises(rebuild.RebuildRejected, match=reason):
        rebuild.inspect_source(Source(fault=fault), TARGET)


def test_default_plan_is_select_only_and_explicitly_not_cutover_acceptance():
    source = Source()
    plan = rebuild.inspect_source(source, TARGET)
    assert plan["status"] == "PLAN_ONLY"
    assert plan["source_mutated"] is False
    assert plan["cutover_authorized"] is False
    assert plan["venue_flat_verified"] is False
    assert plan["backup_restore_verified"] is False
    assert all(statement.startswith("SELECT ") for statement, _ in source.commands)


@pytest.mark.parametrize(
    "row,reason",
    [
        (("UNKNOWN", "runtime-policy-v1"), "UNKNOWN_STATE_ENVIRONMENT"),
        ((None, "runtime-policy-v1"), "UNKNOWN_STATE_ENVIRONMENT"),
        (("GLOBAL", "new-policy-v2"), "UNKNOWN_GLOBAL_STATE"),
        (("LIVE", "account-drawdown"), "LIVE_BUSINESS_STATE_PRESENT"),
        (("SANDBOX", "new-risk-control-v8"), "UNKNOWN_SANDBOX_STATE_NAMESPACE"),
        (
            ("SANDBOX", "innocent-name-but-unclassified"),
            "UNKNOWN_SANDBOX_STATE_NAMESPACE",
        ),
    ],
)
def test_unknown_state_never_silently_discarded(row, reason):
    with pytest.raises(rebuild.RebuildRejected, match=reason):
        rebuild.validate_namespaces([row])


def test_every_known_control_and_full_history_preserved():
    for namespace in rebuild.CONFIG_NAMESPACES:
        rebuild.validate_namespaces([("SANDBOX", namespace), ("LIVE", namespace)])
    for namespace in rebuild.GLOBAL_NAMESPACES:
        rebuild.validate_namespaces([("GLOBAL", namespace)])
    queries = rebuild.seed_queries()
    assert "shared-market-policy" in queries["v2_business_state"]
    assert "liquidity-controls-v1" in queries["v2_business_state"]
    assert "symbol-opening-quarantine-v1" in queries["v2_business_state"]
    assert "jev-advisory-day-budget-v1" in queries["v2_business_state"]
    assert "SELECT h.*" in queries["v2_state_history"]
    assert "USING(state_id)" in queries["v2_state_history"]
    assert "h.version=" not in queries["v2_state_history"]
    assert "NOT deleted" not in queries["v2_business_state"]


def test_pg_tools_do_not_inherit_credentials_or_print_errors(monkeypatch):
    calls = []
    monkeypatch.setenv("PGPASSWORD", "not-a-real-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "not-a-real-secret")
    monkeypatch.setattr(
        rebuild.subprocess,
        "run",
        lambda command, **kw: (
            calls.append((command, kw)) or SimpleNamespace(returncode=1)
        ),
    )
    with pytest.raises(rebuild.RebuildRejected, match="POSTGRES_TOOL_FAILED"):
        rebuild.run_pg(["pg_restore"])
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 300


def test_copy_refuses_non_allowlisted_table_or_query():
    with pytest.raises(rebuild.RebuildRejected, match="COPY_NOT_ALLOWLISTED"):
        rebuild.copy_seed(None, None, "v2_orders", "SELECT * FROM trade_v2.v2_orders")
    with pytest.raises(rebuild.RebuildRejected, match="COPY_NOT_ALLOWLISTED"):
        rebuild.copy_seed(
            None,
            None,
            "v2_credential_vault",
            "DELETE FROM trade_v2.v2_credential_vault",
        )


def test_failed_candidate_is_fenced_never_deleted_or_switched(monkeypatch):
    source = Source()
    plan = rebuild.inspect_source(source, TARGET)
    admin = Source()
    commands = []

    def run(arguments):
        commands.append(arguments)
        if "pg_dump" in arguments[0]:
            Path(arguments[-1]).write_bytes(b"schema-only-test-fixture")
        else:
            raise rebuild.RebuildRejected("POSTGRES_TOOL_FAILED")

    monkeypatch.setattr(rebuild, "run_pg", run)
    with pytest.raises(rebuild.RebuildRejected, match="POSTGRES_TOOL_FAILED"):
        rebuild.build_candidate(
            source,
            target=TARGET,
            plan=plan,
            connect=lambda *args, **kwargs: nullcontext(admin),
        )
    sql = "\n".join(statement for statement, _ in admin.commands)
    assert "CREATE DATABASE" in sql
    assert "CONNECTION LIMIT 0" in sql
    assert "ALLOW_CONNECTIONS false" in sql
    assert "DROP DATABASE" not in sql
    assert "RENAME" not in sql
    assert "--snapshot" in commands[0]
    assert "--schema-only" in commands[0]
    assert all(statement.startswith("SELECT ") for statement, _ in source.commands)


def test_plan_change_prevents_any_candidate_creation():
    source = Source()
    plan = rebuild.inspect_source(source, TARGET)
    plan["seed_counts"]["v2_credential_vault"] += 1
    with pytest.raises(rebuild.RebuildRejected, match="PLAN_CHANGED"):
        rebuild.build_candidate(source, target=TARGET, plan=plan, connect=None)


def test_cli_has_no_database_path_source_or_cutover_override():
    text = Path(rebuild.__file__).read_text()
    assert 'add_argument("--target"' in text
    assert 'add_argument("--build-candidate"' in text
    assert 'add_argument("--source"' not in text
    assert 'add_argument("--dsn"' not in text
    assert "DROP DATABASE" not in text
    assert "RENAME TO" not in text
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in text


def test_pg_small_fixture_schema_seed_acl_constraints_and_source_untouched(monkeypatch):
    """Real pg_dump/pg_restore/COPY, only on the disposable QA Unix socket.

    Uses repository core+vault DDL and a small row set. Only deployment identity
    and the all-63-table preflight are replaced; production CLI has no overrides.
    This is NOT an acceptance of restoring the 86 GB deployed database.
    """
    dsn = os.environ.get("V2_CORE_TEST_DSN")
    if not dsn or os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires explicit isolated QA database")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from v2_core.state import BusinessState, StateKey

    params = conninfo_to_dict(dsn)
    if not params.get("host", "").startswith(
        ("/tmp/v2-data-qa.", "/tmp/codex-v2-data-qa.")
    ):
        pytest.fail("refuse non-isolated PostgreSQL")
    suffix = uuid4().hex[:8]
    source_name = "v2_rebuild_fixture_" + suffix
    target = "trade_v2_rebuild_20261021_" + suffix
    owner = "v2_rebuild_owner_" + suffix
    reader = "v2_rebuild_reader_" + suffix
    root = Path(__file__).resolve().parents[2]

    def connect(database, **kwargs):
        return psycopg.connect(make_conninfo(dsn, dbname=database), **kwargs)

    with connect("postgres", autocommit=True) as admin:
        assert admin.execute("SHOW cluster_name").fetchone()[0] == "v2_isolated_qa"
        actual_user = admin.execute("SELECT current_user").fetchone()[0]
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(owner)))
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(reader)))
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                sql.Identifier(source_name), sql.Identifier(owner)
            )
        )
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET TimeZone='Asia/Shanghai'").format(
                sql.Identifier(source_name)
            )
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(source_name), sql.Identifier(reader)
            )
        )
    try:
        with connect(source_name) as source:
            source.execute("CREATE SCHEMA trade_v2")
            source.execute("SET search_path TO trade_v2,pg_catalog")
            for relative in (
                "db/postgres_v2_core_schema.sql",
                "db/migrations/20260925_tenant_registry.sql",
                "db/migrations/20260926_credential_vault.sql",
            ):
                source.execute((root / relative).read_text())
            source.execute(
                "CREATE FUNCTION trade_v2.v2_qa_security_definer() RETURNS integer "
                "LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS 'SELECT 1'"
            )
            source.execute(
                sql.SQL("GRANT SELECT ON trade_v2.v2_tenant_accounts TO {}").format(
                    sql.Identifier(reader)
                )
            )
            source.execute(
                sql.SQL("GRANT USAGE ON SCHEMA trade_v2 TO {}").format(
                    sql.Identifier(reader)
                )
            )
            tenant = uuid4()
            source.execute(
                "INSERT INTO trade_v2.v2_tenants(tenant_id,display_name) VALUES(%s,'QA only')",
                (tenant,),
            )
            for environment, nonce in (("LIVE", b"L" * 12), ("SANDBOX", b"S" * 12)):
                credential, registry = uuid4(), uuid4()
                source.execute(
                    "INSERT INTO trade_v2.v2_credential_vault(credential_ref,tenant_id,environment,key_id,nonce,ciphertext) "
                    "VALUES(%s,%s,%s,'qa-key',%s,%s)",
                    (credential, tenant, environment, nonce, bytes(range(64))),
                )
                source.execute(
                    "INSERT INTO trade_v2.v2_tenant_accounts(registry_id,tenant_id,exchange,account_id,environment,"
                    "product,display_name,credential_ref,version,status) "
                    "VALUES(%s,%s,'BINANCE',%s,%s,'FUTURES','QA account',%s,1,'ENROLLED_UNVERIFIED')",
                    (registry, tenant, "qa-" + environment, environment, credential),
                )
            source.execute(
                "INSERT INTO trade_v2.v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
                "VALUES(%s,'tradingview','SANDBOX','qa-old-signal','{}')",
                (uuid4(),),
            )
            actual_tables = {
                r[0]
                for r in source.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='trade_v2'"
                )
            }

        def app_connect():
            return psycopg.connect(
                make_conninfo(dsn, dbname=source_name),
                options="-c search_path=trade_v2,pg_catalog",
            )

        states = BusinessState(app_connect)
        scope = {
            "exchange": "BINANCE",
            "account_id": "qa-only",
            "environment": "SANDBOX",
            "product": "FUTURES",
        }
        config_key = StateKey(**scope, namespace="shared-market-policy", key="current")
        for version in (0, 1):
            states.change(
                config_key,
                expected_version=version,
                request_key=f"qa-{version}",
                payload={"interval_ms": 5000 + version},
                reason="QA_CONFIG",
            )
        states.change(
            StateKey(**scope, namespace="account-drawdown", key="current"),
            expected_version=0,
            request_key="old-runtime",
            payload={"loss": "1"},
            reason="QA_RUNTIME",
        )
        seed_tables = ("v2_tenants", "v2_credential_vault", "v2_tenant_accounts")
        monkeypatch.setattr(rebuild, "SOURCE", source_name)
        monkeypatch.setattr(rebuild, "HOST", params["host"])
        monkeypatch.setattr(rebuild, "PORT", int(params["port"]))
        monkeypatch.setattr(rebuild, "PG_USER", actual_user)
        monkeypatch.setattr(rebuild, "SEED_TABLES", seed_tables)
        monkeypatch.setattr(
            rebuild,
            "EMPTY_TABLES",
            actual_tables
            - set(seed_tables)
            - {"v2_business_state", "v2_state_history"},
        )

        def fixture_plan(source, target_name):
            return {
                "target": target_name,
                "seed_counts": {
                    table: source.execute(
                        "SELECT count(*) FROM (" + query + ") seed"
                    ).fetchone()[0]
                    for table, query in rebuild.seed_queries().items()
                },
                "database_properties": rebuild.database_properties(source),
            }

        monkeypatch.setattr(rebuild, "inspect_source", fixture_plan)
        with connect(source_name) as source:
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            plan = fixture_plan(source, target)
            result = rebuild.build_candidate(
                source, target=target, plan=plan, connect=connect
            )
        assert result["status"] == "CANDIDATE_BUILT_FENCED"
        with connect("postgres", autocommit=True) as admin:
            assert admin.execute(
                "SELECT datallowconn,datconnlimit FROM pg_database WHERE datname=%s",
                (target,),
            ).fetchone() == (False, 0)
            admin.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                    sql.Identifier(target)
                )
            )
        with connect(target) as candidate:
            assert rebuild.database_properties(candidate) == plan["database_properties"]
            assert (
                candidate.execute(
                    "SELECT count(*) FROM trade_v2.v2_business_state"
                ).fetchone()[0]
                == 1
            )
            assert (
                candidate.execute(
                    "SELECT count(*) FROM trade_v2.v2_state_history"
                ).fetchone()[0]
                == 2
            )
            assert (
                candidate.execute(
                    "SELECT count(*) FROM trade_v2.v2_inbound_signals"
                ).fetchone()[0]
                == 0
            )
            assert (
                candidate.execute(
                    "SELECT count(*) FROM trade_v2.v2_tenant_accounts WHERE environment='LIVE'"
                ).fetchone()[0]
                == 1
            )
            assert candidate.execute(
                "SELECT ciphertext FROM trade_v2.v2_credential_vault LIMIT 1"
            ).fetchone()[0] == bytes(range(64))
            assert candidate.execute(
                "SELECT prosecdef FROM pg_proc WHERE oid='trade_v2.v2_qa_security_definer()'::regprocedure"
            ).fetchone()[0]
            assert candidate.execute(
                "SELECT has_table_privilege(%s,'trade_v2.v2_tenant_accounts','SELECT')",
                (reader,),
            ).fetchone()[0]
            assert not candidate.execute(
                "SELECT has_table_privilege(%s,'trade_v2.v2_credential_vault','SELECT')",
                (reader,),
            ).fetchone()[0]
            with pytest.raises(psycopg.Error), candidate.transaction():
                candidate.execute(
                    "UPDATE trade_v2.v2_credential_vault SET ciphertext=repeat('x',64)::bytea"
                )
            with pytest.raises(psycopg.Error), candidate.transaction():
                candidate.execute(
                    "INSERT INTO trade_v2.v2_state_history(event_id,state_id,request_key,expected_version,version,payload,deleted,reason) "
                    "VALUES(%s,%s,'orphan',0,1,'{}',false,'QA')",
                    (uuid4(), uuid4()),
                )
        with connect(source_name) as source:
            assert (
                source.execute(
                    "SELECT count(*) FROM trade_v2.v2_business_state"
                ).fetchone()[0]
                == 2
            )
            assert (
                source.execute(
                    "SELECT count(*) FROM trade_v2.v2_state_history"
                ).fetchone()[0]
                == 3
            )
            assert (
                source.execute(
                    "SELECT count(*) FROM trade_v2.v2_inbound_signals"
                ).fetchone()[0]
                == 1
            )
    finally:
        with connect("postgres", autocommit=True) as admin:
            for name in (target, source_name):
                if admin.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
                ).fetchone():
                    admin.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
            for role in (reader, owner):
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
