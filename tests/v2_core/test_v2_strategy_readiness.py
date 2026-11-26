"""Read-only readiness checks; PG fixtures use only the isolated QA harness."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import pytest
from psycopg import sql
from test_v2_settlement_execution_fence import database as database_fixture
from test_v2_settlement_execution_fence import fenced as fenced_fixture
from test_v2_strategy_market_reader import CLOSED, MINUTE, Client, row

from services import v2_strategy_readiness as subject

ROOT = Path(__file__).resolve().parents[2]
database = database_fixture
fenced = fenced_fixture


def values(**changes):
    return {
        "V2_CONTROLLER_TENANT": str(uuid4()),
        "V2_ACCOUNT_ID": "readiness-qa",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_CONTROLLER_WORKER_MODE": "TYPED_STRATEGY",
        "V2_CONTROLLER_ENABLE_ENTRIES": "false",
        "V2_ENABLE_ENTRIES": "false",
        "V2_INTERVAL_SECONDS": "10",
        "V2_EXIT_FEE_RATE": "0.0004",
        **changes,
    }


class Market(Client):
    identity: ClassVar = [("tradev2strategy", "trade_v2_testnet")]

    def query(self, text, *, parameters, settings):
        if text == "SELECT currentUser(),currentDatabase()":
            return SimpleNamespace(result_rows=self.identity)
        return super().query(text, parameters=parameters, settings=settings)


def check(monkeypatch, **overrides):
    monkeypatch.setattr(subject, "_postgres", lambda *a, **k: [])
    return subject.check_readiness(
        values(),
        **{
            "connect": lambda: None,
            "importer": lambda name: None,
            "host_probe": lambda: dict.fromkeys(subject._HOST_KEYS, True),
            "clock_ms": lambda: CLOSED + 7,
            **overrides,
        },
    )


def test_paused_boot_is_not_entry_or_worker_or_venue_authorization(monkeypatch):
    result = check(monkeypatch)
    assert result["paused_boot_ready"] is True
    assert result["entry_ready"] is result["entry_data_ready"] is False
    assert result["execution_authorized"] is result["runtime_verified"] is False
    assert result["market"]["issues"] == [{"code": "CH_DEDICATED_READER_NOT_VERIFIED"}]


@pytest.mark.parametrize(
    "changes",
    [
        {"V2_CONTROLLER_ENABLE_ENTRIES": "true"},
        {"V2_ENABLE_ENTRIES": "true"},
        {"V2_ENVIRONMENT": "LIVE"},
        {"V2_CONTROLLER_WORKER_MODE": "LEGACY"},
        {"V2_CONTROLLER_TENANT": "private-tenant-value"},
        {"V2_TYPED_LEVERAGE": "20"},
        {"V2_EXIT_FEE_RATE": "nan"},
    ],
)
def test_paused_config_failures_do_not_return_values(changes):
    result = subject._config(values(**changes))
    assert result
    assert all(set(item) == {"code"} for item in result)
    assert "private-tenant-value" not in json.dumps(result)


def test_host_and_import_failures_are_specific_fixed_codes(monkeypatch):
    def bad_import(name):
        raise RuntimeError("secret-import-text")

    result = check(monkeypatch, importer=bad_import, host_probe=dict)
    assert result["paused_boot_ready"] is False
    assert "secret-import-text" not in json.dumps(result)
    assert {x["code"] for x in result["blockers"]} >= {
        "CONTROLLER_IMPORT_UNAVAILABLE",
        "HOST_POSTGRES_RUNNING_UNVERIFIED",
    }


def test_complete_closed_window_is_data_ready_only(monkeypatch):
    client = Market([row(CLOSED - (i + 1) * MINUTE) for i in range(1440)])
    result = check(monkeypatch, clickhouse=client, symbol="BTCUSDT")
    assert result["paused_boot_ready"] is result["entry_data_ready"] is True
    assert result["entry_ready"] is False
    assert len(client.calls) == 1
    assert client.calls[0][1]["end"] == CLOSED


def test_missing_closed_data_does_not_claim_controller_is_broken(monkeypatch):
    result = check(monkeypatch, clickhouse=Market([]), symbol="BTCUSDT")
    assert result["paused_boot_ready"] is True
    assert result["entry_data_ready"] is False
    assert result["market"]["profile_ready"] is True
    assert result["market"]["issues"] == [{"code": "CH_CLOSED_WINDOW_INCOMPLETE"}]


def test_wrong_or_mutable_readonly_profile_cannot_be_data_ready(monkeypatch):
    client = Market([], settings={"readonly": 0})
    result = check(monkeypatch, clickhouse=client, symbol="BTCUSDT")
    assert result["entry_data_ready"] is False
    assert result["market"]["issues"] == [{"code": "CH_READONLY_PROFILE_UNVERIFIED"}]
    client = Market([])
    client.identity = [("default", "trade_v2_testnet")]
    assert check(monkeypatch, clickhouse=client)["market"]["profile_ready"] is False


def test_host_probe_only_reads_unit_metadata_not_encrypted_files(monkeypatch):
    units = (
        ("trade-v2-testnet-daemon.service", "inactive"),
        ("trade-v2-testnet-watchdog.service", "inactive"),
        ("trade-v2-execution-controller.service", "inactive"),
        ("postgresql@16-tradev2.service", "active"),
        ("trade-v2-cache.service", "active"),
        ("trade-v2-clickhouse.service", "active"),
    )
    output = "\n\n".join(
        f"Id={unit}\nActiveState={state}\nLoadCredentialEncrypted="
        + (
            "v2-master-v1:private-value v2-notifications:private-value"
            if i == 2
            else ""
        )
        for i, (unit, state) in enumerate(units)
    )

    def invoke(command, **kwargs):
        assert command[0] == "systemctl"
        assert kwargs["timeout"] == 3
        assert "LoadCredentialEncrypted" in command
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(subject.subprocess, "run", invoke)
    result = subject._host_probe()
    assert all(result.values())
    assert "private-value" not in json.dumps(result)


def test_env_overlay_does_not_execute_or_export_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("V2_ENABLE_ENTRIES", "true")
    config = tmp_path / "paused.env"
    config.write_text(
        'V2_ENABLE_ENTRIES=false\nV2_MARKER="$(touch forbidden)"\nSECRET=hidden\n'
    )
    result = subject._values(config)
    assert result["V2_ENABLE_ENTRIES"] == "false"
    assert result["V2_MARKER"] == "$(touch forbidden)"
    assert "SECRET" not in result
    assert not (tmp_path / "forbidden").exists()


def test_unprintable_credential_property_falls_back_to_unit_labels_and_honors_reset(
    monkeypatch,
):
    units = "\n\n".join(
        "Id=" + name + "\nActiveState=" + state
        for name, state in (
            ("trade-v2-testnet-daemon.service", "inactive"),
            ("trade-v2-testnet-watchdog.service", "inactive"),
            ("trade-v2-execution-controller.service", "inactive"),
            ("postgresql@16-tradev2.service", "active"),
            ("trade-v2-cache.service", "active"),
            ("trade-v2-clickhouse.service", "active"),
        )
    )
    configured = "[Service]\nLoadCredentialEncrypted=v2-master-v1:opaque\nLoadCredentialEncrypted=v2-notifications:opaque"

    def invoke(command, **kwargs):
        assert kwargs["timeout"] == 3
        return SimpleNamespace(stdout=units if command[1] == "show" else configured)

    monkeypatch.setattr(subject.subprocess, "run", invoke)
    assert all(subject._host_probe().values())
    configured += "\n[Service]\nLoadCredentialEncrypted=\nLoadCredentialEncrypted=v2-master-v1:opaque"
    assert subject._host_probe()["encrypted_credentials_configured"] is False


def test_cli_wall_timeout_does_not_echo_child_diagnostics(monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            "secret-command", 5, output="secret-output", stderr="secret-stderr"
        )

    monkeypatch.setattr(subject.subprocess, "run", timeout)
    assert subject.main(["--timeout-seconds", "5"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["blockers"] == [{"code": "READINESS_WALL_TIMEOUT"}]


@pytest.fixture
def schema_case(fenced):
    """Actual restricted session roles, not metadata checked as superuser."""
    operator, vault = "qa_ready_op_" + uuid4().hex, "qa_ready_vault_" + uuid4().hex
    with fenced.admin() as conn:
        for name in (
            "20260926_credential_vault.sql",
            "20260929_shared_market.sql",
            "20261126_strategy_entry_candidates.sql",
            "20261126_entry_discovery_cursors.sql",
            "20261126_strategy_maintenance_cursors.sql",
            "20261126_strategy_scan_cursors.sql",
            "20261126_strategy_dispatch_cursors.sql",
        ):
            conn.execute((ROOT / "db/migrations" / name).read_text())
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(fenced.worker)
            )
        )
        for name in subject._ROLE_ONLY | {"v2_execution_routes"}:
            conn.execute(
                sql.SQL("REVOKE ALL ON {} FROM {}").format(
                    sql.Identifier(name), sql.Identifier(fenced.worker)
                )
            )
        conn.execute(
            sql.SQL("GRANT SELECT ON v2_execution_routes TO {}").format(
                sql.Identifier(fenced.worker)
            )
        )
        for role in (operator, vault):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                    sql.Identifier(role), sql.Identifier(fenced.worker)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
        for table, grants in subject._OPERATOR.items():
            conn.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(",".join(grants)),
                    sql.Identifier(table),
                    sql.Identifier(operator),
                )
            )
        conn.execute(
            sql.SQL("GRANT UPDATE(version) ON v2_tenant_accounts TO {}").format(
                sql.Identifier(operator)
            )
        )
        for table, grants in subject._VAULT.items():
            for permission, columns in grants.items():
                conn.execute(
                    sql.SQL("GRANT {} ({}) ON {} TO {}").format(
                        sql.SQL(permission),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.Identifier(table),
                        sql.Identifier(vault),
                    )
                )
    try:
        yield SimpleNamespace(
            fenced=fenced,
            operator=operator,
            vault=vault,
            probe=lambda: subject._postgres(
                fenced.restricted,
                guard_role=fenced.role,
                operator_role=operator,
                vault_role=vault,
            ),
        )
    finally:
        with fenced.admin() as conn:
            for role in (operator, vault):
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_pg_current_business_and_explicit_roles_pass_without_ambient_vault_or_route_writes(
    schema_case,
):
    assert schema_case.probe() == []
    with schema_case.fenced.restricted() as conn:
        assert conn.execute(
            "SELECT has_table_privilege(current_user,'v2_credential_vault','SELECT'),has_table_privilege(current_user,'v2_execution_routes','UPDATE')"
        ).fetchone() == (False, False)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_maintenance_cursors"
        ).fetchone() == (0,)


def test_pg_missing_required_column_is_explicit(schema_case):
    with schema_case.fenced.admin() as conn:
        conn.execute(
            "ALTER TABLE v2_strategy_dispatch_cursors RENAME COLUMN order_id TO qa_missing"
        )
    assert {
        "code": "PG_REQUIRED_COLUMN_MISSING",
        "table": "v2_strategy_dispatch_cursors",
        "column": "order_id",
    } in schema_case.probe()


def test_pg_missing_required_table_is_explicit(schema_case):
    with schema_case.fenced.admin() as conn:
        conn.execute(
            "ALTER TABLE v2_strategy_dispatch_cursors RENAME TO qa_missing_dispatch"
        )
    assert {
        "code": "PG_REQUIRED_TABLE_MISSING",
        "table": "v2_strategy_dispatch_cursors",
    } in schema_case.probe()


def test_pg_business_and_guard_privilege_failures_are_separate(schema_case):
    case = schema_case.fenced
    with case.admin() as conn:
        conn.execute(
            sql.SQL("REVOKE INSERT ON v2_strategy_maintenance_cursors FROM {}").format(
                sql.Identifier(case.worker)
            )
        )
        conn.execute(
            sql.SQL("REVOKE SELECT(account_id) ON v2_tenant_accounts FROM {}").format(
                sql.Identifier(case.role)
            )
        )
    result = schema_case.probe()
    assert {
        "code": "PG_BUSINESS_PERMISSION_MISSING",
        "table": "v2_strategy_maintenance_cursors",
        "permission": "INSERT",
    } in result
    assert {"code": "PG_GUARD_PERMISSION_MISSING"} in result


def test_pg_unavailable_set_role_is_not_hidden_by_roles_own_grants(schema_case):
    with schema_case.fenced.admin() as conn:
        conn.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(schema_case.operator),
                sql.Identifier(schema_case.fenced.worker),
            )
        )
    assert {"code": "PG_OPERATOR_SET_ROLE_DENIED"} in schema_case.probe()


def test_pg_vault_metadata_missing_does_not_read_secret_rows(schema_case):
    with schema_case.fenced.admin() as conn:
        conn.execute(
            sql.SQL("REVOKE SELECT(ciphertext) ON v2_credential_vault FROM {}").format(
                sql.Identifier(schema_case.vault)
            )
        )
    assert {"code": "PG_VAULT_PERMISSION_MISSING"} in schema_case.probe()
