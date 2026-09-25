"""Explicitly approved Testnet exposure reduction; never resolves old UNKNOWNs.

Separate PG maintenance evidence, not a fabricated strategy fill. The caller
must stop the trading runner; a session account lock also fences cooperating
workers. No automatic retry of any external write after its durable CAS.
"""

import json
import re
import time
from dataclasses import asdict
from uuid import UUID, uuid5

from services.v2_testnet_cleanup import Cleanup
from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey


class ScopedCleanup(Cleanup):
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        case_id,
        symbol,
        approved_algos,
        runner_stopped,
        pause,
    ):
        if (
            scope.environment != "SANDBOX"
            or scope.exchange != "BINANCE"
            or scope.product != "FUTURES"
        ):
            raise ValueError("TESTNET_FUTURES_ONLY")
        if (request.account_id, request.environment) != (
            scope.account_id,
            scope.environment,
        ):
            raise ValueError("ACCOUNT_BINDING_MISMATCH")
        if not re.fullmatch(r"[A-Z0-9_]{1,40}", symbol):
            raise ValueError("INVALID_SYMBOL")
        if any(type(x) is not int or x <= 0 for x in approved_algos):
            raise ValueError("INVALID_APPROVED_ALGOS")
        self.connect, self.request, self.scope = connect, request, scope
        self.case_id, self.symbol = str(UUID(str(case_id))), symbol
        self.approved_algos = tuple(sorted(set(approved_algos)))
        self.runner_stopped, self.pause = runner_stopped, pause
        self.store = BusinessState(connect)

    def key(self, action):
        return StateKey(
            **asdict(self.scope),
            namespace="testnet-scoped-cleanup-v1",
            key=self.case_id + ":" + action["key"],
        )

    def flat(self, *, check_orders=True):
        rows = self.request("GET", "/fapi/v3/positionRisk", {"symbol": self.symbol})
        if not isinstance(rows, list) or any(
            r["symbol"] != self.symbol or amount(r["positionAmt"]) for r in rows
        ):
            raise ValueError("TARGET_NOT_FLAT")
        if (
            check_orders
            and self.request("GET", "/fapi/v1/openOrders", {"symbol": self.symbol})
            != []
        ):
            raise ValueError("ORDINARY_ORDERS_PRESENT")

    def preflight(self, action):
        if not self.runner_stopped():
            raise ValueError("TRADING_RUNNER_NOT_STOPPED")
        if action["kind"] != "CLOSE":
            return super().preflight(action)
        p = action["params"]
        if p["symbol"] != self.symbol or p["reduceOnly"] != "true":
            raise ValueError("OUTSIDE_APPROVED_SCOPE")
        if (
            self.request("GET", "/fapi/v1/positionSide/dual", {}).get(
                "dualSidePosition"
            )
            is not False
        ):
            raise ValueError("ONE_WAY_REQUIRED")
        if self.request("GET", "/fapi/v1/openOrders", {"symbol": self.symbol}) != []:
            raise ValueError("ORDINARY_ORDERS_PRESENT")
        rows = self.request("GET", "/fapi/v3/positionRisk", {"symbol": self.symbol})
        active = [r for r in rows if amount(r["positionAmt"])]
        if not active:
            return False
        if len(active) != 1 or any(
            active[0].get(k) != v for k, v in action["fingerprint"].items()
        ):
            raise ValueError("POSITION_CHANGED")
        qty = amount(active[0]["positionAmt"])
        if p["side"] != ("SELL" if qty > 0 else "BUY") or amount(p["quantity"]) != abs(
            qty
        ):
            raise ValueError("REDUCTION_ONLY")
        return True

    def plan(self):
        key = self.key({"key": "plan"})
        saved = self.store.read(key)
        if saved:
            plan = json.loads(saved.payload_json)
            if plan["symbol"] != self.symbol or plan["approved_algos"] != list(
                self.approved_algos
            ):
                raise ValueError("APPROVAL_CONFLICT")
            return plan
        if (
            self.request("GET", "/fapi/v1/positionSide/dual", {}).get(
                "dualSidePosition"
            )
            is not False
        ):
            raise ValueError("ONE_WAY_REQUIRED")
        if self.request("GET", "/fapi/v1/openOrders", {"symbol": self.symbol}) != []:
            raise ValueError("ORDINARY_ORDERS_PRESENT")
        rows = self.request("GET", "/fapi/v3/positionRisk", {"symbol": self.symbol})
        active = [r for r in rows if amount(r["positionAmt"])]
        if (
            len(active) != 1
            or active[0]["symbol"] != self.symbol
            or active[0]["positionSide"] != "BOTH"
        ):
            raise ValueError("ONE_APPROVED_POSITION_REQUIRED")
        row = active[0]
        qty = amount(row["positionAmt"])
        side = "SELL" if qty > 0 else "BUY"
        algos = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": self.symbol})
        if not isinstance(algos, list) or sorted(r["algoId"] for r in algos) != list(
            self.approved_algos
        ):
            raise ValueError("UNAPPROVED_ALGO_SET")
        if any(
            r["symbol"] != self.symbol
            or r["side"] != side
            or r["positionSide"] != "BOTH"
            or r.get("closePosition") is not True
            or r.get("algoStatus") != "NEW"
            for r in algos
        ):
            raise ValueError("UNAPPROVED_PROTECTION")
        quantity = format(abs(qty), "f")
        if "." in quantity:
            quantity = quantity.rstrip("0").rstrip(".")
        actions = [
            {
                "key": "close",
                "kind": "CLOSE",
                "fingerprint": {
                    k: row[k]
                    for k in (
                        "symbol",
                        "positionSide",
                        "positionAmt",
                        "entryPrice",
                        "updateTime",
                    )
                },
                "params": {
                    "symbol": self.symbol,
                    "side": side,
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "quantity": quantity,
                    "reduceOnly": "true",
                    "newClientOrderId": "v2"
                    + uuid5(UUID(self.case_id), self.symbol).hex,
                    "newOrderRespType": "RESULT",
                },
            }
        ]
        actions.extend(
            {
                "key": "cancel:" + str(r["algoId"]),
                "kind": "CANCEL",
                "symbol": self.symbol,
                "params": {"algoId": r["algoId"]},
            }
            for r in algos
        )
        plan = {
            "symbol": self.symbol,
            "approved_algos": list(self.approved_algos),
            "actions": actions,
            "approval_reason": "USER_APPROVED_TESTNET_EXPOSURE_RECONCILIATION",
        }
        result = self.store.change(
            key,
            expected_version=0,
            request_key="plan",
            payload=plan,
            reason="APPROVED_SCOPED_MAINTENANCE",
        )
        if result.code != "APPLIED":
            raise ValueError("PLAN_CONFLICT")
        return plan

    def run(self):
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                raise ValueError("ACCOUNT_BUSY")
            guard.commit()
            if not self.runner_stopped():
                raise ValueError("TRADING_RUNNER_NOT_STOPPED")
            result = {}
            for action in self.plan()["actions"]:
                result[action["key"]] = self.run_action(action)
            self.verify_target()
            return {
                "status": "TARGET_FLAT",
                "actions": result,
                "historical_unknown_resolved": False,
            }

    def verify_target(self):
        """Read-only venue recheck; retain each observation in PG audit history."""
        if self.store.read(self.key({"key": "plan"})) is None:
            raise ValueError("MAINTENANCE_PLAN_REQUIRED")
        self.flat()
        algos = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": self.symbol})
        if algos != []:
            raise ValueError("CONDITIONAL_ORDERS_REMAIN")
        self.flat()
        key = self.key({"key": "flat-verification"})
        prior = self.store.read(key)
        version = prior.version if prior else 0
        result = self.store.change(
            key,
            expected_version=version,
            request_key=f"verify:{version + 1}",
            payload={
                "symbol": self.symbol,
                "case_id": self.case_id,
                "observed_at_ms": time.time_ns() // 1000000,
                "venue_flat": True,
                "ordinary_orders_empty": True,
                "conditional_orders_empty": True,
                "historical_unknown_resolved": False,
                "accounting_status": "MAINTENANCE_FILL_RECORDED_RECONCILIATION_PENDING",
            },
            reason="SCOPED_MAINTENANCE_FLAT_VERIFIED",
        )
        if result.code != "APPLIED":
            raise ValueError("VERIFICATION_CONFLICT")
