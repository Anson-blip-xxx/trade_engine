"""Deployment ACL tests: real restricted PostgreSQL session, real guard SQL.

Only the HTTPS socket is substituted. These tests intentionally do not execute
the guard as the fixture's PostgreSQL superuser, which masked the live defect.
"""

import hashlib
import hmac
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_credential_console import add
from test_v2_execution_routes import case, database, routes, setup
from test_v2_transport import Connection, transport

from services import v2_execution_guard
from services.v2_execution_guard import create_execution_guard
from v2_core.errors import SubmissionNotSent
from v2_core.guarded_transport import GuardedSignedRequest

__all__ = ["case", "database", "routes", "setup"]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def restricted(routes, monkeypatch):
    _, console = routes
    role = "qa_execution_guard_" + uuid4().hex
    worker = "qa_execution_child_" + uuid4().hex
    registry = add(console)["registry_id"]
    args = {
        "tenant_id": console.tenant,
        "environment": "SANDBOX",
        "registry_id": registry,
        "binding_version": 1,
        "epoch": 1,
        "worker_token": str(uuid4()),
    }
    with console.connect() as connection:
        connection.execute(
            (ROOT / "db/migrations/20260928_execution_controller.sql").read_text()
        )
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        connection.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token) "
            "VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (console.tenant, registry, args["worker_token"]),
        )
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(worker))
        )
        # Apply the actual deployment grants, changing identifiers only. Never
        # hand-write a more permissive QA grant set than the deployed artifact.
        grants = (ROOT / "deploy/v2-testnet/execution-guard-grants.sql").read_text()
        for original, replacement in (
            ("trade_v2_execution_guard", sql.Identifier(role).as_string(connection)),
            (
                "SCHEMA trade_v2",
                "SCHEMA " + sql.Identifier(schema).as_string(connection),
            ),
            ("trade_v2.", sql.Identifier(schema).as_string(connection) + "."),
            ("TO ubuntu", "TO " + sql.Identifier(worker).as_string(connection)),
        ):
            grants = grants.replace(original, replacement)
        connection.execute(grants)
    monkeypatch.setattr(v2_execution_guard, "EXECUTION_GUARD_ROLE", role)

    @contextmanager
    def worker_connection():
        with console.connect() as connection:
            # SET ROLE alone would leave the fixture superuser as session_user,
            # allowing any role switch. Remove that escape in the real session.
            connection.execute(
                sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(worker))
            )
            yield connection

    try:
        yield SimpleNamespace(
            admin=console.connect,
            worker=worker_connection,
            role=role,
            worker_role=worker,
            schema=schema,
            args=args,
            guard=create_execution_guard(worker_connection, **args),
        )
    finally:
        with console.connect() as connection:
            for name in (worker, role):
                connection.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name))
                )
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def signed_request(guard, connection=None):
    connection = connection or Connection()
    request = transport(
        connection,
        enable_trading=True,
        enable_testnet_settings=True,
        enable_testnet_protection=True,
        enable_testnet_cancellation=True,
        enable_testnet_order_cancellation=True,
    )
    return GuardedSignedRequest(
        request, account_id="qa", environment="SANDBOX", guard=guard
    ), connection


