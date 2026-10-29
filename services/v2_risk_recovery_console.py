"""Authenticated narrow Testnet probe authorization; no venue calls or secrets."""

from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from v2_core.account_registry import uid

ERRORS = frozenset(
    {
        "RECOVERY_ACCOUNT_NOT_FOUND",
        "RECOVERY_REQUEST_INVALID",
        "RECOVERY_REQUEST_CONFLICT",
        "RECOVERY_PREVIEW_EXPIRED",
        "RECOVERY_STATE_CHANGED",
        "RECOVERY_NOT_ELIGIBLE",
    }
)


class RiskRecoveryConsole:
    def __init__(self, connect, *, tenant_id, actor):
        if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 120:
            raise ValueError("AUTHENTICATED_ACTOR_REQUIRED")
        self.connect, self.tenant, self.actor = connect, uid(tenant_id), actor

    def inspect(self, registry):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            result = conn.execute(
                "SELECT v2_web_risk_recovery_check(%s,%s)",
                (self.tenant, uid(registry)),
            ).fetchone()[0]
            result["history"] = [
                row[0]
                for row in conn.execute(
                    """SELECT jsonb_build_object('at_ms',result->'at_ms',
                   'status',result->'status','reason',document->'reason')
                   FROM v2_risk_recovery_events WHERE tenant_id=%s AND registry_id=%s
                   ORDER BY created_at DESC LIMIT 10""",
                    (self.tenant, uid(registry)),
                )
            ]
            return result

    def apply(self, registry, *, request_id, proof, expires_at_ms, reason):
        if (
            not isinstance(proof, dict)
            or type(expires_at_ms) is not int
            or not isinstance(reason, str)
            or not 5 <= len(reason.strip()) <= 240
            or any(ord(c) < 32 for c in reason)
        ):
            raise ValueError("RECOVERY_REQUEST_INVALID")
        document = {
            "actor": self.actor,
            "reason": reason.strip(),
            "proof": proof,
            "expires_at_ms": expires_at_ms,
        }
        try:
            with self.connect() as conn:
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                return conn.execute(
                    "SELECT v2_web_risk_recovery_apply(%s,%s,%s,%s)",
                    (self.tenant, uid(registry), uid(request_id), Jsonb(document)),
                ).fetchone()[0]
        except RaiseException as exc:
            code = exc.diag.message_primary
            raise ValueError(
                code if code in ERRORS else "RECOVERY_NOT_ELIGIBLE"
            ) from None
