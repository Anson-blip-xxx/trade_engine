import copy
import json
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_testnet_roundtrip import Venue, install

from services import v2_testnet_roundtrip as rt
from services.v2_testnet_settlement import proof_from_facts, settle_protocol
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey

database = database_fixture


@pytest.fixture
def completed(database, monkeypatch):
    class Balances(Venue):
        def __call__(self, method, path, params):
            if path.endswith("/account"):
                balance = str(Decimal(1000) - Decimal(".01") * len(self.orders))
                return {
                    k: balance
                    for k in (
                        "totalWalletBalance",
                        "availableBalance",
                        "totalMarginBalance",
                    )
                }
            return super().__call__(method, path, params)

    venue = Balances()
    install(monkeypatch, venue)
    rt.execute(database, "unused")
    data = TradingData(database)
    store = BusinessState(database)
    plan = json.loads(
        store.read(
            StateKey(
                **asdict(rt.SCOPE), namespace="testnet-roundtrip-v1", key=rt.CAMPAIGN
            )
        ).payload_json
    )

    def inventory(identity):
        return json.loads(
            store.read(
                StateKey(
                    **asdict(rt.SCOPE), namespace="account-inventory-v1", key=identity
                )
            ).payload_json
        )

    before = inventory(plan["baseline_id"])
    with database() as c:
        verification = c.execute(
            "SELECT payload FROM v2_business_state WHERE scope->>'namespace'='testnet-roundtrip-verification-v1'"
        ).fetchone()[0]
    after = inventory(verification["final_inventory"])
    trace = data.trace(rt.EPISODE)
    rows = [
        {
            "income_type": "COMMISSION",
            "source_id": str(i + 1),
            "symbol": "BTCUSDT",
            "amount": "-0.01",
            "currency": "USDT",
            "occurred_at_ms": f["occurred_at_ms"],
            "evidence": {
                "source": "binance-income",
                "trade_id": f["evidence"]["trade_id"],
            },
        }
        for i, f in enumerate(trace["fills"])
    ]
    run = {"status": "FETCHED", "rows": len(rows), "run_id": "qa-run"}
    return [
        trace,
        data.ledger.report(rt.EPISODE, settlement_currency="USDT"),
        before,
        after,
        rows,
        run,
        rt.now() + 120000,
    ], venue


def test_matching_income_wallet_and_fills_generate_proof(completed):
    facts, _ = completed
    proof = proof_from_facts(*facts)
    assert proof["cash_complete"] and proof["wallet_delta"] == "-0.02"


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "unknown_trade",
        "currency",
        "funding",
        "wallet",
        "nonflat",
        "partial",
        "nonterminal",
        "quantity",
        "future",
        "scope",
    ],
)
def test_missing_or_conflicting_facts_never_authorize_settlement(completed, failure):
    facts, _ = completed
    trace, report, before, after, rows, run, timestamp = copy.deepcopy(facts)
    if failure == "missing":
        rows.pop()
        run["rows"] = len(rows)
    elif failure == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
        run["rows"] = len(rows)
    elif failure == "unknown_trade":
        rows[0]["evidence"]["trade_id"] = "999"
    elif failure == "currency":
        rows[0]["currency"] = "BNB"
    elif failure == "funding":
        rows[0]["income_type"] = "FUNDING_FEE"
    elif failure == "wallet":
        after["responses"]["account"]["totalWalletBalance"] = "999"
    elif failure == "nonflat":
        after["responses"]["positions_after"][0]["positionAmt"] = "1"
    elif failure == "partial":
        run["status"] = "PARTIAL"
    elif failure == "nonterminal":
        trace["orders"][0]["status"] = "UNKNOWN"
    elif failure == "quantity":
        trace["fills"][0]["quantity"] = "1"
    elif failure == "future":
        timestamp = after["finished_at_ms"]
    elif failure == "scope":
        before["account_scope"]["account_id"] = "other"
    with pytest.raises(ValueError):
        proof_from_facts(trace, report, before, after, rows, run, timestamp)


def test_pg_settlement_releases_budget_and_replay_has_no_new_orders(
    database, monkeypatch, completed
):
    facts, venue = completed
    rows = facts[4]

    class Income:
        account_id = rt.SCOPE.account_id
        environment = "SANDBOX"
        calls = 0

        def __call__(self, method, path, params):
            assert method == "GET" and path == "/fapi/v1/income"
            self.calls += 1
            return [
                {
                    "tranId": int(r["source_id"]),
                    "incomeType": r["income_type"],
                    "symbol": r["symbol"],
                    "income": r["amount"],
                    "asset": r["currency"],
                    "time": r["occurred_at_ms"],
                    "tradeId": r["evidence"]["trade_id"],
                }
                for r in rows
            ]

    request = Income()
    assert settle_protocol(database, request, lambda: facts[6])["status"] == "SETTLED"
    trace = TradingData(database).trace(rt.EPISODE)
    assert trace["episode"]["status"] == "SETTLED"
    assert trace["account_risk"]["status"] == "RELEASED"
    assert (
        settle_protocol(database, request, lambda: facts[6])["status"]
        == "ALREADY_SETTLED"
    )
    assert request.calls == 1
    rt.execute(database, "unused")
    assert len(venue.writes) == 4
