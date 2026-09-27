"""Account-scoped business health, independent of the trading process.

Only reads trade records. Diagnostic timers/summaries live in PostgreSQL CAS
state; no exchange client, automatic cancellation or local-file fallback.
"""

import json
import time
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4

from services.v2_trading_pipeline import failed
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import EXPECTED_ADMISSION_WAITS
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyScope

# Only these machine codes may cross from durable rejection evidence into the
# health report. Legacy/raw transport errors must never become dashboard/TG text.
_REJECTION_CODES = frozenset(
    {
        "VENUE_PREFLIGHT_UNAVAILABLE",
        "VENUE_MODE_UNSUPPORTED",
        "ENDPOINT_OR_WRITE_DISABLED",
        "QUOTA_DENIED",
        "EXCHANGE_CREATED_ORDER_QUERY_ONLY",
        "VENUE_READINESS_BLOCKED",
        "EXECUTION_FENCED",
        "EXECUTION_GUARD_PERMISSION_DENIED",
    }
)
_OPENING_INCIDENTS = frozenset(
    {
        "OPENING_SUBMISSION_STALLED",
        "OPENING_REJECTION_RATE_HIGH",
        "EXECUTION_GUARD_PERMISSION_DENIED",
    }
)


def opening_health(conn, scope, settings, now):
    """Summarize OPEN outcomes and urgent guard faults on either order leg.

    Count orders, not repeated event rows. An acknowledgement, venue identity or
    durable fill is positive acceptance evidence even after a later cancellation.
    This is observation only: uncertain submissions still require reconciliation.
    """
    rows = conn.execute(
        """SELECT o.leg,o.status,
            (o.exchange_order_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM v2_order_events a WHERE a.order_id=o.order_id
                AND a.status IN ('ACKNOWLEDGED','FILLED')) OR EXISTS (
                SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id)) AS accepted,
            CASE WHEN COALESCE(e.evidence->>'error_code',e.evidence->>'reason')=ANY(%s)
                THEN COALESCE(e.evidence->>'error_code',e.evidence->>'reason')
                ELSE 'UNCLASSIFIED_REJECTION' END AS reason_code,
            count(*),max(floor(extract(epoch FROM o.updated_at)*1000)::bigint),
            max((SELECT floor(extract(epoch FROM max(a.created_at))*1000)::bigint
                FROM v2_order_events a WHERE a.order_id=o.order_id
                AND a.status IN ('ACKNOWLEDGED','FILLED'))),
            max((SELECT floor(extract(epoch FROM max(s.created_at))*1000)::bigint
                FROM v2_order_events s WHERE s.order_id=o.order_id
                AND s.status='SUBMITTING' AND EXISTS (
                    SELECT 1 FROM v2_order_events a WHERE a.order_id=s.order_id
                    AND a.status IN ('ACKNOWLEDGED','FILLED')
                    AND a.version>s.version AND a.created_at>=s.created_at)))
        FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
        JOIN v2_order_events e ON e.order_id=o.order_id AND e.version=o.version
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        AND o.status<>'PREPARED'
        AND o.updated_at>=to_timestamp(%s/1000.0)
        AND o.updated_at<to_timestamp(%s/1000.0)
        GROUP BY o.leg,o.status,accepted,reason_code""",
        (
            sorted(_REJECTION_CODES),
            *scope,
            now - settings["health.opening_window_seconds"] * 1000,
            now + 1,
        ),
    ).fetchall()
    summary = {
        "window_seconds": settings["health.opening_window_seconds"],
        "rejected": 0,
        "accepted": 0,
        "unresolved": 0,
        "rejection_codes": {},
        "last_accepted_at_ms": None,
        "last_rejected_at_ms": None,
    }
    findings = {}
    permission_rejections = 0
    last_permission_failure = None
    last_guarded_acceptance = None
    for (
        leg,
        status,
        accepted,
        code,
        count,
        last_at,
        last_accepted,
        submitted_at,
    ) in rows:
        if last_accepted is not None and submitted_at is not None:
            # A read-only recovery can discover an old fill after a new guard
            # failure. Only an accepted attempt STARTED after that failure proves
            # that the write path recovered; observation time alone cannot.
            last_guarded_acceptance = max(
                submitted_at, last_guarded_acceptance or submitted_at
            )
        if status == "REJECTED" and code == "EXECUTION_GUARD_PERMISSION_DENIED":
            permission_rejections += count
            last_permission_failure = max(last_at, last_permission_failure or last_at)
        if leg != "OPEN":
            continue
        if accepted:
            summary["accepted"] += count
            if last_accepted is not None:
                summary["last_accepted_at_ms"] = max(
                    last_accepted, summary["last_accepted_at_ms"] or last_accepted
                )
        elif status == "REJECTED":
            summary["rejected"] += count
            summary["last_rejected_at_ms"] = max(
                last_at, summary["last_rejected_at_ms"] or last_at
            )
            summary["rejection_codes"][code] = (
                summary["rejection_codes"].get(code, 0) + count
            )
        if status in {"SUBMITTING", "UNKNOWN"}:
            summary["unresolved"] += count
    if permission_rejections and (
        last_guarded_acceptance is None
        or last_permission_failure >= last_guarded_acceptance
    ):
        # A denied database guard is never a normal symbol/risk rejection and
        # needs attention on its first occurrence, not after three lost signals.
        findings["EXECUTION_GUARD_PERMISSION_DENIED"] = {
            "rejected": permission_rejections,
            "last_rejected_at_ms": last_permission_failure,
        }
    rejected, accepted = summary["rejected"], summary["accepted"]
    if rejected >= settings["health.opening_min_rejections"]:
        if accepted == 0:
            findings["OPENING_SUBMISSION_STALLED"] = summary
        elif Decimal(rejected) / Decimal(rejected + accepted) >= Decimal(
            settings["health.opening_reject_fraction"]
        ):
            findings["OPENING_REJECTION_RATE_HIGH"] = summary
    return summary, findings


