import copy
import json
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_testnet_roundtrip import Venue, install

from services import v2_testnet_roundtrip as rt
from services.v2_testnet_settlement import proof_from_facts, settle_protocol
from v2_core.ledger import Ledger
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey

database = database_fixture


@pytest.fixture
def completed(database, monkeypatch, request):
    precision = getattr(request, "param", "0")

    class Balances(Venue):
        def __call__(self, method, path, params):
            if path.endswith("/account"):
                balance = str(Decimal(1000) - Decimal(".01") * len(self.orders))
                if len(self.orders) == 2:
                    balance = str(Decimal(balance) + Decimal(precision))
                return {
                    k: balance
                    for k in (
                        "totalWalletBalance",
                        "availableBalance",
                        "totalMarginBalance",
                    )
                }
            result = super().__call__(method, path, params)
            if path.endswith("userTrades") and len(self.orders) == 2:
                for fill in result:
                    if fill["side"] == "SELL":
                        fill["realizedPnl"] = precision
            return result

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
    for fill in trace["fills"]:
        if Decimal(fill["evidence"]["venue_realized_pnl"]):
            rows.append(
                {
                    "income_type": "REALIZED_PNL",
                    "source_id": "100",
                    "symbol": "BTCUSDT",
                    "amount": precision,
                    "currency": "USDT",
                    "occurred_at_ms": fill["occurred_at_ms"],
                    "evidence": {
                        "source": "binance-income",
                        "trade_id": fill["evidence"]["trade_id"],
                    },
                }
            )
    run["rows"] = len(rows)
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


@pytest.mark.parametrize("completed", ["0", "0.00000001", "-0.00000001"], indirect=True)
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

    def fail_settlement(*args, **kwargs):
        raise RuntimeError("injected settlement failure")

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "settle", fail_settlement)
        with pytest.raises(RuntimeError, match="injected"):
            settle_protocol(database, request, lambda: facts[6])
    failed_trace = TradingData(database).trace(rt.EPISODE)
    assert not failed_trace["cash"] and not failed_trace["settlements"]
    assert failed_trace["account_risk"]["status"] == "HELD"
    assert settle_protocol(database, request, lambda: facts[6])["status"] == "SETTLED"
    trace = TradingData(database).trace(rt.EPISODE)
    assert trace["episode"]["status"] == "SETTLED"
    assert trace["account_risk"]["status"] == "RELEASED"
    assert len(trace["cash"]) == (1 if len(rows) == 3 else 0)
    assert (
        settle_protocol(database, request, lambda: facts[6])["status"]
        == "ALREADY_SETTLED"
    )
    assert request.calls == 2
    rt.execute(database, "unused")
    assert len(venue.writes) == 4


@pytest.mark.parametrize("completed", ["0.00000001"], indirect=True)
@pytest.mark.parametrize("failure", ["wallet", "income", "large", "cash", "none"])
def test_precision_difference_requires_all_evidence(completed, failure):
    facts, _ = completed
    trace, report, _before, after, rows, _run, _timestamp = facts
    if failure == "wallet":
        after["responses"]["account"]["totalWalletBalance"] = "999.98"
    elif failure == "income":
        rows[-1]["amount"] = "0.00000002"
    elif failure == "large":
        report["net_pnl"] = "-0.03"
    elif failure == "cash":
        trace["cash"] = [
            {"amount": "0.00000001", "kind": "FUNDING", "currency": "USDT"}
        ]
    if failure != "none":
        with pytest.raises(ValueError):
            proof_from_facts(*facts, propose=True)
    else:
        with pytest.raises(ValueError):
            proof_from_facts(*facts)
        proof = proof_from_facts(*facts, propose=True)
        assert not proof["cash_complete"]
        assert Decimal(proof["proposed_correction"]["amount"]) == Decimal("0.00000001")
