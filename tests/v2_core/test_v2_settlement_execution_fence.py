"""Same-transaction fencing with real isolated PG roles, locks and accounting.

Venue responses are explicit synthetic fixtures; no exchange socket is opened.
The ACL tests remove the fixture superuser's session identity. Full settlement
tests separately exercise the production journals and financial transaction.
"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_strategy_settlement import SCOPE, closed_plan

from services import v2_execution_guard
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_execution_guard import create_execution_guard
from tests.support.v2_admission import database as database_fixture
from v2_core.account_registry import AccountRegistry
from v2_core.ledger import Ledger

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]


class ScopedRead:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, request):
        self.request = request

    def __call__(self, method, path, params):
        assert method == "GET"
        return self.request(method, path, params)


@pytest.fixture
def fenced(database, monkeypatch):
    role = "qa_settle_guard_" + uuid4().hex
    worker = "qa_settle_worker_" + uuid4().hex
    business = "qa_settle_business_" + uuid4().hex
    with database() as c:
        for migration in (
            "20260925_tenant_registry.sql",
            "20260927_account_retirements.sql",
            "20260928_execution_routes.sql",
            "20260928_execution_draining.sql",
            "20260928_execution_controller.sql",
        ):
            c.execute((ROOT / "db/migrations" / migration).read_text())
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute("CREATE TABLE qa_fence_commits (label TEXT PRIMARY KEY)")
        for name in (worker, business):
            c.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(name))
            )
        c.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                sql.Identifier(business), sql.Identifier(worker)
            )
        )
        for name in (worker, business):
            c.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(name)
                )
            )
            c.execute(
                sql.SQL("GRANT SELECT,INSERT ON qa_fence_commits TO {}").format(
                    sql.Identifier(name)
                )
            )
        # Execute deployment grants verbatim except for disposable identifiers.
        grants = (ROOT / "deploy/v2-testnet/execution-guard-grants.sql").read_text()
        for original, replacement in (
            ("trade_v2_execution_guard", sql.Identifier(role).as_string(c)),
            ("SCHEMA trade_v2", "SCHEMA " + sql.Identifier(schema).as_string(c)),
            ("trade_v2.", sql.Identifier(schema).as_string(c) + "."),
            ("TO ubuntu", "TO " + sql.Identifier(worker).as_string(c)),
        ):
            grants = grants.replace(original, replacement)
        c.execute(grants)
    registry = AccountRegistry(database)
    tenant = registry.create_tenant(str(uuid4()), "QA settlement tenant")
    bound = registry.enroll(
        tenant,
        SCOPE,
        display_name="QA synthetic account",
        credential_ref=str(uuid4()),
        request_id=str(uuid4()),
    )
    args = {
        "tenant_id": tenant,
        "environment": SCOPE.environment,
        "registry_id": bound["registry_id"],
        "binding_version": 1,
        "epoch": 1,
        "worker_token": str(uuid4()),
    }
    with database() as c:
        c.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,"
            "target_registry,binding_version,phase,worker_token) "
            "VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (tenant, bound["registry_id"], args["worker_token"]),
        )
    monkeypatch.setattr(v2_execution_guard, "EXECUTION_GUARD_ROLE", role)

    @contextmanager
    def restricted():
        with database() as c:
            # SET ROLE alone would still allow any role via session superuser.
            c.execute(
                sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(worker))
            )
            yield c

    try:
        yield SimpleNamespace(
            admin=database,
            restricted=restricted,
            role=role,
            worker=worker,
            business=business,
            args=args,
            guard=create_execution_guard(restricted, **args),
        )
    finally:
        with database() as c:
            for name in (worker, business, role):
                c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def route_update(fenced, statement="SET blockers=blockers", params=()):
    with fenced.admin() as c:
        c.execute("SET LOCAL lock_timeout='75ms'")
        c.execute("UPDATE v2_execution_routes " + statement, params)


def assert_financially_pending(case):
    trace = case.data.trace(case.episode)
    assert trace["settlements"] == trace["cash"] == trace["income_allocations"] == []
    assert trace["account_risk"]["status"] == "HELD"


def test_fence_uses_supplied_connection_restores_exact_role_and_keeps_locks(fenced):
    def forbidden_connect():
        pytest.fail("the financial fence must never open its own connection")

    guard = create_execution_guard(forbidden_connect, **fenced.args)
    with fenced.restricted() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(fenced.business)))
        backend = c.execute("SELECT pg_backend_pid()").fetchone()[0]
        guard.require_transaction(c, scope=SCOPE)
        assert c.execute(
            "SELECT current_user,session_user,pg_backend_pid()"
        ).fetchone() == (
            fenced.business,
            fenced.worker,
            backend,
        )
        c.execute("INSERT INTO qa_fence_commits VALUES ('business role retained')")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced)
        with pytest.raises(psycopg.errors.LockNotAvailable), fenced.admin() as other:
            other.execute("SET LOCAL lock_timeout='75ms'")
            other.execute("UPDATE v2_tenant_accounts SET version=version+1")
    route_update(fenced)
    with fenced.admin() as c:
        assert c.execute("SELECT count(*) FROM qa_fence_commits").fetchone() == (1,)
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        fenced.guard.connect() as c,
    ):
        c.execute("INSERT INTO qa_fence_commits VALUES ('guard cannot write finance')")


@pytest.mark.parametrize(
    "phase,operation,allowed",
    [
        ("ACTIVE", "MANAGE", True),
        ("ACTIVE", "OPEN", True),
        ("DRAINING", "MANAGE", True),
        ("DRAINING", "OPEN", False),
        ("STARTING", "MANAGE", False),
        ("BLOCKED", "MANAGE", False),
        ("STOPPED", "MANAGE", False),
    ],
)
def test_financial_fence_uses_same_route_phase_rules(fenced, phase, operation, allowed):
    token = (
        fenced.args["worker_token"]
        if phase in {"ACTIVE", "DRAINING", "STARTING"}
        else None
    )
    route_update(fenced, "SET phase=%s,worker_token=%s", (phase, token))

    def attempt():
        with fenced.restricted() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            fenced.guard.require_transaction(c, scope=SCOPE, operation=operation)
            c.execute("INSERT INTO qa_fence_commits VALUES ('authorized')")

    if allowed:
        attempt()
    else:
        with pytest.raises(ValueError, match="EXECUTION_FENCED"):
            attempt()
    with fenced.admin() as c:
        assert c.execute("SELECT count(*) FROM qa_fence_commits").fetchone() == (
            int(allowed),
        )


@pytest.mark.parametrize(
    "field",
    [
        "epoch",
        "worker_token",
        "registry_id",
        "binding_version",
        "tenant_id",
        "environment",
    ],
)
def test_stale_or_foreign_binding_cannot_commit(fenced, field):
    args = dict(fenced.args)
    args[field] = (
        2
        if field in {"epoch", "binding_version"}
        else "LIVE"
        if field == "environment"
        else str(uuid4())
    )
    guard = create_execution_guard(fenced.restricted, **args)
    with pytest.raises(ValueError), fenced.restricted() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        guard.require_transaction(c, scope=SCOPE)
        pytest.fail("stale binding reached financial writes")


def test_registry_rotation_retirement_and_full_account_scope_are_checked(fenced):
    with (
        pytest.raises(ValueError, match="ACCOUNT_SCOPE_MISMATCH"),
        fenced.restricted() as c,
    ):
        fenced.guard.require_transaction(c, scope=replace(SCOPE, account_id="other-qa"))
    with fenced.admin() as c:
        c.execute("UPDATE v2_tenant_accounts SET version=2")
    with (
        pytest.raises(ValueError, match="ROUTE_BINDING_REJECTED"),
        fenced.restricted() as c,
    ):
        fenced.guard.require_transaction(c, scope=SCOPE)
    with fenced.admin() as c:
        c.execute("UPDATE v2_tenant_accounts SET version=1")
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) "
            "VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (fenced.args["tenant_id"], fenced.args["registry_id"]),
        )
    with (
        pytest.raises(ValueError, match="ROUTE_BINDING_REJECTED"),
        fenced.restricted() as c,
    ):
        fenced.guard.require_transaction(c, scope=SCOPE)


@pytest.mark.parametrize("missing", ["scope_column", "role_membership"])
def test_missing_deployment_privilege_fails_closed(fenced, missing):
    with fenced.admin() as c:
        statement = (
            sql.SQL("REVOKE SELECT(account_id) ON v2_tenant_accounts FROM {}").format(
                sql.Identifier(fenced.role)
            )
            if missing == "scope_column"
            else sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(fenced.role), sql.Identifier(fenced.worker)
            )
        )
        c.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), fenced.restricted() as c:
        fenced.guard.require_transaction(c, scope=SCOPE)
        pytest.fail("missing ACL reached financial writes")


def test_autocommit_and_non_scope_values_are_not_fenced_transactions(fenced):
    with pytest.raises(ValueError, match="TRANSACTION_BINDING_REQUIRED"):
        fenced.guard.require_transaction(SimpleNamespace(autocommit=True), scope=SCOPE)
    with pytest.raises(ValueError, match="TRANSACTION_BINDING_REQUIRED"):
        fenced.guard.require_transaction(SimpleNamespace(autocommit=False), scope={})


def test_stage_retains_explicit_optional_guard_contract(database):
    stage = DirectionalSettlementStage(
        database, ScopedRead(lambda *_: None), scope=SCOPE, clock_ms=lambda: 1
    )
    assert stage.commit_guard is None
    with pytest.raises(TypeError, match="EXPLICIT_SETTLEMENT_COMMIT_GUARD_REQUIRED"):
        DirectionalSettlementStage(
            database,
            lambda *_: None,
            scope=SCOPE,
            clock_ms=lambda: 1,
            commit_guard=True,
        )


def guarded_stage(fenced, case, request=None, commit_guard=None):
    return DirectionalSettlementStage(
        fenced.admin,
        ScopedRead(request) if request is not None else case.venue,
        scope=SCOPE,
        clock_ms=case.stage.clock,
        commit_guard=commit_guard or fenced.guard.require_transaction,
    )


def test_real_settlement_has_no_route_lock_across_venue_reads(fenced):
    case = closed_plan(fenced.admin)
    calls, commits = [], []

    def request(method, path, params):
        assert method == "GET"
        route_update(fenced)
        calls.append(path)
        return case.venue(method, path, params)

    def fence(c, **kwargs):
        fenced.guard.require_transaction(c, **kwargs)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced)
        commits.append(c.execute("SHOW transaction_isolation").fetchone()[0])

    stage = guarded_stage(fenced, case, request=request, commit_guard=fence)
    assert stage.settle(case.episode)["status"] == "SETTLED"
    assert "/fapi/v1/income" in calls and "/fapi/v3/account" in calls
    assert commits == ["serializable"]
    trace = case.data.trace(case.episode)
    assert len(trace["settlements"]) == 1
    assert trace["account_risk"]["status"] == "RELEASED"
    route_update(fenced)


@pytest.mark.parametrize("revocation", ["epoch", "token"])
def test_authority_revoked_during_network_prevents_financial_commit(fenced, revocation):
    case = closed_plan(fenced.admin)
    revoked = []

    def request(method, path, params):
        assert method == "GET"
        if path == "/fapi/v1/income" and not revoked:
            if revocation == "epoch":
                route_update(fenced, "SET epoch=epoch+1")
            else:
                route_update(fenced, "SET worker_token=%s", (str(uuid4()),))
            revoked.append(True)
        return case.venue(method, path, params)

    stage = guarded_stage(fenced, case, request=request)
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        stage.settle(case.episode)
    assert revoked
    assert_financially_pending(case)
    route_update(fenced)


def test_fence_financial_settlement_and_risk_release_share_one_rollback(
    fenced, monkeypatch
):
    case = closed_plan(fenced.admin)
    stage = guarded_stage(fenced, case)
    original = Ledger.settle

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("QA crash after financial writes before outer commit")

    monkeypatch.setattr(Ledger, "settle", crash)
    with pytest.raises(RuntimeError, match="QA crash"):
        stage.settle(case.episode)
    assert_financially_pending(case)
    route_update(fenced)
    monkeypatch.setattr(Ledger, "settle", original)
    assert stage.settle(case.episode)["status"] == "SETTLED"
    trace = case.data.trace(case.episode)
    assert len(trace["settlements"]) == len(trace["income_allocations"]) == 1
    assert trace["account_risk"]["status"] == "RELEASED"


@pytest.mark.parametrize("phase", ["STARTING", "ACTIVE", "DRAINING"])
def test_full_scope_preflight_checks_startup_without_granting_authority(fenced, phase):
    route_update(fenced, "SET phase=%s", (phase,))
    fenced.guard.preflight(scope=SCOPE)
    # Existing callers keep the original no-argument readiness contract.
    fenced.guard.preflight()
    if phase == "STARTING":
        with (
            pytest.raises(ValueError, match="EXECUTION_FENCED"),
            fenced.restricted() as c,
        ):
            fenced.guard.require_transaction(c, scope=SCOPE)


def test_full_scope_preflight_detects_missing_metadata_grant_before_ready(fenced):
    route_update(fenced, "SET phase='STARTING'")
    with fenced.admin() as c:
        c.execute(
            sql.SQL("REVOKE SELECT(account_id) ON v2_tenant_accounts FROM {}").format(
                sql.Identifier(fenced.role)
            )
        )
    # This proves why the old preflight was insufficient for the new writer.
    fenced.guard.preflight()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        fenced.guard.preflight(scope=SCOPE)


def test_full_scope_preflight_rejects_foreign_account_and_non_exact_scope(fenced):
    route_update(fenced, "SET phase='STARTING'")
    with pytest.raises(ValueError, match="EXECUTION_ACCOUNT_SCOPE_MISMATCH"):
        fenced.guard.preflight(scope=replace(SCOPE, account_id="foreign-qa"))

    class ScopeSubclass(type(SCOPE)):
        pass

    for invalid in (
        {},
        True,
        replace(SCOPE, environment="LIVE"),
        ScopeSubclass(
            SCOPE.exchange, SCOPE.account_id, SCOPE.environment, SCOPE.product
        ),
    ):
        with pytest.raises(ValueError, match="EXECUTION_PREFLIGHT_SCOPE_REQUIRED"):
            fenced.guard.preflight(scope=invalid)
