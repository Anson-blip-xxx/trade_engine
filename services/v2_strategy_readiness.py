"""Read-only paused-controller preflight, not a trading activation permit.

No schema/policy/route changes, credential decryption or Binance calls. Run the
CLI under the service's Python, OS user and merged environment. Its isolated
subprocess has a wall timeout; direct callers must provide their own hard bound.
Database checks use a READ ONLY transaction. Host credential checks inspect only
systemd credential binding labels, never encrypted files. A successful result proves these boot
prerequisites, not decrypted credentials, a live worker's READY or venue health.
"""

import argparse
import importlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from services.v2_execution_guard import EXECUTION_GUARD_ROLE
from services.v2_strategy_market_reader import ClosedMarketReader
from v2_core.ingress import symbol as validate_symbol

OPERATOR_ROLE = "trade_v2_execution_operator"
VAULT_ROLE = "trade_v2_vault_operator"
IMPORTS = (
    "services.v2_execution_supervisor",
    "services.v2_management_process",
    "services.v2_settlement_worker",
    "services.v2_strategy_runtime",
    "services.v2_strategy_runtime_job",
    "services.v2_strategy_account_facts",
    "services.v2_strategy_signal_maintenance",
    "cryptography.hazmat.primitives.ciphers.aead",
    "psycopg",
)

