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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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


def plan_from_market(instrument, mark, timestamp):
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
    stop = (price * Decimal("0.98") / tick).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick
    take = (price * Decimal("1.02") / tick).to_integral_value(
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
    if closes:
        # PREPARED has never been sent. Anything else is query-only.
        runtime.execution.dispatch(closes[0]["order_id"])
        await_fill(runtime, closes[0]["order_id"])
        return
    rows = request("GET", "/fapi/v3/positionRisk", {})
    active = [r for r in rows if amount(r["positionAmt"]) != 0]
    quantity = trace["orders"][0]["quantity"]
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--execute-testnet-roundtrip", action="store_true", required=True
    )
    args = parser.parse_args()
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
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (CAMPAIGN,)
        ).fetchone()[0]
        if not locked:
            raise ValueError("ROUNDTRIP_ALREADY_RUNNING")
        execute(connect, args.config)


def execute(connect, config_path):
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
            instrument, public("/fapi/v1/premiumIndex?symbol=BTCUSDT"), now()
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
                    if confirmed is None:
                        raise ValueError("PROTECTION_NEVER_CONFIRMED")
        elif accepted.get("order_id"):
            runtime.execution.dispatch(accepted["order_id"])
            await_fill(runtime, accepted["order_id"])
        else:
            raise ValueError("OPEN_NOT_PREPARED")
        for spec in [] if already_closed else specs:
            result = protection.submit_once(spec)
            print(json.dumps({"phase": spec.kind, **result}), flush=True)
            if result["status"] != "NEW":
                raise ValueError("PROTECTION_NOT_CONFIRMED")
    finally:
        close_owned(runtime, request)
        # Never cancel an ambiguous/absent protection blindly, nor while open.
        for spec in specs:
            current, payload = protection.read(spec)
            if current and payload.get("observation"):
                for _ in range(5):
                    result = protection.cancel_flat_once(spec)
                    if result["status"] in {"CANCELED", "EXPIRED"}:
                        break
                    time.sleep(0.5)
                print(
                    json.dumps({"phase": "CANCEL_" + spec.kind, **result}), flush=True
                )
                if result["status"] not in {"CANCELED", "EXPIRED"}:
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
