"""Durable Sandbox switch preparation; explicitly NOT target activation.

Account claims prevent overlapping switch preparations, not trading workers.
Venue identity, readiness and target execution ownership remain separate gates.
"""

from contextlib import nullcontext
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_draining import AccountDraining
from v2_core.account_registry import uid


class AccountSwitches:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def _lock(c, tenant):
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("account-switch:" + tenant,),
        )

    @staticmethod
    def _accounts(c, tenant, source, target):
        if source == target:
            raise ValueError("DISTINCT_SWITCH_ACCOUNTS_REQUIRED")
        rows = c.execute(
            "SELECT registry_id::text,exchange,environment,product,version,status "
            "FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=ANY(%s::uuid[]) "
            "ORDER BY registry_id FOR SHARE",
            (tenant, [source, target]),
        ).fetchall()
        if len(rows) != 2:
            raise ValueError("SWITCH_ACCOUNT_NOT_FOUND")
        accounts = {r[0]: r[1:] for r in rows}
        if (
            accounts[source][:3] != accounts[target][:3]
            or accounts[source][1] != "SANDBOX"
        ):
            raise ValueError("SANDBOX_SWITCH_SCOPE_REQUIRED")
        return accounts

    @staticmethod
    def _row(c, tenant, switch):
        row = c.execute(
            "SELECT source_registry::text,target_registry::text,source_version,target_version,version,status "
            "FROM v2_account_switches WHERE tenant_id=%s AND switch_id=%s",
            (tenant, switch),
        ).fetchone()
        if row is None:
            raise ValueError("SWITCH_NOT_FOUND")
        return row

    @staticmethod
    def _result(switch, row):
        return {
            "switch_id": switch,
            "source_registry": row[0],
            "target_registry": row[1],
            "source_version": row[2],
            "target_version": row[3],
            "version": row[4],
            "status": row[5],
            "target_activation_authorized": False,
            "switch_complete": False,
        }

    @staticmethod
    def _event(c, tenant, switch, version, status, evidence):
        c.execute(
            "INSERT INTO v2_account_switch_events(event_id,tenant_id,switch_id,version,status,evidence) VALUES (%s,%s,%s,%s,%s,%s)",
            (str(uuid4()), tenant, switch, version, status, Jsonb(evidence)),
        )

    def request(self, tenant_id, source_registry, target_registry, *, request_id):
        tenant, source, target, request = map(
            uid, (tenant_id, source_registry, target_registry, request_id)
        )
        with self.connect() as c:
            self._lock(c, tenant)
            accounts = self._accounts(c, tenant, source, target)
            prior = c.execute(
                "SELECT switch_id::text,source_registry::text,target_registry::text FROM v2_account_switches WHERE tenant_id=%s AND request_id=%s",
                (tenant, request),
            ).fetchone()
            if prior:
                if prior[1:] != (source, target):
                    raise ValueError("SWITCH_REQUEST_CONFLICT")
                return self._result(prior[0], self._row(c, tenant, prior[0]))
            if c.execute(
                "SELECT 1 FROM v2_account_switch_claims WHERE registry_id=ANY(%s::uuid[])",
                ([source, target],),
            ).fetchone():
                raise ValueError("ACCOUNT_ALREADY_IN_SWITCH")
            switch = str(uuid4())
            c.execute(
                "INSERT INTO v2_account_switches(switch_id,tenant_id,request_id,source_registry,target_registry,source_version,target_version,version,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,1,'REQUESTED')",
                (
                    switch,
                    tenant,
                    request,
                    source,
                    target,
                    accounts[source][3],
                    accounts[target][3],
                ),
            )
            for account in (source, target):
                c.execute(
                    "INSERT INTO v2_account_switch_claims(registry_id,tenant_id,switch_id) VALUES (%s,%s,%s)",
                    (account, tenant, switch),
                )
            self._event(c, tenant, switch, 1, "REQUESTED", {"request_id": request})
            return self._result(switch, self._row(c, tenant, switch))

    def advance(self, tenant_id, switch_id, *, expected_version, action):
        """Explicitly cancel or drain source only. Never an automatic readiness decision."""
        tenant, switch = uid(tenant_id), uid(switch_id)
        if (
            type(expected_version) is not int
            or expected_version < 1
            or action not in {"CANCEL", "DRAIN_SOURCE"}
        ):
            raise ValueError("EXPLICIT_SWITCH_ACTION_REQUIRED")
        desired = "CANCELLED" if action == "CANCEL" else "DRAINING"
        with self.connect() as c:
            self._lock(c, tenant)
            row = self._row(c, tenant, switch)
            if row[5] == desired and row[4] == expected_version + 1:
                return self._result(switch, row)
            if row[4] != expected_version or row[5] != "REQUESTED":
                raise ValueError("SWITCH_VERSION_OR_STATE_CONFLICT")
            accounts = self._accounts(c, tenant, row[0], row[1])
            evidence = {"action": action}
            if action == "DRAIN_SOURCE":
                if (accounts[row[0]][3], accounts[row[1]][3]) != row[2:4]:
                    raise ValueError("SWITCH_ACCOUNT_BINDING_CHANGED")
                evidence["source_drain"] = AccountDraining(
                    lambda: nullcontext(c)
                ).begin(tenant, row[0], request_id=switch)
            else:
                c.execute(
                    "DELETE FROM v2_account_switch_claims WHERE tenant_id=%s AND switch_id=%s",
                    (tenant, switch),
                )
            c.execute(
                "UPDATE v2_account_switches SET status=%s,version=version+1 WHERE tenant_id=%s AND switch_id=%s",
                (desired, tenant, switch),
            )
            self._event(c, tenant, switch, row[4] + 1, desired, evidence)
            return self._result(switch, self._row(c, tenant, switch))

    def inspect(self, tenant_id, switch_id):
        tenant, switch = uid(tenant_id), uid(switch_id)
        with self.connect() as c:
            self._lock(c, tenant)
            row = self._row(c, tenant, switch)
            accounts = self._accounts(c, tenant, row[0], row[1])
            result = self._result(switch, row)
            blockers = [
                "VENUE_IDENTITY_UNVERIFIED",
                "TARGET_READINESS_UNVERIFIED",
                "EXECUTION_OWNERSHIP_UNVERIFIED",
            ]
            if (accounts[row[0]][3], accounts[row[1]][3]) != row[2:4]:
                blockers.append("ACCOUNT_BINDING_CHANGED")
            result["activation_blockers"] = blockers
            result["source_drain"] = AccountDraining(lambda: nullcontext(c)).inspect(
                tenant, row[0]
            )
            return result
