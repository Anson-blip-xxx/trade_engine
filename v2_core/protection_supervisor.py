"""Bounded Testnet protection recovery, not an opening or replacement PM.

Exchange GET only. A session advisory lock serializes account scans with the
bounded protocol runner. Durable rotating cursor prevents poison-item starvation.
No DB transaction spans exchange I/O; failed checkpoints are safely replayed.
"""

import json
from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical, digest
from v2_core.operational import OperationalProjector
from v2_core.protection import ProtectionSpec
from v2_core.protection_child import ProtectionChildReconciler
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey


class ProtectionAlertProjector(OperationalProjector):
    def __init__(self, connect, *, scope, sink):
        self.account = scope
        super().__init__(connect, "protection-tg:" + digest(scope.key), sink)

    def _scope_filter(self):
        return (
            "e.event_type='PROTECTION_RECOVERY' AND e.scope_id=%s AND e.payload->'account_scope'=%s::jsonb",
            (self.account.environment, self.account.key),
        )


class ProtectionSupervisor:
    def __init__(self, connect, request, *, scope):
        def read(method, path, params):
            if method != "GET":
                raise ValueError("PROTECTION_SUPERVISOR_QUERY_ONLY")
            return request(method, path, params)

        read.account_id = request.account_id
        read.environment = request.environment
        self.child = ProtectionChildReconciler(connect, read, scope=scope)
        self.connect, self.scope = connect, scope
        self.store = BusinessState(connect)
        self.cursor = StateKey(
            **asdict(scope), namespace="protection-supervisor-v1", key="cursor"
        )

    def _result(self, payload):
        spec = ProtectionSpec(**payload["spec"])
        result = self.child.reconcile(spec)
        if result["status"] == "WAITING_TRIGGER":
            trace = TradingData(self.connect).trace(spec.episode)
            legs = {o["order_id"]: o["leg"] for o in trace["orders"]}
            if any(
                o["status"] not in {"FILLED", "CANCELLED", "REJECTED"}
                for o in trace["orders"]
            ):
                return {
                    "status": "EXPOSURE_UNCONFIRMED",
                    "parent_status": result["parent_status"],
                }
            with localcontext() as ctx:
                ctx.prec = 100
                remaining = sum(
                    (
                        Decimal(f["quantity"])
                        * (1 if legs[f["order_id"]] == "OPEN" else -1)
                        for f in trace["fills"]
                    ),
                    Decimal(0),
                )
            if result["parent_status"] in {"CANCELED", "EXPIRED", "REJECTED"}:
                result["status"] = (
                    "UNPROTECTED" if remaining else "TERMINAL_FLAT_LEDGER"
                )
            elif not remaining:
                result["status"] = "PROTECTION_WITHOUT_LEDGER_EXPOSURE"
        return result

    def _record(self, state_id, result):
        key = StateKey(
            **asdict(self.scope),
            namespace="protection-recovery-result-v1",
            key=state_id,
        )
        previous = self.store.read(key)
        if previous is None or previous.payload_json != canonical(result):
            version = previous.version if previous else 0
            saved = self.store.change(
                key,
                expected_version=version,
                request_key=f"result:{version + 1}",
                payload=result,
                reason="QUERY_ONLY_PROTECTION_RECOVERY",
            )
            if saved.code != "APPLIED":
                raise ValueError("RECOVERY_RESULT_CONFLICT")
        healthy = result["status"] in {"FILLED", "TERMINAL_FLAT_LEDGER"} or result == {
            "status": "WAITING_TRIGGER",
            "parent_status": "NEW",
        }
        if healthy:
            return
        with self.connect() as conn:
            bucket = conn.execute(
                "SELECT floor(extract(epoch FROM clock_timestamp())/300)::bigint"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload) VALUES (%s,%s,%s,'PROTECTION_RECOVERY',%s) ON CONFLICT(scope_id,dedup_key) DO NOTHING",
                (
                    str(uuid4()),
                    self.scope.environment,
                    f"protection:{digest(self.scope.key)}:{state_id}:{digest(canonical(result))}:{bucket}",
                    Jsonb(
                        {
                            "account_scope": asdict(self.scope),
                            "state_id": state_id,
                            **result,
                        }
                    ),
                ),
            )

    def run_once(self, limit=20, *, stop_requested=lambda: False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded protection batch required")
        if not callable(stop_requested):
            raise TypeError("callable shutdown check required")
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BUSY", "results": {}}
            guard.commit()  # session lock survives; do not hold a transaction over I/O
            cursor = self.store.read(self.cursor)
            after = json.loads(cursor.payload_json)["after"] if cursor else ""
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT s.state_id::text,s.payload FROM v2_business_state s
                    JOIN v2_trade_intents i ON i.intent_id::text=s.payload->'spec'->>'episode'
                    WHERE NOT s.deleted AND s.scope->>'namespace'='testnet-protection-v1'
                    AND s.scope @> %s::jsonb
                    AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND NOT EXISTS (
                        SELECT 1 FROM v2_episodes e JOIN v2_business_state r
                        ON r.scope->>'namespace'='protection-recovery-result-v1'
                        AND r.scope->>'key'=s.state_id::text AND NOT r.deleted
                        AND r.scope @> %s::jsonb
                        WHERE e.episode_id=i.intent_id AND e.status='SETTLED'
                        AND r.payload->>'status' IN ('FILLED','TERMINAL_FLAT_LEDGER')
                        AND s.payload->>'status' IN ('FINISHED','CANCELED','EXPIRED','REJECTED'))
                    ORDER BY (s.state_id::text<=%s),s.state_id LIMIT %s""",
                    (
                        canonical(asdict(self.scope)),
                        *asdict(self.scope).values(),
                        canonical(asdict(self.scope)),
                        after,
                        limit,
                    ),
                ).fetchall()
            results = {}
            for state_id, payload in rows:
                if stop_requested():
                    break
                try:
                    spec = ProtectionSpec(**payload["spec"])
                    if self.child.protection.key(spec).identity != state_id:
                        raise ValueError("PROTECTION_STATE_IDENTITY_MISMATCH")
                    result = self._result(payload)
                except Exception as exc:  # noqa: BLE001 - isolate malformed/ambiguous items, never expose exception text
                    result = {
                        "status": "RECOVERY_PENDING",
                        "error_code": type(exc).__name__,
                    }
                self._record(state_id, result)
                version = cursor.version if cursor else 0
                saved = self.store.change(
                    self.cursor,
                    expected_version=version,
                    request_key=f"cursor:{version + 1}",
                    payload={"after": state_id},
                    reason="PROTECTION_SCAN_CHECKPOINT",
                )
                if saved.code != "APPLIED":
                    raise ValueError("RECOVERY_CURSOR_CONFLICT")
                cursor = self.store.read(self.cursor)
                results[state_id] = result
            return {"status": "SCANNED", "results": results}
