"""Monotonic SANDBOX entry drain; not a completed account switch.

Pre-existing submission permits may still reach the venue. Never erase them,
release reservations or infer flatness. No activation/unhalt operation exists.
"""

from contextlib import nullcontext
from dataclasses import asdict

from v2_core.account_registry import uid
from v2_core.account_risk import AccountRiskDenied, AccountScope, _lock
from v2_core.evidence import canonical
from v2_core.scoping import FIELDS
from v2_core.state import BusinessState, StateKey


def drain_key(scope):
    return StateKey(
        **{field: getattr(scope, field) for field in FIELDS},
        namespace="account-opening-drain-v1",
        key="gate",
    )


def require_not_draining(conn, scope):
    # Lock order remains intent root -> account risk lock -> scoped state.
    _lock(conn, canonical({field: getattr(scope, field) for field in FIELDS}))
    if BusinessState(lambda: nullcontext(conn)).read(drain_key(scope)) is not None:
        # Tombstones/malformed state also fail closed; no implicit unhalt.
        raise AccountRiskDenied("ACCOUNT_OPENING_DRAINING")


class AccountDraining:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def _scope(c, tenant, registry):
        row = c.execute(
            "SELECT exchange,account_id,environment,product FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
            (tenant, registry),
        ).fetchone()
        if row is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        scope = AccountScope(*row)
        if scope.environment != "SANDBOX":
            raise ValueError("SANDBOX_DRAIN_ONLY")
        return scope

    def begin(self, tenant_id, registry_id, *, request_id):
        tenant, registry, request = uid(tenant_id), uid(registry_id), uid(request_id)
        with self.connect() as c:
            scope = self._scope(c, tenant, registry)
            _lock(c, scope.key)
            store = BusinessState(lambda: nullcontext(c))
            key = drain_key(scope)
            if store.read(key) is None:
                result = store.change(
                    key,
                    expected_version=0,
                    request_key="begin-drain",
                    payload={
                        "tenant_id": tenant,
                        "registry_id": registry,
                        "request_id": request,
                        "status": "DRAINING",
                        "reason": "ACCOUNT_SWITCH_PREPARATION",
                    },
                    reason="ACCOUNT_OPENING_DRAIN_REQUESTED",
                )
                if result.code != "APPLIED":
                    raise ValueError("DRAIN_STATE_CONFLICT")
            return self._snapshot(c, scope, store.read(key))

    def inspect(self, tenant_id, registry_id):
        with self.connect() as c:
            scope = self._scope(c, uid(tenant_id), uid(registry_id))
            _lock(c, scope.key)
            return self._snapshot(
                c, scope, BusinessState(lambda: nullcontext(c)).read(drain_key(scope))
            )

    @staticmethod
    def _snapshot(c, scope, state):
        rows = c.execute(
            """SELECT o.status,count(*) FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
            WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
            AND o.leg='OPEN' AND o.status IN ('PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED') GROUP BY o.status""",
            tuple(asdict(scope).values()),
        ).fetchall()
        counts = dict(rows)
        return {
            "status": "DRAINING" if state is not None else "NOT_DRAINING",
            "gate_version": state.version if state else None,
            "pending_open_orders": counts,
            "existing_permits_pending": sum(
                counts.get(k, 0) for k in ("SUBMITTING", "UNKNOWN", "ACKNOWLEDGED")
            ),
            "venue_flat_verified": False,
            "switch_complete": False,
            "target_activation_authorized": False,
        }
