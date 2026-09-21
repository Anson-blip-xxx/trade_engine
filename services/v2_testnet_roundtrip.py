"""Bounded one-position protocol acceptance, NOT a strategy performance run.

Only Binance Testnet, one immutable campaign, <=100 USDT reference notional.
Uses V2 intents/orders/fills and PG account reservation, never legacy executors.
Recovery only queries sent orders. A confirmed open is closed in finally even
when protection fails; no protection cancellation while the account has exposure.
"""

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from http.client import HTTPSConnection
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.v2_testnet_cleanup import OLD_UNITS, SCOPE
from services.v2_testnet_inventory import credentials, deployment_database
from v2_core.account_inventory import AccountInventory, persist_inventory
from v2_core.account_risk import AccountPolicy, AccountRisk, RiskReference
from v2_core.binance import BinanceFutures
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import OpenIntent
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.protection_child import ProtectionChildReconciler
from v2_core.public_market import PublicRateBudget
from v2_core.runner import RiskVerdict
from v2_core.runtime import DataRuntime
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport

CAMPAIGN = "testnet-protected-roundtrip-20260921-v1"
EPISODE = str(uuid5(NAMESPACE_URL, CAMPAIGN))
SYMBOL = "BTCUSDT"


def now():
    return time.time_ns() // 1000000


def trigger_identity(attempt):
    if type(attempt) is not int or not 1 <= attempt <= 3:
        raise ValueError("BOUNDED_TRIGGER_ATTEMPT_REQUIRED")
    campaign = f"testnet-native-trigger-20260921-{attempt}"
    return campaign, str(uuid5(NAMESPACE_URL, campaign))


def plan_from_market(instrument, mark, timestamp, *, trigger=False):
    if (
        instrument["symbol"] != SYMBOL
        or instrument["status"] != "TRADING"
        or mark["symbol"] != SYMBOL
    ):
        raise ValueError("MARKET_SCOPE")
    if type(mark["time"]) is not int or not 0 <= timestamp - mark["time"] < 10000:
        raise ValueError("STALE_MARK")
    filters = {f["filterType"]: f for f in instrument["filters"]}
    price = amount(mark["markPrice"], positive=True)
    lot, pf = filters["MARKET_LOT_SIZE"], filters["PRICE_FILTER"]
    step, tick = (
        amount(lot["stepSize"], positive=True),
        amount(pf["tickSize"], positive=True),
    )
    minimum = max(
        Decimal(55),
        amount(filters["MIN_NOTIONAL"]["notional"], positive=True) * Decimal("1.05"),
    )
    qty = (minimum / price / step).to_integral_value(rounding=ROUND_CEILING) * step
    stop = (price * Decimal("0.9999" if trigger else "0.98") / tick).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick
    take = (price * Decimal("1.0001" if trigger else "1.02") / tick).to_integral_value(
        rounding=ROUND_CEILING
    ) * tick
    if not (
        amount(lot["minQty"]) <= qty <= amount(lot["maxQty"])
        and qty * price <= 100
        and amount(pf["minPrice"]) <= stop < price < take <= amount(pf["maxPrice"])
    ):
        raise ValueError("OUTSIDE_TEST_BUDGET_OR_FILTERS")
    return {
        "quantity": str(qty),
        "stop": str(stop),
        "take": str(take),
        "observed_at": mark["time"],
        "decided_at": timestamp,
        "expires_at_ms": timestamp + 60000,
        "instrument": instrument,
        "mark": mark,
    }


def await_fill(runtime, order_id):
    for _ in range(10):
        status = runtime.execution.recover(order_id)
        if status == "FILLED":
            return
        if status in {"REJECTED", "CANCELLED", "PREPARED"}:
            raise ValueError("ORDER_NOT_FILLED")
        time.sleep(0.5)
    raise ValueError("ORDER_RECONCILIATION_PENDING")


