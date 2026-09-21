"""Evidence-derived settlement for the one exclusive testnet protocol episode.

GET-only exchange access. No caller-supplied completeness booleans. Deliberately
rejects funding, external activity and non-USDT cases pending a general reconciler.
"""

import argparse
import json
import time
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal, localcontext

from services.v2_testnet_inventory import credentials, deployment_database
from services.v2_testnet_roundtrip import CAMPAIGN, EPISODE, SCOPE, trigger_identity
from v2_core.binance_income import BinanceIncomeImporter
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.public_market import PublicRateBudget
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport


def proof_from_facts(
    trace, report, before, after, rows, run, timestamp, *, propose=False
):
    scope = asdict(SCOPE)
    if trace["intent_id"] != EPISODE or any(
        trace["scope"].get(k) != v for k, v in scope.items()
    ):
        raise ValueError("WRONG_EPISODE_SCOPE")
    if (
        trace["scope"]["producer"] != "protocol-test"
        or trace["scope"]["request_key"] != CAMPAIGN
    ):
        raise ValueError("NOT_PROTOCOL_EPISODE")
    for inventory in (before, after):
        if (
            inventory["account_scope"] != scope
            or inventory["blockers"]
            or inventory["failures"]
        ):
            raise ValueError("INVENTORY_NOT_CLEAR")
        raw = inventory["responses"]
        if (
            raw["mode"].get("dualSidePosition") is not False
            or raw["margin_mode"].get("multiAssetsMargin") is not False
        ):
            raise ValueError("UNSUPPORTED_ACCOUNT_MODE")
        if (
            any(amount(r["positionAmt"]) != 0 for r in raw["positions_after"])
            or raw["ordinary_orders"]
            or raw["conditional_orders"]
        ):
            raise ValueError("UNRECONCILED_EXPOSURE")
    if (
        not trace["fills"]
        or len(trace["orders"]) != 2
        or {o["leg"] for o in trace["orders"]} != {"OPEN", "CLOSE"}
        or any(o["status"] != "FILLED" for o in trace["orders"])
    ):
        raise ValueError("ORDERS_NOT_FINAL")
    first = min(f["occurred_at_ms"] for f in trace["fills"])
    last = max(f["occurred_at_ms"] for f in trace["fills"])
    if (
        not before["finished_at_ms"]
        <= first
        <= last
        <= after["started_at_ms"]
        <= after["finished_at_ms"]
        <= timestamp - 60000
    ):
        raise ValueError("INVALID_SETTLEMENT_WINDOW")
    if run["status"] != "FETCHED" or run["rows"] != len(rows):
        raise ValueError("INCOME_IMPORT_INCOMPLETE")
    if (
        report["intent_id"] != EPISODE
        or report["settlement_currency"] != "USDT"
        or report["net_pnl"] is None
        or report["missing_valuations"]
    ):
        raise ValueError("UNSUPPORTED_ACCOUNTING")
    with localcontext() as ctx:
        ctx.prec = 100
        expected, actual = {}, {}
        fill_ids = set()
        totals = {o["order_id"]: Decimal(0) for o in trace["orders"]}
        for fill in trace["fills"]:
            tid = fill["evidence"]["trade_id"]
            if (
                tid in fill_ids
                or fill["fee_currency"] != "USDT"
                or fill["evidence"]["source"] != "binance-userTrades"
            ):
                raise ValueError("INVALID_FILL_PROOF")
            fill_ids.add(tid)
            totals[fill["order_id"]] += amount(fill["quantity"], positive=True)
            expected[("COMMISSION", tid)] = -amount(fill["fee"])
            expected[("REALIZED_PNL", tid)] = amount(
                fill["evidence"]["venue_realized_pnl"]
            )
        if any(totals[o["order_id"]] != amount(o["quantity"]) for o in trace["orders"]):
            raise ValueError("FILL_QUANTITY_MISMATCH")
        seen = set()
        for row in rows:
            key = (row["income_type"], row["evidence"]["trade_id"])
            identity = (row["income_type"], row["source_id"])
            if (
                identity in seen
                or key not in expected
                or row["symbol"] != trace["request"]["symbol"]
                or row["currency"] != "USDT"
                or row["evidence"]["source"] != "binance-income"
                or not before["started_at_ms"] - 1000
                <= row["occurred_at_ms"]
                <= after["finished_at_ms"]
            ):
                raise ValueError("UNATTRIBUTED_INCOME")
            seen.add(identity)
            actual[key] = actual.get(key, Decimal(0)) + amount(row["amount"])
        if {k: v for k, v in actual.items() if v} != {
            k: v for k, v in expected.items() if v
        }:
            raise ValueError("INCOME_FILL_MISMATCH")
        # Aggregated quantity*price may have 36 decimals; do not constrain or
        # round an exact report to the 18-decimal domain of individual facts.
        if not isinstance(report["net_pnl"], str) or len(report["net_pnl"]) > 200:
            raise ValueError("INVALID_REPORT_AMOUNT")
        net = Decimal(report["net_pnl"])
        if not net.is_finite():
            raise ValueError("INVALID_REPORT_AMOUNT")
        delta = amount(after["responses"]["account"]["totalWalletBalance"]) - amount(
            before["responses"]["account"]["totalWalletBalance"]
        )
        venue_net = sum(actual.values(), Decimal(0))
        if delta != venue_net:
            raise ValueError("WALLET_INCOME_LEDGER_MISMATCH")
        cash = trace["cash"]
        if len(cash) > 1:
            raise ValueError("UNSUPPORTED_ACCOUNTING")
        correction = sum((amount(c["amount"]) for c in cash), Decimal(0))
        base_net = net - correction
        difference = venue_net - base_net
        proposal = None
        if difference:
            # Deliberately narrow acceptance policy: one closing fill, at most
            # one USDT reporting quantum. Never silently round the ledger.
            close_ids = {o["order_id"] for o in trace["orders"] if o["leg"] == "CLOSE"}
            if (
                abs(difference) > Decimal("0.00000001")
                or sum(f["order_id"] in close_ids for f in trace["fills"]) != 1
            ):
                raise ValueError("VENUE_PRECISION_DIFFERENCE_TOO_LARGE")
            proposal = {
                "source": "exclusive-testnet-venue-precision-v1",
                "episode": EPISODE,
                "amount": str(difference.normalize()),
                "base_net": str(base_net.normalize()),
                "venue_net": str(venue_net.normalize()),
                "trade_ids": sorted(fill_ids),
                "income_digest": digest(canonical({"rows": rows})),
                "baseline_inventory": before["observation_id"],
                "final_inventory": after["observation_id"],
            }
        if cash:
            if (
                proposal is None
                or cash[0]["kind"] != "CORRECTION"
                or cash[0]["currency"] != "USDT"
                or correction != difference
                or cash[0]["evidence"] != proposal
                or cash[0]["occurred_at_ms"] != after["finished_at_ms"]
            ):
                raise ValueError("UNSUPPORTED_ACCOUNTING")
        elif difference and not propose:
            raise ValueError("WALLET_INCOME_LEDGER_MISMATCH")
        if amount(report["opened_quantity"], positive=True) != amount(
            report["closed_quantity"]
        ):
            raise ValueError("EXPOSURE_NOT_CLOSED")
    return {
        "source": "exclusive-testnet-protocol-reconciliation-v1",
        "observed_at_ms": timestamp,
        "ledger_revision": report["accounting_revision"],
        "exchange_flat": True,
        "orders_terminal": True,
        "fills_complete": True,
        "cash_complete": delta == net,
        "proposed_correction": proposal if not cash else None,
        "baseline_inventory": before["observation_id"],
        "final_inventory": after["observation_id"],
        "inventory_digests": [digest(canonical(x)) for x in (before, after)],
        "income_run": run["run_id"],
        "income_digest": digest(canonical({"rows": rows})),
        "trade_ids": sorted(fill_ids),
        "wallet_delta": str(delta),
        "net_pnl": str(net),
        "scope_limit": "one exclusive flat-to-flat USDT protocol episode; no funding or external activity",
    }


