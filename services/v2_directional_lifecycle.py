"""Actual Testnet protection stage; not an exit/settlement substitute.

Recovers registered parents/children, freezes possibly submitted openings and
restores their original stop, then obtains a fresh full-account coverage audit.
No opening capability. Writes remain explicit opt-in and guarded below.
"""

import json
from dataclasses import asdict
from decimal import Decimal

from services.v2_directional_protection import DirectionalStopRecovery
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.evidence import canonical
from v2_core.external_close_recovery import ExternalCloseRecovery
from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.session_lock import session_advisory_lock
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
        self.external_close = ExternalCloseRecovery(
            connect, request, scope=scope, clock_ms=clock_ms
        )
        self.audit = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.store = BusinessState(connect)
        self.request = request
        self.cursor = StateKey(
            **asdict(scope), namespace="directional-protection-stage-v1", key="cursor"
        )

    def _cleanup_flat(self, state_id):
        """Cancel one proven-owned flat parent; never replace a live hard stop."""
        from v2_core.protection import ProtectionSpec, TestnetProtection
        from v2_core.service import TradingData

        if not self.allow_writes:
            return {"status": "CLEANUP_WRITE_DISABLED"}
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BUSY"}
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM v2_business_state WHERE state_id=%s AND NOT deleted AND scope @> %s::jsonb",
                    (
                        state_id,
                        canonical(
                            {**asdict(self.scope), "namespace": "testnet-protection-v1"}
                        ),
                    ),
                ).fetchone()
            if row is None:
                raise ValueError("FLAT_PROTECTION_OWNERSHIP_REQUIRED")
            spec = ProtectionSpec(**row[0]["spec"])
            protection = TestnetProtection(self.store, self.request, scope=self.scope)
            if protection.key(spec).identity != state_id:
                raise ValueError("FLAT_PROTECTION_IDENTITY_REQUIRED")
            trace = TradingData(self.connect).trace(spec.episode)
            if any(trace["scope"].get(k) != v for k, v in asdict(self.scope).items()):
                raise ValueError("FLAT_PROTECTION_SCOPE_REQUIRED")
            orders = {o["order_id"]: o for o in trace["orders"]}
            if not orders or any(
                o["status"] not in {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
                for o in orders.values()
            ):
                raise ValueError("FLAT_PROTECTION_ORDERS_UNCONFIRMED")
            opened = sum(
                (
                    Decimal(f["quantity"])
                    for f in trace["fills"]
                    if orders[f["order_id"]]["leg"] == "OPEN"
                ),
                Decimal(0),
            )
            closed = sum(
                (
                    Decimal(f["quantity"])
                    for f in trace["fills"]
                    if orders[f["order_id"]]["leg"] == "CLOSE"
                ),
                Decimal(0),
            )
            if opened <= 0 or opened != closed:
                raise ValueError("FLAT_PROTECTION_LEDGER_REQUIRED")
            opening = [o for o in orders.values() if o["leg"] == "OPEN"]
            if len(opening) != 1 or (
                spec.kind == "STOP_MARKET"
                and self.stop.plan(opening[0]["order_id"]) != spec
            ):
                raise ValueError("FLAT_PROTECTION_ORIGINAL_PLAN_REQUIRED")
            with self.connect() as conn:
                pending = conn.execute(
                    "SELECT 1 FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
                    "WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) AND i.payload->>'symbol'=%s "
                    "AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED','PREPARED') LIMIT 1",
                    (*asdict(self.scope).values(), spec.symbol),
                ).fetchone()
            if pending:
                raise ValueError("FLAT_PROTECTION_TARGET_SUBMISSION_UNCONFIRMED")
            ordinary = self.request("GET", "/fapi/v1/openOrders", {})
            if not isinstance(ordinary, list) or any(
                not isinstance(o, dict)
                or not o.get("symbol")
                or o["symbol"] == spec.symbol
                for o in ordinary
            ):
                raise ValueError("FLAT_PROTECTION_VENUE_ORDERS")
            # Same account lock serializes normal submissions; transport guard
            # additionally fences account switches during the targeted DELETE.
            result = protection.cancel_flat_once(spec, symbol_only=True)
            return {
                "status": "CLEANED"
                if result["status"] in {"CANCELED", "EXPIRED"}
                else "CLEANUP_PENDING",
                "parent_status": result["status"],
            }

    def run_once(self):
        # Separate orchestration lock: children acquire the venue/account lock.
        with session_advisory_lock(
            self.connect, "directional-protection-stage:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "BUSY"}
            try:
                recovery = self.supervisor.run_once(limit=self.limit)
            except Exception as exc:  # noqa: BLE001 - still attempt original stop recovery
                recovery = {"status": "BLOCKED", "error_code": type(exc).__name__}
            # Give registered native children first ownership of their fills.
            # Failed parent queries must not prevent independent external-close
            # recovery; after adoption revalidate the terminal parent evidence.
            try:
                external = self.external_close.run_once(limit=self.limit)
                if any(
                    r.get("status") == "RECONCILED" for r in external.get("results", [])
                ):
                    recovery = self.supervisor.run_once(limit=self.limit)
            except Exception as exc:  # noqa: BLE001 - preserve the admission fence
                external = {"status": "BLOCKED", "error_code": type(exc).__name__}
            cleanup = {}
            for state_id, result in recovery.get("results", {}).items():
                if result.get("status") == "PROTECTION_WITHOUT_LEDGER_EXPOSURE":
                    try:
                        cleanup[state_id] = self._cleanup_flat(state_id)
                        if cleanup[state_id]["status"] == "CLEANED":
                            recovery["results"][state_id] = {
                                "status": "TERMINAL_FLAT_LEDGER",
                                "parent_status": cleanup[state_id]["parent_status"],
                            }
                    except Exception as exc:  # noqa: BLE001 - query-only retry on uncertain DELETE
                        cleanup[state_id] = {
                            "status": "BLOCKED",
                            "error_code": type(exc).__name__,
                        }
            cursor = self.store.read(self.cursor)
            after = json.loads(cursor.payload_json)["after"] if cursor else ""
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT o.order_id::text FROM v2_orders o
                    JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                    AND o.leg='OPEN' AND o.status<>'PREPARED'
                    AND (o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED') OR
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
                if external.get("status") == "CLEAR"
                and healthy_recovery
                and healthy_stops
                and coverage.get("status") == "ACCOUNT_COVERAGE_CLEAR"
                else "BLOCKED",
                "recovery": recovery,
                "external_closes": external,
                "flat_cleanup": cleanup,
                "stops": results,
                "coverage": coverage,
                "execution_authorized": False,
            }
