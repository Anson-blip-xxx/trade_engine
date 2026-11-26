"""Resume committed typed intents without evaluating strategies or sending orders.

The immutable admission receipt is the recovery input. Preparation retains the
existing intent -> account -> episode lock order and current opening fences.
This adapter is deliberately not wired into a scheduler or dispatch pipeline.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from v2_core.account_risk import AccountRiskDenied, AccountScope
from v2_core.account_risk import _lock as lock_account
from v2_core.intents import IntentStore
from v2_core.orders import Orders
from v2_core.scheduling import diagnostic_code
from v2_core.strategy_plan_admission import require_plan_admission
from v2_core.strategy_plan_store import read_trade_plan

_VALUE_DIAGNOSTICS = frozenset(
    {
        "STRATEGY_PREPARE_INTENT_NOT_FOUND",
        "STRATEGY_PREPARE_TYPED_PLAN_REQUIRED",
        "STRATEGY_PREPARE_ROOT_STATE_CONFLICT",
        "STRATEGY_PLAN_INTENT_REQUIRED",
        "STRATEGY_PLAN_EVIDENCE_INVALID",
        "STRATEGY_PLAN_INTENT_MISMATCH",
    }
)


class StrategyPrepareRecovery:
    def __init__(self, connect, *, scope):
        if not callable(connect) or type(scope) is not AccountScope:
            raise TypeError("STRATEGY_PREPARE_PORTS_REQUIRED")
        self.connect = connect
        self.scope = AccountScope(**asdict(scope))

    @staticmethod
    def _timeouts(conn):
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='5s'")

    def recover(self, intent_id):
        """Prepare one original MARKET/OPEN request, or report its durable state.

        Only a valid, admitted, order-free RECEIVED intent whose PG deadline has
        passed becomes EXPIRED. Other opening denials and unknown DB outcomes
        raise without terminalizing the intent. Retry the same identity after
        an ambiguous commit; existing orders are never recreated or resubmitted.
        """
        intent_id = str(UUID(intent_id))
        with self.connect() as conn:
            self._timeouts(conn)
            row = conn.execute(
                """SELECT i.status,e.snapshot FROM v2_trade_intents i
                JOIN v2_decision_evidence e USING(evidence_ref)
                WHERE i.intent_id=%s
                  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                FOR UPDATE OF i""",
                (intent_id, *asdict(self.scope).values()),
            ).fetchone()
            if row is None:
                raise ValueError("STRATEGY_PREPARE_INTENT_NOT_FOUND")
            plan = read_trade_plan(conn, intent_id)
            if plan is None:
                raise ValueError("STRATEGY_PREPARE_TYPED_PLAN_REQUIRED")
            lock_account(conn, self.scope.key)
            # Historical proof remains valid after a deployment stops. Validate
            # it even for expiry; malformed/orphan records are not terminal work.
            require_plan_admission(
                conn, intent_id, deployment=row[1].get("strategy_deployment")
            )
            existing = conn.execute(
                """SELECT order_id::text,client_order_id,leg,status,request_key
                FROM v2_orders WHERE episode_id=%s
                ORDER BY (leg='OPEN' AND request_key='initial') DESC,order_id LIMIT 1""",
                (intent_id,),
            ).fetchone()
            result = {"intent_id": intent_id, "execution_authorized": False}
            if existing is not None:
                result.update(
                    status="EXISTING_ORDER",
                    intent_status=row[0],
                    **dict(
                        zip(
                            (
                                "order_id",
                                "client_order_id",
                                "leg",
                                "order_status",
                                "request_key",
                            ),
                            existing,
                            strict=True,
                        )
                    ),
                )
            elif row[0] != "RECEIVED":
                result.update(status="UNCHANGED", intent_status=row[0])
            else:
                now_ms = conn.execute(
                    "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
                ).fetchone()[0]
                bound = lambda: nullcontext(conn)
                if now_ms >= plan.expires_at_ms:
                    changed = IntentStore(bound).terminate_unstarted(
                        intent_id,
                        status="EXPIRED",
                        reason="STRATEGY_PLAN_EXPIRED_BEFORE_PREPARATION",
                    )
                    if not changed:
                        raise ValueError("STRATEGY_PREPARE_ROOT_STATE_CONFLICT")
                    result.update(status="EXPIRED", intent_status="EXPIRED")
                else:
                    # This call rechecks current deployment, PG time, account
                    # drain, symbol/episode halt and the permanent claim before
                    # atomically inserting the episode, order, event and outbox.
                    order_id, client_id = Orders(bound).prepare(intent_id)
                    result.update(
                        status="PREPARED",
                        intent_status="RECEIVED",
                        order_id=order_id,
                        client_order_id=client_id,
                    )
        # Never acknowledge preparation/expiry before the outer commit succeeds.
        return result

    def run_once(self, *, limit=100, after=None):
        """Read one bounded page, independently recover each candidate, then stop.

        Pass ``next_after`` into the next call, including after failed rows. A
        null cursor starts a new sweep, revisiting blocked work and late commits
        behind a previous cursor. No cursor or lease is an execution permit.
        Page-read failures propagate; per-intent failures are safe diagnostics.

        The limit bounds returned candidates, not database rows examined or
        total batch duration. Timeouts are per statement. This slice adds no
        recovery-specific index, scheduler cadence or production load acceptance.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("STRATEGY_PREPARE_LIMIT_INVALID")
        after = None if after is None else str(UUID(after))
        with self.connect() as conn:
            self._timeouts(conn)
            rows = conn.execute(
                """SELECT i.intent_id::text FROM v2_trade_intents i
                JOIN v2_decision_evidence e USING(evidence_ref)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                  AND i.status='RECEIVED' AND e.snapshot ? 'strategy_plan'
                  AND NOT EXISTS (SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id)
                  AND (%s::uuid IS NULL OR i.intent_id>%s::uuid)
                ORDER BY i.intent_id LIMIT %s""",
                (*asdict(self.scope).values(), after, after, limit),
            ).fetchall()
        results = []
        for (intent_id,) in rows:
            try:
                result = self.recover(intent_id)
            except AccountRiskDenied as exc:
                result = {
                    "intent_id": intent_id,
                    "status": "BLOCKED",
                    "reason": diagnostic_code(exc),
                    "execution_authorized": False,
                }
            except Exception as exc:  # noqa: BLE001 - isolate one candidate only
                code = diagnostic_code(exc) if type(exc) is ValueError else None
                result = {
                    "intent_id": intent_id,
                    "status": "ERROR",
                    "error_code": code
                    if code in _VALUE_DIAGNOSTICS
                    else type(exc).__name__,
                    "execution_authorized": False,
                }
            results.append(result)
        return {
            "results": results,
            "next_after": rows[-1][0] if len(rows) == limit else None,
            "execution_authorized": False,
        }