def close_owned(runtime, request):
    trace = runtime.data.trace(EPISODE)
    if trace is None:
        return
    opens = [o for o in trace["orders"] if o["leg"] == "OPEN"]
    if not opens or opens[0]["status"] in {"PREPARED", "REJECTED", "CANCELLED"}:
        return
    await_fill(runtime, opens[0]["order_id"])
    trace = runtime.data.trace(EPISODE)
    closes = [o for o in trace["orders"] if o["leg"] == "CLOSE"]
    for close in closes:
        if close["status"] not in {"FILLED", "CANCELLED", "REJECTED"}:
            # Only PREPARED can submit; ambiguous outcomes remain query-only.
            runtime.execution.dispatch(close["order_id"])
            await_fill(runtime, close["order_id"])
    trace = runtime.data.trace(EPISODE)
    legs = {o["order_id"]: o["leg"] for o in trace["orders"]}
    with localcontext() as ctx:
        ctx.prec = 80
        remaining = sum(
            (
                amount(f["quantity"]) * (1 if legs[f["order_id"]] == "OPEN" else -1)
                for f in trace["fills"]
            ),
            Decimal(0),
        )
    if remaining == 0:
        return
    rows = request("GET", "/fapi/v3/positionRisk", {})
    active = [r for r in rows if amount(r["positionAmt"]) != 0]
    quantity = str(remaining)
    if (
        len(active) != 1
        or active[0]["symbol"] != SYMBOL
        or active[0]["positionSide"] != "BOTH"
        or amount(active[0]["positionAmt"]) != amount(quantity)
    ):
        raise ValueError("POSITION_CHANGED_RECONCILIATION_REQUIRED")
    order, _ = runtime.data.orders.prepare(
        EPISODE,
        leg="CLOSE",
        quantity=quantity,
        request_key="bounded-protocol-exit",
        evidence={"reason": "PROTOCOL_TEST_FINISHED_OR_FAILED"},
    )
    runtime.execution.dispatch(order)
    await_fill(runtime, order)


def main():
    global CAMPAIGN, EPISODE  # fixed single-process acceptance case, never a daemon setting
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--execute-testnet-roundtrip", action="store_true", required=True
    )
    parser.add_argument("--trigger-attempt", type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.trigger_attempt is not None:
        CAMPAIGN, EPISODE = trigger_identity(args.trigger_attempt)
    for unit in OLD_UNITS:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        if result.stdout.strip() != "inactive":
            raise ValueError("OLD_TRADING_SERVICE_ACTIVE")
    connect = deployment_database()
    # Session lock excludes concurrent copies across all external actions.
    with connect() as lock_conn:
        locked = lock_conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        ).fetchone()[0]
        if not locked:
            raise ValueError("ROUNDTRIP_ALREADY_RUNNING")
        execute(
            connect,
            args.config,
            trigger=args.trigger_attempt is not None,
            trigger_kind="TAKE_PROFIT_MARKET"
            if args.trigger_attempt == 3
            else "STOP_MARKET",
        )


