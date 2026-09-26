"""Owner-bound internal account console. No exchange I/O or activation.

Caller must bind tenant from authenticated server context, never request JSON.
The existing read-only operator dashboard is deliberately unchanged.
"""

from contextlib import nullcontext
from uuid import UUID, uuid5

from v2_core.account_registry import AccountRegistry, label, uid
from v2_core.account_risk import AccountScope
from v2_core.credential_vault import CredentialVault


class AccountConsole:
    def __init__(self, connect, *, tenant_id, key_provider, active_key_id):
        self.connect, self.tenant = connect, uid(tenant_id)
        self.key_provider, self.active_key_id = key_provider, active_key_id

    def add(self, *, request_id, alias, environment, api_key, api_secret):
        request, alias = uid(request_id), label(alias)
        if environment != "SANDBOX":
            raise ValueError("SANDBOX_ENROLLMENT_ONLY")
        # Same request deterministically addresses the same encrypted credential.
        ref = str(uuid5(UUID(self.tenant), "credential:" + request))
        account = "registered-" + str(uuid5(UUID(self.tenant), "account:" + request))
        with self.connect() as c:
            nested = lambda: nullcontext(c)
            CredentialVault(
                nested, key_provider=self.key_provider, active_key_id=self.active_key_id
            ).put(
                self.tenant,
                ref,
                environment=environment,
                api_key=api_key,
                api_secret=api_secret,
            )
            result = AccountRegistry(nested).enroll(
                self.tenant,
                AccountScope("BINANCE", account, environment, "FUTURES"),
                display_name=alias,
                credential_ref=ref,
                request_id=request,
            )
        return {
            "registry_id": result["registry_id"],
            "status": result["status"],
            "execution_authorized": False,
        }

    def accounts(self):
        with self.connect() as c:
            rows = c.execute(
                """SELECT a.registry_id::text,COALESCE(n.alias,a.display_name),
                a.environment,a.status,COALESCE(n.version,0),a.version,
                EXISTS(SELECT 1 FROM v2_credential_vault v WHERE v.tenant_id=a.tenant_id AND v.credential_ref=a.credential_ref AND v.environment=a.environment)
                FROM v2_tenant_accounts a LEFT JOIN v2_account_aliases n USING(tenant_id,registry_id)
                WHERE a.tenant_id=%s ORDER BY a.created_at,a.registry_id LIMIT 100""",
                (self.tenant,),
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "registry_id",
                        "alias",
                        "environment",
                        "status",
                        "alias_version",
                        "binding_version",
                        "encrypted_credential_present",
                    ),
                    r,
                    strict=True,
                )
            )
            for r in rows
        ]

    def rename(self, registry_id, *, alias, expected_version, request_id):
        registry, alias, request = uid(registry_id), label(alias), uid(request_id)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("ALIAS_VERSION_REQUIRED")
        with self.connect() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("account-alias:" + self.tenant,),
            )
            if not c.execute(
                "SELECT 1 FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
                (self.tenant, registry),
            ).fetchone():
                raise ValueError("ACCOUNT_NOT_FOUND")
            prior = c.execute(
                "SELECT registry_id::text,alias,version FROM v2_account_alias_events WHERE tenant_id=%s AND request_id=%s",
                (self.tenant, request),
            ).fetchone()
            if prior:
                if prior != (registry, alias, expected_version + 1):
                    raise ValueError("ALIAS_REQUEST_CONFLICT")
                return {"version": prior[2]}
            current = c.execute(
                "SELECT version FROM v2_account_aliases WHERE tenant_id=%s AND registry_id=%s",
                (self.tenant, registry),
            ).fetchone()
            if (current[0] if current else 0) != expected_version:
                raise ValueError("ALIAS_VERSION_CONFLICT")
            version = expected_version + 1
            c.execute(
                "INSERT INTO v2_account_aliases(tenant_id,registry_id,alias,version) VALUES (%s,%s,%s,%s) ON CONFLICT(tenant_id,registry_id) DO UPDATE SET alias=EXCLUDED.alias,version=EXCLUDED.version",
                (self.tenant, registry, alias, version),
            )
            c.execute(
                "INSERT INTO v2_account_alias_events(tenant_id,registry_id,request_id,alias,version) VALUES (%s,%s,%s,%s,%s)",
                (self.tenant, registry, request, alias, version),
            )
        return {"version": version}

    def overview(self, registry_id):
        registry = uid(registry_id)
        with self.connect() as c:
            scope = c.execute(
                "SELECT exchange,account_id,environment,product FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
                (self.tenant, registry),
            ).fetchone()
            if scope is None:
                raise ValueError("ACCOUNT_NOT_FOUND")
            rows = c.execute(
                """SELECT i.intent_id::text,i.status,i.payload->>'symbol',i.created_at,
                s.evidence->>'net_pnl',s.settled_at
                FROM v2_trade_intents i LEFT JOIN LATERAL (
                    SELECT evidence,settled_at FROM v2_settlements WHERE episode_id=i.intent_id AND currency='USDT' ORDER BY revision DESC LIMIT 1
                ) s ON TRUE WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                ORDER BY i.created_at DESC,i.intent_id LIMIT 100""",
                scope,
            ).fetchall()
            summary = c.execute(
                """SELECT count(*),count(s.episode_id),COALESCE(sum((s.evidence->>'net_pnl')::numeric),0)
                FROM v2_trade_intents i LEFT JOIN LATERAL (
                    SELECT episode_id,evidence FROM v2_settlements WHERE episode_id=i.intent_id AND currency='USDT' ORDER BY revision DESC LIMIT 1
                ) s ON TRUE WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
                scope,
            ).fetchone()
        from services.v2_dashboard import _value

        return {
            "registry_id": registry,
            "environment": scope[2],
            "execution_authorized": False,
            "summary": {
                "trade_intents": summary[0],
                "settled_trades": summary[1],
                "settled_net_pnl_usdt": str(summary[2]),
            },
            "trades": [
                dict(
                    zip(
                        (
                            "id",
                            "status",
                            "symbol",
                            "created_at",
                            "net_pnl_usdt",
                            "settled_at",
                        ),
                        map(_value, r),
                        strict=True,
                    )
                )
                for r in rows
            ],
        }