# Explicit required startup/maintenance surfaces, not a schema migration engine
# or proof that every historical migration/constraint has been applied.
TABLES = {
    "v2_tenants": ("tenant_id",),
    "v2_tenant_accounts": (
        "tenant_id",
        "registry_id",
        "exchange",
        "account_id",
        "environment",
        "product",
        "version",
        "credential_ref",
    ),
    "v2_account_retirements": ("tenant_id", "registry_id"),
    "v2_credential_vault": (
        "credential_ref",
        "tenant_id",
        "environment",
        "key_id",
        "nonce",
        "ciphertext",
    ),
    "v2_execution_routes": (
        "tenant_id",
        "environment",
        "epoch",
        "target_registry",
        "binding_version",
        "phase",
        "worker_token",
        "blockers",
    ),
    "v2_execution_route_events": ("tenant_id", "environment", "epoch", "result"),
    "v2_execution_controller_events": (
        "event_id",
        "tenant_id",
        "environment",
        "epoch",
        "target_registry",
        "phase",
        "blockers",
    ),
    "v2_decision_evidence": (
        "evidence_ref",
        "signal_id",
        "strategy_version",
        "config_digest",
        "config",
        "snapshot",
    ),
    "v2_trade_intents": (
        "intent_id",
        "exchange",
        "account_id",
        "environment",
        "product",
        "producer",
        "status",
        "evidence_ref",
        "signal_id",
        "payload",
        "version",
    ),
    "v2_episodes": ("episode_id", "status", "accounting_revision"),
    "v2_orders": (
        "order_id",
        "episode_id",
        "status",
        "version",
        "leg",
        "client_order_id",
        "exchange_order_id",
        "request_evidence",
    ),
    "v2_order_events": ("order_id", "status", "version", "evidence"),
    "v2_order_recovery": ("order_id", "next_attempt_at", "lease_token", "lease_until"),
    "v2_fills": ("order_id", "quantity", "price"),
    "v2_cash_adjustments": ("episode_id",),
    "v2_exchange_income": ("account_id", "environment", "currency"),
    "v2_income_allocations": ("episode_id",),
    "v2_settlements": ("episode_id", "revision", "currency"),
    "v2_directional_outcomes": ("episode_id",),
    "v2_business_state": ("state_id", "scope", "version", "payload", "deleted"),
    "v2_state_history": (
        "state_id",
        "version",
        "expected_version",
        "payload",
        "deleted",
        "reason",
    ),
    "v2_domain_outbox": ("event_id", "intent_id", "event_type", "payload"),
    "v2_risk_accounts": ("scope", "version"),
    "v2_risk_policies": ("scope", "version", "config"),
    "v2_risk_reservations": ("episode_id", "status"),
    "v2_inbound_signals": (
        "signal_id",
        "source",
        "environment",
        "request_key",
        "snapshot",
    ),
    "v2_signal_receipts": ("consumer", "signal_id", "outcome", "intent_id", "reason"),
    "v2_strategy_decisions": (
        "decision_id",
        "consumer",
        "signal_id",
        "evidence_ref",
        "action",
        "side",
        "quantity",
    ),
    "v2_strategy_tasks": (
        "consumer",
        "signal_id",
        "next_attempt_at",
        "lease_token",
        "lease_until",
        "completed_at",
    ),
    "v2_strategy_ready": ("consumer", "signal_id"),
    "v2_strategy_ready_control": ("singleton", "enabled", "validated_at"),
    "v2_strategy_entry_candidates": ("consumer", "signal_id", "evidence_ref"),
    "v2_strategy_maintenance_cursors": (
        "consumer",
        "source",
        "environment",
        "request_cursor",
        "intent_cursor",
        "next_lane",
    ),
    "v2_strategy_scan_cursors": ("consumer", "window_ms", "after_symbol", "completed"),
    "v2_entry_discovery_cursors": (
        "consumer",
        "source",
        "environment",
        "request_cursor",
    ),
    "v2_strategy_dispatch_cursors": ("consumer", "order_id"),
    "v2_market_catalog": ("environment", "observed_at_ms", "contracts", "digest"),
    "v2_egress_budgets": (
        "host",
        "window_id",
        "used_weight",
        "market_weight",
        "blocked_until",
    ),
}
_IMMUTABLE = frozenset(
    {
        "v2_decision_evidence",
        "v2_order_events",
        "v2_fills",
        "v2_cash_adjustments",
        "v2_exchange_income",
        "v2_income_allocations",
        "v2_settlements",
        "v2_state_history",
        "v2_signal_receipts",
        "v2_strategy_decisions",
        "v2_strategy_entry_candidates",
        "v2_execution_controller_events",
        "v2_risk_policies",
    }
)
_READ_ONLY = frozenset(
    {
        "v2_tenants",
        "v2_tenant_accounts",
        "v2_account_retirements",
        "v2_credential_vault",
        "v2_execution_route_events",
        "v2_market_catalog",
        "v2_execution_routes",
        "v2_strategy_ready_control",
    }
)
# These are deliberately accessed only through a restricted SET ROLE factory.
# Do not mistake the absence of ambient vault/route-write privileges for a fault.
_ROLE_ONLY = frozenset(
    {
        "v2_tenants",
        "v2_tenant_accounts",
        "v2_account_retirements",
        "v2_credential_vault",
        "v2_execution_route_events",
        "v2_execution_controller_events",
    }
)
_GUARD = {
    "v2_execution_routes": {
        "SELECT": (
            "tenant_id",
            "environment",
            "target_registry",
            "binding_version",
            "epoch",
            "worker_token",
            "phase",
        ),
        "UPDATE": ("blockers",),
    },
    "v2_tenant_accounts": {
        "SELECT": (
            "tenant_id",
            "registry_id",
            "exchange",
            "account_id",
            "environment",
            "product",
            "version",
        ),
        "UPDATE": ("version",),
    },
    "v2_account_retirements": {"SELECT": ("tenant_id", "registry_id")},
}
_OPERATOR = {
    "v2_execution_routes": ("SELECT", "INSERT", "UPDATE"),
    "v2_execution_route_events": ("SELECT",),
    "v2_tenant_accounts": ("SELECT",),
    "v2_account_retirements": ("SELECT",),
    "v2_execution_controller_events": ("SELECT", "INSERT"),
}
_VAULT = {
    "v2_tenant_accounts": {
        "SELECT": (
            "tenant_id",
            "registry_id",
            "exchange",
            "account_id",
            "environment",
            "product",
            "version",
            "credential_ref",
        ),
        "UPDATE": ("version",),
    },
    "v2_account_retirements": {"SELECT": ("tenant_id", "registry_id")},
    "v2_credential_vault": {
        "SELECT": (
            "credential_ref",
            "tenant_id",
            "environment",
            "key_id",
            "nonce",
            "ciphertext",
        )
    },
}
_HOST_KEYS = (
    "legacy_inactive",
    "controller_inactive",
    "postgres_running",
    "cache_running",
    "clickhouse_running",
    "encrypted_credentials_configured",
)


