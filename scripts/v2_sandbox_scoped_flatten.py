"""Explicit account-bound Testnet maintenance using existing cleanup journals.

All trading runners must be stopped, with the route held DRAINING. Only the
provided symbols may be reduced, followed by cancellation of their exact old
protection IDs. This does not resume trading, clear history, or invent fills.
Secrets are resolved in memory through the existing systemd credential provider.
"""

import argparse
import json
import re
import time
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID, uuid5

from scripts.v2_sandbox_maintenance_close import (
    SAFE_FAILURES,
    maintenance_binding,
    runners_stopped,
)
from services.v2_managed_testnet import database, private_for, scope_for
from services.v2_testnet_daemon_entry import PrivateRatePermit
from services.v2_testnet_scoped_cleanup import ScopedCleanup
from v2_core.evidence import canonical
from v2_core.ledger import amount
from v2_core.shared_market import HostBudget
from v2_core.state import BusinessState, StateKey
from v2_core.symbols import is_supported_symbol
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

FAILURE_CODES = SAFE_FAILURES | frozenset(
    {
        "SANDBOX_REQUIRED",
        "IMMUTABLE_PLAN_REQUIRED",
        "INVALID_CLEANUP_PLAN",
        "EXACT_PROTECTION_ID_REQUIRED",
        "CLEANUP_ACTION_DENIED",
        "DUPLICATE_ACTION",
        "EXACT_CLEANUP_ACTION_SET_REQUIRED",
        "EXACT_CLEANUP_WRITE_DENIED",
        "CLEANUP_EXECUTION_FENCED",
        "CLEANUP_PREFLIGHT_CHANGED",
        "BOUNDED_EXPLICIT_SYMBOLS_REQUIRED",
        "APPROVED_ACCOUNT_CHANGED",
        "APPROVED_POSITION_SET_CHANGED",
        "SAVED_CLEANUP_PLAN_INVALID",
        "SAVED_CLEANUP_PLAN_CHANGED",
        "ACCOUNT_ORDERS_REMAIN",
        "ACCOUNT_BINDING_MISMATCH",
        "TESTNET_FUTURES_ONLY",
        "INVALID_APPROVED_ALGOS",
        "CANCEL_UNCONFIRMED",
        "ALGO_IDENTITY_MISMATCH",
        "ALGO_REQUIRES_RECONCILIATION",
        "UNAPPROVED_PROTECTION",
    }
)


