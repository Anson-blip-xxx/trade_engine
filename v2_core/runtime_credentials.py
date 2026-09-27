"""Pinned, account-bound Testnet credential resolution, not switch activation.

Startup validation cannot stop an already running old worker. Worker fencing and
safe activation remain separate rollout requirements; registry IDs are not auth.
"""

from contextlib import nullcontext
from dataclasses import asdict

from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.credential_vault import CredentialVault, VaultError


def resolve_runtime_credentials(
    connect, *, scope, tenant_id, registry_id, binding_version, key_provider
):
    if (
        not isinstance(scope, AccountScope)
        or scope.environment != "SANDBOX"
        or type(binding_version) is not int
        or binding_version < 1
    ):
        raise VaultError("PINNED_SANDBOX_BINDING_REQUIRED")
    tenant, registry = uid(tenant_id), uid(registry_id)
    with connect() as c:
        row = c.execute(
            "SELECT exchange,account_id,environment,product,credential_ref::text,version FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s FOR SHARE",
            (tenant, registry),
        ).fetchone()
        if (
            row is None
            or row[:4] != tuple(asdict(scope).values())
            or row[5] != binding_version
        ):
            raise VaultError("RUNTIME_CREDENTIAL_BINDING_MISMATCH")
        return CredentialVault(
            lambda: nullcontext(c), key_provider=key_provider, active_key_id="read-only"
        ).resolve(tenant, row[4], environment=scope.environment)