def test_role_is_noninherited_and_scope_does_not_leak(restricted):
    with restricted.worker() as connection:
        assert connection.execute("SELECT current_user,session_user").fetchone() == (
            restricted.worker_role,
            restricted.worker_role,
        )
        assert connection.execute(
            "SELECT rolsuper,rolinherit FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False)
        assert connection.execute(
            "SELECT inherit_option,set_option FROM pg_auth_members m "
            "JOIN pg_roles r ON m.roleid=r.oid WHERE r.rolname=%s",
            (restricted.role,),
        ).fetchone() == (False, True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), restricted.worker() as c:
        c.execute(
            sql.SQL("SELECT tenant_id FROM {}.v2_tenant_accounts").format(
                sql.Identifier(restricted.schema)
            )
        )
    with restricted.guard.connect() as connection:
        assert connection.execute("SELECT current_user,session_user").fetchone() == (
            restricted.role,
            restricted.worker_role,
        )
        assert connection.execute(
            "SELECT rolsuper,rolcanlogin,rolinherit FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False, False)
    restricted.guard.preflight()
    with restricted.worker() as connection:
        assert connection.execute("SELECT current_user").fetchone() == (
            restricted.worker_role,
        )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT credential_ref FROM v2_tenant_accounts",
        "SELECT ciphertext FROM v2_credential_vault",
        "SELECT result FROM v2_execution_route_events",
        "UPDATE v2_execution_routes SET phase='ACTIVE'",
        "UPDATE v2_execution_routes SET epoch=epoch+1",
        "UPDATE v2_execution_routes SET worker_token=NULL",
        "UPDATE v2_execution_routes SET binding_version=binding_version+1",
        "UPDATE v2_execution_routes SET target_registry=target_registry",
        "DELETE FROM v2_execution_routes",
        "UPDATE v2_tenant_accounts SET credential_ref=credential_ref",
    ],
)
def test_guard_cannot_read_credentials_or_activate_routes(restricted, statement):
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        restricted.guard.connect() as c,
    ):
        c.execute(statement)


@pytest.mark.parametrize(
    "privilege,table",
    [
        ("SELECT(environment)", "v2_tenant_accounts"),
        ("UPDATE(version)", "v2_tenant_accounts"),
        ("UPDATE(blockers)", "v2_execution_routes"),
        ("SELECT(registry_id)", "v2_account_retirements"),
    ],
)
def test_missing_select_or_share_lock_privilege_fails_before_send(
    restricted, privilege, table
):
    with restricted.admin() as connection:
        connection.execute(
            sql.SQL("REVOKE {} ON {} FROM {}").format(
                sql.SQL(privilege),
                sql.Identifier(table),
                sql.Identifier(restricted.role),
            )
        )
    if privilege.startswith("UPDATE"):
        # Plain SELECT passing is not enough: FOR SHARE additionally needs a
        # column UPDATE grant. This reproduces the second layer of ACL failure.
        with restricted.guard.connect() as connection:
            assert connection.execute(
                "SELECT version FROM v2_tenant_accounts WHERE registry_id=%s",
                (restricted.args["registry_id"],),
            ).fetchone() == (1,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
        restricted.guard.preflight()
    assert caught.value.sqlstate == "42501"
    request, wire = signed_request(restricted.guard)
    with pytest.raises(SubmissionNotSent):
        request("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 2})
    assert wire.calls == []


def test_missing_role_membership_fails_before_preflight_or_send(restricted):
    with restricted.admin() as connection:
        connection.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(restricted.role), sql.Identifier(restricted.worker_role)
            )
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        restricted.guard.preflight()
    request, wire = signed_request(restricted.guard)
    with pytest.raises(SubmissionNotSent):
        request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})
    assert not wire.calls


@pytest.mark.parametrize("phase", ["STARTING", "ACTIVE", "DRAINING"])
def test_startup_preflight_does_not_authorize_open_or_manage(restricted, phase):
    with restricted.admin() as connection:
        connection.execute("UPDATE v2_execution_routes SET phase=%s", (phase,))
    restricted.guard.preflight()
    request, wire = signed_request(restricted.guard)
    for operation, params in (("OPEN", {}), ("MANAGE", {"reduceOnly": "true"})):
        allowed = phase == "ACTIVE" or (phase == "DRAINING" and operation == "MANAGE")
        if allowed:
            assert request("POST", "/fapi/v1/order", params) == {"ok": True}
        else:
            before = len(wire.calls)
            with pytest.raises(SubmissionNotSent):
                request("POST", "/fapi/v1/order", params)
            assert len(wire.calls) == before
    with restricted.admin() as connection:
        assert connection.execute(
            "SELECT phase FROM v2_execution_routes"
        ).fetchone() == (phase,)


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "environment",
        "registry_id",
        "binding_version",
        "epoch",
        "worker_token",
    ],
)
def test_changed_binding_cannot_preflight_or_send(restricted, field):
    change = {
        "tenant_id": str(uuid4()),
        "environment": "LIVE",
        "registry_id": str(uuid4()),
        "binding_version": 2,
        "epoch": 2,
        "worker_token": str(uuid4()),
    }
    guard = create_execution_guard(
        restricted.worker, **{**restricted.args, field: change[field]}
    )
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        guard.preflight()
    request, wire = signed_request(guard)
    with pytest.raises(SubmissionNotSent):
        request("POST", "/fapi/v1/order", {})
    assert not wire.calls


