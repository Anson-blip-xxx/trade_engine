"""One approved BR Demo parent cancellation, never a close or trading permit.

No production credential selection, POST, bulk cancellation, original-journal
mutation, or ambiguous write retry exists. The durable latch is parent-scoped,
not case-scoped: another case UUID cannot authorize another DELETE.
"""

import argparse
import json
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID

from scripts.v2_sandbox_maintenance_close import maintenance_binding, runners_stopped
from services.v2_managed_testnet import database, private_for, scope_for
from services.v2_testnet_daemon_entry import PrivateRatePermit
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.recovery_diagnostics import transport_evidence
from v2_core.session_lock import session_advisory_lock
from v2_core.shared_market import HostBudget
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport

SYMBOL = "BRUSDT"
EPOCH = 46
READS = frozenset(
    {
        "/fapi/v3/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/algoOrder",
    }
)
FINAL_PARENT = frozenset({"CANCELED", "EXPIRED", "REJECTED", "FINISHED"})
ERRORS = frozenset(
    {
        "EXPLICIT_STUCK_PARENT_APPROVAL_REQUIRED",
        "SANDBOX_SCOPE_REQUIRED",
        "UNIQUE_STUCK_PARENT_REQUIRED",
        "PARENT_LOCAL_IDENTITY_INVALID",
        "QUARANTINE_REQUIRED",
        "RUNNERS_OR_ROUTE_CHANGED",
        "ACCOUNT_BUSY",
        "ACCOUNT_LOCK_LOST",
        "ONE_WAY_REQUIRED",
        "EXACT_BR_EXPOSURE_REQUIRED",
        "ORDINARY_ORDERS_PRESENT",
        "CONDITIONAL_ORDERS_PRESENT",
        "POSITION_CHANGED",
        "PARENT_IDENTITY_INVALID",
        "PARENT_NOT_STUCK",
        "ACTUAL_CHILD_PRESENT",
        "READ_NOT_APPROVED",
        "EXACT_PARENT_DELETE_ONLY",
        "PLAN_CONFLICT",
        "SEND_LATCH_CONFLICT",
        "SAVED_PLAN_INVALID",
        "MAINTENANCE_STATE_CONFLICT",
    }
)


class CancelRejected(ValueError):
    pass


def safe_error(exc):
    if type(exc) is CancelRejected and str(exc) in ERRORS:
        return {"category": str(exc)}
    return transport_evidence(exc)


def no_child(raw):
    value = raw.get("actualOrderId")
    return (
        value is None
        or (type(value) is str and value in {"", "0"})
        or (type(value) is int and value == 0)
    )


def checked_parent(raw, *, spec, expected, algo_id, stuck):
    if (
        not isinstance(raw, dict)
        or any(
            raw.get(key) != expected[key]
            for key in (
                "symbol",
                "side",
                "positionSide",
                "algoType",
                "clientAlgoId",
                "workingType",
            )
        )
        or (
            type(raw.get("algoId")) is not int
            or raw["algoId"] != algo_id
            or raw.get("orderType") != "STOP_MARKET"
            or ("type" in raw and raw["type"] != "STOP_MARKET")
            or raw.get("closePosition") is not True
            or raw.get("priceProtect") is not False
            or amount(raw.get("triggerPrice"), positive=True)
            != amount(spec.trigger_price)
            or raw.get("algoStatus")
            not in FINAL_PARENT | {"NEW", "TRIGGERING", "TRIGGERED"}
        )
    ):
        raise CancelRejected("PARENT_IDENTITY_INVALID")
    if stuck and raw["algoStatus"] != "TRIGGERING":
        raise CancelRejected("PARENT_NOT_STUCK")
    if stuck and not no_child(raw):
        raise CancelRejected("ACTUAL_CHILD_PRESENT")
    return {
        "algo_id": algo_id,
        "client_algo_id": expected["clientAlgoId"],
        "venue_status": raw["algoStatus"],
        "actual_child_id_present": not no_child(raw),
    }


class DeleteOnlyPermit:
    def __init__(self, budget):
        self.budget = budget
        self.read = PrivateRatePermit(
            budget, entries=False, protection=False, exits=False
        )

    def __call__(self, method, path):
        if method == "GET" and path in READS:
            return self.read(method, path)
        return (
            method == "DELETE"
            and path == "/fapi/v1/algoOrder"
            and self.budget.permit(1) is True
        )

    def penalize(self, seconds):
        self.budget.penalize(seconds)


