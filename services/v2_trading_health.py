"""Account-scoped business health, independent of the trading process.

Only reads trade records. Diagnostic timers/summaries live in PostgreSQL CAS
state; no exchange client, automatic cancellation or local-file fallback.
"""

import json
import time
from dataclasses import asdict
from decimal import Decimal
from uuid import UUID, uuid4

from services.v2_jev_budget_health import (
    CODE as JEV_BUDGET_CODE,
)
from services.v2_jev_budget_health import (
    MODEL_CODE as JEV_MODEL_CODE,
)
from services.v2_jev_budget_health import (
    apply_budget_health,
    read_budget_health,
)
from services.v2_settlement_entry import settled_result_valid, settlement_result_valid
from services.v2_trading_pipeline import failed
from v2_core.account_risk import AccountScope
from v2_core.directional import directional_events
from v2_core.recovery_diagnostics import summaries as recovery_summaries
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import (
    ADMISSION_READ_QUOTA_CODES,
    CANDIDATE_CONTEXT_WAITS,
    EXPECTED_ADMISSION_WAITS,
    EXPECTED_CONTEXT_WAITS,
    JEV_RETRY_CODES,
)
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyScope
from v2_core.telegram import health_notification_details

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

# These are diagnostic categories, never permission to consume or trade. Keep
# unknown task errors visible, but never publish arbitrary saved error text.
_CANDIDATE_DEPENDENCY_CODES = (
    frozenset(CANDIDATE_CONTEXT_WAITS) | EXPECTED_CONTEXT_WAITS
)
_CAPACITY_WAIT_CODES = EXPECTED_ADMISSION_WAITS - ADMISSION_READ_QUOTA_CODES
_QUEUE_REASON_CODES = (
    EXPECTED_ADMISSION_WAITS | _CANDIDATE_DEPENDENCY_CODES | JEV_RETRY_CODES
)
_DETAIL_MAX_AGE_MS = 120000


def settlement_health_projection(cycle):
    """Separate derived-analysis health only for a verified result contract."""
    phases = cycle.get("phases", {})
    result = phases.get("settlement") if isinstance(phases, dict) else None
    clear = settlement_result_valid(result, status="CLEAR") and all(
        settled_result_valid(episode, record)
        for episode, record in result["settlements"].items()
    )
    if not clear:
        return False, None
    errors = [
        {"episode": episode, "error_code": record["error_code"]}
        for episode, record in sorted(result["outcomes"].items())
        if record.get("status") == "BLOCKED"
    ]
    if not errors:
        return True, None
    return True, {
        "cycle_id": cycle.get("cycle_id"),
        "observed_at_ms": cycle.get("finished_at_ms"),
        "episodes": errors,
        "financial_status": "CLEAR",
        "execution_authorized": False,
    }


def entry_blocking_stages(phases, *, settlement_clear=False):
    """Attribute blocked entry without promoting background bookkeeping.

    Maintenance keeps its independent degraded finding. It never participates
    in the pipeline's safety gate, including when a real blocker co-occurs.
    """
    return [
        name
        for name, value in phases.items()
        if name != "signal_maintenance"
        and failed(value)
        and not (name == "settlement" and settlement_clear)
    ]