def _issue(code, **details):
    return {"code": code, **details}


def _config(values):
    if type(values) is not dict or any(type(v) is not str for v in values.values()):
        return [_issue("PAUSED_CONFIG_INVALID")]
    if (
        values.get("V2_CONTROLLER_ENABLE_ENTRIES") != "false"
        or values.get("V2_ENABLE_ENTRIES") != "false"
    ):
        return [_issue("PAUSED_ENTRIES_MUST_BE_DISABLED")]
    try:
        from services.v2_management_process import ManagementProcessConfig
        from services.v2_strategy_runtime import StrategyRuntimeFactory
        from services.v2_strategy_runtime_job import typed_entry_ports_for

        tenant = str(UUID(values["V2_CONTROLLER_TENANT"]))
        if tenant != values["V2_CONTROLLER_TENANT"]:
            raise ValueError
        StrategyRuntimeFactory(tenant, values, entry_ports_for=typed_entry_ports_for)
        # The existing account template belongs in the external environment,
        # never this repository or the diagnostic response.
        account = values["V2_ACCOUNT_ID"]
        ManagementProcessConfig.from_mapping(
            {
                **values,
                "V2_ENABLE_ENTRIES": "false",
                "V2_ENABLE_PROTECTION_WRITES": "true",
                "V2_ENABLE_REDUCE_ONLY_EXITS": "true",
                "V2_TESTNET_WRITE_ACK": "SANDBOX:" + account,
            }
        )
        if values.get("V2_TYPED_LEVERAGE", "2") not in {"2", "3", "5"}:
            raise ValueError
        from v2_core.strategy_registry import strategy_identifier

        strategy_identifier(values.get("V2_TYPED_INSTANCE", "causal-trend-v1"))
    except Exception:  # noqa: BLE001 - do not expose configuration or identifiers
        return [_issue("PAUSED_CONFIG_INVALID")]
    return []


def _permissions(conn, role, requirements):
    """Bulk metadata only; a column privilege is not a table-wide grant."""
    if not requirements:
        return []
    oids, columns, permissions = zip(*requirements, strict=True)
    return conn.execute(
        "SELECT CASE WHEN p.col IS NULL THEN has_table_privilege(%s,p.obj,p.perm) "
        "ELSE has_column_privilege(%s,p.obj,p.col,p.perm) END "
        "FROM unnest(%s::oid[],%s::text[],%s::text[]) AS p(obj,col,perm)",
        (role, role, list(oids), list(columns), list(permissions)),
    ).fetchall()


