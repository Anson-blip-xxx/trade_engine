"""Typed-mode observations, not admission or execution authorization.

The management heartbeat binds the current checkpoint AND immutable history to
the current route token. A fresh STARTED record proves only local cycle activity,
not successful protection. Runtime snapshots are local ownership diagnostics;
they never prove an exchange position, settlement correctness or queue coverage.
"""

import time
from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from services.v2_management_runtime import PHASES, REASON, SCHEMA, ManagementBinding
from services.v2_testnet_watchdog import WatchdogDeliveryState
from services.v2_trading_pipeline import failed
from v2_core.state import StateKey


def current_binding(tenant, scope, route):
    if (
        type(route) is not dict
        or route.get("environment", "SANDBOX") != "SANDBOX"
        or route.get("phase")
        not in {
            "STARTING",
            "ACTIVE",
            "DRAINING",
            "BLOCKED",
        }
    ):
        raise ValueError("TYPED_HEALTH_ROUTE_INVALID")
    return ManagementBinding(
        scope,
        tenant,
        route["target_registry"],
        route["binding_version"],
        route["epoch"],
        route["worker_token"],
    )


class ManagementHeartbeat:
    """Read only management-latest; never let the old daemon satisfy typed health."""

    def __init__(
        self, connect, *, tenant, scope, route_for, max_age_seconds, clock_ms=None
    ):
        if (
            not callable(connect)
            or not callable(route_for)
            or type(max_age_seconds) is not int
            or not 30 <= max_age_seconds <= 900
        ):
            raise ValueError("TYPED_HEARTBEAT_PORTS_REQUIRED")
        self.connect, self.tenant, self.scope, self.route_for = (
            connect,
            tenant,
            scope,
            route_for,
        )
        self.max_age = max_age_seconds * 1000
        self.clock = clock_ms or (lambda: time.time_ns() // 1000000)
        self.key = StateKey(
            **asdict(scope),
            namespace="testnet-trading-daemon-v1",
            key="management-latest",
        )
        self.completion_status = "UNKNOWN"

    def __call__(self):
        self.completion_status = "UNKNOWN"
        binding = current_binding(self.tenant, self.scope, self.route_for())
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='2s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            row = conn.execute(
                "SELECT b.scope,b.payload,b.deleted,h.payload,h.deleted,h.reason,"
                "extract(epoch FROM clock_timestamp()-h.created_at)*1000 "
                "FROM v2_business_state b JOIN v2_state_history h "
                "ON h.state_id=b.state_id AND h.version=b.version WHERE b.state_id=%s",
                (self.key.identity,),
            ).fetchone()
        if row is None:
            return "DAEMON_HEARTBEAT_MISSING"
        scope, payload, deleted, original, original_deleted, reason, age = row
        now = self.clock()
        try:
            age = Decimal(str(age))
        except (InvalidOperation, ValueError):
            return "DAEMON_HEARTBEAT_INVALID"
        if not age.is_finite() or not 0 <= age <= self.max_age:
            return "DAEMON_HEARTBEAT_STALE"
        if (
            scope != asdict(self.key)
            or deleted is not False
            or original_deleted is not False
            or original != payload
            or reason != REASON
            or type(payload) is not dict
            or payload.get("schema") != SCHEMA
            or payload.get("account_scope") != asdict(self.scope)
            or payload.get("binding_digest") != binding.digest
            or payload.get("execution_authorized") is not False
            or payload.get("entry_dispatch_enabled") is not False
            or type(now) is not int
            or type(payload.get("started_at_ms")) is not int
            or not 0 <= payload["started_at_ms"] <= now
            or now - payload["started_at_ms"] > self.max_age
            or type(payload.get("phases")) is not dict
        ):
            return "DAEMON_HEARTBEAT_INVALID"
        status, phases = payload.get("status"), payload["phases"]
        if status == "STARTED" and not phases and "finished_at_ms" not in payload:
            self.completion_status = "IN_PROGRESS"
            return None  # Activity only; the aggregate execution health stays UNKNOWN.
        if (
            type(payload.get("finished_at_ms")) is not int
            or not payload["started_at_ms"] <= payload["finished_at_ms"] <= now
        ):
            return "DAEMON_HEARTBEAT_INVALID"
        if status == "BLOCKED":
            if any(failed(phases.get(name, {})) for name in ("protection", "exits")):
                return "POSITION_SAFETY_BLOCKED"
            return "DAEMON_DEGRADED"
        if status != "CLEAR" or set(phases) != set(PHASES):
            return "DAEMON_HEARTBEAT_INVALID"
        prior = payload["started_at_ms"]
        for name in PHASES:
            phase = phases[name]
            if (
                type(phase) is not dict
                or phase.get("status") != "CLEAR"
                or failed(phase)
                or type(phase.get("started_at_ms")) is not int
                or type(phase.get("finished_at_ms")) is not int
                or not prior
                <= phase["started_at_ms"]
                <= phase["finished_at_ms"]
                <= payload["finished_at_ms"]
            ):
                return "DAEMON_HEARTBEAT_INVALID"
            prior = phase["finished_at_ms"]
        self.completion_status = "CURRENT"
        return None


