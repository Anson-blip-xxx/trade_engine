"""Asynchronous SANDBOX decision observation, never an execution gate.

Separate DB role can write research tables only. No venue or notification client.
Archive availability at observation is NOT proof of availability at decision time.
"""

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from services.v2_trade_review import trade_review
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.evidence import digest as encoded_digest
from v2_core.quant_indicators import aggregate_minutes
from v2_core.reentry import VERSION, ReentryPolicy, assess_reentry
from v2_core.strategy import StrategyScope


def digest(value):
    return encoded_digest(canonical(value))


@dataclass(frozen=True)
class ObserverLimits:
    poll_seconds: int = 15
    batch: int = 5
    lookback_ms: int = 300000
    max_lag_ms: int = 120000
    max_minutes: int = 720
    daily_rows: int = 1000
    daily_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self):
        bounds = {
            "poll_seconds": (5, 300),
            "batch": (1, 10),
            "lookback_ms": (60000, 900000),
            "max_lag_ms": (1000, 300000),
            "max_minutes": (30, 1440),
            "daily_rows": (1, 5000),
            "daily_bytes": (1024, 128 * 1024 * 1024),
            "total_bytes": (1024, 2 * 1024 * 1024 * 1024),
        }
        for key, (lo, hi) in bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError("OBSERVER_LIMIT_INVALID")
        if self.max_lag_ms > self.lookback_ms:
            raise ValueError("OBSERVER_LAG_WINDOW_INVALID")


def read_only(conn):
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    conn.execute("SET LOCAL statement_timeout='4s'")
    conn.execute("SET LOCAL lock_timeout='250ms'")


def active_route(conn):
    rows = conn.execute("""SELECT a.registry_id::text,a.exchange,a.account_id,a.environment,a.product,r.epoch
      FROM v2_execution_routes r JOIN v2_tenant_accounts a ON a.registry_id=r.target_registry
      WHERE r.environment='SANDBOX' AND a.environment='SANDBOX' AND r.phase='ACTIVE'
      AND NOT EXISTS(SELECT 1 FROM v2_account_retirements x
        WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id)""").fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return {
        "registry_id": row[0],
        "scope": asdict(AccountScope(*row[1:5])),
        "epoch": row[5],
    }


def initialize(conn, now):
    conn.execute(
        """INSERT INTO v2_reentry_observer_state(name,started_ms)
      VALUES ('sandbox',%s) ON CONFLICT DO NOTHING""",
        (now,),
    )
    return conn.execute(
        "SELECT started_ms FROM v2_reentry_observer_state WHERE name='sandbox'"
    ).fetchone()[0]


def discover(conn, route, *, started, now, policy_digest, limits):
    scope = AccountScope(**route["scope"])
    consumers = [
        StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
    ]
    rows = conn.execute(
        """SELECT d.decision_id::text,
      coalesce((e.snapshot->>'decided_at')::bigint,(extract(epoch FROM d.created_at)*1000)::bigint),d.side,
      e.snapshot->>'symbol',e.snapshot->>'observed_at',e.snapshot->>'source',
      e.snapshot->'features'->'signal'->>'episode_id',e.config_digest,e.strategy_version
      FROM v2_strategy_decisions d JOIN v2_decision_evidence e USING(evidence_ref)
      WHERE d.consumer=ANY(%s) AND d.action='OPEN'
      AND d.created_at BETWEEN to_timestamp(%s/1000.0) AND to_timestamp(%s/1000.0)
      AND NOT EXISTS (SELECT 1 FROM v2_reentry_observations o
        WHERE o.decision_id=d.decision_id AND o.policy_digest=%s)
      ORDER BY d.created_at,d.decision_id LIMIT %s""",
        (
            consumers,
            max(started, now - limits.lookback_ms),
            now,
            policy_digest,
            limits.batch + 1,
        ),
    ).fetchall()
    keys = (
        "decision_id",
        "decided_ms",
        "side",
        "symbol",
        "signal_observed_ms",
        "source",
        "signal_episode",
        "config_digest",
        "strategy_version",
    )
    return [dict(zip(keys, r)) for r in rows[: limits.batch]], len(rows) > limits.batch


