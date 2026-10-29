"""Read-only venue adoption of a registered testnet protection's MARKET child.

Internal and venue client identities remain distinct and immutable. Preparation,
query-only state and fill application commit atomically; no externally visible
PREPARED child can be picked up by an execution worker and submitted again.
"""

import time
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
from v2_core.transport import ExchangeTransportError


class ProtectionChildReconciler:
    def __init__(self, connect, request, *, scope, clock_ms=None):
        if clock_ms is not None and not callable(clock_ms):
            raise TypeError("callable diagnostic clock required")
        self.connect, self.scope = connect, scope
        self.clock_ms = (
            clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        )
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

    def _pending(self, status, parent, *, child=None):
        """Stable, allowlisted diagnostics; never authority to infer a fill.

        A changing exact age would defeat the supervisor's result/alert dedup.
        These coarse display bands are not trading or retry thresholds. The
        venue parent update time is not necessarily the start of child waiting.
        """
        now, updated = self.clock_ms(), parent.get("updateTime")
        valid_time = type(now) is int and type(updated) is int and 0 < updated <= now
        band = "UNKNOWN"
        if valid_time:
            age = now - updated
            band = (
                "LT_5_MINUTES"
                if age < 300000
                else "LT_1_HOUR"
                if age < 3600000
                else "LT_1_DAY"
                if age < 86400000
                else "GE_1_DAY"
            )
        return {
            "status": status,
            "parent_status": parent["algoStatus"]
            if isinstance(parent.get("algoStatus"), str)
            and parent["algoStatus"] in {"TRIGGERING", "TRIGGERED", "FINISHED"}
            else "UNKNOWN",
            "parent_update_at_ms": updated if valid_time else None,
            "parent_update_age_band": band,
            "age_basis": "VENUE_PARENT_UPDATE_NOT_WAIT_START",
            "child_order_id": child,
            "reason": "CHILD_ID_NOT_RETURNED"
            if status == "WAITING_CHILD_ID"
            else "CHILD_ORDER_NOT_VISIBLE",
            "execution_authorized": False,
        }

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
            return self._pending("WAITING_CHILD_ID", parent)
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
        try:
            raw = self.request(
                "GET", "/fapi/v1/order", {"symbol": spec.symbol, "orderId": int(child)}
            )
        except ExchangeTransportError as exc:
            if (
                str(exc) != "EXCHANGE_RESPONSE_ERROR"
                or type(exc.status) is not int
                or exc.status != 400
                or type(exc.code) is not int
                or exc.code != -2013
            ):
                raise
            return self._pending("WAITING_CHILD_VISIBILITY", parent, child=child)
        if type(raw.get("code")) is int and raw["code"] == -2013:
            return self._pending("WAITING_CHILD_VISIBILITY", parent, child=child)
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
        if observation is None or (
            observation.status == "UNKNOWN"
            and (observation.evidence or {}).get("reason")
            == "BOUND_ORDER_LOOKUPS_ABSENT"
        ):
            # A once-visible child that disappeared on both identity lookups
            # must not acquire a local owner or CLOSE reservation. Its original
            # parent remains registered and can be queried again without POST.
            return self._pending("WAITING_CHILD_VISIBILITY", parent, child=child)
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
