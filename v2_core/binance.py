"""USD-M one-way protocol adapter; injected authenticated, bounded transport only.

The transport must NOT retry writes. Construction performs no I/O. Account and
environment are deployment bindings, never inferred from an incoming intent.
Missing historical orders are ambiguous, not permission to submit again.
"""

import re
from dataclasses import replace
from decimal import Decimal, localcontext

from v2_core.errors import SubmissionNotSent
from v2_core.ledger import amount
from v2_core.precision import decimal_text
from v2_core.reduction_fence import revalidate_reduction_write
from v2_core.runner import ExchangeObservation
from v2_core.transport import ExchangeTransportError


def identifier(value):
    if type(value) is not int or value < 0:
        raise ValueError("exchange identifier must be a nonnegative integer")
    return str(value)


def wire_amount(value):
    """Exact fixed-point serialization; remove only insignificant zero padding."""
    result = format(amount(value, positive=True), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _receipt_time(value):
    # Bounded Unix milliseconds, representable through 9999-12-31. A receipt
    # timestamp is venue evidence, not a local clock or freshness assertion.
    if type(value) is not int or not 0 <= value <= 253402300799999:
        raise ValueError("unavailable venue receipt time")
    return value


def _receipt_price(value):
    return decimal_text(amount(value, positive=True))


def _optional_receipt(raw, key, read):
    if key not in raw:
        return {"status": "MISSING"}
    try:
        value = read(raw[key])
    except ValueError:
        # Optional malformed diagnostics cannot reject an accepted order. Do
        # not copy the original value, exception text, or other response keys.
        return {"status": "UNAVAILABLE"}
    return {"status": "OBSERVED", "value": value}


def _submission_evidence(order, raw, executed):
    """Whitelisted receipt only; no synthetic fill or historical backfill."""
    status = raw.get("status")
    if not isinstance(status, str) or status not in {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "REJECTED",
    }:
        status = "UNAVAILABLE"
    return {
        "source": "binance-submit",
        "venue_status": status,
        "fills_complete": False,
        "submission_receipt": {
            "schema": "binance-submit-receipt-v1",
            # _scope and _order validated these against the persisted request.
            # IDs already belong to the typed observation, not duplicated here.
            "symbol": order["symbol"],
            "side": order["side"],
            "position_side": "BOTH",
            "order_type": order["order_type"],
            "reduce_only": order["reduce_only"],
            "original_quantity": wire_amount(order["quantity"]),
            "executed_quantity": decimal_text(executed),
            "average_price": _optional_receipt(raw, "avgPrice", _receipt_price),
            "venue_time_ms": _optional_receipt(raw, "time", _receipt_time),
            "venue_update_time_ms": _optional_receipt(raw, "updateTime", _receipt_time),
        },
    }


class BinanceFutures:
    def __init__(self, request, *, account_id, environment, max_pages=20):
        if not callable(request) or not account_id or not environment:
            raise ValueError("explicit transport and account binding required")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("bounded pagination required")
        if (
            getattr(request, "account_id", account_id) != account_id
            or getattr(request, "environment", environment) != environment
        ):
            raise ValueError("transport account binding mismatch")
        self.request = request
        self.account_id, self.environment = account_id, environment
        self.max_pages = max_pages

    def _scope(self, order):
        if (
            order["exchange"],
            order["product"],
            order["account_id"],
            order["environment"],
        ) != ("BINANCE", "FUTURES", self.account_id, self.environment):
            raise ValueError("order outside bound venue account")
        if order["side"] not in {"BUY", "SELL"} or order["order_type"] not in {
            "MARKET",
            "LIMIT",
        }:
            raise ValueError("unsupported order")
        if type(order["reduce_only"]) is not bool:
            raise ValueError("explicit reduce-only flag required")
        amount(order["quantity"], positive=True)
        if not re.fullmatch(r"[A-Z0-9_]{1,40}", order["symbol"]) or not re.fullmatch(
            r"[.A-Z:/a-z0-9_-]{1,36}", order["client_order_id"]
        ):
            raise ValueError("invalid venue symbol or client identity")
        if order["order_type"] == "LIMIT":
            amount(order["limit_price"], positive=True)
            if order["time_in_force"] not in {"GTC", "IOC", "FOK"}:
                raise ValueError("invalid limit time in force")

    def _order(self, order, raw):
        if not isinstance(raw, dict):
            raise ValueError("invalid exchange response")  # noqa: TRY004 - protocol validation
        for key, expected in {
            "clientOrderId": order["client_order_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "positionSide": "BOTH",
            "type": order["order_type"],
        }.items():
            if raw.get(key) != expected:
                raise ValueError("exchange order identity mismatch")
        if (
            type(raw.get("reduceOnly")) is not bool
            or raw["reduceOnly"] != order["reduce_only"]
        ):
            raise ValueError("exchange reduce-only mismatch")
        if amount(raw["origQty"], positive=True) != amount(
            order["quantity"], positive=True
        ):
            raise ValueError("exchange order quantity mismatch")
        if order["order_type"] == "LIMIT" and (
            amount(raw["price"], positive=True)
            != amount(order["limit_price"], positive=True)
            or raw.get("timeInForce") != order["time_in_force"]
        ):
            raise ValueError("exchange limit terms mismatch")
        order_id = identifier(raw["orderId"])
        if order.get("exchange_order_id") not in (None, order_id):
            raise ValueError("exchange order ID changed")
        executed = amount(raw["executedQty"])
        if not 0 <= executed <= amount(order["quantity"]):
            raise ValueError("invalid executed quantity")
        return order_id, executed

    def submit(self, order):
        if order.get("request_evidence", {}).get("origin") in {
            "BINANCE_ALGO_CHILD",
            "APPROVED_TESTNET_MAINTENANCE",
            "BINANCE_EXTERNAL_CLOSE",
        }:
            raise SubmissionNotSent("EXCHANGE_CREATED_ORDER_QUERY_ONLY")
        self._scope(order)
        try:
            mode = self.request("GET", "/fapi/v1/positionSide/dual", {})
        except Exception:  # noqa: BLE001 - only read preflight attempted, no order write
            raise SubmissionNotSent("VENUE_PREFLIGHT_UNAVAILABLE") from None
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
            raise SubmissionNotSent("VENUE_MODE_UNSUPPORTED")
        params = {
            "symbol": order["symbol"],
            "side": order["side"],
            "positionSide": "BOTH",
            "type": order["order_type"],
            "quantity": wire_amount(order["quantity"]),
            "newClientOrderId": order["client_order_id"],
            "newOrderRespType": "RESULT",
            "reduceOnly": "true" if order["reduce_only"] else "false",
        }
        if order["order_type"] == "LIMIT":
            params.update(
                price=wire_amount(order["limit_price"]),
                timeInForce=order["time_in_force"],
            )
        revalidate_reduction_write(
            self.account_id, self.environment, "POST", "/fapi/v1/order", params
        )
        try:
            raw = self.request("POST", "/fapi/v1/order", params)
        except ExchangeTransportError as exc:
            # Only these transport gates are guaranteed to precede any write.
            if str(exc) in {"ENDPOINT_OR_WRITE_DISABLED", "QUOTA_DENIED"}:
                raise SubmissionNotSent(str(exc)) from None
            # Exact synchronous parameter/auth validation failures only. Never
            # infer rejection from 5xx, timeout, duplicate IDs or query -2013.
            if (
                str(exc) == "EXCHANGE_RESPONSE_ERROR"
                and type(exc.status) is int
                and exc.status == 400
                and type(exc.code) is int
                and exc.code
                in {
                    -1021,
                    -1022,
                    -1100,
                    -1101,
                    -1102,
                    -1103,
                    -1111,
                    -1115,
                    -1116,
                    -1117,
                    -1121,
                    -1130,
                    -4131,  # Synchronous MARKET_ORDER_REJECT (PERCENT_PRICE).
                }
            ):
                return ExchangeObservation(
                    order["client_order_id"],
                    "REJECTED",
                    evidence={
                        "source": "binance-submit",
                        "reason": "VENUE_REQUEST_VALIDATION_REJECTED",
                        "submission_sent": True,
                        "transport": exc.diagnostic_evidence(),
                    },
                )
            raise
        order_id, executed = self._order(order, raw)
        # Even a FILLED response lacks per-fill commissions. Query before finality.
        return ExchangeObservation(
            order["client_order_id"],
            "ACKNOWLEDGED",
            order_id,
            evidence=_submission_evidence(order, raw, executed),
        )

    def query(self, order):
        binding = order.get("request_evidence", {})
        if binding.get("origin") in {
            "BINANCE_ALGO_CHILD",
            "APPROVED_TESTNET_MAINTENANCE",
            "BINANCE_EXTERNAL_CLOSE",
        }:
            if (
                order.get("leg") != "CLOSE"
                or order.get("reduce_only") is not True
                or order.get("exchange_order_id") != binding["exchange_order_id"]
            ):
                raise ValueError("invalid native child binding")
            venue_order = {
                **order,
                "client_order_id": binding["venue_client_order_id"],
                "request_evidence": {},
            }
            result = self.query(venue_order)
            return (
                None
                if result is None
                else replace(
                    result,
                    client_order_id=order["client_order_id"],
                    evidence={
                        **result.evidence,
                        (
                            "native_child_binding"
                            if binding["origin"] == "BINANCE_ALGO_CHILD"
                            else "external_close_binding"
                            if binding["origin"] == "BINANCE_EXTERNAL_CLOSE"
                            else "maintenance_binding"
                        ): binding,
                    },
                )
            )
        self._scope(order)
        bound_order_id = order.get("exchange_order_id")
        if bound_order_id is not None and (
            not isinstance(bound_order_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,18}", bound_order_id)
            or int(bound_order_id) > 2**63 - 1
        ):
            raise ValueError("invalid bound exchange order ID")
        raw, attempt = self._query_order(
            order["symbol"], "CLIENT_ORDER_ID", order["client_order_id"]
        )
        recovery_query = None
        if raw is None and bound_order_id is not None:
            # One bounded read through the other *persisted* identity. Absence
            # is not finality, and never authorizes resubmission or a guessed ID.
            raw, fallback = self._query_order(
                order["symbol"], "EXCHANGE_ORDER_ID", int(bound_order_id)
            )
            recovery_query = {
                "bound_exchange_order_id": True,
                "attempts": [attempt, fallback],
            }
            if raw is None:
                return ExchangeObservation(
                    order["client_order_id"],
                    "UNKNOWN",
                    bound_order_id,
                    evidence={
                        "source": "binance-recovery",
                        "reason": "BOUND_ORDER_LOOKUPS_ABSENT",
                        "recovery_query": recovery_query,
                    },
                )
        if raw is None:
            # Repair old UNKNOWN classification only from the original durable
            # synchronous reject, never from absence or a later query failure.
            failure = order.get("submission_failure", {})
            if (
                order.get("status") == "UNKNOWN"
                and order.get("exchange_order_id") is None
                and failure.get("reason") == "submission response unavailable"
                and failure.get("transport")
                == {
                    "category": "EXCHANGE_RESPONSE_ERROR",
                    "http_status": 400,
                    "exchange_code": -4131,
                }
            ):
                return ExchangeObservation(
                    order["client_order_id"],
                    "REJECTED",
                    evidence={
                        "source": "binance-recovery",
                        "reason": "DURABLE_MARKET_REJECT_CONFIRMED",
                        "submission_failure": failure,
                        "query_code": -2013,
                    },
                )
            return None
        return self._query_trades(order, raw, recovery_query=recovery_query)

    def _query_order(self, symbol, method, identity):
        """Read once; report fixed diagnostics, never venue text or request data."""
        key = "origClientOrderId" if method == "CLIENT_ORDER_ID" else "orderId"
        try:
            raw = self.request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, key: identity},
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
            return None, {
                "method": method,
                "outcome": "ABSENT",
                "transport": exc.diagnostic_evidence(),
            }
        if (
            isinstance(raw, dict)
            and type(raw.get("code")) is int
            and raw["code"] == -2013
        ):
            # Compatibility with injected transports returning the API error
            # body. HTTP status is unknown here and must not be invented.
            return None, {
                "method": method,
                "outcome": "ABSENT",
                "transport": {
                    "category": "EXCHANGE_RESPONSE_ERROR",
                    "exchange_code": -2013,
                },
            }
        if raw is None:
            # None is our internal explicit-absence sentinel, never a valid
            # wire response or permission to try another lookup identity.
            raise ValueError("invalid exchange response")
        return raw, {"method": method, "outcome": "FOUND", "transport": {}}

    def _query_trades(self, order, raw, *, recovery_query):
        order_id, executed = self._order(order, raw)
        fills, seen, cursor, complete = [], set(), 0, False
        for _ in range(self.max_pages):
            page = self.request(
                "GET",
                "/fapi/v1/userTrades",
                {
                    "symbol": order["symbol"],
                    "orderId": int(order_id),
                    "fromId": cursor,
                    "limit": 1000,
                },
            )
            if not isinstance(page, list) or len(page) > 1000:
                raise ValueError("invalid trade page")
            for trade in page:
                trade_id = identifier(trade["id"])
                if int(trade_id) < cursor or trade_id in seen:
                    raise ValueError("nonadvancing or duplicate trade page")
                if (
                    identifier(trade["orderId"]),
                    trade["symbol"],
                    trade["side"],
                    trade["positionSide"],
                ) != (order_id, order["symbol"], order["side"], "BOTH"):
                    raise ValueError("trade belongs to another order")
                qty, price, fee = (trade[k] for k in ("qty", "price", "commission"))
                amount(qty, positive=True)
                amount(price, positive=True)
                amount(fee)
                if type(trade["time"]) is not int or trade["time"] < 0:
                    raise ValueError("invalid trade time")
                if (
                    not isinstance(trade["commissionAsset"], str)
                    or not trade["commissionAsset"].strip()
                ):
                    raise ValueError("missing commission asset")
                proof = {
                    "source": "binance-userTrades",
                    "order_id": order_id,
                    "trade_id": trade_id,
                    "symbol": order["symbol"],
                }
                if "realizedPnl" in trade:
                    amount(trade["realizedPnl"])
                    proof["venue_realized_pnl"] = trade["realizedPnl"]
                fills.append(
                    {
                        "exchange_fill_id": trade_id,
                        "quantity": qty,
                        "price": price,
                        "fee": fee,
                        "fee_currency": trade["commissionAsset"],
                        "occurred_at_ms": trade["time"],
                        "evidence": proof,
                    }
                )
                seen.add(trade_id)
            if len(page) < 1000:
                complete = True
                break
            cursor = max(int(t["id"]) for t in page) + 1
        with localcontext() as ctx:
            ctx.prec = 80
            total = sum((Decimal(f["quantity"]) for f in fills), Decimal(0))
        complete = complete and total == executed
        status = "UNKNOWN"
        if complete:
            venue = raw["status"]
            if venue in {"NEW", "PARTIALLY_FILLED"}:
                status = "ACKNOWLEDGED"
            elif venue == "FILLED" and total == amount(order["quantity"]):
                status = "FILLED"
            elif venue in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
                status = "CANCELLED"
            elif venue == "REJECTED" and total == 0:
                status = "REJECTED"
        return ExchangeObservation(
            order["client_order_id"],
            status,
            order_id,
            tuple(fills),
            {
                "source": "binance-order-and-trades",
                "venue_status": raw["status"],
                "fills_complete": complete,
                "executed_quantity": raw["executedQty"],
                **({"recovery_query": recovery_query} if recovery_query else {}),
            },
        )
