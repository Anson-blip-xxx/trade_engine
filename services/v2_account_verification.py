"""Owner-scoped, Testnet-only signed-read verification. Never activates workers."""

from contextlib import nullcontext

from services.v2_credential_acceptance import check_encrypted_credentials
from services.v2_dashboard import _value
from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope


class AccountVerification:
    def __init__(
        self,
        console,
        *,
        permit_factory,
        clock_ms,
        cooldown_seconds=60,
        freshness_seconds=300,
        checker=check_encrypted_credentials,
    ):
        if any(
            type(v) is not int or not 1 <= v <= 3600
            for v in (cooldown_seconds, freshness_seconds)
        ):
            raise ValueError("BOUNDED_VERIFICATION_INTERVAL_REQUIRED")
        self.console, self.permit_factory, self.clock_ms = (
            console,
            permit_factory,
            clock_ms,
        )
        self.cooldown, self.freshness, self.checker = (
            cooldown_seconds,
            freshness_seconds,
            checker,
        )

    def _binding(self, c, registry):
        row = c.execute(
            "SELECT exchange,account_id,environment,product,version FROM v2_tenant_accounts "
            "WHERE tenant_id=%s AND registry_id=%s FOR SHARE",
            (self.console.tenant, registry),
        ).fetchone()
        if (
            row is None
            or c.execute(
                "SELECT 1 FROM v2_account_retirements WHERE tenant_id=%s AND registry_id=%s",
                (self.console.tenant, registry),
            ).fetchone()
        ):
            raise ValueError("ACCOUNT_NOT_FOUND")
        return row

    def _status(self, c, registry, binding):
        row = c.execute(
            "SELECT outcome,diagnostic,created_at,extract(epoch FROM clock_timestamp()-created_at) "
            "FROM v2_credential_checks WHERE tenant_id=%s AND registry_id=%s AND binding_version=%s "
            "ORDER BY created_at DESC,check_id DESC LIMIT 1",
            (self.console.tenant, registry, binding[4]),
        ).fetchone()
        return {
            "registry_id": registry,
            "environment": binding[2],
            "binding_version": binding[4],
            "outcome": row[0] if row else "NOT_CHECKED",
            "diagnostic": row[1] if row else {},
            "checked_at": _value(row[2]) if row else None,
            "fresh": bool(row and 0 <= row[3] <= self.freshness),
            "retry_after_seconds": max(0, self.cooldown - int(row[3])) if row else 0,
            "execution_authorized": False,
            "venue_identity_verified": False,
        }

    def status(self, registry_id):
        registry = uid(registry_id)
        with self.console.connect() as c:
            return self._status(c, registry, self._binding(c, registry))

    def verify(self, registry_id, *, expected_version):
        registry = uid(registry_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("BINDING_VERSION_REQUIRED")
        with self.console.connect() as c:
            if not c.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                ("account-verification:" + registry,),
            ).fetchone()[0]:
                raise ValueError("ACCOUNT_VERIFICATION_BUSY")
            binding = self._binding(c, registry)
            if binding[2] != "SANDBOX" or binding[4] != expected_version:
                raise ValueError("PINNED_SANDBOX_BINDING_REQUIRED")
            previous = self._status(c, registry, binding)
            if previous["retry_after_seconds"]:
                return {**previous, "cached": True}
            self.checker(
                lambda: nullcontext(c),
                tenant_id=self.console.tenant,
                registry_id=registry,
                scope=AccountScope(*binding[:4]),
                binding_version=binding[4],
                key_provider=self.console.key_provider,
                permit=self.permit_factory(binding[1]),
                clock_ms=self.clock_ms,
            )
            return {**self._status(c, registry, binding), "cached": False}
