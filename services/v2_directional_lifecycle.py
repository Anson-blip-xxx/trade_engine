"""Actual Testnet protection stage; not an exit/settlement substitute.

Recovers registered parents/children, freezes possibly submitted openings and
restores their original stop, then obtains a fresh full-account coverage audit.
No opening capability. Writes remain explicit opt-in and guarded below.
"""

import json
from dataclasses import asdict

from services.v2_directional_protection import DirectionalStopRecovery
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.state import BusinessState, StateKey


class DirectionalProtectionStage:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        reference,
        clock_ms,
        allow_writes=False,
        excluded_position_symbols=(),
        limit=20,
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded protection batch required")
        self.connect, self.scope, self.limit, self.allow_writes = (
            connect,
            scope,
            limit,
            allow_writes,
        )
        self.stop = DirectionalStopRecovery(
            connect,
            request,
            scope=scope,
            reference=reference,
            clock_ms=clock_ms,
            allow_writes=allow_writes,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.supervisor = ProtectionSupervisor(connect, request, scope=scope)
        self.audit = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.store = BusinessState(connect)
        self.cursor = StateKey(
            **asdict(scope), namespace="directional-protection-stage-v1", key="cursor"
        )

    def run_once(self):
        # Separate orchestration lock: children acquire the venue/account lock.
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("directional-protection-stage:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BLOCKED", "reason": "BUSY"}
            guard.commit()
            try:
                recovery = self.supervisor.run_once(limit=self.limit)
            except Exception as exc:  # noqa: BLE001 - still attempt original stop recovery
                recovery = {"status": "BLOCKED", "error_code": type(exc).__name__}
            cursor = self.store.read(self.cursor)
            after = json.loads(cursor.payload_json)["after"] if cursor else ""
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT o.order_id::text FROM v2_orders o
                    JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                    AND o.leg='OPEN' AND o.status<>'PREPARED'
                    AND (o.status NOT IN ('FILLED','CANCELLED','REJECTED') OR
                        (SELECT COALESCE(sum(CASE WHEN x.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)
                         FROM v2_orders x JOIN v2_fills f USING(order_id) WHERE x.episode_id=i.intent_id)>0)
                    ORDER BY (o.order_id::text<=%s),o.order_id LIMIT %s""",
                    (*asdict(self.scope).values(), after, self.limit),
                ).fetchall()
            results = {}
            for (order_id,) in rows:
                try:
                    results[order_id] = self.stop.ensure(order_id)
                except Exception as exc:  # noqa: BLE001 - isolate poison episodes
                    results[order_id] = {
                        "status": "BLOCKED",
                        "error_code": type(exc).__name__,
                    }
                version = cursor.version if cursor else 0
                saved = self.store.change(
                    self.cursor,
                    expected_version=version,
                    request_key=f"scan:{version + 1}",
                    payload={"after": order_id},
                    reason="DIRECTIONAL_PROTECTION_SCAN",
                )
                if saved.code != "APPLIED":
                    raise ValueError("protection stage cursor conflict")
                cursor = self.store.read(self.cursor)
            # A POST response alone is not confirmed account protection.
            try:
                coverage = self.audit.run_once()
            except Exception as exc:  # noqa: BLE001 - no exception details in journal
                coverage = {"status": "BLOCKED", "error_code": type(exc).__name__}
            healthy_recovery = recovery.get("status") == "SCANNED" and all(
                r.get("status") in {"FILLED", "TERMINAL_FLAT_LEDGER"}
                or r == {"status": "WAITING_TRIGGER", "parent_status": "NEW"}
                for r in recovery.get("results", {}).values()
            )
            healthy_stops = all(
                r.get("status") in {"NEW", "NO_LEDGER_EXPOSURE"}
                for r in results.values()
            )
            return {
                "status": "CLEAR"
                if healthy_recovery
                and healthy_stops
                and coverage.get("status") == "ACCOUNT_COVERAGE_CLEAR"
                else "BLOCKED",
                "recovery": recovery,
                "stops": results,
                "coverage": coverage,
                "execution_authorized": False,
            }
