"""Account-scoped business health, independent of the trading process.

Only reads trade records. Diagnostic timers/summaries live in PostgreSQL CAS
state; no exchange client, automatic cancellation or local-file fallback.
"""

import json
import time
from dataclasses import asdict
from uuid import uuid4

from services.v2_trading_pipeline import failed
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import EXPECTED_ADMISSION_WAITS
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyScope


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
                    findings["CAPITAL_DRAWDOWN_HALT"] = capital
        scope = tuple(asdict(self.scope).values())
        with self.connect() as conn:
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
        previous = self.store.read(self.key)
        old = (
            json.loads(previous.payload_json)
            if previous and not previous.deleted
            else {}
        )
        timers = old.get("first_seen_ms", {})
        first = {code: min(now, timers.get(code, now)) for code in findings}
        # Direct age checks already debounce order/settlement/signal diagnostics.
        delays = {
            "PIPELINE_ENTRY_BLOCKED": settings["health.pipeline_seconds"] * 1000,
            "POSITION_SAFETY_BLOCKED": settings["health.safety_seconds"] * 1000,
        }
        active = sorted(
            code for code in findings if now - first[code] >= delays.get(code, 0)
        )
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
                "findings": findings,
                "expected_waits": expected_waits,
                "capital": capital,
            },
        )
        if result.code != "APPLIED":
            raise ValueError("BUSINESS_HEALTH_STATE_CONFLICT")
        return frozenset(active)