def test_retired_account_is_rejected_after_prior_preflight(restricted):
    restricted.guard.preflight()
    with restricted.admin() as connection:
        connection.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (restricted.args["tenant_id"], restricted.args["registry_id"]),
        )
    with pytest.raises(ValueError, match="ROUTE_BINDING_REJECTED"):
        restricted.guard.preflight()
    request, wire = signed_request(restricted.guard)
    with pytest.raises(SubmissionNotSent):
        request("POST", "/fapi/v1/order", {})
    assert not wire.calls


def test_standalone_managed_factory_uses_real_scoped_role(restricted):
    from services.v2_testnet_daemon_entry import execution_guard_for

    args = restricted.args
    guard = execution_guard_for(
        {
            "V2_CREDENTIAL_SOURCE": "vault",
            "V2_VAULT_TENANT_ID": args["tenant_id"],
            "V2_VAULT_REGISTRY_ID": args["registry_id"],
            "V2_VAULT_BINDING_VERSION": str(args["binding_version"]),
            "V2_EXECUTION_EPOCH": str(args["epoch"]),
            "V2_EXECUTION_WORKER_TOKEN": args["worker_token"],
        },
        config=SimpleNamespace(daemon=SimpleNamespace(write_enabled=True)),
        connect=restricted.worker,
    )
    guard.preflight()
    with guard.connect() as connection:
        assert connection.execute("SELECT current_user").fetchone() == (
            restricted.role,
        )
    request, wire = signed_request(guard)
    assert request(
        "POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 2}
    ) == {"ok": True}
    assert len(wire.calls) == 1


@pytest.mark.parametrize(
    "path,params",
    [
        ("/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 2}),
        ("/fapi/v1/marginType", {"symbol": "BTCUSDT", "marginType": "ISOLATED"}),
        (
            "/fapi/v1/order",
            {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"},
        ),
    ],
)
def test_signed_transport_sends_only_under_real_route_and_binding_locks(
    restricted, path, params
):
    class LockedConnection(Connection):
        def request(self, *args, **kwargs):
            for statement in (
                "UPDATE v2_execution_routes SET phase='DRAINING'",
                "UPDATE v2_tenant_accounts SET version=version+1 WHERE registry_id=%s",
            ):
                with (
                    pytest.raises(psycopg.errors.LockNotAvailable),
                    restricted.admin() as c,
                ):
                    c.execute("SET LOCAL lock_timeout='50ms'")
                    c.execute(
                        statement,
                        (restricted.args["registry_id"],)
                        if "%s" in statement
                        else None,
                    )
            super().request(*args, **kwargs)

    restricted.guard.preflight()
    request, wire = signed_request(restricted.guard, LockedConnection())
    assert request("POST", path, params) == {"ok": True}
    assert len(wire.calls) == 1
    _, kwargs = wire.calls[0]
    payload, signature = kwargs["body"].rsplit("&signature=", 1)
    assert (
        signature
        == hmac.new(b"dummy-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    # Locks are released after response/transaction completion, not retained as
    # a cached capability. The next mutation observes revocation immediately.
    with restricted.admin() as connection:
        connection.execute("UPDATE v2_execution_routes SET phase='DRAINING'")
    with pytest.raises(SubmissionNotSent):
        request("POST", path, params)
    assert len(wire.calls) == 1
