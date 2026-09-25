"""GET-only adoption of a confirmed maintenance close into its original episode.

Does not resolve the original UNKNOWN or release its safety gate. Adopted orders
are query-only and never externally visible in PREPARED state. Network evidence
is fetched before the atomic ledger transaction; case binding is checked again
inside it. This is an internal Testnet operator workflow, not a public endpoint.
"""

from contextlib import nullcontext
from dataclasses import asdict, replace
from uuid import UUID

from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical
from v2_core.maintenance_binding import confirmed_close
from v2_core.orders import Orders
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState, StateKey


class MaintenanceCloseReconciler:
    def __init__(self, connect, request, *, scope):
        if scope.environment != "SANDBOX" or (
            request.account_id,
            request.environment,
        ) != (scope.account_id, scope.environment):
            raise ValueError("BOUND_TESTNET_ACCOUNT_REQUIRED")
        self.connect, self.scope = connect, scope

        def read(method, path, params):
            if method != "GET":
                raise ValueError("MAINTENANCE_ADOPTION_READ_ONLY")
            return request(method, path, params)

        self.venue = BinanceFutures(
            read, account_id=scope.account_id, environment=scope.environment
        )

    def reconcile(self, episode, case_id):
        episode, case_id = str(UUID(str(episode))), str(UUID(str(case_id)))
        with self.connect() as conn:
            scope, params, raw, binding = confirmed_close(conn, episode, case_id)
        if scope != tuple(asdict(self.scope).values()):
            raise ValueError("MAINTENANCE_ACCOUNT_MISMATCH")
        candidate = {
            **asdict(self.scope),
            "symbol": params["symbol"],
            "side": params["side"],
            "quantity": params["quantity"],
            "client_order_id": params["newClientOrderId"],
            "exchange_order_id": binding["exchange_order_id"],
            "order_type": "MARKET",
            "reduce_only": True,
        }
        self.venue._scope(candidate)
        self.venue._order(candidate, raw)
        observation = self.venue.query(candidate)
        if (
            observation is None
            or observation.status != "FILLED"
            or observation.evidence.get("fills_complete") is not True
        ):
            raise ValueError("MAINTENANCE_FILLS_NOT_CONFIRMED")
        with self.connect() as conn:
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            )
            _, fresh_params, _, fresh_binding = confirmed_close(conn, episode, case_id)
            if fresh_params != params or fresh_binding != binding:
                raise ValueError("MAINTENANCE_EVIDENCE_CHANGED")
            owned = conn.execute(
                """SELECT o.episode_id::text FROM v2_orders o JOIN v2_trade_intents i
                ON i.intent_id=o.episode_id WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND i.payload->>'symbol'=%s AND o.exchange_order_id=%s""",
                (*scope, params["symbol"], binding["exchange_order_id"]),
            ).fetchall()
            if any(row[0] != episode for row in owned):
                raise ValueError("VENUE_ORDER_ALREADY_OWNED")
            orders = Orders(lambda: nullcontext(conn))
            oid, cid = orders.prepare(
                episode,
                leg="CLOSE",
                quantity=params["quantity"],
                request_key="maintenance:" + case_id,
                evidence=binding,
            )
            runner = ExecutionRunner(
                lambda: nullcontext(conn),
                submit=lambda _: None,
                query=lambda _: None,
                risk_check=lambda _: False,
                scope=self.scope,
            )
            current = runner.snapshot(oid, connection=conn)
            if current["status"] == "PREPARED":
                orders.transition(
                    oid,
                    expected_version=current["version"],
                    status="SUBMITTING",
                    exchange_order_id=binding["exchange_order_id"],
                    evidence={
                        "reason": "MAINTENANCE_ALREADY_EXECUTED_NO_POST",
                        "binding": binding,
                    },
                )
            status = runner._apply_locked(
                conn,
                oid,
                replace(
                    observation,
                    client_order_id=cid,
                    evidence={**observation.evidence, "maintenance_binding": binding},
                ),
            )
            state = BusinessState(lambda: nullcontext(conn))
            key = StateKey(
                **asdict(self.scope), namespace="maintenance-adoption-v1", key=case_id
            )
            result = {
                "status": status,
                "episode": episode,
                "order_id": oid,
                "binding": binding,
                "exit_reason": "USER_APPROVED_MAINTENANCE",
                "historical_unknown_resolved": False,
            }
            prior = state.read(key)
            if prior:
                if prior.deleted or prior.payload_json != canonical(result):
                    raise ValueError("MAINTENANCE_ADOPTION_CONFLICT")
            else:
                state.change(
                    key,
                    expected_version=0,
                    request_key="adopt",
                    payload=result,
                    reason="VERIFIED_MAINTENANCE_FILL_ADOPTED",
                )
        return result