def settle_protocol(connect, request, clock_ms):
    if request.account_id != SCOPE.account_id or request.environment != "SANDBOX":
        raise ValueError("WRONG_TRANSPORT_SCOPE")
    data, store = TradingData(connect), BusinessState(connect)
    trace = data.trace(EPISODE)
    if trace is None:
        raise ValueError("MISSING_PROTOCOL_EPISODE")
    report = data.ledger.report(EPISODE, settlement_currency="USDT")
    if report["accounting_status"] == "SETTLED":
        return {
            "status": "ALREADY_SETTLED",
            "episode": EPISODE,
            "net_pnl": report["net_pnl"],
        }

    def read_inventory(identity):
        value = store.read(
            StateKey(**asdict(SCOPE), namespace="account-inventory-v1", key=identity)
        )
        if value is None or value.deleted:
            raise ValueError("MISSING_INVENTORY")
        return json.loads(value.payload_json)

    plan = store.read(
        StateKey(**asdict(SCOPE), namespace="testnet-roundtrip-v1", key=CAMPAIGN)
    )
    if plan is None or plan.deleted:
        raise ValueError("MISSING_PLAN")
    before = read_inventory(json.loads(plan.payload_json)["baseline_id"])
    with connect() as conn:
        verified = conn.execute(
            "SELECT payload FROM v2_business_state WHERE scope->>'namespace'='testnet-roundtrip-verification-v1' AND scope->>'account_id'=%s AND scope->>'environment'='SANDBOX' AND NOT deleted AND payload->>'episode'=%s AND payload->>'status' IN ('PROTECTED_ROUNDTRIP_PASSED','ROUNDTRIP_CLOSED') LIMIT 101",
            (SCOPE.account_id, EPISODE),
        ).fetchall()
    if not verified or len(verified) > 100:
        raise ValueError("MISSING_OR_EXCESS_VERIFICATION")
    after = max(
        (read_inventory(v[0]["final_inventory"]) for v in verified),
        key=lambda x: x["finished_at_ms"],
    )
    if clock_ms() < after["finished_at_ms"] + 60000:
        raise ValueError("INCOME_LAG_WAIT")
    start, end = before["started_at_ms"] - 1000, after["finished_at_ms"]
    run = BinanceIncomeImporter(
        connect,
        request,
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        clock_ms=clock_ms,
    ).import_window(start_ms=start, end_ms=end)
    with connect() as conn:
        raw = conn.execute(
            "SELECT income_type,source_id,symbol,amount::text,currency,occurred_at_ms,evidence FROM v2_exchange_income WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s) AND occurred_at_ms BETWEEN %s AND %s ORDER BY income_type,source_id",
            (*asdict(SCOPE).values(), start, end),
        ).fetchall()
    rows = [
        dict(
            zip(
                (
                    "income_type",
                    "source_id",
                    "symbol",
                    "amount",
                    "currency",
                    "occurred_at_ms",
                    "evidence",
                ),
                r,
                strict=True,
            )
        )
        for r in raw
    ]
    # Re-read under the intent lock. Correction and final proof commit together;
    # a failed settlement never leaves a standalone balancing adjustment.
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute(
            "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
            (EPISODE,),
        )
        bound = TradingData(lambda: nullcontext(conn))
        trace = bound.trace(EPISODE, connection=conn)
        report = bound.ledger.report(
            EPISODE, settlement_currency="USDT", connection=conn
        )
        proof = proof_from_facts(
            trace, report, before, after, rows, run, clock_ms(), propose=True
        )
        correction = proof["proposed_correction"]
        if correction:
            bound.ledger.adjustment(
                episode_id=EPISODE,
                source_id="protocol-venue-precision:" + EPISODE,
                amount_text=correction["amount"],
                currency="USDT",
                kind="CORRECTION",
                occurred_at_ms=after["finished_at_ms"],
                evidence=correction,
            )
            trace = bound.trace(EPISODE, connection=conn)
            report = bound.ledger.report(
                EPISODE, settlement_currency="USDT", connection=conn
            )
        proof = proof_from_facts(trace, report, before, after, rows, run, clock_ms())
        bound.ledger.settle(EPISODE, currency="USDT", evidence=proof)
    return {
        "status": "SETTLED",
        "episode": EPISODE,
        "net_pnl": report["net_pnl"],
        "income_run": run["run_id"],
    }


def main():
    global CAMPAIGN, EPISODE  # explicit bounded acceptance case, not arbitrary account selection
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--trigger-attempt", type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.trigger_attempt is not None:
        CAMPAIGN, EPISODE = trigger_identity(args.trigger_attempt)
    connect = deployment_database()
    cfg = credentials(args.config, notify=False)
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        return (
            method == "GET"
            and path == "/fapi/v1/income"
            and all(budget.permit(10) for _ in range(3))
        )

    clock = lambda: time.time_ns() // 1000000
    request = BinanceSignedTransport(
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock,
        permit=permit,
    )
    # Serialize settlement invocations without holding a DB transaction over I/O.
    with connect() as lock:
        if not lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            (CAMPAIGN + ":settle",),
        ).fetchone()[0]:
            raise ValueError("SETTLEMENT_ALREADY_RUNNING")
        print(json.dumps(settle_protocol(connect, request, clock)), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - sanitized operational output
        print(
            json.dumps(
                {"status": "SETTLEMENT_PENDING", "error_class": type(exc).__name__}
            ),
            flush=True,
        )
        raise SystemExit(1) from None