class ExactCleanupRequest:
    """An exact, one-attempt write whitelist around an existing scoped plan."""

    def __init__(self, request, *, stopped, route_unchanged):
        if request.environment != "SANDBOX":
            raise ValueError("SANDBOX_REQUIRED")
        self.account_id, self.environment = request.account_id, request.environment
        self.request, self.stopped, self.unchanged = request, stopped, route_unchanged
        self.allowed, self.attempted = {}, set()
        self.bound = False

    def bind(self, plan, *, preflight):
        if self.bound or not callable(preflight):
            raise ValueError("IMMUTABLE_PLAN_REQUIRED")
        symbol, actions = plan["symbol"], plan["actions"]
        if not is_supported_symbol(symbol) or not actions:
            raise ValueError("INVALID_CLEANUP_PLAN")
        allowed = {}
        closes, cancels = 0, []
        for action in actions:
            params = action["params"]
            if action["kind"] == "CLOSE":
                closes += 1
                expected = {
                    "symbol",
                    "side",
                    "positionSide",
                    "type",
                    "quantity",
                    "reduceOnly",
                    "newClientOrderId",
                    "newOrderRespType",
                }
                if (
                    set(params) != expected
                    or params["symbol"] != symbol
                    or params["side"] not in {"BUY", "SELL"}
                    or params["positionSide"] != "BOTH"
                    or params["type"] != "MARKET"
                    or params["reduceOnly"] != "true"
                    or params["newOrderRespType"] != "RESULT"
                    or not isinstance(params["newClientOrderId"], str)
                    or not re.fullmatch(r"v2[0-9a-f]{32}", params["newClientOrderId"])
                ):
                    raise ValueError("REDUCTION_ONLY")
                amount(params["quantity"], positive=True)
                method, path = "POST", "/fapi/v1/order"
            elif action["kind"] == "CANCEL":
                if (
                    set(params) != {"algoId"}
                    or type(params["algoId"]) is not int
                    or params["algoId"] <= 0
                    or action["symbol"] != symbol
                ):
                    raise ValueError("EXACT_PROTECTION_ID_REQUIRED")
                cancels.append(params["algoId"])
                method, path = "DELETE", "/fapi/v1/algoOrder"
            else:
                raise ValueError("CLEANUP_ACTION_DENIED")
            key = (method, path, canonical(params))
            if key in allowed:
                raise ValueError("DUPLICATE_ACTION")
            allowed[key] = deepcopy(action)
        if closes != 1 or sorted(cancels) != sorted(plan["approved_algos"]):
            raise ValueError("EXACT_CLEANUP_ACTION_SET_REQUIRED")
        self.allowed, self.preflight, self.bound = allowed, preflight, True

    def __call__(self, method, path, params):
        if method == "GET" and path in PrivateRatePermit._READS:
            return self.request(method, path, params)
        key = (method, path, canonical(params))
        if key not in self.allowed or key in self.attempted:
            raise ValueError("EXACT_CLEANUP_WRITE_DENIED")
        if not self.stopped() or not self.unchanged():
            raise ValueError("CLEANUP_EXECUTION_FENCED")
        if self.preflight(deepcopy(self.allowed[key])) is not True:
            raise ValueError("CLEANUP_PREFLIGHT_CHANGED")
        self.attempted.add(key)
        return self.request(method, path, params)


def saved_plan(store, scope, case_id, symbol):
    """Restore the original approval, never derive it from today's open algos."""
    snapshot = store.read(
        StateKey(
            **asdict(scope),
            namespace="testnet-scoped-cleanup-v1",
            key=case_id + ":plan",
        )
    )
    if snapshot is None:
        return None
    if snapshot.deleted or snapshot.version != 1:
        raise ValueError("SAVED_CLEANUP_PLAN_INVALID")
    try:
        plan = json.loads(snapshot.payload_json)
        approved = plan["approved_algos"]
        if (
            plan["symbol"] != symbol
            or not isinstance(approved, list)
            or any(type(identity) is not int or identity <= 0 for identity in approved)
            or len(approved) != len(set(approved))
            or not isinstance(plan["actions"], list)
            or not plan["actions"]
        ):
            raise ValueError("SAVED_CLEANUP_PLAN_INVALID")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("SAVED_CLEANUP_PLAN_INVALID") from exc
    return plan


def active_symbols(rows):
    if not isinstance(rows, list):
        raise TypeError("INCOMPLETE_ACCOUNT_READ")
    return {row["symbol"] for row in rows if amount(row["positionAmt"])}


