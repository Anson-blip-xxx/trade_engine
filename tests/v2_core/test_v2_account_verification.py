from functools import partial
from uuid import uuid4

import pytest
from test_v2_credential_acceptance import (
    bound,
    case,
    database,
    prepared,
    setup,
)
from test_v2_credential_console import http

from services.v2_account_verification import AccountVerification
from services.v2_credential_acceptance import check_encrypted_credentials

__all__ = ["bound", "case", "database", "prepared", "setup"]


@pytest.fixture
def verifier(prepared):
    console, args = prepared
    calls = []

    def factory(**config):
        assert config["environment"] == "SANDBOX"
        assert not any(v for k, v in config.items() if k.startswith("enable_"))

        def request(method, path, params):
            calls.append((method, path, params))
            return {"dualSidePosition": False}

        return request

    service = AccountVerification(
        console,
        permit_factory=lambda _: lambda *args: True,
        clock_ms=lambda: 1,
        checker=partial(check_encrypted_credentials, transport_factory=factory),
    )
    return service, console, args, calls


def test_verify_cache_and_no_activation(verifier):
    service, _, args, calls = verifier
    registry = args["registry_id"]
    assert service.status(registry)["outcome"] == "NOT_CHECKED"
    first = service.verify(registry, expected_version=1)
    assert first["outcome"] == "SIGNED_READ_ACCEPTED" and first["fresh"]
    assert not first["execution_authorized"] and not first["cached"]
    assert "UTC+8" in first["checked_at"] or "+08:00" in first["checked_at"]
    assert service.verify(registry, expected_version=1)["cached"]
    assert calls == [("GET", "/fapi/v1/positionSide/dual", {})]


@pytest.mark.parametrize("bad_version", [0, 2, True, "1"])
def test_binding_version_rejected(verifier, bad_version):
    service, _, args, calls = verifier
    with pytest.raises(ValueError):
        service.verify(args["registry_id"], expected_version=bad_version)
    assert not calls


def test_foreign_live_retired_rejected_before_io(verifier):
    service, console, args, calls = verifier
    with console.connect() as c:
        foreign = c.execute(
            "SELECT registry_id FROM v2_tenant_accounts WHERE tenant_id<>%s LIMIT 1",
            (console.tenant,),
        ).fetchone()[0]
        live = c.execute(
            "SELECT registry_id FROM v2_tenant_accounts WHERE tenant_id=%s AND environment='LIVE'",
            (console.tenant,),
        ).fetchone()[0]
    for registry in [foreign, live, str(uuid4())]:
        with pytest.raises(ValueError):
            service.verify(registry, expected_version=1)
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, args["registry_id"]),
        )
    with pytest.raises(ValueError):
        service.verify(args["registry_id"], expected_version=1)
    assert not calls


def test_concurrent_verify_fails_closed(verifier):
    service, console, args, calls = verifier
    with console.connect() as c:
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("account-verification:" + args["registry_id"],),
        )
        with pytest.raises(ValueError, match="BUSY"):
            service.verify(args["registry_id"], expected_version=1)
    assert not calls


def test_stale_receipt_is_not_readiness(verifier):
    service, console, args, _ = verifier
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_credential_checks(check_id,tenant_id,registry_id,binding_version,outcome,created_at) VALUES (%s,%s,%s,1,'SIGNED_READ_ACCEPTED',clock_timestamp()-interval '1 hour')",
            (str(uuid4()), console.tenant, args["registry_id"]),
        )
    result = service.status(args["registry_id"])
    assert not result["fresh"] and not result["execution_authorized"]


def test_http_verification_auth_and_scope(verifier):
    service, console, args, calls = verifier
    path = "/api/accounts/" + args["registry_id"] + "/verification"
    assert http(console, path, {"expected_version": 1}, verification=service)[0] == 200
    assert http(console, path, verification=service)[0] == 200
    assert (
        http(
            console,
            path,
            {"expected_version": 1},
            {"Origin": "https://evil.invalid"},
            verification=service,
        )[0]
        == 403
    )
    assert (
        http(
            console,
            path,
            {"expected_version": 1, "tenant_id": console.tenant},
            verification=service,
        )[0]
        == 400
    )
    assert len(calls) == 1
