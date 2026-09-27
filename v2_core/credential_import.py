"""Atomic, idempotent import preserving an existing account's historical scope.

Internal migration step only: no source deletion, exchange I/O, activation,
registry rotation or automatic adoption of another tenant's account.
"""

from contextlib import nullcontext
from uuid import UUID, uuid5

from v2_core.account_registry import AccountRegistry, label, uid
from v2_core.account_risk import AccountScope
from v2_core.credential_vault import CredentialVault, VaultError


def import_existing_credentials(
    connect,
    *,
    tenant_id,
    scope,
    alias,
    request_id,
    api_key,
    api_secret,
    key_provider,
    active_key_id,
):
    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise VaultError("SANDBOX_IMPORT_ONLY")
    tenant, request, alias = uid(tenant_id), uid(request_id), label(alias)
    ref = str(uuid5(UUID(tenant), "existing-account-import:" + request))
    with connect() as c:
        nested = lambda: nullcontext(c)
        # Match registry lock order. Serialize imports, enrollment and rotations.
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("tenant-registry:" + tenant,),
        )
        vault = CredentialVault(
            nested, key_provider=key_provider, active_key_id=active_key_id
        )
        vault.put(
            tenant,
            ref,
            environment=scope.environment,
            api_key=api_key,
            api_secret=api_secret,
        )
        result = AccountRegistry(nested).enroll(
            tenant, scope, display_name=alias, credential_ref=ref, request_id=request
        )
        # Roundtrip before commit; failed key-provider/readback rolls back both rows.
        decoded = vault.resolve(tenant, ref, environment=scope.environment)
        if decoded != {"api_key": api_key, "api_secret": api_secret}:
            raise VaultError("IMPORT_ROUNDTRIP_FAILED")
    return {
        "registry_id": result["registry_id"],
        "credential_ref": ref,
        "binding_version": result["version"],
        "status": "IMPORTED_UNVERIFIED",
        "execution_authorized": False,
        "source_cleanup_authorized": False,
    }