def previous_close(conn, route, candidate, trades, *, truncated=False):
    if truncated:
        return None, "PRIOR_HISTORY_TRUNCATED"
    eligible = [
        r
        for r in trades
        if r["symbol"] == candidate["symbol"]
        and r.get("closed_ms") is not None
        and r.get("close_known_ms") is not None
        and max(r["closed_ms"], r["close_known_ms"]) <= candidate["decided_ms"]
    ]
    if not eligible:
        return None, "NO_PRIOR_CLOSE_IN_BOUNDED_HISTORY"
    latest = max(r["closed_ms"] for r in eligible)
    previous = [r for r in eligible if r["closed_ms"] == latest]
    if len(previous) != 1:
        return None, "PRIOR_CLOSE_AMBIGUOUS"
    prev = previous[0]
    if prev["side"] != candidate["side"] or prev["maintenance"]:
        return None, "PRIOR_NOT_SAME_SIDE_OR_MAINTENANCE"
    # Project only the prior closing evidence, never net PnL or later outcomes.
    now = candidate["decided_ms"]
    rows = conn.execute(
        """SELECT f.price::text FROM v2_orders o JOIN v2_fills f USING(order_id)
      JOIN v2_trade_intents i ON i.intent_id=o.episode_id
      WHERE o.episode_id=%s AND o.leg='CLOSE' AND f.occurred_at_ms=%s
      AND f.created_at<=to_timestamp(%s/1000.0)
      AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
      LIMIT 101""",
        (
            prev["id"],
            prev["closed_ms"],
            now,
            *(
                route["scope"][k]
                for k in ("exchange", "account_id", "environment", "product")
            ),
        ),
    ).fetchall()
    prices = {r[0] for r in rows}
    if len(prices) != 1 or len(rows) > 100:
        return None, "EXIT_FILL_PRICE_AMBIGUOUS_OR_MISSING"
    return {
        "previous_episode": prev["id"],
        "exit_reason": prev["exit_reason"],
        "exit_known_ms": max(prev["closed_ms"], prev["close_known_ms"]),
        "exit_price": next(iter(prices)),
    }, None


