"""Audited local disposition of an unbound UNKNOWN reduce-only Testnet request.

RECONCILED never claims exchange cancellation/rejection. The symbol stays
quarantined with no automatic expiry, so a late request cannot reduce a new
position. Every real fill is reconciled before disposing of the local request.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID

from v2_core.binance import identifier
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.orders import Orders
from v2_core.state import BusinessState, StateKey


def facts(conn, episode):
    return conn.execute(
        """SELECT f.fill_key,o.exchange_order_id,o.leg,f.quantity::text,
        f.price::text,f.fee::text,f.fee_currency,f.occurred_at_ms
        FROM v2_fills f JOIN v2_orders o USING(order_id) WHERE o.episode_id=%s ORDER BY f.fill_key""",
        (episode,),
    ).fetchall()


def fingerprint(rows):
    return digest(canonical({"fills": [list(r) for r in rows]}))


def state_payload(conn, key):
    row = BusinessState(lambda: nullcontext(conn)).read(key)
    if row is None or row.deleted:
        raise ValueError("MAINTENANCE_RESOLUTION_EVIDENCE_MISSING")
    return json.loads(row.payload_json)


def require_resolution(conn, case, episode, evidence):
    row = conn.execute(
        "SELECT exchange,account_id,environment,product,payload->>'symbol' FROM v2_trade_intents WHERE intent_id=%s",
        (episode,),
    ).fetchone()
    if row is None or row[2] != "SANDBOX":
        raise ValueError("TESTNET_RESOLUTION_ONLY")
    proof = state_payload(
        conn, StateKey(*row[:4], namespace="maintenance-resolution-v1", key=case)
    )
    quarantine = state_payload(
        conn, StateKey(*row[:4], namespace="symbol-opening-quarantine-v1", key=row[4])
    )
    adopted = state_payload(
        conn, StateKey(*row[:4], namespace="maintenance-adoption-v1", key=case)
    )
    expected = {
        "source": "approved-testnet-maintenance-resolution",
        "case_id": case,
        "venue_outcome": "UNKNOWN",
        "resolution_digest": digest(canonical(proof)),
    }
    rows = facts(conn, episode)
    with localcontext() as context:
        context.prec = 100
        balance = sum(
            (amount(r[3]) * (1 if r[2] == "OPEN" else -1) for r in rows), Decimal(0)
        )
    if (
        evidence != expected
        or proof.get("episode") != episode
        or proof.get("case_id") != case
        or proof.get("venue_outcome") != "UNKNOWN"
        or proof.get("venue_flat") is not True
        or proof.get("trade_history_matched") is not True
        or quarantine.get("halted") is not True
        or adopted.get("episode") != episode
        or adopted.get("status") != "FILLED"
        or not rows
        or balance != 0
        or proof.get("ledger_fingerprint") != fingerprint(rows)
        or conn.execute("SELECT 1 FROM v2_fills WHERE order_id=%s", (case,)).fetchone()
        or conn.execute(
            "SELECT 1 FROM v2_orders WHERE episode_id=%s AND order_id<>%s AND status NOT IN ('FILLED','CANCELLED','REJECTED')",
            (episode, case),
        ).fetchone()
    ):
        raise ValueError("INCOMPLETE_MAINTENANCE_RESOLUTION")


class MaintenanceResolution:
    def __init__(self, connect, request, *, scope, runner_stopped, clock_ms):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ) or (request.account_id, request.environment) != (
            scope.account_id,
            scope.environment,
        ):
            raise ValueError("BOUND_TESTNET_ACCOUNT_REQUIRED")
        self.connect, self.request, self.scope = connect, request, scope
        self.runner_stopped, self.clock = runner_stopped, clock_ms

    def read(self, path, params):
        return self.request("GET", path, params)

    def flat(self, symbol):
        rows = self.read("/fapi/v3/positionRisk", {"symbol": symbol})
        if not isinstance(rows, list) or any(
            r["symbol"] != symbol or amount(r["positionAmt"]) for r in rows
        ):
            raise ValueError("RESOLUTION_NOT_FLAT")
        for path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            if self.read(path, {"symbol": symbol}) != []:
                raise ValueError("RESOLUTION_OPEN_ORDERS")

    def resolve(self, case_id):
        case = str(UUID(str(case_id)))
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                raise ValueError("ACCOUNT_BUSY")
            guard.commit()
            if not self.runner_stopped():
                raise ValueError("TRADING_RUNNER_NOT_STOPPED")
            with self.connect() as conn:
                row = conn.execute(
                    """SELECT o.episode_id::text,o.client_order_id,o.status,o.version,
                    o.leg,o.exchange_order_id,i.payload->>'symbol',i.payload->>'side'
                    FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                    WHERE o.order_id=%s AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
                    (case, *asdict(self.scope).values()),
                ).fetchone()
                if row is None:
                    raise ValueError("UNKNOWN_CASE")
                episode, cid, status, version, leg, exchange_id, symbol, side = row
                key = StateKey(
                    **asdict(self.scope),
                    namespace="maintenance-resolution-v1",
                    key=case,
                )
                if status == "RECONCILED":
                    return state_payload(conn, key)
                if status != "UNKNOWN" or leg != "CLOSE" or exchange_id is not None:
                    raise ValueError("ONLY_UNBOUND_UNKNOWN_CLOSE")
                rows = facts(conn, episode)
            if not rows:
                raise ValueError("COMPLETE_LEDGER_REQUIRED")
            now = self.clock()
            start = min(r[7] for r in rows)
            if type(now) is not int or not start <= now or now - start >= 7 * 86400000:
                raise ValueError("BOUNDED_RECENT_HISTORY_REQUIRED")
            self.flat(symbol)
            if (
                self.read("/fapi/v1/positionSide/dual", {}).get("dualSidePosition")
                is not False
            ):
                raise ValueError("ONE_WAY_REQUIRED")
            if self.read(
                "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid}
            ) != {"code": -2013}:
                raise ValueError("ORIGINAL_ORDER_REQUIRES_VENUE_RECOVERY")
            history = self.read(
                "/fapi/v1/userTrades",
                {"symbol": symbol, "startTime": start, "endTime": now, "limit": 1000},
            )
            if not isinstance(history, list) or len(history) >= 1000:
                raise ValueError("INCOMPLETE_TRADE_HISTORY")
            expected = {}
            for r in rows:
                tid = json.loads(r[0])[-1]
                expected[tid] = (
                    r[1],
                    symbol,
                    side if r[2] == "OPEN" else ("SELL" if side == "BUY" else "BUY"),
                    "BOTH",
                    amount(r[3]),
                    amount(r[4]),
                    amount(r[5]),
                    r[6],
                    r[7],
                )
            actual = {}
            for t in history:
                if type(t.get("time")) is not int or not start <= t["time"] <= now:
                    raise ValueError("INVALID_TRADE_HISTORY_TIME")
                tid = identifier(t["id"])
                if tid in actual:
                    raise ValueError("DUPLICATE_TRADE_HISTORY")
                actual[tid] = (
                    identifier(t["orderId"]),
                    t["symbol"],
                    t["side"],
                    t["positionSide"],
                    amount(t["qty"], positive=True),
                    amount(t["price"], positive=True),
                    amount(t["commission"]),
                    t["commissionAsset"],
                    t["time"],
                )
            if actual != expected:
                raise ValueError("TRADE_HISTORY_LEDGER_MISMATCH")
            self.flat(symbol)
            if not self.runner_stopped():
                raise ValueError("TRADING_RUNNER_NOT_STOPPED")
            proof = {
                "episode": episode,
                "case_id": case,
                "symbol": symbol,
                "venue_outcome": "UNKNOWN",
                "venue_flat": True,
                "trade_history_matched": True,
                "trade_history_digest": digest(canonical({"trades": history})),
                "ledger_fingerprint": fingerprint(rows),
                "observed_at_ms": now,
                "disposition": "LOCAL_REQUEST_RECONCILED_SYMBOL_QUARANTINED",
            }
            with self.connect() as conn:
                conn.execute(
                    "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                    (episode,),
                )
                state = BusinessState(lambda: nullcontext(conn))
                quarantine = StateKey(
                    **asdict(self.scope),
                    namespace="symbol-opening-quarantine-v1",
                    key=symbol,
                )
                if state.read(quarantine) is None:
                    state.change(
                        quarantine,
                        expected_version=0,
                        request_key="quarantine",
                        payload={
                            "halted": True,
                            "case_id": case,
                            "reason": "LATE_UNKNOWN_REDUCE_ONLY_REQUEST",
                        },
                        reason="MAINTENANCE_SYMBOL_QUARANTINED",
                    )
                state.change(
                    key,
                    expected_version=0,
                    request_key="resolve",
                    payload=proof,
                    reason="LOCAL_NOT_VENUE_OUTCOME",
                )
                evidence = {
                    "source": "approved-testnet-maintenance-resolution",
                    "case_id": case,
                    "venue_outcome": "UNKNOWN",
                    "resolution_digest": digest(canonical(proof)),
                }
                if not Orders(lambda: nullcontext(conn)).transition(
                    case,
                    expected_version=version,
                    status="RECONCILED",
                    evidence=evidence,
                ):
                    raise ValueError("RESOLUTION_VERSION_CONFLICT")
            return proof
