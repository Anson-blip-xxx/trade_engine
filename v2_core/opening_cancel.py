"""Bounded Testnet opening cancellation; durable intent before a single DELETE.

A cancellation response is never fill evidence. Recovery queries the original
order and trades, and never retries an ambiguous cancellation automatically.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from v2_core.binance import BinanceFutures
from v2_core.ledger import lock_order_episode
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState, StateKey

FINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


class TestnetOpeningCancel:
    __test__ = False

    def __init__(self, connect, request, *, scope, allow_cancel=False):
        if (
            (scope.exchange, scope.environment, scope.product)
            != ("BINANCE", "SANDBOX", "FUTURES")
            or getattr(request, "account_id", None) != scope.account_id
            or getattr(request, "environment", None) != scope.environment
            or type(allow_cancel) is not bool
        ):
            raise ValueError("explicit testnet cancellation binding required")
        self.connect, self.request, self.scope = connect, request, scope
        self.allow_cancel = allow_cancel
        adapter = BinanceFutures(
            request, account_id=scope.account_id, environment=scope.environment
        )

        def never_submit(_):
            raise ValueError("OPENING_SUBMISSION_DISABLED")

        self.runner = ExecutionRunner(
            connect,
            submit=never_submit,
            query=adapter.query,
            risk_check=lambda _: False,
            scope=scope,
        )

    def key(self, order_id):
        return StateKey(
            **asdict(self.scope),
            namespace="opening-cancel-v1",
            key=str(UUID(str(order_id))),
        )

    def snapshot(self, order_id, *, connection=None):
        order = self.runner.snapshot(order_id, connection=connection)
        if order["leg"] != "OPEN":
            raise ValueError("OPENING_ORDER_REQUIRED")
        with self.connect() if connection is None else nullcontext(connection) as conn:
            order["episode_id"] = conn.execute(
                "SELECT episode_id::text FROM v2_orders WHERE order_id=%s", (order_id,)
            ).fetchone()[0]
        return order

    def cancel_once(self, order_id):
        self.snapshot(order_id)  # Reject foreign accounts before any venue I/O.
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return "BUSY"
            guard.commit()
            order = self.snapshot(order_id)
            if order["status"] in FINAL:
                return order["status"]
            if BusinessState(self.connect).read(self.key(order_id)) is not None:
                return self.runner.recover(order_id)
            if not self.allow_cancel:
                return "CANCELLATION_DISABLED"
            if order["status"] != "PREPARED":
                # A fill/expiry can win before cancellation. Reconcile it first.
                status = self.runner.recover(order_id)
                if status in FINAL:
                    return status
            with self.connect() as conn:
                lock_order_episode(conn, order_id)
                order = self.snapshot(order_id, connection=conn)
                if order["status"] in FINAL:
                    return order["status"]
                if order["status"] == "PREPARED":
                    # Same root lock serializes dispatch; no exchange request.
                    from v2_core.orders import Orders

                    Orders(lambda: nullcontext(conn)).transition(
                        order_id,
                        expected_version=order["version"],
                        status="CANCELLED",
                        evidence={"reason": "opening withdrawn before dispatch"},
                    )
                    return "CANCELLED"
                params = {
                    "symbol": order["symbol"],
                    "origClientOrderId": order["client_order_id"],
                }
                result = BusinessState(lambda: nullcontext(conn)).change(
                    self.key(order_id),
                    expected_version=0,
                    request_key="request",
                    payload={
                        "status": "REQUESTED",
                        "params": params,
                        "order_version": order["version"],
                    },
                    reason="TESTNET_OPENING_CANCEL",
                )
                permitted = result.code == "APPLIED"
            if permitted:
                # Crash after commit but before transport also becomes query-only.
                # Do not store a raw response/exception that may contain secrets.
                error = None
                try:
                    self.request("DELETE", "/fapi/v1/order", params)
                except Exception as exc:  # noqa: BLE001 - outcome may be ambiguous
                    error = type(exc).__name__
                BusinessState(self.connect).change(
                    self.key(order_id),
                    expected_version=1,
                    request_key="transport-returned",
                    payload={
                        "status": "QUERY_REQUIRED",
                        "params": params,
                        "error_class": error,
                    },
                    reason="TESTNET_OPENING_CANCEL",
                )
            return self.runner.recover(order_id)