def maintenance_health_projection(maintenance, previous, now, observed_at_ms=None):
    """Keep fault continuity per consumer/lane, never per aggregate alert code.

    The lane's observed_at_ms is written only after its transaction commits.
    A later failed attempt may coexist with that evidence: it starts a new
    failure interval, not an uninterrupted failure since before the commit.
    Consumer-wide progress cannot recover an unrelated lane or a dead worker.
    An explicit CURRENT report must also have a strictly newer observation to
    prove recovery. Missing evidence is not recovery. This projection grants
    no trading permission.
    """
    previous = previous if isinstance(previous, dict) else {}
    maintenance = (
        maintenance
        if isinstance(maintenance, dict)
        else {"unknown": {"status": "UNAVAILABLE"}}
    )
    lanes, errors = {}, {}

    def timestamp(value):
        return value if type(value) is int and 0 <= value <= now else None

    observation = timestamp(observed_at_ms)
    for consumer in sorted(set(maintenance) | set(previous)):
        value = maintenance.get(consumer)
        old = previous.get(consumer, {})
        old = old if isinstance(old, dict) else {}
        snapshot = value if isinstance(value, dict) else {}
        legacy_success = type(value) is int and value >= 0
        components = snapshot.get("component_failures", {})
        components = components if isinstance(components, dict) else {}
        failing = {
            name if name in {"cold_expiry", "discovery", "expiry"} else "unknown": error
            for name, error in components.items()
            if error
        }
        if (value is not None and failed(value) and not failing) or (
            consumer in maintenance
            and not isinstance(value, dict)
            and not legacy_success
        ):
            failing["worker"] = "MAINTENANCE_WORKER_UNAVAILABLE"
        # A thread-level failure can overlay cached component state. Only an
        # exact component error explains the consumer-level failure_type.
        if (
            snapshot.get("status") == "UNAVAILABLE"
            and snapshot.get("failure_type")
            and snapshot["failure_type"] not in components.values()
        ):
            failing["worker"] = "MAINTENANCE_WORKER_UNAVAILABLE"
        recovered = legacy_success or (
            snapshot.get("status") == "CURRENT" and not failed(snapshot)
        )
        history = snapshot.get("history", {})
        history = history if isinstance(history, dict) else {}
        for lane in sorted(set(failing) | set(old)):
            prior = old.get(lane, {})
            prior = prior if isinstance(prior, dict) else {}
            old_first = timestamp(prior.get("first_seen_ms"))
            old_observation = timestamp(prior.get("last_observed_at_ms"))
            watermark = old_observation if old_observation is not None else old_first
            fresh_report = observation is not None and (
                watermark is None or observation > watermark
            )
            detail = history.get(lane, {})
            detail = detail if isinstance(detail, dict) else {}
            committed = timestamp(
                snapshot.get("expiry_observed_at_ms")
                if lane == "expiry"
                else detail.get("observed_at_ms")
                if lane in {"cold_expiry", "discovery"}
                else None
            )
            if (
                committed is not None
                and observation is not None
                and committed > observation
            ):
                committed = None
            old_commit = timestamp(prior.get("last_committed_at_ms"))
            progressed = (
                fresh_report
                and committed is not None
                and (old_commit is None or committed > old_commit)
            )
            if lane not in failing and (
                (recovered and fresh_report)
                or (progressed and old_first is not None and committed >= old_first)
            ):
                continue
            if lane not in failing and old_first is None:
                continue
            first = (
                now
                if old_first is None
                or (lane in failing and progressed and committed >= old_first)
                else old_first
            )
            entry = {
                "first_seen_ms": first,
                "last_committed_at_ms": committed if progressed else old_commit,
                "last_observed_at_ms": observation if fresh_report else old_observation,
                "status": "FAILING" if lane in failing else "RECOVERY_UNPROVEN",
            }
            lanes.setdefault(consumer, {})[lane] = entry
            errors.setdefault(consumer, {})[lane] = {
                **entry,
                "failure_type": (
                    failing[lane]
                    if isinstance(failing.get(lane), str)
                    and failing[lane]
                    in {
                        "TimeoutError",
                        "QueryCanceled",
                        "LockNotAvailable",
                        "ConnectionError",
                        "OperationalError",
                        "ValueError",
                        "KeyError",
                        "MAINTENANCE_WORKER_UNAVAILABLE",
                    }
                    else "UNKNOWN_MAINTENANCE_FAILURE"
                ),
            }
    if not lanes:
        return None, {}, None
    first = min(
        entry["first_seen_ms"]
        for consumers in lanes.values()
        for entry in consumers.values()
    )
    return (
        {
            "observed_at_ms": observation,
            "consumers": sorted(lanes),
            "lanes": errors,
        },
        lanes,
        first,
    )