def typed_lane_failures(snapshot_for, *, tenant, scope, route):
    """Fixed alert codes only. No old generation or unbound owner can recover it."""
    try:
        binding = current_binding(tenant, scope, route)
        report = snapshot_for()
        if (
            type(report) is not dict
            or report.get("mode") != "TYPED_STRATEGY"
            or report.get("binding") != binding.guard_args
            or report.get("execution_authorized") is not False
            or type(report.get("entries_enabled")) is not bool
        ):
            return frozenset({"BUSINESS_HEALTH_UNAVAILABLE"})
        entry, settlement = report.get("entry"), report.get("settlement")
        errors = set()
        if (
            type(entry) is not dict
            or entry.get("execution_authorized") is not False
            or entry.get("entry_dispatch_enabled") is not report["entries_enabled"]
            or entry.get("owner_alive") is not True
            or entry.get("status")
            not in {
                "WAITING_RELEASE",
                "CONSTRUCTING",
                "RUNNING",
                "DEGRADED",
                "RETRY_WAIT",
                "EXHAUSTED",
                "STOP_UNCONFIRMED",
                "STOPPED",
            }
            or entry.get("status")
            in {"DEGRADED", "RETRY_WAIT", "EXHAUSTED", "STOP_UNCONFIRMED", "STOPPED"}
        ):
            errors.add("TYPED_ENTRY_DEGRADED")
        if (
            type(settlement) is not dict
            or settlement.get("execution_authorized") is not False
            or settlement.get("status")
            not in {
                "READY",
                "IDLE",
                "RUNNING",
                "STOPPING",
                "COMPLETED",
                "BLOCKED",
                "BUSY",
                "FAILED",
                "TIMEOUT",
                "STOPPED",
                "STOP_UNCONFIRMED",
            }
        ):
            errors.add("TYPED_FINANCIAL_SETTLEMENT_UNAVAILABLE")
        else:
            progress = settlement.get("progress_fresh")
            failure = settlement.get("status") in {
                "FAILED",
                "TIMEOUT",
                "STOP_UNCONFIRMED",
                "BLOCKED",
                "UNAVAILABLE",
                "STOPPED",
            }
            if (
                type(progress) is not dict
                or progress.get("FINANCIAL") is not True
                or (failure and settlement.get("kind") == "FINANCIAL")
            ):
                errors.add("TYPED_FINANCIAL_SETTLEMENT_UNAVAILABLE")
            if failure and settlement.get("kind") == "OUTCOME":
                errors.add("TYPED_SETTLEMENT_ANALYSIS_DEGRADED")
        return frozenset(errors)
    except Exception:  # noqa: BLE001 - fixed diagnostic; no callback or route payload escapes
        return frozenset({"BUSINESS_HEALTH_UNAVAILABLE"})


class TypedDeliveryState(WatchdogDeliveryState):
    """Same durable deduplication, without claiming old legacy alerts recovered."""

    def __init__(self, connect, account_id):
        super().__init__(connect, account_id)
        self.key = StateKey(
            "BINANCE",
            account_id,
            "SANDBOX",
            "FUTURES",
            "watchdog-delivery-v1",
            "typed-latest",
        )
