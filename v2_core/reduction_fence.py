"""Request-local, fail-closed revalidation of one exact reduce-only write.

This carries no execution authority. Normal submission gates, quota and the
account/worker guard remain required. Each actual write boundary may recheck the
same immutable request after preceding network I/O or lock acquisition.
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from v2_core.errors import SubmissionNotSent
from v2_core.ingress import identity, symbol
from v2_core.precision import amount

_ACTIVE = ContextVar("trade_v2_reduction_fence", default=None)
_CHECKING = ContextVar("trade_v2_reduction_fence_checking", default=False)
_FAILURE = "REDUCE_ONLY_REVALIDATION_FAILED"


@dataclass(frozen=True)
class _Fence:
    order: dict
    expected: dict
    check: object


def _bind(order, check):
    if not isinstance(order, dict) or not callable(check):
        raise TypeError("invalid reduction fence")
    original = deepcopy(order)
    if (
        original["leg"] != "CLOSE"
        or original["reduce_only"] is not True
        or original["exchange"] != "BINANCE"
        or original["product"] != "FUTURES"
        or original["environment"] not in {"SANDBOX", "LIVE"}
        or original["side"] not in {"BUY", "SELL"}
        or not isinstance(original["client_order_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,36}", original["client_order_id"])
    ):
        raise ValueError("invalid reduction binding")
    identity(original["account_id"])
    expected = {
        "symbol": symbol(original["symbol"]),
        "side": original["side"],
        "positionSide": "BOTH",
        "type": original["order_type"],
        "quantity": amount(original["quantity"], positive=True),
        "newClientOrderId": original["client_order_id"],
        "newOrderRespType": "RESULT",
        "reduceOnly": "true",
    }
    if original["order_type"] == "MARKET":
        if (
            original.get("limit_price") is not None
            or original.get("time_in_force") is not None
        ):
            raise ValueError("market order has limit terms")
    elif original["order_type"] == "LIMIT" and original.get("time_in_force") in {
        "GTC",
        "IOC",
        "FOK",
    }:
        expected.update(
            price=amount(original["limit_price"], positive=True),
            timeInForce=original["time_in_force"],
        )
    else:
        raise ValueError("unsupported reduction type")
    return _Fence(original, expected, check)


@contextmanager
def reduction_fence(order, check):
    """Bind a single CLOSE request; nested fences cannot replace this binding."""
    try:
        if _ACTIVE.get() is not None:
            raise ValueError("nested reduction fence")
        bound = _bind(order, check)
    except Exception:  # noqa: BLE001 - no raw request or callback errors may escape
        raise SubmissionNotSent(_FAILURE) from None
    token = _ACTIVE.set(bound)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def revalidate_reduction_write(account_id, environment, method, path, params):
    """Reject a mismatching or stale write without sending or granting authority.

    GETs are left to their original read gates. A successful return is deliberately
    None: it must not replace any ordinary execution authorization.
    """
    bound = _ACTIVE.get()
    if bound is None or method == "GET":
        return
    try:
        if (
            _CHECKING.get()
            or (account_id, environment)
            != (bound.order["account_id"], bound.order["environment"])
            or method != "POST"
            or path != "/fapi/v1/order"
            or not isinstance(params, dict)
            or set(params) != set(bound.expected)
        ):
            raise ValueError("reduction write identity mismatch")
        for field, expected in bound.expected.items():
            actual = params[field]
            if field in {"quantity", "price"}:
                actual = amount(actual, positive=True)
            elif not isinstance(actual, str):
                raise ValueError("invalid wire value")
            if actual != expected:
                raise ValueError("reduction write terms mismatch")
        token = _CHECKING.set(True)
        try:
            if bound.check(deepcopy(bound.order)) is not True:
                raise ValueError("reduction check denied")
        finally:
            _CHECKING.reset(token)
    except Exception:  # noqa: BLE001 - only a fixed definitely-unsent diagnostic
        raise SubmissionNotSent(_FAILURE) from None