def rejected_residual_closes(conn, scope, now, safety_seconds):
    """Read owned active episodes, then indexed order/fill children only.

    A rejected CLOSE plus a local residual is not proof that the exchange cannot
    close it. It is an independently observable unresolved exit requiring a
    venue/ledger check, even if the pipeline has stopped producing snapshots.
    """
    rows = conn.execute(
        """WITH candidates AS MATERIALIZED (
        SELECT i.intent_id,i.payload->>'symbol' AS symbol,
            latest.order_id,latest.rejected_at
        FROM v2_episodes e JOIN v2_trade_intents i ON i.intent_id=e.episode_id
        CROSS JOIN LATERAL (
            SELECT o.order_id,o.status,event.created_at AS rejected_at
            FROM v2_orders o
            JOIN v2_order_events prepared ON prepared.order_id=o.order_id AND prepared.version=1
            JOIN v2_order_events event ON event.order_id=o.order_id AND event.version=o.version
            WHERE o.episode_id=e.episode_id AND o.leg='CLOSE'
            ORDER BY prepared.created_at DESC,o.order_id DESC LIMIT 1
        ) latest
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        AND e.status='ACTIVE' AND latest.status='REJECTED'
        AND latest.rejected_at<=to_timestamp(%s/1000.0)-%s*interval '1 second')
        SELECT c.intent_id::text,c.symbol,c.order_id::text,
            floor(extract(epoch FROM c.rejected_at)*1000)::bigint,q.remaining::text
        FROM candidates c CROSS JOIN LATERAL (
            SELECT COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0) AS remaining
            FROM v2_orders o JOIN v2_fills f ON f.order_id=o.order_id
            WHERE o.episode_id=c.intent_id
        ) q WHERE q.remaining>0
        ORDER BY c.rejected_at,c.intent_id LIMIT 20""",
        (*scope, now, safety_seconds),
    ).fetchall()
    return [
        {
            "episode_id": episode,
            "symbol": symbol,
            "order_id": order,
            "status": "REJECTED_CLOSE_RESIDUAL",
            "source": "LEDGER",
            "reason": "LOCAL_RESIDUAL_REQUIRES_RECONCILIATION",
            "idle_seconds": max(0, (now - rejected_at) // 1000),
            "rejected_at_ms": rejected_at,
            "remaining_quantity": remaining,
        }
        for episode, symbol, order, rejected_at, remaining in rows
    ]


def pipeline_position_details(connect, scope, cycle, now):
    """Current bounded owned-episode diagnostics; never arbitrary error text."""
    observed = cycle.get("finished_at_ms", cycle.get("started_at_ms"))
    if type(observed) is not int or not 0 <= now - observed <= _DETAIL_MAX_AGE_MS:
        return {"status": "STALE", "protection": "UNKNOWN"}
    phases = cycle.get("phases", {})
    if not isinstance(phases, dict):
        return {"status": "UNAVAILABLE", "protection": "UNKNOWN"}
    exit_phase = phases.get("exits", {})
    exit_phase = exit_phase if isinstance(exit_phase, dict) else {}
    coverage_codes = []
    for name in ("protection", "exits"):
        phase = phases.get(name, {})
        coverage = phase.get("coverage", {}) if isinstance(phase, dict) else {}
        if isinstance(coverage, dict) and isinstance(coverage.get("blockers"), list):
            coverage_codes.extend(coverage["blockers"][:20])
    candidates = []
    in_progress = 0
    for group in ("exits", "emergency_exits", "recovery"):
        values = exit_phase.get(group, {})
        if not isinstance(values, dict):
            continue
        for identifier, value in list(values.items())[:20]:
            try:
                identifier = str(UUID(identifier))
            except (ValueError, TypeError, AttributeError):
                continue
            if isinstance(value, str):
                if value in {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}:
                    # A completed recovery is not still recovering just
                    # because another episode keeps the stage blocked.
                    # Rejected residuals are independently age/ledger checked.
                    continue
                value = {"status": "RECOVERING_CLOSE", "outcome": value}
            if not isinstance(value, dict):
                continue
            if value.get("status") in {
                "WAIT",
                "WAIT_NATIVE_STOP",
                "PROTECTION_PRESENT",
            }:
                continue
            candidates.append((identifier, group, value))
    candidates = candidates[:20]
    owners = {}
    if candidates:
        identifiers = sorted({item[0] for item in candidates})
        with connect() as conn:
            rows = conn.execute(
                """SELECT i.intent_id::text,i.payload->>'symbol',o.order_id::text
                FROM v2_trade_intents i JOIN v2_episodes e ON e.episode_id=i.intent_id
                LEFT JOIN v2_orders o ON o.episode_id=i.intent_id AND o.order_id=ANY(%s::uuid[])
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND e.status='ACTIVE' AND (i.intent_id=ANY(%s::uuid[]) OR o.order_id IS NOT NULL)
                LIMIT 40""",
                (identifiers, *scope, identifiers),
            ).fetchall()
        for episode, symbol, order in rows:
            owners[episode] = (episode, symbol)
            if order:
                owners[order] = (episode, symbol)
    episodes = []
    for identifier, group, value in candidates:
        if identifier not in owners:
            continue
        episode, symbol = owners[identifier]
        if value.get("status") == "DISPATCHED":
            in_progress += 1
            continue
        episodes.append(
            {
                "episode_id": episode,
                "symbol": symbol,
                "source": "EMERGENCY" if group == "emergency_exits" else "EXIT",
                "status": value.get("status"),
                "reason": value.get("reason"),
                "outcome": value.get("outcome"),
            }
        )
    return health_notification_details(
        "POSITION_SAFETY_BLOCKED",
        {
            "status": "CURRENT",
            "observed_at_ms": observed,
            "episodes": episodes,
            "coverage_codes": coverage_codes,
            "protection": "UNKNOWN",
            "in_progress": in_progress,
            "truncated": len(candidates) == 20,
            "exit_reason": exit_phase.get("reason"),
            "scan": exit_phase.get("scan"),
        },
    )


def signal_lag_state(findings, active, clear_since, expected_waits, now, settings):
    """Explain an alarm's latch without reporting a quiet queue as new lag."""
    code = "SIGNAL_CONSUMPTION_LAG"
    if code in findings:
        status = "CURRENT_LAG" if code in active else "CONFIRMATION_PENDING"
    elif "signals_waiting_for_safety" in expected_waits:
        status = "UPSTREAM_SAFETY_WAIT"
    elif code in active:
        status = "RECOVERY_PENDING"
    else:
        status = "CLEAR"
    quiet_since = clear_since.get(code)
    return {
        "status": status,
        "current_lag_observed": code in findings,
        "alarm_active": code in active,
        "recovery_started_at_ms": quiet_since,
        "recovery_remaining_ms": max(
            0,
            settings["health.signal_recovery_seconds"] * 1000 - (now - quiet_since),
        )
        if status == "RECOVERY_PENDING" and quiet_since is not None
        else 0,
    }


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
    def __init__(
        self,
        connect,
        *,
        account_id,
        tv_enabled=False,
        quant_enabled=False,
        clock_ms=None,
    ):
        self.connect = connect
        self.scope = AccountScope("BINANCE", account_id, "SANDBOX", "FUTURES")
        self.sources = (
            ("s3",)
            + (("tv_bridge",) if tv_enabled else ())
            + (("quant",) if quant_enabled else ())
        )
        self.clock = clock_ms or (lambda: time.time_ns() // 1000000)
        self.store = BusinessState(connect)
        self.key = StateKey(
            **asdict(self.scope), namespace="trading-health-v1", key="latest"
        )

    def notification_details(self, code):
        """Optional current-account display details; not a health/trade permit."""
        if code not in {
            "ORDER_PROGRESS_STALLED",
            "POSITION_SAFETY_BLOCKED",
            JEV_BUDGET_CODE,
            JEV_MODEL_CODE,
        }:
            return None
        saved = self.store.read(self.key)
        if saved is None or saved.deleted:
            return {"status": "UNAVAILABLE"}
        payload = json.loads(saved.payload_json)
        observed = payload.get("observed_at_ms")
        if payload.get("account_scope") != asdict(self.scope):
            return {"status": "UNAVAILABLE"}
        if (
            type(observed) is not int
            or not 0 <= self.clock() - observed <= _DETAIL_MAX_AGE_MS
        ):
            return {"status": "STALE"}
        findings = payload.get("findings", {})
        finding = findings.get(code) if isinstance(findings, dict) else None
        if not isinstance(finding, dict):
            return {"status": "UNAVAILABLE"}
        details = finding.get("details", finding)
        if not isinstance(details, dict):
            return {"status": "UNAVAILABLE"}
        return health_notification_details(
            code,
            {
                "status": "CURRENT",
                "observed_at_ms": observed,
                **details,
            },
        )

    def __call__(self):
        now = self.clock()
        policy = PolicyStore(self.connect, self.scope).read()
        settings = policy.values
        findings = {}
        expected_waits = {}
        queue_diagnostics = {}
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
            conn.execute("SET LOCAL jit=off")
            if "quant" in self.sources:
                hub = conn.execute(
                    "SELECT observed_at_ms,payload FROM v2_market_hub_status WHERE environment=%s",
                    (self.scope.environment,),
                ).fetchone()
                if hub and hub[1].get("quant", {}).get("mode", "OFF") != "OFF":
                    quant = hub[1]["quant"]
                    if quant.get("failures") or quant.get("budget_skips"):
                        findings["QUANT_RESEARCH_DEGRADED"] = {
                            **quant,
                            "observed_at_ms": hub[0],
                        }
            opening, opening_findings = opening_health(conn, scope, settings, now)
            findings.update(opening_findings)
            orders = conn.execute(
                """SELECT o.order_id::text,i.payload->>'symbol',o.status,
                    extract(epoch FROM clock_timestamp()-o.updated_at)::bigint,o.version,
                    o.leg,o.episode_id::text
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
                diagnostics = recovery_summaries(
                    conn, self.scope, [(row[0], row[4]) for row in orders]
                )
                findings["ORDER_PROGRESS_STALLED"] = {
                    "orders": [
                        {
                            **dict(
                                zip(
                                    ("order_id", "symbol", "status", "idle_seconds"),
                                    row[:4],
                                    strict=True,
                                )
                            ),
                            **(
                                {"recovery_diagnostic": diagnostics[row[0]]}
                                if row[0] in diagnostics
                                else {}
                            ),
                            "leg": row[5],
                            "episode_id": row[6],
                        }
                        for row in orders
                    ]
                }
            rejected_closes = rejected_residual_closes(
                conn, scope, now, settings["health.safety_seconds"]
            )
            if rejected_closes:
                findings["POSITION_SAFETY_BLOCKED"] = {
                    "stages": ["exits"],
                    "episodes": rejected_closes,
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
                    query_started = time.perf_counter()
                    row = conn.execute(
                        """WITH unreceipted AS MATERIALIZED (
                        SELECT s.signal_id FROM v2_inbound_signals s
                        WHERE s.environment=%s AND s.source=%s
                        AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                            WHERE r.consumer=%s AND r.signal_id=s.signal_id)
                        AND s.received_at<=to_timestamp(%s/1000.0)),
                        pending AS (
                        SELECT s.received_at,s.snapshot,t.error_code,t.lease_until,t.next_attempt_at,
                            (s.snapshot->>'signal'=ANY(%s::text[])
                             OR t.error_code IS NOT NULL
                             OR EXISTS (SELECT 1 FROM v2_strategy_decisions d
                                 WHERE d.consumer=%s AND d.signal_id=s.signal_id OFFSET 0)) AS actionable
                        FROM unreceipted p CROSS JOIN LATERAL (
                            SELECT i.signal_id,i.received_at,i.snapshot FROM v2_inbound_signals i
                            WHERE i.signal_id=p.signal_id OFFSET 0) s
                        LEFT JOIN LATERAL (
                            SELECT t.error_code,t.lease_until,t.next_attempt_at FROM v2_strategy_tasks t
                            WHERE t.signal_id=s.signal_id AND t.consumer=%s OFFSET 0) t ON true)
                        SELECT count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)),
                        min(floor(extract(epoch FROM s.received_at)*1000)::bigint)
                            FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)),
                        count(*) FILTER(WHERE error_code=ANY(%s)),
                        count(*) FILTER(WHERE NOT actionable),
                        count(*) FILTER(WHERE actionable AND (snapshot->>'expires_at_ms')::bigint>%s),
                        count(*) FILTER(WHERE actionable AND (snapshot->>'expires_at_ms')::bigint<=%s),
                        count(*) FILTER(WHERE actionable AND lease_until>=statement_timestamp()),
                        count(*) FILTER(WHERE actionable AND next_attempt_at>statement_timestamp()
                            AND (lease_until IS NULL OR lease_until<statement_timestamp())),
                        count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)
                            AND (snapshot->>'expires_at_ms')::bigint>%s),
                        count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)
                            AND (snapshot->>'expires_at_ms')::bigint<=%s),
                        count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
                        count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
                        count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
                        COALESCE((SELECT jsonb_object_agg(reason_code,reason_count) FROM (
                            SELECT CASE WHEN error_code=ANY(%s) THEN error_code
                                ELSE 'UNCLASSIFIED_TASK_ERROR' END AS reason_code,
                                count(*) AS reason_count FROM pending
                            WHERE actionable AND error_code IS NOT NULL GROUP BY 1
                        ) reasons),'{}'::jsonb)
                        FROM pending s""",
                        (
                            self.scope.environment,
                            source,
                            consumer,
                            now - settings["health.queue_seconds"] * 1000,
                            list(directional_events(producer.upper())),
                            consumer,
                            consumer,
                            sorted(EXPECTED_ADMISSION_WAITS),
                            sorted(EXPECTED_ADMISSION_WAITS),
                            sorted(EXPECTED_ADMISSION_WAITS),
                            now,
                            now,
                            sorted(EXPECTED_ADMISSION_WAITS),
                            now,
                            sorted(EXPECTED_ADMISSION_WAITS),
                            now,
                            sorted(ADMISSION_READ_QUOTA_CODES),
                            sorted(_CAPACITY_WAIT_CODES),
                            sorted(_CANDIDATE_DEPENDENCY_CODES),
                            sorted(_QUEUE_REASON_CODES),
                        ),
                    ).fetchone()
                    queue_diagnostics[producer + ":" + source] = {
                        "query_ms": round((time.perf_counter() - query_started) * 1000),
                        "scope": "OVER_LAG_THRESHOLD_UNRECEIPTED",
                        "valid_actionable": row[4],
                        "expired_actionable": row[5],
                        "leased_actionable": row[6],
                        "backoff_actionable": row[7],
                        "valid_lag": row[8],
                        "expired_bookkeeping_lag": row[9],
                        "admission_read_quota_wait": row[10],
                        "account_capacity_wait": row[11],
                        "candidate_dependency_wait": row[12],
                        "reason_codes": row[13],
                    }
                    if row[0]:
                        findings.setdefault("SIGNAL_CONSUMPTION_LAG", {})[
                            producer + ":" + source
                        ] = {
                            "count": row[0],
                            "oldest_received_at_ms": row[1],
                            "classification": "VALID_SIGNAL_LAG"
                            if row[8]
                            else "EXPIRED_SIGNAL_BOOKKEEPING_LAG",
                            "valid_pending": row[8],
                            "expired_pending": row[9],
                            "reason_codes": {
                                code: count
                                for code, count in row[13].items()
                                if code not in EXPECTED_ADMISSION_WAITS
                            },
                        }
                    if row[2]:
                        expected_waits[producer + ":" + source] = {
                            "count": row[2],
                            "reason": "MIXED_CAPACITY_AND_ADMISSION_READ_QUOTA_WAIT"
                            if row[10] and row[11]
                            else "ADMISSION_READ_QUOTA_WAIT"
                            if row[10]
                            else "ACCOUNT_CAPACITY_OR_EXISTING_POSITION",
                            "admission_read_quota_wait": row[10],
                            "account_capacity_wait": row[11],
                        }
                    if row[3]:
                        expected_waits[producer + ":" + source + ":non_actionable"] = {
                            "count": row[3],
                            "reason": "NON_DIRECTIONAL_BOOKKEEPING",
                        }
            latest = conn.execute(
                """SELECT payload FROM v2_business_state
                WHERE scope @> %s::jsonb AND NOT deleted
                AND scope->>'namespace'='trading-pipeline-cycle-v1'
                AND scope->>'account_id'=%s
                AND payload->>'status' IN ('CYCLE_COMPLETE','ENTRY_BLOCKED')
                ORDER BY (payload->>'started_at_ms')::bigint DESC LIMIT 1""",
                (
                    json.dumps(
                        {**asdict(self.scope), "namespace": "trading-pipeline-cycle-v1"}
                    ),
                    self.scope.account_id,
                ),
            ).fetchone()
        if latest and latest[0].get("capacity_declines"):
            declines = latest[0]["capacity_declines"]
            expected_waits["opening_capacity"] = {
                "reason": "LOCAL_PRE_SUBMIT_CAPACITY_DECLINE",
                "count": len(declines),
                "codes": sorted(set(declines.values())),
                "cycle_id": latest[0].get("cycle_id"),
                "observed_at_ms": latest[0].get("finished_at_ms"),
            }
            # This explains a refusal; it never suppresses a queue-lag finding
            # or a safety failure in the same cycle.
        settlement_clear = False
        if latest:
            settlement_clear, analysis_finding = settlement_health_projection(latest[0])
            if analysis_finding is not None:
                findings["SETTLEMENT_ANALYSIS_DEGRADED"] = analysis_finding
        if latest and latest[0].get("status") == "ENTRY_BLOCKED":
            phases = latest[0].get("phases", {})
            blocked = entry_blocking_stages(phases, settlement_clear=settlement_clear)
            findings["PIPELINE_ENTRY_BLOCKED"] = {"stages": blocked}
            if any(name in blocked for name in ("protection", "exits")):
                finding = findings.setdefault("POSITION_SAFETY_BLOCKED", {})
                finding["stages"] = sorted(
                    set(finding.get("stages", [])) | set(blocked)
                )
                lag = findings.pop("SIGNAL_CONSUMPTION_LAG", None)
                if lag:
                    expected_waits["signals_waiting_for_safety"] = {
                        "reason": "UPSTREAM_POSITION_SAFETY_BLOCKED",
                        "queues": lag,
                    }
        if "POSITION_SAFETY_BLOCKED" in findings:
            finding = findings["POSITION_SAFETY_BLOCKED"]
            try:
                details = (
                    pipeline_position_details(self.connect, scope, latest[0], now)
                    if latest
                    else {
                        "status": "UNAVAILABLE",
                        "protection": "UNKNOWN",
                    }
                )
            except Exception:  # noqa: BLE001 - optional enrichment cannot hide a safety alert
                details = {"status": "UNAVAILABLE", "protection": "UNKNOWN"}
            if rejected_closes:
                # The ledger diagnosis is fresh even when the last pipeline
                # observation is stale. Do not present stale coverage as current.
                details = {
                    **(details if details.get("status") == "CURRENT" else {}),
                    "status": "CURRENT",
                    "observed_at_ms": now,
                    "episodes": rejected_closes + details.get("episodes", []),
                    "protection": "UNKNOWN",
                }
            details["stages"] = finding["stages"]
            finding["details"] = health_notification_details(
                "POSITION_SAFETY_BLOCKED", details
            )
        previous = self.store.read(self.key)
        old = (
            json.loads(previous.payload_json)
            if previous and not previous.deleted
            else {}
        )
        # A cycle can finish while these DB reads are in flight. Validate its
        # maintenance evidence against an actual post-read clock sample, not
        # the older queue-query boundary; never trust a source timestamp as
        # our clock. Other health clocks and thresholds remain unchanged.
        maintenance_now = self.clock()
        maintenance_finding, maintenance_lanes, maintenance_first = (
            maintenance_health_projection(
                latest[0].get("phases", {}).get("signal_maintenance", {})
                if latest
                else {},
                old.get("maintenance_lanes", {}),
                maintenance_now,
                latest[0].get("finished_at_ms") if latest else None,
            )
        )
        if maintenance_finding is not None:
            findings["SIGNAL_MAINTENANCE_DEGRADED"] = {
                **maintenance_finding,
                "cycle_id": latest[0].get("cycle_id") if latest else None,
            }
        jev_budget = read_budget_health(self.connect, self.scope)
        jev_budget = apply_budget_health(findings, old, jev_budget)
        opening_incidents = retain_opening_incidents(
            self.connect, scope, old, findings, now
        )
        timers = old.get("first_seen_ms", {})
        first = {code: min(now, timers.get(code, now)) for code in findings}
        if maintenance_first is not None:
            first["SIGNAL_MAINTENANCE_DEGRADED"] = maintenance_first
        if rejected_closes:
            # Already aged from the durable rejection event: no second debounce
            # or restart reset. Never infer a venue position from the local sum.
            first["POSITION_SAFETY_BLOCKED"] = min(
                first["POSITION_SAFETY_BLOCKED"],
                min(item["rejected_at_ms"] for item in rejected_closes),
            )
        # Direct age checks already debounce order/settlement/signal diagnostics.
        delays = {
            "SIGNAL_CONSUMPTION_LAG": settings["health.signal_confirm_seconds"] * 1000,
            "SIGNAL_MAINTENANCE_DEGRADED": settings["health.pipeline_seconds"] * 1000,
            "SETTLEMENT_ANALYSIS_DEGRADED": settings["health.pipeline_seconds"] * 1000,
            "PIPELINE_ENTRY_BLOCKED": settings["health.pipeline_seconds"] * 1000,
            "POSITION_SAFETY_BLOCKED": settings["health.safety_seconds"] * 1000,
        }
        active = sorted(
            code
            for code in findings
            if (maintenance_now if code == "SIGNAL_MAINTENANCE_DEGRADED" else now)
            - first[code]
            >= delays.get(code, 0)
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
                "queue_diagnostics": queue_diagnostics,
                "maintenance_lanes": maintenance_lanes,
                "maintenance_observed_at_ms": maintenance_now,
                "signal_lag_state": signal_lag_state(
                    findings, active, clear_since, expected_waits, now, settings
                ),
                "capital": capital,
                "opening": opening,
                "opening_incidents": opening_incidents,
                "jev_budget": jev_budget,
            },
        )
        if result.code != "APPLIED":
            raise ValueError("BUSINESS_HEALTH_STATE_CONFLICT")
        return frozenset(active)