def retain_opening_incidents(connect, scope, old, findings, now):
    """Silence is not recovery: retain a failure until a newer venue acceptance.

    This ledger-backed latch only affects monitoring. It does not disable entries
    or bypass their gates, and survives monitor/process restarts.
    """
    incidents = {}
    for code in _OPENING_INCIDENTS:
        if code in findings:
            incidents[code] = findings[code]
            continue
        previous = old.get("opening_incidents", {}).get(code)
        if not previous:
            continue
        with connect() as conn:
            recovered = conn.execute(
                """SELECT 1 FROM v2_orders o
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                JOIN v2_order_events e ON e.order_id=o.order_id
                JOIN v2_order_events submitted ON submitted.order_id=o.order_id
                    AND submitted.status='SUBMITTING' AND submitted.version<e.version
                    AND submitted.created_at<=e.created_at
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND (o.leg='OPEN' OR %s)
                AND e.status IN ('ACKNOWLEDGED','FILLED')
                AND submitted.created_at>=to_timestamp(%s/1000.0)
                AND e.created_at<to_timestamp(%s/1000.0) LIMIT 1""",
                (
                    *scope,
                    code == "EXECUTION_GUARD_PERMISSION_DENIED",
                    previous["last_rejected_at_ms"] + 1,
                    now + 1,
                ),
            ).fetchone()
        if not recovered:
            incidents[code] = {**previous, "awaiting_successful_submission": True}
            findings[code] = incidents[code]
    return incidents