def _postgres(
    connect,
    *,
    guard_role=EXECUTION_GUARD_ROLE,
    operator_role=OPERATOR_ROLE,
    vault_role=VAULT_ROLE,
):
    problems = []
    try:
        with connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL lock_timeout='500ms'")
            conn.execute("SET LOCAL statement_timeout='3s'")
            schema, user, readonly = conn.execute(
                "SELECT current_schema(),current_user,current_setting('transaction_read_only')"
            ).fetchone()
            if (
                not schema
                or schema in {"public", "pg_catalog", "information_schema"}
                or readonly != "on"
            ):
                return [_issue("PG_DEDICATED_READONLY_CHECK_REQUIRED")]
            rows = conn.execute(
                "SELECT c.relname,c.oid,a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid=c.oid "
                "WHERE n.nspname=current_schema() AND c.relname=ANY(%s::text[]) "
                "AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped",
                (list(TABLES),),
            ).fetchall()
            columns, oids = {}, {}
            for name, oid, column in rows:
                columns.setdefault(name, set()).add(column)
                oids[name] = oid
            for name, required in TABLES.items():
                if name not in columns:
                    problems.append(_issue("PG_REQUIRED_TABLE_MISSING", table=name))
                else:
                    for column in required:
                        if column not in columns[name]:
                            problems.append(
                                _issue(
                                    "PG_REQUIRED_COLUMN_MISSING",
                                    table=name,
                                    column=column,
                                )
                            )
            requests, labels = [], []
            for name, oid in oids.items():
                if name in _ROLE_ONLY:
                    continue
                permissions = (
                    ("SELECT",)
                    if name in _READ_ONLY
                    else (
                        ("SELECT", "INSERT")
                        if name in _IMMUTABLE
                        else ("SELECT", "INSERT", "UPDATE")
                    )
                )
                for permission in permissions:
                    requests.append((oid, None, permission))
                    labels.append((name, permission))
            for allowed, (name, permission) in zip(
                _permissions(conn, user, requests), labels, strict=True
            ):
                if allowed != (True,):
                    problems.append(
                        _issue(
                            "PG_BUSINESS_PERMISSION_MISSING",
                            table=name,
                            permission=permission,
                        )
                    )
            for kind, role in (
                ("GUARD", guard_role),
                ("OPERATOR", operator_role),
                ("VAULT", vault_role),
            ):
                info = conn.execute(
                    "SELECT rolsuper,rolcanlogin,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_roles WHERE rolname=%s",
                    (role,),
                ).fetchone()
                if info is None:
                    problems.append(_issue("PG_" + kind + "_ROLE_MISSING"))
                    continue
                if any(info):
                    problems.append(_issue("PG_" + kind + "_ROLE_TOO_PRIVILEGED"))
                if conn.execute(
                    "SELECT pg_has_role(current_user,%s,'SET')", (role,)
                ).fetchone() != (True,):
                    problems.append(_issue("PG_" + kind + "_SET_ROLE_DENIED"))
                if conn.execute(
                    "SELECT has_schema_privilege(%s,current_schema(),'USAGE')", (role,)
                ).fetchone() != (True,):
                    problems.append(_issue("PG_" + kind + "_SCHEMA_USAGE_DENIED"))
                requests = []
                if kind in {"GUARD", "VAULT"}:
                    for name, grants in (_GUARD if kind == "GUARD" else _VAULT).items():
                        for permission, names in grants.items():
                            requests.extend(
                                (oids[name], column, permission)
                                for column in names
                                if name in oids and column in columns[name]
                            )
                else:
                    for name, grants in _OPERATOR.items():
                        requests.extend(
                            (oids[name], None, p) for p in grants if name in oids
                        )
                    if "version" in columns.get("v2_tenant_accounts", ()):
                        requests.append(
                            (oids["v2_tenant_accounts"], "version", "UPDATE")
                        )
                if any(row != (True,) for row in _permissions(conn, role, requests)):
                    problems.append(_issue("PG_" + kind + "_PERMISSION_MISSING"))
    except Exception:  # noqa: BLE001 - never return DSN/SQL/role/exception text
        return [_issue("PG_READINESS_UNAVAILABLE")]
    return problems


