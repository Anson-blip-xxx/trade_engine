from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from v2_core.account_registry import AccountRegistry

database = database_fixture


@pytest.fixture
def registry(database):
    with database() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_tenant_registry.sql"
            ).read_text()
        )
    return AccountRegistry(database)


def test_two_tenants_cannot_share_scope_or_read_each_others_accounts(registry):
    a = registry.create_tenant(uuid4(), "Owner A")
    b = registry.create_tenant(uuid4(), "Owner B")
    request = uuid4()
    ref = uuid4()
    first = registry.enroll(
        a, SCOPE, display_name="Account A", credential_ref=ref, request_id=request
    )
    assert (
        registry.enroll(
            a, SCOPE, display_name="Account A", credential_ref=ref, request_id=request
        )
        == first
    )
    assert first["execution_authorized"] is False
    assert registry.get(b, first["registry_id"]) is None
    with pytest.raises(ValueError, match="ALREADY_ENROLLED"):
        registry.enroll(
            b, SCOPE, display_name="Other", credential_ref=uuid4(), request_id=uuid4()
        )
    second = registry.enroll(
        a,
        replace(SCOPE, account_id="account-b"),
        display_name="Account B",
        credential_ref=uuid4(),
        request_id=uuid4(),
    )
    assert first["registry_id"] != second["registry_id"]


def test_rotation_is_versioned_idempotent_and_does_not_change_account_identity(
    registry,
):
    tenant = registry.create_tenant(uuid4(), "Owner")
    first = registry.enroll(
        tenant, SCOPE, display_name="A", credential_ref=uuid4(), request_id=uuid4()
    )
    args = {"credential_ref": uuid4(), "request_id": uuid4(), "expected_version": 1}
    rotated = registry.rotate_credential(tenant, first["registry_id"], **args)
    assert registry.rotate_credential(tenant, first["registry_id"], **args) == rotated
    saved = registry.get(tenant, first["registry_id"])
    assert saved["account_id"] == SCOPE.account_id and saved["version"] == 2
    with pytest.raises(ValueError, match="CONFLICT"):
        registry.rotate_credential(
            tenant,
            first["registry_id"],
            credential_ref=uuid4(),
            expected_version=1,
            request_id=uuid4(),
        )
    with pytest.raises(ValueError):
        registry.enroll(
            tenant,
            SCOPE,
            display_name="secret",
            credential_ref="raw-api-secret",
            request_id=uuid4(),
        )


def test_foreign_tenant_rotation_and_changed_request_are_rejected(registry):
    tenant = registry.create_tenant(uuid4(), "Owner")
    foreign = registry.create_tenant(uuid4(), "Other")
    request, ref = uuid4(), uuid4()
    first = registry.enroll(
        tenant, SCOPE, display_name="A", credential_ref=ref, request_id=request
    )
    with pytest.raises(ValueError, match="REGISTRY_REQUEST_CONFLICT"):
        registry.enroll(
            tenant,
            SCOPE,
            display_name="Changed",
            credential_ref=ref,
            request_id=request,
        )
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND_OR_VERSION_CONFLICT"):
        registry.rotate_credential(
            foreign,
            first["registry_id"],
            credential_ref=uuid4(),
            expected_version=1,
            request_id=uuid4(),
        )
    assert registry.get(tenant, first["registry_id"])["credential_ref"] == str(ref)
    with registry.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_registry_events").fetchone()[0] == 1
        )


def test_concurrent_enrollment_replay_creates_one_account_and_event(registry):
    tenant = registry.create_tenant(uuid4(), "Owner")
    request, ref = uuid4(), uuid4()

    def enroll(_):
        return registry.enroll(
            tenant, SCOPE, display_name="A", credential_ref=ref, request_id=request
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(enroll, range(8)))
    assert all(result == results[0] for result in results)
    with registry.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_tenant_accounts").fetchone()[0] == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_registry_events").fetchone()[0] == 1
        )


def test_concurrent_rotations_allow_only_one_version_winner(registry):
    tenant = registry.create_tenant(uuid4(), "Owner")
    first = registry.enroll(
        tenant, SCOPE, display_name="A", credential_ref=uuid4(), request_id=uuid4()
    )

    def rotate(_):
        try:
            return registry.rotate_credential(
                tenant,
                first["registry_id"],
                credential_ref=uuid4(),
                expected_version=1,
                request_id=uuid4(),
            )
        except ValueError as exc:
            assert str(exc) == "ACCOUNT_NOT_FOUND_OR_VERSION_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(rotate, range(4)))
    assert sum(result is not None for result in results) == 1
    assert registry.get(tenant, first["registry_id"])["version"] == 2