class ExactParentRequest:
    def __init__(self, raw, *, scope, algo_id, client_id, fence):
        if (
            (scope.exchange, scope.environment, scope.product)
            != ("BINANCE", "SANDBOX", "FUTURES")
            or (raw.account_id, raw.environment) != (scope.account_id, "SANDBOX")
            or type(algo_id) is not int
            or algo_id <= 0
        ):
            raise CancelRejected("SANDBOX_SCOPE_REQUIRED")
        self.account_id, self.environment = scope.account_id, "SANDBOX"
        self.raw, self.algo_id, self.client_id, self.fence = (
            raw,
            algo_id,
            client_id,
            fence,
        )
        self.preflight, self.latch, self.attempted = None, None, False

    def bind(self, preflight, latch):
        if self.preflight is not None or not callable(preflight) or not callable(latch):
            raise CancelRejected("EXACT_PARENT_DELETE_ONLY")
        self.preflight, self.latch = preflight, latch

    def __call__(self, method, path, params):
        if method == "GET" and path in READS:
            allowed = (
                ({"algoId": self.algo_id}, {"clientAlgoId": self.client_id})
                if path.endswith("/algoOrder")
                else ({},)
            )
            if params not in allowed:
                raise CancelRejected("READ_NOT_APPROVED")
            return self.raw(method, path, deepcopy(params))
        if (
            method != "DELETE"
            or path != "/fapi/v1/algoOrder"
            or params != {"algoId": self.algo_id}
            or self.attempted
            or self.preflight is None
            or self.latch is None
        ):
            raise CancelRejected("EXACT_PARENT_DELETE_ONLY")
        if self.fence() is not True:
            raise CancelRejected("RUNNERS_OR_ROUTE_CHANGED")
        if self.preflight() is not True:
            raise CancelRejected("PARENT_NOT_STUCK")
        if self.latch() is not True:  # Committed before any possible external write.
            raise CancelRejected("SEND_LATCH_CONFLICT")
        if self.fence() is not True:
            raise CancelRejected("RUNNERS_OR_ROUTE_CHANGED")
        self.attempted = True
        return self.raw(method, path, {"algoId": self.algo_id})