def observe(candidate, previous, missing, *, read_bars, now, policy, limits):
    result = {
        "kind": "ASYNCHRONOUS_DECISION_SHADOW",
        "version": VERSION,
        "candidate": candidate,
        "previous": previous,
        "observed_ms": now,
        "lag_ms": now - candidate["decided_ms"],
        "policy": asdict(policy),
        "bars": [],
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "archive_arrival_at_decision_proven": False,
    }
    assessment = {"status": "UNKNOWN", "reason": missing or "INPUT_INVALID"}
    if result["lag_ms"] < 0:
        raise ValueError("OBSERVER_CLOCK_REVERSED")
    if result["lag_ms"] > limits.max_lag_ms:
        assessment["reason"] = "OBSERVATION_TOO_LATE"
    elif missing:
        if missing == "PRIOR_NOT_SAME_SIDE_OR_MAINTENANCE":
            assessment["status"] = "NOT_APPLICABLE"
    elif previous:
        width = policy.timeframe_minutes * 60000
        warmup = (
            max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
        )
        end = candidate["decided_ms"] // width * width
        start = max(0, (previous["exit_known_ms"] // width - warmup) * width)
        if end <= start or end - start > limits.max_minutes * 60000:
            assessment["reason"] = "ARCHIVE_WINDOW_EXCEEDS_BUDGET"
        else:
            try:
                signal = int(candidate["signal_observed_ms"])
                raw = read_bars(candidate["symbol"], start, end)
                if len(raw) != (end - start) // 60000:
                    raise ValueError("ARCHIVE_INCOMPLETE")
                bars = aggregate_minutes(
                    list(reversed(raw)),
                    timeframe_minutes=policy.timeframe_minutes,
                    closed_at_ms=end,
                )
                result["bars"] = [b.payload() for b in bars]
                assessment = assess_reentry(
                    bars=bars,
                    side=candidate["side"],
                    signal_observed_ms=signal,
                    as_of_ms=candidate["decided_ms"],
                    policy=policy,
                    **previous,
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                # Do not serialize raw exception strings (may contain connection data).
                assessment = {"status": "UNKNOWN", "reason": "ARCHIVE_OR_INPUT_INVALID"}
            except Exception:  # noqa: BLE001 - archive failure is a sanitized research gap
                assessment = {"status": "UNKNOWN", "reason": "ARCHIVE_UNAVAILABLE"}
    result["assessment"] = assessment
    result["inputs_digest"] = digest(
        {"candidate": candidate, "previous": previous, "bars": result["bars"]}
    )
    return result


def persist(conn, route, observation, *, limits):
    """Atomic research-only quota + opportunity claim + immutable observation.

    Serializes only our singleton research row, never business/admission locks.
    """
    conn.execute("SET LOCAL statement_timeout='4s'")
    conn.execute("SET LOCAL lock_timeout='250ms'")
    state = conn.execute(
        "SELECT total_bytes FROM v2_reentry_observer_state WHERE name='sandbox' FOR UPDATE"
    ).fetchone()
    if not state:
        raise ValueError("OBSERVER_NOT_INITIALIZED")
    candidate = observation["candidate"]
    identity = str(UUID(candidate["decision_id"]))
    policy_digest = digest({"version": VERSION, "policy": observation["policy"]})
    if conn.execute(
        "SELECT 1 FROM v2_reentry_observations WHERE decision_id=%s AND policy_digest=%s",
        (identity, policy_digest),
    ).fetchone():
        return "ALREADY_RECORDED"
    if active_route(conn) != route:
        return "ROUTE_CHANGED"
    # PostgreSQL's own JSONB encoding matches the table's exact payload cap.
    size = (
        conn.execute(
            "SELECT octet_length(%s::jsonb::text)", (Jsonb(observation),)
        ).fetchone()[0]
        + 2048
    )
    if size > 262144:
        raise ValueError("OBSERVER_PAYLOAD_TOO_LARGE")
    day = datetime.fromtimestamp(
        observation["observed_ms"] / 1000, ZoneInfo("Asia/Shanghai")
    ).date()
    conn.execute(
        "INSERT INTO v2_reentry_observer_budget VALUES (%s,0,0) ON CONFLICT DO NOTHING",
        (day,),
    )
    used = conn.execute(
        "SELECT rows_used,bytes_used FROM v2_reentry_observer_budget WHERE day=%s FOR UPDATE",
        (day,),
    ).fetchone()
    if (
        used[0] >= limits.daily_rows
        or used[1] + size > limits.daily_bytes
        or state[0] + size > limits.total_bytes
    ):
        return "STORAGE_BUDGET_EXHAUSTED"
    assessment = observation["assessment"]
    claim = "NOT_READY"
    if assessment["status"] == "READY":
        key = digest(
            {
                "scope": route["scope"],
                "symbol": candidate["symbol"],
                "side": candidate["side"],
                "previous_episode": assessment["previous_episode"],
                "trigger_closed_ms": assessment["trigger_closed_ms"],
            }
        )
        inserted = conn.execute(
            "INSERT INTO v2_reentry_observer_claims VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING identity",
            (key, identity, policy_digest),
        ).fetchone()
        claim = "RECORDED" if inserted else "DUPLICATE_OPPORTUNITY"
    conn.execute(
        """INSERT INTO v2_reentry_observations VALUES
      (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            identity,
            policy_digest,
            route["registry_id"],
            Jsonb(route["scope"]),
            route["epoch"],
            candidate["decided_ms"],
            observation["observed_ms"],
            candidate["symbol"],
            candidate["side"],
            assessment["status"],
            assessment["reason"],
            claim,
            Jsonb(observation),
        ),
    )
    conn.execute(
        "UPDATE v2_reentry_observer_budget SET rows_used=rows_used+1,bytes_used=bytes_used+%s WHERE day=%s",
        (size, day),
    )
    conn.execute(
        "UPDATE v2_reentry_observer_state SET total_bytes=total_bytes+%s WHERE name='sandbox'",
        (size,),
    )
    return claim


class ReentryObserver:
    def __init__(self, connect, read_bars, *, policy=None, limits=None, clock_ms=None):
        self.connect, self.read_bars = connect, read_bars
        self.policy, self.limits = policy or ReentryPolicy(), limits or ObserverLimits()
        self.clock = clock_ms or (lambda: time.time_ns() // 1000000)
        self.policy_digest = digest({"version": VERSION, "policy": asdict(self.policy)})

    def tick(self):
        now = self.clock()
        with self.connect() as conn:
            started = initialize(conn, now)
            last_heartbeat, prior_health = conn.execute(
                "SELECT heartbeat_ms,health FROM v2_reentry_observer_state WHERE name='sandbox'"
            ).fetchone()
        with self.connect() as conn:
            read_only(conn)
            route = active_route(conn)
            if route:
                candidates, backlog = discover(
                    conn,
                    route,
                    started=started,
                    now=now,
                    policy_digest=self.policy_digest,
                    limits=self.limits,
                )
                review = (
                    trade_review(
                        conn, AccountScope(**route["scope"]), days=7, now_ms=now
                    )
                    if candidates
                    else {"trades": [], "truncated": False}
                )
                work = [
                    (
                        c,
                        *previous_close(
                            conn,
                            route,
                            c,
                            review["trades"],
                            truncated=review["truncated"],
                        ),
                    )
                    for c in candidates
                ]
            else:
                work, backlog = [], False
        results = []
        for candidate, previous, missing in work:
            observation = observe(
                candidate,
                previous,
                missing,
                read_bars=self.read_bars,
                now=self.clock(),
                policy=self.policy,
                limits=self.limits,
            )
            observation["collection_completed_ms"] = self.clock()
            with self.connect() as conn:
                outcome = persist(conn, route, observation, limits=self.limits)
            results.append(outcome)
            if outcome in {"ROUTE_CHANGED", "STORAGE_BUDGET_EXHAUSTED"}:
                break
        health = {
            "status": (
                "NO_ACTIVE_SANDBOX"
                if not route
                else results[-1]
                if results
                and results[-1] in {"ROUTE_CHANGED", "STORAGE_BUDGET_EXHAUSTED"}
                else "OBSERVING"
            ),
            "registry_id": route["registry_id"] if route else None,
            "route_epoch": route["epoch"] if route else None,
            "processed": len(results),
            "backlog_in_window": backlog,
            "policy_digest": self.policy_digest,
            "limits": asdict(self.limits),
            "execution_authorized": False,
            "max_poll_gap_ms": max(
                prior_health.get("max_poll_gap_ms", 0),
                now - last_heartbeat if last_heartbeat else 0,
            ),
        }
        with self.connect() as conn:
            conn.execute(
                "UPDATE v2_reentry_observer_state SET heartbeat_ms=%s,health=%s WHERE name='sandbox'",
                (self.clock(), Jsonb(health)),
            )
        return health