def _clickhouse(client, symbol, clock_ms):
    result = {"profile_ready": False, "closed_window_ready": False, "issues": []}
    if client is None:
        result["issues"].append(_issue("CH_DEDICATED_READER_NOT_VERIFIED"))
        return result
    try:
        reader = ClosedMarketReader(client, clock_ms=clock_ms)
        identity = client.query(
            "SELECT currentUser(),currentDatabase()",
            parameters={},
            settings=reader._settings(),
        ).result_rows
        if identity != [("tradev2strategy", "trade_v2_testnet")]:
            raise ValueError
        result["profile_ready"] = True
    except Exception:  # noqa: BLE001 - no transport/client metadata leaks
        result["issues"].append(_issue("CH_READONLY_PROFILE_UNVERIFIED"))
        return result
    if symbol is None:
        result["issues"].append(_issue("CH_CLOSED_WINDOW_NOT_PROBED"))
        return result
    try:
        validate_symbol(symbol)
        facts = reader.read(symbol, closed_at_ms=clock_ms() // 60000 * 60000)
        result["closed_window_ready"] = len(facts.bars) == 1440
    except ValueError as exc:
        code = str(exc)
        # Fixed allowlist only; missing data is not broken controller startup.
        result["issues"].append(
            _issue(
                "CH_CLOSED_WINDOW_INCOMPLETE"
                if code == "CANDLE_HISTORY_INCOMPLETE"
                else "CH_CLOSED_WINDOW_UNVERIFIED"
            )
        )
    except Exception:  # noqa: BLE001
        result["issues"].append(_issue("CH_CLOSED_WINDOW_UNVERIFIED"))
    return result


def check_readiness(
    values,
    *,
    connect,
    clickhouse=None,
    symbol=None,
    host_probe=None,
    importer=importlib.import_module,
    clock_ms=lambda: time.time_ns() // 1000000,
    guard_role=EXECUTION_GUARD_ROLE,
    operator_role=OPERATOR_ROLE,
    vault_role=VAULT_ROLE,
):
    """Inspect current permissions, not administrator-granted hypothetical ones."""
    problems = _config(values)
    for name in IMPORTS:
        try:
            importer(name)
        except Exception:  # noqa: BLE001
            problems.append(_issue("CONTROLLER_IMPORT_UNAVAILABLE", component=name))
    if not callable(connect):
        problems.append(_issue("PG_CONNECTION_REQUIRED"))
    else:
        problems.extend(
            _postgres(
                connect,
                guard_role=guard_role,
                operator_role=operator_role,
                vault_role=vault_role,
            )
        )
    try:
        host = host_probe() if callable(host_probe) else {}
        if type(host) is not dict:
            host = {}
    except Exception:  # noqa: BLE001
        host = {}
    for key in _HOST_KEYS:
        if host.get(key) is not True:
            problems.append(_issue("HOST_" + key.upper() + "_UNVERIFIED"))
    market = _clickhouse(clickhouse, symbol, clock_ms)
    return {
        "schema": "typed-strategy-readiness-v1",
        "scope": "READ_ONLY_PAUSED_BOOT_PREREQUISITES",
        "paused_boot_ready": not problems,
        "entry_ready": False,
        "entry_data_ready": market["profile_ready"] and market["closed_window_ready"],
        "execution_authorized": False,
        "runtime_verified": False,
        "blockers": problems,
        "market": market,
        "not_verified": [
            "ENCRYPTED_CREDENTIAL_CONTENT_NOT_DECRYPTED",
            "SYSTEMD_CREDENTIAL_BINDINGS_RUNTIME_NOT_OBSERVED",
            "TARGET_VENUE_INVENTORY_NOT_QUERIED",
            "ACTIVE_STRATEGY_DEPLOYMENT_NOT_APPROVED",
            "WORKER_READY_AND_RUNNING_NOT_OBSERVED",
            "ENTRY_ACTIVATION_REQUIRES_SEPARATE_ACCEPTANCE",
        ],
    }


def _host_probe():
    units = (
        "trade-v2-testnet-daemon.service",
        "trade-v2-testnet-watchdog.service",
        "trade-v2-execution-controller.service",
        "postgresql@16-tradev2.service",
        "trade-v2-cache.service",
        "trade-v2-clickhouse.service",
    )
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "--no-pager",
            "-p",
            "Id",
            "-p",
            "ActiveState",
            "-p",
            "LoadCredentialEncrypted",
            *units,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    if len(result.stdout) > 16384:
        raise ValueError("HOST_STATUS_BOUND")
    states = {}
    credential_labels = set()
    for block in result.stdout.strip().split("\n\n"):
        parsed = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        states[parsed.get("Id")] = parsed.get("ActiveState")
        if parsed.get("Id") == units[2]:
            credential_labels = {
                field.split(":", 1)[0]
                for field in parsed.get("LoadCredentialEncrypted", "").split()
                if ":" in field
            }
    if not {"v2-master-v1", "v2-notifications"} <= credential_labels:
        # Some systemd versions render this compound property as [unprintable].
        # Parse only the unit's configured labels; this does not prove that
        # systemd loaded/decrypted them (reported separately as not verified).
        unit = subprocess.run(
            ["systemctl", "cat", units[2], "--no-pager"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        if len(unit.stdout) > 65536:
            raise ValueError("HOST_CONFIG_BOUND")
        section, credential_labels = None, set()
        for line in unit.stdout.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line
            elif section == "[Service]" and line.startswith("LoadCredentialEncrypted="):
                value = line.partition("=")[2].strip()
                if not value:
                    credential_labels.clear()
                elif ":" in value:
                    credential_labels.add(value.split(":", 1)[0].strip('"'))
    return {
        "legacy_inactive": all(states.get(unit) == "inactive" for unit in units[:2]),
        "controller_inactive": states.get(units[2]) == "inactive",
        "postgres_running": states.get(units[3]) == "active",
        "cache_running": states.get(units[4]) == "active",
        "clickhouse_running": states.get(units[5]) == "active",
        "encrypted_credentials_configured": {"v2-master-v1", "v2-notifications"}
        <= credential_labels,
    }


def _values(path):
    values = {key: value for key, value in os.environ.items() if key.startswith("V2_")}
    if path is not None:
        with Path(path).open() as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError("READINESS_CONFIG_BOUND")
        for line in content.splitlines():
            fields = shlex.split(line, comments=True)
            if not fields:
                continue
            if len(fields) != 1 or "=" not in fields[0]:
                raise ValueError("READINESS_CONFIG_INVALID")
            key, value = fields[0].split("=", 1)
            if key.startswith("V2_"):
                values[key] = value
    return values


def _child(args):
    from v2_core.database import connection_factory

    values = _values(args.env_file)
    connect = connection_factory(
        "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=ubuntu",
        schema="trade_v2",
        read_only=True,
    )
    client = None
    try:
        if not args.skip_clickhouse:
            try:
                from services.v2_strategy_runtime_job import _market_client

                client = _market_client()
            except Exception:  # noqa: BLE001,S110 - recorded as unavailable, no details
                pass
        return check_readiness(
            values,
            connect=connect,
            clickhouse=client,
            symbol=args.symbol,
            host_probe=_host_probe,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001,S110 - no mutable state or owned writer
                pass


def _failure(code):
    return {
        "paused_boot_ready": False,
        "entry_ready": False,
        "execution_authorized": False,
        "blockers": [_issue(code)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    parser.add_argument(
        "--symbol", help="Optional one-symbol complete 1440-minute closed-window probe"
    )
    parser.add_argument("--skip-clickhouse", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 5 <= args.timeout_seconds <= 60:
        report = _failure("READINESS_TIMEOUT_CONFIG_INVALID")
    elif args._child:
        try:
            report = _child(args)
        except Exception:  # noqa: BLE001 - CLI prints fixed fields only
            report = _failure("READINESS_CHECK_UNAVAILABLE")
    else:
        command = [sys.executable, "-m", "services.v2_strategy_readiness", "--_child"]
        if args.env_file is not None:
            command += ["--env-file", args.env_file]
        if args.symbol is not None:
            command += ["--symbol", args.symbol]
        if args.skip_clickhouse:
            command.append("--skip-clickhouse")
        try:
            child = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            if len(child.stdout) > 65536:
                raise ValueError
            report = json.loads(child.stdout)
            if (
                type(report) is not dict
                or type(report.get("paused_boot_ready")) is not bool
            ):
                raise ValueError
        except subprocess.TimeoutExpired:
            report = _failure("READINESS_WALL_TIMEOUT")
        except Exception:  # noqa: BLE001 - never echo stdout/stderr or command values
            report = _failure("READINESS_PROCESS_UNAVAILABLE")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["paused_boot_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