class TradingHealth:
    def __init__(self, connect, *, account_id, tv_enabled=False, clock_ms=None):
        self.connect = connect
        self.scope = AccountScope("BINANCE", account_id, "SANDBOX", "FUTURES")
        self.sources = ("s3", "tv_bridge") if tv_enabled else ("s3",)
        self.clock = clock_ms or (lambda: time.time_ns() // 1000000)
        self.store = BusinessState(connect)
        self.key = StateKey(
            **asdict(self.scope), namespace="trading-health-v1", key="latest"
        )

    def __call__(self):
        now = self.clock()
        policy = PolicyStore(self.connect, self.scope).read()
        settings = policy.values
        findings = {}
        expected_waits = {}
        capital = None
        if settings["capital.model_enabled"]:
            snapshot = self.store.read(
                StateKey(
                    **asdict(self.scope), namespace="capital-snapshot-v1", key="latest"
                )
            )
            if snapshot and not snapshot.deleted:
                saved = json.loads(snapshot.payload_json)
                capital = saved.get("capital_health")
                if capital and capital.get("factor") == "0":
                    if capital.get("recovery", {}).get("mode") == "PAUSED":
                        expected_waits["CAPITAL_RECOVERY_PAUSED"] = capital
                    else:
                        findings["CAPITAL_DRAWDOWN_HALT"] = capital
        scope = tuple(asdict(self.scope).values())
        with self.connect() as conn:
            opening, opening_findings = opening_health(conn, scope, settings, now)
            findings.update(opening_findings)
            orders = conn.execute(
                """SELECT o.order_id::text,i.payload->>'symbol',o.status,
                    extract(epoch FROM clock_timestamp()-o.updated_at)::bigint
                FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND ((o.status IN ('SUBMITTING','UNKNOWN') AND
                        o.updated_at<clock_timestamp()-%s*interval '1 second')
                    OR (o.status='ACKNOWLEDGED' AND o.order_type='MARKET' AND
                        o.updated_at<clock_timestamp()-%s*interval '1 second')
                    OR (o.status='PREPARED' AND
                        o.updated_at<clock_timestamp()-%s*interval '1 second'))
                ORDER BY o.updated_at LIMIT 20""",
                (
                    *scope,
                    settings["health.order_seconds"],
                    settings["health.order_seconds"],
                    settings["health.prepared_seconds"],
                ),
            ).fetchall()
            if orders:
                findings["ORDER_PROGRESS_STALLED"] = {
                    "orders": [
                        dict(
                            zip(
                                ("order_id", "symbol", "status", "idle_seconds"),
                                row,
                                strict=True,
                            )
                        )
                        for row in orders
                    ]
                }
            settlements = conn.execute(
                """SELECT i.intent_id::text,i.payload->>'symbol',max(f.occurred_at_ms)
                FROM v2_trade_intents i JOIN v2_episodes e ON e.episode_id=i.intent_id
                JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND e.status='ACTIVE' GROUP BY i.intent_id
                HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)=0
                AND sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE 0 END)>0
                AND max(f.occurred_at_ms)<%s ORDER BY max(f.occurred_at_ms) LIMIT 20""",
                (*scope, now - settings["health.settlement_seconds"] * 1000),
            ).fetchall()
            if settlements:
                findings["SETTLEMENT_OVERDUE"] = {
                    "episodes": [
                        dict(
                            zip(
                                ("episode_id", "symbol", "last_fill_at_ms"),
                                row,
                                strict=True,
                            )
                        )
                        for row in settlements
                    ]
                }
            for source in self.sources:
                for producer in ("s6", "s8"):
                    consumer = StrategyScope(*scope, producer).consumer
                    row = conn.execute(
                        """SELECT count(*) FILTER(WHERE COALESCE(t.error_code,'')<>ALL(%s)),
                        min(floor(extract(epoch FROM s.received_at)*1000)::bigint)
                            FILTER(WHERE COALESCE(t.error_code,'')<>ALL(%s)),
                        count(*) FILTER(WHERE t.error_code=ANY(%s))
                        FROM v2_inbound_signals s
                        LEFT JOIN v2_strategy_tasks t ON t.signal_id=s.signal_id AND t.consumer=%s
                        WHERE s.environment=%s AND s.source=%s
                        AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                            WHERE r.consumer=%s AND r.signal_id=s.signal_id)
                        AND s.received_at<=to_timestamp(%s/1000.0)""",
                        (
                            sorted(EXPECTED_ADMISSION_WAITS),
                            sorted(EXPECTED_ADMISSION_WAITS),
                            sorted(EXPECTED_ADMISSION_WAITS),
                            consumer,
                            self.scope.environment,
                            source,
                            consumer,
                            now - settings["health.queue_seconds"] * 1000,
                        ),
                    ).fetchone()
                    if row[0]:
                        findings.setdefault("SIGNAL_CONSUMPTION_LAG", {})[
                            producer + ":" + source
                        ] = {"count": row[0], "oldest_received_at_ms": row[1]}
                    if row[2]:
                        expected_waits[producer + ":" + source] = {
                            "count": row[2],
                            "reason": "ACCOUNT_CAPACITY_OR_EXISTING_POSITION",
                        }
            latest = conn.execute(
                """SELECT payload FROM v2_business_state
                WHERE scope @> %s::jsonb AND NOT deleted
                AND payload->>'status' IN ('CYCLE_COMPLETE','ENTRY_BLOCKED')
                ORDER BY (payload->>'started_at_ms')::bigint DESC LIMIT 1""",
                (
                    json.dumps(
                        {**asdict(self.scope), "namespace": "trading-pipeline-cycle-v1"}
                    ),
                ),
            ).fetchone()
        if latest and latest[0].get("status") == "ENTRY_BLOCKED":
            phases = latest[0].get("phases", {})
            blocked = [name for name, value in phases.items() if failed(value)]
            findings["PIPELINE_ENTRY_BLOCKED"] = {"stages": blocked}
            if any(name in blocked for name in ("protection", "exits")):
                findings["POSITION_SAFETY_BLOCKED"] = {"stages": blocked}
                lag = findings.pop("SIGNAL_CONSUMPTION_LAG", None)
                if lag:
                    expected_waits["signals_waiting_for_safety"] = {
                        "reason": "UPSTREAM_POSITION_SAFETY_BLOCKED",
                        "queues": lag,
                    }
        previous = self.store.read(self.key)
        old = (
            json.loads(previous.payload_json)
            if previous and not previous.deleted
            else {}
        )
        opening_incidents = retain_opening_incidents(
            self.connect, scope, old, findings, now
        )
        timers = old.get("first_seen_ms", {})
        first = {code: min(now, timers.get(code, now)) for code in findings}
        # Direct age checks already debounce order/settlement/signal diagnostics.
        delays = {
            "SIGNAL_CONSUMPTION_LAG": settings["health.signal_confirm_seconds"] * 1000,
            "PIPELINE_ENTRY_BLOCKED": settings["health.pipeline_seconds"] * 1000,
            "POSITION_SAFETY_BLOCKED": settings["health.safety_seconds"] * 1000,
        }
        active = sorted(
            code for code in findings if now - first[code] >= delays.get(code, 0)
        )
        # Signals arrive in minute-sized batches. A single empty queue between
        # batches is not a confirmed recovery; otherwise TG alternates alarm /
        # recovery every minute. Persist the quiet timer across process restarts.
        clear_since = {}
        code = "SIGNAL_CONSUMPTION_LAG"
        if code in old.get("active", []) and code not in findings:
            clear_since[code] = min(now, old.get("clear_since_ms", {}).get(code, now))
            if (
                now - clear_since[code]
                < settings["health.signal_recovery_seconds"] * 1000
            ):
                active.append(code)
        elif code in old.get("active", []) and code in findings and code not in active:
            active.append(code)
        result = self.store.change(
            self.key,
            expected_version=previous.version if previous else 0,
            request_key=str(uuid4()),
            reason="TRADING_BUSINESS_HEALTH",
            payload={
                "observed_at_ms": now,
                "policy_version": policy.version,
                "policy_digest": policy.digest,
                "account_scope": asdict(self.scope),
                "active": active,
                "first_seen_ms": first,
                "clear_since_ms": clear_since,
                "findings": findings,
                "expected_waits": expected_waits,
                "capital": capital,
                "opening": opening,
                "opening_incidents": opening_incidents,
            },
        )
        if result.code != "APPLIED":
            raise ValueError("BUSINESS_HEALTH_STATE_CONFLICT")
        return frozenset(active)
