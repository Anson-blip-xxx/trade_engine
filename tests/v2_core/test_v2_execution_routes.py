from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_v2_credential_console import add, case, database, setup

from v2_core.execution_routes import ExecutionRoutes

__all__ = ["case", "database", "setup"]


@pytest.fixture
def routes(case):
    console, *_ = case
    with console.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260927_credential_checks.sql"
            ).read_text()
        )
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260928_execution_routes.sql"
            ).read_text()
        )
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260928_execution_draining.sql"
            ).read_text()
        )
    return ExecutionRoutes(console.connect), console


def request(store, console, registry, **changes):
    args = {
        "target_registry": registry,
        "binding_version": 1,
        "expected_epoch": 0,
        "request_id": str(uuid4()),
    }
    args.update(changes)
    return store.request(console.tenant, "SANDBOX", **args)


def test_request_never_activates_and_replay_is_idempotent(routes):
    store, console = routes
    registry = add(console)["registry_id"]
    rid = str(uuid4())
    assert store.inspect(console.tenant, "SANDBOX")["epoch"] == 0
    first = request(store, console, registry, request_id=rid)
    assert first["phase"] == "BLOCKED" and not first["execution_authorized"]
    assert request(store, console, registry, request_id=rid) == first
    with pytest.raises(ValueError, match="CONFLICT"):
        request(store, console, registry, request_id=rid, expected_epoch=1)


def test_environments_are_independent_and_cannot_cross(routes):
    store, console = routes
    sandbox = add(console)["registry_id"]
    live = add(console, environment="LIVE")["registry_id"]
    request(store, console, sandbox)
    with pytest.raises(ValueError):
        request(store, console, live, expected_epoch=1)
    result = store.request(
        console.tenant,
        "LIVE",
        target_registry=live,
        binding_version=1,
        expected_epoch=0,
        request_id=str(uuid4()),
    )
    assert "LIVE_DEPLOYMENT_NOT_APPROVED" in result["blockers"]
    assert store.inspect(console.tenant, "SANDBOX")["target_registry"] == sandbox


def test_concurrent_requests_one_winner(routes):
    store, console = routes
    registry = add(console)["registry_id"]

    def attempt(_):
        try:
            return request(store, console, registry)["epoch"]
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2)), key=lambda x: x or 0) == [None, 1]


def test_stale_worker_and_retired_binding_cannot_submit(routes):
    store, console = routes
    registry = add(console)["registry_id"]
    request(store, console, registry)
    token = str(uuid4())
    args = {
        "tenant_id": console.tenant,
        "environment": "SANDBOX",
        "registry_id": registry,
        "binding_version": 1,
        "epoch": 1,
        "worker_token": token,
    }
    with pytest.raises(ValueError, match="FENCED"), store.submit_guard(**args):
        pytest.fail("write must not run")
    # Simulated trusted controller only, not any exposed web action.
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token) VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (console.tenant, registry, token),
        )
    with store.submit_guard(**args):
        import psycopg

        with pytest.raises(psycopg.errors.LockNotAvailable), console.connect() as c:
            c.execute("SET LOCAL lock_timeout='100ms'")
            c.execute(
                "UPDATE v2_execution_routes SET phase='DRAINING' WHERE tenant_id=%s",
                (console.tenant,),
            )
    with (
        pytest.raises(ValueError, match="FENCED"),
        store.submit_guard(**{**args, "worker_token": str(uuid4())}),
    ):
        pass
    pending = request(store, console, registry, expected_epoch=1)
    assert pending["epoch"] == 2
    assert store.inspect(console.tenant, "SANDBOX")["phase"] == "ACTIVE"
    with pytest.raises(ValueError, match="ROTATION_REQUIRES_HANDOVER"):
        console.rotate(
            registry,
            request_id=str(uuid4()),
            expected_version=1,
            api_key="qa-replacement-key-12345",
            api_secret="qa-replacement-secret-12345",
        )
    with console.connect() as c:
        c.execute(
            "UPDATE v2_execution_routes SET phase='DRAINING' WHERE tenant_id=%s",
            (console.tenant,),
        )
    with pytest.raises(ValueError, match="FENCED"), store.submit_guard(**args):
        pass
    with store.submit_guard(**args, operation="MANAGE"):
        pass
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, registry),
        )
    with (
        pytest.raises(ValueError, match="REJECTED"),
        store.submit_guard(**args, operation="MANAGE"),
    ):
        pass


def test_cross_tenant_rejected(routes):
    store, console = routes
    with console.connect() as c:
        registry = c.execute(
            "SELECT registry_id FROM v2_tenant_accounts WHERE tenant_id<>%s LIMIT 1",
            (console.tenant,),
        ).fetchone()[0]
    with pytest.raises(ValueError):
        request(store, console, registry)
