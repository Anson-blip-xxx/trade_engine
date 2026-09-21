"""Read-only venue adoption of a registered testnet protection's MARKET child.

Internal and venue client identities remain distinct and immutable. Preparation,
query-only state and fill application commit atomically; no externally visible
PREPARED child can be picked up by an execution worker and submitted again.
"""

from contextlib import nullcontext
from dataclasses import asdict, replace
from uuid import UUID

from v2_core.binance import BinanceFutures, identifier
from v2_core.evidence import canonical
from v2_core.ledger import amount
from v2_core.orders import Orders
from v2_core.protection import TestnetProtection
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState, StateKey


class ProtectionChildReconciler:
    def __init__(self, connect, request, *, scope):
        self.connect, self.scope = connect, scope
        self.protection = TestnetProtection(
            BusinessState(connect), request, scope=scope
        )

        def read(method, path, params):
            if method != "GET":
                raise ValueError("CHILD_RECONCILER_IS_READ_ONLY")
            return request(method, path, params)

        self.request = read
        self.venue = BinanceFutures(
            read, account_id=scope.account_id, environment=scope.environment
        )

    def reconcile(self, spec):
        episode = str(UUID(spec.episode))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT exchange,account_id,environment,product,payload FROM v2_trade_intents WHERE intent_id=%s",
                (episode,),
            ).fetchone()
        if (
            row is None
            or row[:4] != tuple(asdict(self.scope).values())
            or row[4]["symbol"] != spec.symbol
            or row[4]["side"] == spec.side
        ):
            raise ValueError("PARENT_EPISODE_SCOPE_MISMATCH")
        result = self.protection.query(spec)
        if result["status"] not in {"TRIGGERING", "TRIGGERED", "FINISHED"}:
            return {"status": "WAITING_TRIGGER", "parent_status": result["status"]}
        _, payload = self.protection.read(spec)
        parent = payload["observation"]
        child = parent.get("actualOrderId")
        if child in (None, "", 0, "0"):
            return {"status": "WAITING_CHILD_ID"}
        if type(child) is int:
            child = identifier(child)
        if (
            not isinstance(child, str)
            or not child.isascii()
            or not child.isdecimal()
            or int(child) <= 0
            or str(int(child)) != child
        ):
            raise ValueError("INVALID_CHILD_ID")
        raw = self.request(
            "GET", "/fapi/v1/order", {"symbol": spec.symbol, "orderId": int(child)}
        )
        if raw.get("code") == -2013:
            return {"status": "WAITING_CHILD_VISIBILITY"}
        if (
            raw.get("type") != "MARKET"
            or raw.get("reduceOnly") is not True
            or str(raw.get("orderId")) != child
        ):
            raise ValueError("UNSUPPORTED_NATIVE_CHILD")
        quantity = raw["origQty"]
        amount(quantity, positive=True)
        if parent.get("actualQty") not in (None, "") and amount(
            parent["actualQty"]
        ) != amount(quantity):
            raise ValueError("PARENT_CHILD_QUANTITY_MISMATCH")
        candidate = {
            **asdict(self.scope),
            "symbol": spec.symbol,
            "side": spec.side,
            "quantity": quantity,
            "client_order_id": raw["clientOrderId"],
            "exchange_order_id": child,
            "order_type": "MARKET",
            "reduce_only": True,
        }
        # Validate the first order response as well as the separate order/fills
        # query; never trust an algo status as proof of complete fills.
        self.venue._scope(candidate)
        self.venue._order(candidate, raw)
        observation = self.venue.query(candidate)
        if observation is None:
            return {"status": "WAITING_CHILD_VISIBILITY"}
        binding = {
            "origin": "BINANCE_ALGO_CHILD",
            "parent_algo_id": parent["algoId"],
            "parent_client_algo_id": parent["clientAlgoId"],
            "exchange_order_id": child,
            "venue_client_order_id": raw["clientOrderId"],
        }
        ownership = StateKey(
            **asdict(self.scope),
            namespace="native-child-owner-v1",
            key=spec.symbol + ":" + child,
        )
        with self.connect() as conn:
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            ).fetchone()
            state = BusinessState(lambda: nullcontext(conn))
            owned = state.read(ownership)
            expected = {"episode": episode, "binding": binding}
            if owned and (owned.deleted or owned.payload_json != canonical(expected)):
                raise ValueError("NATIVE_CHILD_ALREADY_OWNED")
            if owned is None:
                state.change(
                    ownership,
                    expected_version=0,
                    request_key="bind",
                    payload=expected,
                    reason="CONFIRMED_PARENT_CHILD_IDENTITY",
                )
            orders = Orders(lambda: nullcontext(conn))
            order_id, client_id = orders.prepare(
                episode,
                leg="CLOSE",
                quantity=quantity,
                request_key="native-algo:" + str(parent["algoId"]),
                evidence=binding,
            )
            runner = ExecutionRunner(
                lambda: nullcontext(conn),
                submit=lambda _: None,
                query=lambda _: None,
                risk_check=lambda _: False,
                scope=self.scope,
            )
            current = runner.snapshot(order_id, connection=conn)
            if current["status"] == "PREPARED":
                orders.transition(
                    order_id,
                    expected_version=current["version"],
                    status="SUBMITTING",
                    exchange_order_id=child,
                    evidence={"reason": "EXCHANGE_CREATED_NO_POST", "binding": binding},
                )
            outcome = runner._apply_locked(
                conn,
                order_id,
                replace(
                    observation,
                    client_order_id=client_id,
                    evidence={
                        **observation.evidence,
                        "native_child_binding": binding,
                        "parent_observation": parent,
                    },
                ),
            )
        return {"status": outcome, "order_id": order_id, "exchange_order_id": child}
