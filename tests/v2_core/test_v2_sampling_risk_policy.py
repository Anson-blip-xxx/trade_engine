"""Reviewed sampling ceilings: narrow diff, bound CAS, preserved existing risk."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.v2_sampling_risk_policy import main
from tests.support.v2_admission import database as database_fixture
from v2_core.account_registry import AccountRegistry
from v2_core.account_risk import AccountScope
from v2_core.capital_model import portfolio_budget, rehearsal_profile
from v2_core.evidence import canonical
from v2_core.execution_routes import ExecutionRoutes
from v2_core.runtime_policy import PolicySnapshot, PolicyStore, resolve
from v2_core.sampling_risk import build_sampling_risk_proof

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


def settings(**changes):
    return resolve(
        {
            **rehearsal_profile("10000", started_at_ms=123),
            "settlement.cash_basis_version": 2,
            "sizing.risk_fraction": "0.02",
            "sizing.max_margin_fraction": "0.70",
            "exit.symbol_isolation_enabled": True,
            **changes,
        }
    )


def snapshot(**changes):
    return PolicySnapshot(7, canonical(settings(**changes)))


def test_pure_proof_changes_only_reviewed_two_fields():
    before = snapshot()
    proof = build_sampling_risk_proof(before, SCOPE)
    assert proof["changes"] == {
        "sizing.risk_fraction": "0.0025",
        "portfolio_risk.max_fraction": "0.01",
    }
    assert proof["before_version"] == 7
    assert proof["before_digest"] == before.digest
    after = PolicySnapshot(8, canonical({**before.values, **proof["changes"]}))
    assert after.digest == proof["after_digest"]
    assert {
        key for key in before.values if before.values[key] != after.values[key]
    } == set(proof["changes"])
    assert before == snapshot()  # neither caller snapshot nor defaults are mutated
    assert build_sampling_risk_proof(after, SCOPE)["already_applied"]
    assert build_sampling_risk_proof(after, SCOPE)["changes"] == {}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"portfolio_risk.enabled": False}, "CAPITAL_GUARDS_REQUIRED"),
        ({"capital.model_enabled": False}, "CAPITAL_MODEL_REQUIRED"),
        (
            {
                "capital.enabled": False,
                "capital.model_enabled": False,
                "portfolio_risk.enabled": False,
            },
            "CAPITAL_GUARDS_REQUIRED",
        ),
        ({"settlement.cash_basis_version": 1}, "CASH_BASIS_2_REQUIRED"),
        (
            {"capital.pool_fraction": "0.71", "sizing.pool_fraction": "0.71"},
            "POOL_CAP_REQUIRED",
        ),
        ({"sizing.risk_fraction": "0.00249"}, "MUST_NOT_INCREASE"),
        ({"portfolio_risk.max_fraction": "0.009"}, "MUST_NOT_INCREASE"),
    ],
)
def test_unsafe_prerequisites_fail_closed(changes, code):
    with pytest.raises(ValueError, match=code):
        build_sampling_risk_proof(snapshot(**changes), SCOPE)


def test_live_scope_and_mismatched_pool_fail_closed():
    with pytest.raises(ValueError, match="SANDBOX_FUTURES_ONLY"):
        build_sampling_risk_proof(snapshot(), replace(SCOPE, environment="LIVE"))
    invalid = {**settings(), "sizing.pool_fraction": "0.60"}
    with pytest.raises(ValueError, match="SIZING_AND_CAPITAL_BUDGET_MISMATCH"):
        build_sampling_risk_proof(PolicySnapshot(1, canonical(invalid)), SCOPE)
    with pytest.raises(ValueError, match="EXISTING_POLICY_REQUIRED"):
        build_sampling_risk_proof(PolicySnapshot(0, canonical(settings())), SCOPE)


@pytest.fixture
def configured(database):
    with database() as conn:
        for name in (
            "20260925_tenant_registry.sql",
            "20260927_account_retirements.sql",
            "20260928_execution_routes.sql",
            "20260928_execution_draining.sql",
        ):
            conn.execute(
                (
                    Path(__file__).resolve().parents[2] / "db/migrations" / name
                ).read_text()
            )
    registry = AccountRegistry(database)
    tenant = registry.create_tenant(uuid4(), "QA sampling owner")
    enrolled = registry.enroll(
        tenant,
        SCOPE,
        display_name="QA sampling account",
        credential_ref=uuid4(),
        request_id=uuid4(),
    )
    token = str(uuid4())
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,"
            "target_registry,binding_version,phase,worker_token) "
            "VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (tenant, enrolled["registry_id"], token),
        )
    before = PolicyStore(database, SCOPE).patch(
        settings(), expected_version=0, reason="QA existing profile"
    )
    args = [
        "--account-id",
        SCOPE.account_id,
        "--tenant-id",
        tenant,
        "--expected-registry-id",
        enrolled["registry_id"],
        "--expected-binding-version",
        "1",
        "--expected-route-epoch",
        "1",
    ]
    return database, args, before, tenant, enrolled["registry_id"], token


def execute_args(args, before):
    return [
        *args,
        "--execute",
        "--expected-version",
        str(before.version),
        "--expected-digest",
        before.digest,
        "--reason",
        "QA reviewed lower risk sampling",
    ]


@pytest.fixture
def restricted_sampling_roles(configured):
    """Reproduce separate production privileges on the isolated QA cluster."""
    database, args, before, tenant, registry, _ = configured
    writer = "qa_sampling_writer_" + uuid4().hex
    control = "qa_sampling_control_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        for role in (writer, control):
            conn.execute(
                psycopg.sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(psycopg.sql.Identifier(role))
            )
            conn.execute(
                psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(role)
                )
            )
        for privilege, tables, role in (
            ("SELECT,UPDATE", "v2_business_state", writer),
            ("SELECT,INSERT", "v2_state_history", writer),
            (
                "SELECT",
                (
                    "v2_tenant_accounts,v2_execution_routes,v2_execution_route_events,"
                    "v2_account_retirements"
                ),
                control,
            ),
            ("UPDATE(version)", "v2_tenant_accounts", control),
            ("UPDATE(blockers)", "v2_execution_routes", control),
        ):
            conn.execute(
                psycopg.sql.SQL(
                    "GRANT " + privilege + " ON " + tables + " TO {}"
                ).format(psycopg.sql.Identifier(role))
            )
        conn.execute(
            psycopg.sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                psycopg.sql.Identifier(control), psycopg.sql.Identifier(writer)
            )
        )
    opened = []

    @contextmanager
    def business_connection():
        with database() as conn:
            # Remove the fixture superuser's ability to switch to any role.
            conn.execute(
                psycopg.sql.SQL("SET SESSION AUTHORIZATION {}").format(
                    psycopg.sql.Identifier(writer)
                )
            )
            opened.append(conn)
            yield conn

    try:
        yield SimpleNamespace(
            connect=business_connection,
            admin=database,
            args=args,
            before=before,
            tenant=tenant,
            registry=registry,
            writer=writer,
            control=control,
            opened=opened,
        )
    finally:
        with database() as conn:
            for role in (writer, control):
                conn.execute(
                    psycopg.sql.SQL("DROP OWNED BY {}").format(
                        psycopg.sql.Identifier(role)
                    )
                )
                conn.execute(
                    psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role))
                )


@pytest.mark.parametrize("deployment_default", [False, True])
def test_separate_route_role_preserves_locks_and_policy_transaction(
    restricted_sampling_roles,
    monkeypatch,
    deployment_default,
):
    import services.v2_sampling_risk_policy as service

    ports = restricted_sampling_roles
    with pytest.raises(psycopg.errors.InsufficientPrivilege), ports.connect() as conn:
        conn.execute("SELECT account_id FROM v2_tenant_accounts")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), ports.connect() as conn:
        conn.execute(
            psycopg.sql.SQL("SET LOCAL ROLE {}").format(
                psycopg.sql.Identifier(ports.control)
            )
        )
        conn.execute("SELECT version FROM v2_business_state FOR UPDATE")

    observed = {}
    original_route, original_patch = service._bound_route, service.PolicyStore.patch

    def route(conn, *args, **kwargs):
        role, login, backend, transaction = conn.execute(
            "SELECT current_user,session_user,pg_backend_pid(),txid_current()"
        ).fetchone()
        assert (role, login) == (ports.control, ports.writer)
        observed.update(conn=conn, backend=backend, transaction=transaction)
        return original_route(conn, *args, **kwargs)

    def patch(store, *args, **kwargs):
        with store.connect() as conn:
            assert conn is observed["conn"] is ports.opened[-1]
            assert conn.execute(
                "SELECT current_user,session_user,pg_backend_pid(),txid_current()"
            ).fetchone() == (
                ports.writer,
                ports.writer,
                observed["backend"],
                observed["transaction"],
            )
            # Role restoration must not release either lock before the CAS.
            for statement, params in (
                (
                    "UPDATE v2_execution_routes SET phase='DRAINING' WHERE tenant_id=%s",
                    (ports.tenant,),
                ),
                (
                    "UPDATE v2_tenant_accounts SET version=2 WHERE registry_id=%s",
                    (ports.registry,),
                ),
                (
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("execution-route:" + ports.tenant + ":SANDBOX",),
                ),
            ):
                with (
                    pytest.raises(psycopg.errors.LockNotAvailable),
                    ports.admin() as other,
                ):
                    other.execute("SET LOCAL lock_timeout='50ms'")
                    other.execute(statement, params)
        return original_patch(store, *args, **kwargs)

    monkeypatch.setattr(service, "_bound_route", route)
    monkeypatch.setattr(service.PolicyStore, "patch", patch)
    if deployment_default:
        monkeypatch.setattr(service, "deployment_database", lambda: ports.connect)
        monkeypatch.setattr(service, "DEPLOYMENT_ROUTE_ROLE", ports.control)
        options = {}
    else:
        options = {"connect": ports.connect, "route_role": ports.control}
    result = service.main(execute_args(ports.args, ports.before), **options)
    assert result["status"] == "APPLIED"
    assert result["version"] == ports.before.version + 1
    assert PolicyStore(ports.admin, SCOPE).read().digest == result["digest"]


def test_rejected_route_under_control_role_rolls_back_without_policy_write(
    restricted_sampling_roles,
):
    ports = restricted_sampling_roles
    args = execute_args(ports.args, ports.before)
    args[args.index("--expected-route-epoch") + 1] = "2"
    with pytest.raises(ValueError, match="ACTIVE_ROUTE_CONFLICT"):
        main(args, connect=ports.connect, route_role=ports.control)
    assert PolicyStore(ports.admin, SCOPE).read() == ports.before
    with ports.connect() as conn:
        assert conn.execute("SELECT current_user").fetchone() == (ports.writer,)
    with ports.admin() as conn:
        conn.execute("SET LOCAL lock_timeout='50ms'")
        ExecutionRoutes._lock(conn, ports.tenant, "SANDBOX")
        conn.execute(
            "SELECT 1 FROM v2_execution_routes WHERE tenant_id=%s FOR UPDATE",
            (ports.tenant,),
        )


def test_dry_run_is_default_and_execute_is_exact_idempotent_cas(configured):
    database, args, before, *_ = configured
    preview = main(args, connect=database)
    store = PolicyStore(database, SCOPE)
    assert preview["status"] == "DRY_RUN"
    assert store.read() == before
    applied = main(execute_args(args, before), connect=database)
    assert applied["status"] == "APPLIED"
    assert applied["version"] == before.version + 1
    assert applied["digest"] == preview["after_digest"]
    after = store.read()
    assert {
        key for key in before.values if before.values[key] != after.values[key]
    } == {"sizing.risk_fraction", "portfolio_risk.max_fraction"}
    with pytest.raises(ValueError, match="POLICY_VERSION_CONFLICT"):
        main(execute_args(args, before), connect=database)
    assert main(args, connect=database)["already_applied"]
    assert (
        main(execute_args(args, after), connect=database)["status"] == "ALREADY_APPLIED"
    )
    assert store.read() == after
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_state_history WHERE state_id=%s",
            (store.key.identity,),
        ).fetchone() == (2,)


def test_wrong_digest_and_incomplete_execute_cannot_write(configured):
    database, args, before, *_ = configured
    bad = execute_args(args, before)
    bad[bad.index("--expected-digest") + 1] = "0" * 64
    with pytest.raises(ValueError, match="POLICY_DIGEST_CONFLICT"):
        main(bad, connect=database)
    with pytest.raises(SystemExit):
        main([*args, "--execute"], connect=lambda: pytest.fail("must not connect"))
    assert PolicyStore(database, SCOPE).read() == before


@pytest.mark.parametrize("field,value", [("epoch", "2"), ("phase", "DRAINING")])
def test_route_change_after_preview_rejects_activation(configured, field, value):
    database, args, before, tenant, *_ = configured
    main(args, connect=database)
    with database() as conn:
        conn.execute(
            psycopg.sql.SQL(
                "UPDATE v2_execution_routes SET {}=%s WHERE tenant_id=%s"
            ).format(psycopg.sql.Identifier(field)),
            (value, tenant),
        )
    with pytest.raises(ValueError, match="ACTIVE_ROUTE_CONFLICT"):
        main(execute_args(args, before), connect=database)
    assert PolicyStore(database, SCOPE).read() == before


@pytest.mark.parametrize(
    "flag", ["--account-id", "--tenant-id", "--expected-registry-id"]
)
def test_cross_account_or_tenant_cannot_change_policy(configured, flag):
    database, args, before, *_ = configured
    bad = list(args)
    bad[bad.index(flag) + 1] = (
        "other-account" if flag == "--account-id" else str(uuid4())
    )
    with pytest.raises(ValueError, match="BINDING"):
        main(execute_args(bad, before), connect=database)
    assert PolicyStore(database, SCOPE).read() == before


def test_route_is_locked_through_policy_cas(configured, monkeypatch):
    import services.v2_sampling_risk_policy as service

    database, args, before, tenant, *_ = configured
    original = service.build_sampling_risk_proof

    def concurrent_route_update(snapshot, scope):
        with pytest.raises(psycopg.errors.LockNotAvailable), database() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute(
                "UPDATE v2_execution_routes SET phase='DRAINING' WHERE tenant_id=%s",
                (tenant,),
            )
        return original(snapshot, scope)

    monkeypatch.setattr(service, "build_sampling_risk_proof", concurrent_route_update)
    assert main(execute_args(args, before), connect=database)["status"] == "APPLIED"


def test_busy_account_lock_fails_without_writing(configured):
    database, args, before, *_ = configured
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        with pytest.raises(psycopg.errors.LockNotAvailable):
            main(execute_args(args, before), connect=database)
    assert PolicyStore(database, SCOPE).read() == before


def test_concurrent_reviewed_applications_have_one_cas_winner(configured):
    database, args, before, *_ = configured

    def apply(_):
        try:
            return main(execute_args(args, before), connect=database)["status"]
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, range(2))) == [
            "APPLIED",
            "POLICY_VERSION_CONFLICT",
        ]


@pytest.mark.parametrize("state", ["retired", "pending", "binding"])
def test_retired_pending_or_rotated_binding_cannot_apply(configured, state):
    database, args, before, tenant, registry, _ = configured
    with database() as conn:
        if state == "retired":
            conn.execute(
                "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) "
                "VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
                (tenant, registry),
            )
        elif state == "binding":
            conn.execute(
                "UPDATE v2_tenant_accounts SET version=2 WHERE registry_id=%s",
                (registry,),
            )
        else:
            conn.execute(
                "INSERT INTO v2_execution_route_events(event_id,tenant_id,environment,epoch,"
                "request_id,operation,request_digest,result) VALUES (%s,%s,'SANDBOX',2,%s,'REQUEST','qa',%s)",
                (
                    str(uuid4()),
                    tenant,
                    str(uuid4()),
                    Jsonb(
                        {
                            "phase": "BLOCKED",
                            "execution_authorized": False,
                            "epoch": 2,
                            "target_registry": registry,
                            "binding_version": 1,
                        }
                    ),
                ),
            )
    with pytest.raises(ValueError, match="BINDING|PENDING_ROUTE_CHANGE"):
        main(execute_args(args, before), connect=database)
    assert PolicyStore(database, SCOPE).read() == before


def test_existing_above_ceiling_reservation_remains_and_close_can_submit(
    configured, monkeypatch
):
    import test_v2_account_risk as helpers
    from test_v2_capital_model import account

    from v2_core.evidence import DecisionEvidence
    from v2_core.managed_portfolio import publish_capital_snapshot

    database, args, before, tenant, registry, token = configured
    base = helpers.evidence()
    evidence = json.loads(base.snapshot_json)
    evidence["features"] = {
        "evaluation": {
            "market_plan": {
                "leverage": 2,
                "stop_fraction": "0.048",
                "margin_mode": "ISOLATED",
            }
        }
    }
    proof = DecisionEvidence(
        base.strategy_version, base.config_json, canonical(evidence)
    )
    monkeypatch.setattr(helpers, "evidence", lambda: proof)
    helpers.configured(database, max_notional=None, max_positions=None)
    now = helpers.now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account=account(),
        started=now,
        deadline=now + 60000,
        observation_id="qa-existing-sampling-position",
    )
    original, data, order, _ = helpers.prepare(database, quantity="3")
    assert helpers.runner(database).dispatch(order) == "ACKNOWLEDGED"
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="qa-open",
        quantity="3",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=now,
        evidence={"source": "qa"},
    )
    data.orders.transition(
        order, expected_version=3, status="FILLED", evidence={"source": "qa-fill-query"}
    )
    with database() as conn:
        reservation = conn.execute(
            "SELECT row_to_json(r) FROM v2_risk_reservations r"
        ).fetchall()
        nonpolicy_state = conn.execute(
            "SELECT row_to_json(s) FROM v2_business_state s WHERE state_id<>%s ORDER BY state_id",
            (PolicyStore(database, SCOPE).key.identity,),
        ).fetchall()
    main(execute_args(args, before), connect=database)
    after = PolicyStore(database, SCOPE).read()
    with database() as conn:
        assert (
            conn.execute("SELECT row_to_json(r) FROM v2_risk_reservations r").fetchall()
            == reservation
        )
        assert (
            conn.execute(
                "SELECT row_to_json(s) FROM v2_business_state s WHERE state_id<>%s ORDER BY state_id",
                (PolicyStore(database, SCOPE).key.identity,),
            ).fetchall()
            == nonpolicy_state
        )
        budget = portfolio_budget(
            conn, SCOPE, settings=after.values, capital={"base": "1000", "factor": "1"}
        )
    assert Decimal(budget["held_stop_risk"]) == 15
    assert Decimal(budget["portfolio_risk_limit"]) == 10
    assert Decimal(budget["remaining_stop_risk"]) == 0
    other = helpers.prepare(database, symbol="ETHUSDT", quantity="0.1")[2]
    assert helpers.runner(database).dispatch(other) == "DENIED"
    closing, _ = data.orders.prepare(original.intent_id, leg="CLOSE")
    with ExecutionRoutes(database).submit_guard(
        tenant_id=tenant,
        environment="SANDBOX",
        registry_id=registry,
        binding_version=1,
        epoch=1,
        worker_token=token,
        operation="MANAGE",
    ):
        assert helpers.runner(database).dispatch(closing) == "ACKNOWLEDGED"
