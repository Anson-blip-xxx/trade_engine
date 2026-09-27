from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_v2_credential_console import KEY, SECRET, add, case, database, http, setup

__all__ = ["case", "database", "setup"]


def payload():
    return {
        "request_id": str(uuid4()),
        "expected_version": 1,
        "api_key": KEY + "new",
        "api_secret": SECRET + "new",
    }


def test_rotation_preserves_identity_idempotent_and_encrypted(case):
    console, vault, _, _ = case
    registry = add(console)["registry_id"]
    before = console.overview(registry)
    body = payload()
    result = console.rotate(registry, **body)
    assert result["binding_version"] == 2 and not result["execution_authorized"]
    assert console.rotate(registry, **body) == result
    assert console.overview(registry) == before
    with console.connect() as c:
        ref = c.execute(
            "SELECT credential_ref FROM v2_tenant_accounts WHERE registry_id=%s",
            (registry,),
        ).fetchone()[0]
    assert (
        vault.resolve(console.tenant, ref, environment="SANDBOX")["api_key"]
        == body["api_key"]
    )
    assert KEY not in str(result) and SECRET not in str(result)


def test_stale_version_rolls_back_new_vault_record(case):
    console, _, _, _ = case
    registry = add(console)["registry_id"]
    console.rotate(registry, **payload())
    with console.connect() as c:
        count = c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0]
    with pytest.raises(ValueError):
        console.rotate(registry, **payload())
    with console.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == count
        )


def test_concurrent_rotation_one_winner(case):
    console, _, _, _ = case
    registry = add(console)["registry_id"]

    def attempt(_):
        try:
            return console.rotate(registry, **payload())["binding_version"]
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2)), key=lambda x: x or 0) == [None, 2]


def test_http_rotation_scope_csrf_and_no_environment_change(case):
    console, _, fixture, _ = case
    registry = add(console)["registry_id"]
    path = f"/api/accounts/{registry}/credentials"
    body = payload()
    assert http(console, path, body, {"Origin": "https://evil.invalid"})[0] == 403
    assert http(console, path, {**body, "environment": "LIVE"})[0] == 400
    assert http(console, f"/api/accounts/{fixture[6]}/credentials", body)[0] == 409
    assert http(console, path, body)[0] == 200
    assert http(console, path)[0] == 404
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, registry),
        )
    assert http(console, path, {**payload(), "expected_version": 2})[0] == 409