def execute(connect, config_path, *, trigger=False, trigger_kind="STOP_MARKET"):
    if type(trigger) is not bool or trigger_kind not in {
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
    }:
        raise ValueError("INVALID_TRIGGER_TEST_MODE")
    store = BusinessState(connect)
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        weight = {
            "/fapi/v1/openOrders": 40,
            "/fapi/v1/openAlgoOrders": 40,
            "/fapi/v1/positionSide/dual": 30,
            "/fapi/v1/multiAssetsMargin": 30,
            "/fapi/v3/account": 5,
            "/fapi/v3/positionRisk": 5,
            "/fapi/v1/userTrades": 5,
            "/fapi/v1/exchangeInfo": 10,
        }.get(path, 1)
        while weight:
            chunk = min(weight, 10)
            if not budget.permit(chunk):
                return False
            weight -= chunk
        return True

    def public(path):
        if not permit("GET", path.split("?")[0]):
            raise ValueError("QUOTA_DENIED")
        conn = HTTPSConnection("demo-fapi.binance.com", timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read(2000001)
            if response.status != 200 or len(raw) > 2000000:
                raise ValueError("PUBLIC_MARKET_UNAVAILABLE")
            return json.loads(raw)
        finally:
            conn.close()

    cfg = credentials(config_path, notify=False)
    request = BinanceSignedTransport(
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=now,
        permit=permit,
        enable_trading=True,
        enable_testnet_protection=True,
        enable_testnet_cancellation=True,
    )
    inventory = AccountInventory(request, scope=SCOPE, clock_ms=now)
    key = StateKey(**asdict(SCOPE), namespace="testnet-roundtrip-v1", key=CAMPAIGN)
    snapshot = store.read(key)
    if snapshot is None:
        before = inventory.collect(str(uuid4()))
        persist_inventory(connect, scope=SCOPE, observation=before)
        if before["blockers"]:
            raise ValueError("ROUNDTRIP_REQUIRES_CLEAR_ACCOUNT")
        info = public("/fapi/v1/exchangeInfo")
        instrument = next(s for s in info["symbols"] if s["symbol"] == SYMBOL)
        plan = plan_from_market(
            instrument,
            public("/fapi/v1/premiumIndex?symbol=BTCUSDT"),
            now(),
            trigger=trigger,
        )
        plan["baseline_id"] = before["observation_id"]
        if (
            store.change(
                key,
                expected_version=0,
                request_key="plan",
                payload=plan,
                reason="USER_AUTHORIZED_TESTNET_PROTOCOL_ROUNDTRIP",
            ).code
            != "APPLIED"
        ):
            raise ValueError("PLAN_CONFLICT")
    else:
        plan = json.loads(snapshot.payload_json)
    venue = BinanceFutures(request, account_id=SCOPE.account_id, environment="SANDBOX")

    def risk(order):
        if order["leg"] == "CLOSE":
            return RiskVerdict(True, "REDUCE_ONLY_PROTOCOL_EXIT")
        observation = inventory.collect(str(uuid4()))
        persist_inventory(connect, scope=SCOPE, observation=observation)
        available = amount(
            observation["summary"].get("balance", {}).get("availableBalance", "0")
        )
        return RiskVerdict(
            not observation["blockers"] and available >= 100,
            "EXCLUSIVE_TESTNET_PROTOCOL_BUDGET",
            {"inventory_id": observation["observation_id"]},
        )

    def reference(order):
        mark = public("/fapi/v1/premiumIndex?symbol=BTCUSDT")
        if not 0 <= now() - mark["time"] < 10000:
            raise ValueError("STALE_MARK")
        return RiskReference(
            "SANDBOX",
            SYMBOL,
            "testnet-protocol-mark",
            mark["markPrice"],
            mark["time"],
            valid_until_ms=mark["time"] + 10000,
        )

    AccountRisk(connect).configure(
        SCOPE,
        AccountPolicy("USDT", "100", 1, 60000, "testnet-protocol-mark", 10000),
        expected_version=0,
    )
    runtime = DataRuntime(
        connect,
        submit=venue.submit,
        query=venue.query,
        risk_check=risk,
        risk_reference=reference,
        clock_ms=now,
        scope=SCOPE,
    )
    evidence = DecisionEvidence(
        "protocol-acceptance-v1",
        canonical({"mode": "TESTNET_PROTOCOL_ONLY", "max_notional": "100"}),
        canonical(
            {
                **{k: plan[k] for k in ("observed_at", "decided_at", "expires_at_ms")},
                "source": "testnet-protocol",
                "symbol": SYMBOL,
                "rationale": "user-authorized protected roundtrip, not a strategy signal",
                "features": plan,
            }
        ),
    )
    intent = OpenIntent(
        EPISODE,
        **asdict(SCOPE),
        producer="protocol-test",
        request_key=CAMPAIGN,
        symbol=SYMBOL,
        side="BUY",
        quantity=plan["quantity"],
        strategy_version=evidence.strategy_version,
        config_digest=evidence.config_digest,
        evidence_ref=evidence.evidence_ref,
    )
    accepted = runtime.accept_open(intent, evidence)
    protection = TestnetProtection(store, request, scope=SCOPE)
    specs = [
        ProtectionSpec(EPISODE, SYMBOL, "SELL", kind, plan[field])
        for kind, field in (("STOP_MARKET", "stop"), ("TAKE_PROFIT_MARKET", "take"))
    ]
    if trigger:
        specs = [s for s in specs if s.kind == trigger_kind]
    child_worker = ProtectionChildReconciler(connect, request, scope=SCOPE)

    def child_complete():
        return any(
            o["leg"] == "CLOSE"
            and o["status"] == "FILLED"
            and o["request_evidence"].get("origin") == "BINANCE_ALGO_CHILD"
            for o in runtime.data.trace(EPISODE)["orders"]
        )

    def clean_terminal(result):
        return result["status"] in {"CANCELED", "EXPIRED"} or (
            result["status"] == "FINISHED"
            and any(
                o["status"] == "FILLED"
                and o["request_evidence"].get("parent_algo_id") == result["algo_id"]
                for o in runtime.data.trace(EPISODE)["orders"]
            )
        )

    trace = runtime.data.trace(EPISODE)
    already_closed = any(
        o["leg"] == "CLOSE" and o["status"] == "FILLED" for o in trace["orders"]
    )
    try:
        if already_closed:
            # Recovery must prove that both protections really were confirmed.
            with connect() as conn:
                for spec in specs:
                    confirmed = conn.execute(
                        "SELECT 1 FROM v2_state_history WHERE state_id=%s AND payload->>'status'='NEW' LIMIT 1",
                        (protection.key(spec).identity,),
                    ).fetchone()
                    if confirmed is None and not (trigger and child_complete()):
                        raise ValueError("PROTECTION_NEVER_CONFIRMED")
        elif accepted.get("order_id"):
            runtime.execution.dispatch(accepted["order_id"])
            await_fill(runtime, accepted["order_id"])
        else:
            raise ValueError("OPEN_NOT_PREPARED")
        for spec in [] if already_closed else specs:
            result = protection.submit_once(spec)
            print(json.dumps({"phase": spec.kind, **result}), flush=True)
            if result["status"] not in (
                {"NEW", "TRIGGERING", "TRIGGERED", "FINISHED"} if trigger else {"NEW"}
            ):
                raise ValueError("PROTECTION_NOT_CONFIRMED")
        if trigger and not already_closed:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                result = child_worker.reconcile(specs[0])
                if result["status"] == "FILLED":
                    print(json.dumps({"phase": "NATIVE_CHILD", **result}), flush=True)
                    break
                time.sleep(1)
            else:
                raise ValueError("TRIGGER_WAIT_TIMEOUT")
        if trigger and not child_complete():
            raise ValueError("NATIVE_TRIGGER_NOT_CONFIRMED")
    finally:
        if trigger:
            current, _ = protection.read(specs[0])
            if current is not None:
                try:
                    child_worker.reconcile(specs[0])
                except Exception as exc:  # noqa: BLE001 - still attempt owned reduce-only exit below
                    print(
                        json.dumps(
                            {
                                "phase": "CHILD_RECOVERY_PENDING",
                                "error_class": type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
        close_owned(runtime, request)
        # Never cancel an ambiguous/absent protection blindly, nor while open.
        for spec in specs:
            current, payload = protection.read(spec)
            if current and payload.get("observation"):
                for _ in range(5):
                    result = protection.cancel_flat_once(spec)
                    if clean_terminal(result):
                        break
                    time.sleep(0.5)
                print(
                    json.dumps({"phase": "CANCEL_" + spec.kind, **result}), flush=True
                )
                if not clean_terminal(result):
                    raise ValueError("PROTECTION_CLEANUP_UNCONFIRMED")
        after = inventory.collect(str(uuid4()))
        print(
            json.dumps(persist_inventory(connect, scope=SCOPE, observation=after)),
            flush=True,
        )
        report = runtime.data.ledger.report(EPISODE, settlement_currency="USDT")
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "intent_id",
                        "opened_quantity",
                        "closed_quantity",
                        "gross_pnl",
                        "fees",
                        "net_pnl",
                        "accounting_status",
                    )
                }
            ),
            flush=True,
        )
        if after["blockers"]:
            raise ValueError("FINAL_ACCOUNT_NOT_CLEAR")
        if (
            report["net_pnl"] is not None
            and amount(report["opened_quantity"]) > 0
            and report["opened_quantity"] == report["closed_quantity"]
        ):
            store.change(
                StateKey(
                    **asdict(SCOPE),
                    namespace="testnet-roundtrip-verification-v1",
                    key="closed:" + after["observation_id"],
                ),
                expected_version=0,
                request_key="closed",
                reason="PROTOCOL_POSITION_CLOSED_NOT_TRIGGER_ACCEPTANCE",
                payload={
                    "episode": EPISODE,
                    "final_inventory": after["observation_id"],
                    "report": report,
                    "status": "ROUNDTRIP_CLOSED",
                },
            )
    if (
        report["accounting_status"] not in {"CALCULATED", "SETTLED"}
        or report["opened_quantity"] != report["closed_quantity"]
    ):
        raise ValueError("ROUNDTRIP_ACCOUNTING_PENDING")
    verification = StateKey(
        **asdict(SCOPE),
        namespace="testnet-roundtrip-verification-v1",
        key=after["observation_id"],
    )
    store.change(
        verification,
        expected_version=0,
        request_key="verified",
        reason="REAL_TESTNET_PROTOCOL_PASSED",
        payload={
            "episode": EPISODE,
            "final_inventory": after["observation_id"],
            "report": report,
            "status": "PROTECTED_ROUNDTRIP_PASSED",
            "settlement": report["accounting_status"],
        },
    )
    print(
        json.dumps(
            {
                "status": "PROTECTED_ROUNDTRIP_PASSED",
                "settlement": report["accounting_status"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - no secrets or signed requests in diagnostics
        print(
            json.dumps(
                {
                    "status": "ROUNDTRIP_STOPPED",
                    "error_class": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