def execute(*, expected_registry, expected_account, expected_epoch, case_id, symbols):
    expected_registry, case_id = str(UUID(expected_registry)), str(UUID(case_id))
    if (
        not isinstance(symbols, (list, tuple))
        or not symbols
        or len(symbols) > 20
        or len(set(symbols)) != len(symbols)
        or any(not is_supported_symbol(value) for value in symbols)
    ):
        raise ValueError("BOUNDED_EXPLICIT_SYMBOLS_REQUIRED")
    connect = database()
    with maintenance_binding(connect, expected_epoch, runners_stopped) as (
        tenant,
        target,
        unchanged,
    ):
        scope = scope_for(tenant, target)
        if (
            target["target_registry"] != expected_registry
            or scope.account_id != expected_account
        ):
            raise ValueError("APPROVED_ACCOUNT_CHANGED")
        cases = {symbol: str(uuid5(UUID(case_id), symbol)) for symbol in symbols}
        store = BusinessState(connect)
        previous = {
            symbol: saved_plan(store, scope, cases[symbol], symbol)
            for symbol in symbols
        }
        private = private_for(tenant, target, scope)
        transport = BinanceSignedTransport(
            account_id=scope.account_id,
            environment="SANDBOX",
            api_key=private["api_key"],
            api_secret=private["api_secret"],
            clock_ms=lambda: time.time_ns() // 1000000,
            permit=PrivateRatePermit(
                HostBudget(connect, environment="SANDBOX"),
                entries=False,
                protection=True,
                exits=True,
            ),
            enable_trading=True,
            enable_testnet_cancellation=True,
        )
        if transport("GET", "/fapi/v1/openOrders", {}) != []:
            raise ValueError("ORDINARY_ORDERS_PRESENT")
        active = active_symbols(transport("GET", "/fapi/v3/positionRisk", {}))
        # First use requires the exact approved position set. A restart may
        # observe already-closed symbols only when their original plans exist.
        if active - set(symbols) or any(
            symbol not in active and previous[symbol] is None for symbol in symbols
        ):
            raise ValueError("APPROVED_POSITION_SET_CHANGED")
        cleanups = []
        # Persist all approved plans before any order is submitted. Per-action
        # venue rechecks and durable cleanup CAS still precede each exact write.
        for symbol in symbols:
            original = previous[symbol]
            approved_algos = (
                original["approved_algos"]
                if original is not None
                else [
                    row["algoId"]
                    for row in transport(
                        "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}
                    )
                ]
            )
            request = ExactCleanupRequest(
                transport,
                stopped=runners_stopped,
                route_unchanged=unchanged,
            )
            cleanup = ScopedCleanup(
                connect,
                request,
                scope=scope,
                case_id=cases[symbol],
                symbol=symbol,
                approved_algos=approved_algos,
                runner_stopped=runners_stopped,
                pause=time.sleep,
            )
            planned = cleanup.plan()
            if original is not None and planned != original:
                raise ValueError("SAVED_CLEANUP_PLAN_CHANGED")
            request.bind(planned, preflight=cleanup.preflight)
            cleanups.append(cleanup)
        results = []
        for cleanup in cleanups:
            result = cleanup.run()
            results.append(
                {"symbol": cleanup.symbol, "case_id": cleanup.case_id, **result}
            )
            print(json.dumps(results[-1]), flush=True)
        for _ in range(2):
            if active_symbols(transport("GET", "/fapi/v3/positionRisk", {})):
                raise ValueError("ACCOUNT_NOT_FLAT")
            if any(
                transport("GET", path, {}) != []
                for path in (
                    "/fapi/v1/openOrders",
                    "/fapi/v1/openAlgoOrders",
                )
            ):
                raise ValueError("ACCOUNT_ORDERS_REMAIN")
        return {
            "status": "ACCOUNT_FLAT_VERIFIED",
            "closed": results,
            "new_entries_authorized": False,
        }


def failure_code(exc):
    """Only literal, reviewed codes reach operator output; never raw errors."""
    if isinstance(exc, ExchangeTransportError):
        return "VENUE_REQUEST_FAILED"
    if isinstance(exc, TimeoutError):
        return "VENUE_TIMEOUT"
    if type(exc) in {ValueError, TypeError} and str(exc) in FAILURE_CODES:
        return str(exc)
    return "MAINTENANCE_CHECK_FAILED"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("expected-registry", "expected-account", "case-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-epoch", required=True, type=int)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument(
        "--execute-approved-cleanup", action="store_true", required=True
    )
    args = parser.parse_args(argv)
    try:
        result = execute(
            expected_registry=args.expected_registry,
            expected_account=args.expected_account,
            expected_epoch=args.expected_epoch,
            case_id=args.case_id,
            symbols=args.symbols,
        )
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - never print signed request/credential details
        print(
            json.dumps({"status": "CLEANUP_STOPPED", "error_code": failure_code(exc)}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