class StuckParentCancel:
    def __init__(
        self, connect, raw, *, scope, case_id, stopped, route_unchanged, clock_ms=None
    ):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ) or (raw.account_id, raw.environment) != (scope.account_id, "SANDBOX"):
            raise CancelRejected("SANDBOX_SCOPE_REQUIRED")
        self.connect, self.raw, self.scope = connect, raw, scope
        self.case_id = str(UUID(case_id))
        self.stopped, self.route_unchanged = stopped, route_unchanged
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)
        self.store = BusinessState(connect)

    def quarantine(self):
        key = StateKey(
            **asdict(self.scope), namespace="symbol-opening-quarantine-v1", key=SYMBOL
        )
        row = self.store.read(key)
        if (
            row is None
            or row.deleted
            or json.loads(row.payload_json).get("halted") is not True
        ):
            raise CancelRejected("QUARANTINE_REQUIRED")
        return {"state_id": key.identity, "version": row.version}

    def parent(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT state_id::text,version,scope,payload FROM v2_business_state WHERE NOT deleted "
                "AND (scope->>'exchange',scope->>'account_id',scope->>'environment',scope->>'product')=(%s,%s,%s,%s) "
                "AND scope->>'namespace'='testnet-protection-v1' AND payload->'spec'->>'symbol'='BRUSDT' "
                "AND (payload->>'status' IS NULL OR payload->>'status' NOT IN "
                "('CANCELED','EXPIRED','REJECTED','FINISHED','EXTERNAL_FLAT_RECONCILED')) LIMIT 2",
                (
                    self.scope.exchange,
                    self.scope.account_id,
                    self.scope.environment,
                    self.scope.product,
                ),
            ).fetchall()
        if len(rows) != 1:
            raise CancelRejected("UNIQUE_STUCK_PARENT_REQUIRED")
        identity, version, stored_scope, payload = rows[0]
        spec = ProtectionSpec(**payload["spec"])
        reader = TestnetProtection(self.store, self.raw, scope=self.scope)
        key, expected = reader.key(spec), reader.params(spec)
        algo_id = payload.get("observation", {}).get("algoId")
        if (
            spec.symbol != SYMBOL
            or spec.side != "BUY"
            or spec.kind != "STOP_MARKET"
            or payload.get("status") != "TRIGGERING"
            or identity != key.identity
            or stored_scope != asdict(key)
            or type(algo_id) is not int
            or algo_id <= 0
            or (
                "algoId" in payload.get("submission", {})
                and payload["submission"]["algoId"] != algo_id
            )
        ):
            raise CancelRejected("PARENT_LOCAL_IDENTITY_INVALID")
        checked_parent(
            payload["observation"],
            spec=spec,
            expected=expected,
            algo_id=algo_id,
            stuck=True,
        )
        return spec, expected, algo_id, {"state_id": identity, "version": version}

    def baseline(self, request):
        if (
            request("GET", "/fapi/v1/positionSide/dual", {}).get("dualSidePosition")
            is not False
        ):
            raise CancelRejected("ONE_WAY_REQUIRED")
        positions = self.position(request)
        if request("GET", "/fapi/v1/openOrders", {}) != []:
            raise CancelRejected("ORDINARY_ORDERS_PRESENT")
        if request("GET", "/fapi/v1/openAlgoOrders", {}) != []:
            raise CancelRejected("CONDITIONAL_ORDERS_PRESENT")
        return positions

    @staticmethod
    def position(request):
        rows = request("GET", "/fapi/v3/positionRisk", {})
        if not isinstance(rows, list):
            raise CancelRejected("EXACT_BR_EXPOSURE_REQUIRED")
        active = [row for row in rows if amount(row["positionAmt"])]
        if (
            len(active) != 1
            or active[0].get("symbol") != SYMBOL
            or active[0].get("positionSide") != "BOTH"
            or amount(active[0]["positionAmt"]) != -256
        ):
            raise CancelRejected("EXACT_BR_EXPOSURE_REQUIRED")
        return {
            key: active[0][key]
            for key in (
                "symbol",
                "positionSide",
                "positionAmt",
                "entryPrice",
                "updateTime",
            )
        }

    def change(self, key, row, payload, request_key):
        result = self.store.change(
            key,
            expected_version=0 if row is None else row.version,
            request_key=request_key,
            payload=payload,
            reason="APPROVED_STUCK_PARENT_CANCEL",
        )
        if result.code != "APPLIED":
            raise CancelRejected("MAINTENANCE_STATE_CONFLICT")

    def run(self):
        holder = {}

        @contextmanager
        def lock_connect():
            with self.connect() as conn:
                holder["connection"] = conn
                yield conn

        with session_advisory_lock(
            lock_connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if acquired is not True:
                raise CancelRejected("ACCOUNT_BUSY")

            def fence():
                if self.stopped() is not True or self.route_unchanged() is not True:
                    raise CancelRejected("RUNNERS_OR_ROUTE_CHANGED")
                try:
                    holder["connection"].execute("SET LOCAL statement_timeout='1000ms'")
                    holder["connection"].execute("SELECT 1")
                    holder["connection"].commit()
                except Exception:  # noqa: BLE001 - lock uncertainty never grants authority
                    raise CancelRejected("ACCOUNT_LOCK_LOST") from None
                return True

            fence()
            quarantine = self.quarantine()
            spec, expected, algo_id, local = self.parent()
            request = ExactParentRequest(
                self.raw,
                scope=self.scope,
                algo_id=algo_id,
                client_id=expected["clientAlgoId"],
                fence=fence,
            )
            key = StateKey(
                **asdict(self.scope),
                namespace="testnet-scoped-cleanup-v1",
                key="stuck-parent-cancel:" + expected["clientAlgoId"],
            )
            row = self.store.read(key)
            if row is not None:
                saved = json.loads(row.payload_json)
                plan = saved.get("plan", {})
                if (
                    row.deleted
                    or plan.get("algo_id") != algo_id
                    or plan.get("client_algo_id") != expected["clientAlgoId"]
                ):
                    raise CancelRejected("SAVED_PLAN_INVALID")
                if saved.get("status") != "PLANNED":
                    return self.observe(request, key, spec, expected, algo_id)
                if row.version != 1 or plan.get("case_id") != self.case_id:
                    raise CancelRejected("PLAN_CONFLICT")
            else:
                fingerprint = self.baseline(request)
                for params in (
                    {"clientAlgoId": expected["clientAlgoId"]},
                    {"algoId": algo_id},
                ):
                    checked_parent(
                        request("GET", "/fapi/v1/algoOrder", params),
                        spec=spec,
                        expected=expected,
                        algo_id=algo_id,
                        stuck=True,
                    )
                if self.position(request) != fingerprint:
                    raise CancelRejected("POSITION_CHANGED")
                plan = {
                    "case_id": self.case_id,
                    "symbol": SYMBOL,
                    "algo_id": algo_id,
                    "client_algo_id": expected["clientAlgoId"],
                    "parent": local,
                    "quarantine": quarantine,
                    "position": fingerprint,
                    "spec": asdict(spec),
                }
                self.change(key, None, {"status": "PLANNED", "plan": plan}, "plan")

            def preflight():
                if self.quarantine() != plan["quarantine"] or self.parent() != (
                    spec,
                    expected,
                    algo_id,
                    plan["parent"],
                ):
                    raise CancelRejected("PLAN_CONFLICT")
                if self.baseline(request) != plan["position"]:
                    raise CancelRejected("POSITION_CHANGED")
                # Final exchange read before the durable dispatch claim.
                checked_parent(
                    request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}),
                    spec=spec,
                    expected=expected,
                    algo_id=algo_id,
                    stuck=True,
                )
                return True

            def latch():
                current = self.store.read(key)
                if (
                    current is None
                    or current.deleted
                    or current.version != 1
                    or json.loads(current.payload_json)
                    != {"status": "PLANNED", "plan": plan}
                ):
                    raise CancelRejected("SEND_LATCH_CONFLICT")
                self.change(
                    key,
                    current,
                    {
                        "status": "SENDING",
                        "plan": plan,
                        "send_claimed_at_ms": self.clock_ms(),
                    },
                    "send-once",
                )
                return True

            request.bind(preflight, latch)
            try:
                response = request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                diagnostic = {
                    "transport_attempted": request.attempted,
                    "response_received": True,
                }
                if (
                    isinstance(response, dict)
                    and type(response.get("code")) is int
                    and -100000 <= response["code"] <= 100000
                ):
                    diagnostic["response_code"] = response["code"]
            except (
                Exception
            ) as exc:  # Persist fixed diagnostics before propagating pre-send failures.
                diagnostic = {
                    "transport_attempted": request.attempted,
                    "response_received": False,
                    "error": safe_error(exc),
                }
                current = self.store.read(key)
                if (
                    current is None
                    or json.loads(current.payload_json).get("status") == "PLANNED"
                ):
                    raise
            current = self.store.read(key)
            value = json.loads(current.payload_json)
            self.change(
                key, current, {**value, "dispatch": diagnostic}, "dispatch-result"
            )
            return self.observe(request, key, spec, expected, algo_id)

    def observe(self, request, key, spec, expected, algo_id):
        current = self.store.read(key)
        value = json.loads(current.payload_json)
        try:
            proof = checked_parent(
                request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}),
                spec=spec,
                expected=expected,
                algo_id=algo_id,
                stuck=False,
            )
            status = (
                "CANCEL_CONFIRMED"
                if proof["venue_status"] == "CANCELED"
                and not proof["actual_child_id_present"]
                else "TERMINAL_OTHER"
                if proof["venue_status"] in FINAL_PARENT
                else "QUERY_PENDING"
            )
            observation = {"status": status, "parent": proof}
        except Exception as exc:  # noqa: BLE001 - not-found is not terminal evidence
            observation = {"status": "QUERY_PENDING", "read_error": safe_error(exc)}
        self.change(
            key,
            current,
            {
                **value,
                "status": observation["status"],
                "observation": observation,
                "observed_at_ms": self.clock_ms(),
            },
            f"query:{current.version + 1}",
        )
        return {
            **observation,
            "historical_orders_resolved": False,
            "exposure_flat_verified": False,
            "new_entries_authorized": False,
            "cancel_retry_authorized": False,
        }


def execute(
    *, case_id, expected_epoch, approved, connect=None, stopped=runners_stopped
):
    if (
        approved is not True
        or type(expected_epoch) is not int
        or expected_epoch != EPOCH
    ):
        raise CancelRejected("EXPLICIT_STUCK_PARENT_APPROVAL_REQUIRED")
    case_id = str(UUID(case_id))
    connect = connect or database()
    with maintenance_binding(connect, expected_epoch, stopped) as (
        tenant,
        target,
        unchanged,
    ):
        scope = scope_for(tenant, target)
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ):
            raise CancelRejected("SANDBOX_SCOPE_REQUIRED")
        private = private_for(tenant, target, scope)
        raw = BinanceSignedTransport(
            account_id=scope.account_id,
            environment="SANDBOX",
            api_key=private["api_key"],
            api_secret=private["api_secret"],
            clock_ms=lambda: time.time_ns() // 1000000,
            permit=DeleteOnlyPermit(HostBudget(connect, environment="SANDBOX")),
            enable_trading=False,
            enable_testnet_cancellation=True,
        )
        return StuckParentCancel(
            connect,
            raw,
            scope=scope,
            case_id=case_id,
            stopped=stopped,
            route_unchanged=unchanged,
        ).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--expected-route-epoch", required=True, type=int)
    parser.add_argument("--execute-approved-cancel", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        result = execute(
            case_id=args.case_id,
            expected_epoch=args.expected_route_epoch,
            approved=args.execute_approved_cancel,
        )
    except Exception as exc:  # noqa: BLE001 - never log private signed-request errors
        print(
            json.dumps(
                {"status": "CANCEL_STOPPED", "error": safe_error(exc)}, sort_keys=True
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
