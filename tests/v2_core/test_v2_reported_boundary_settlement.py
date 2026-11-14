"""Real isolated ledger/income/wallet checks for witnessed boundary cash."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario
from test_v2_directional_settlement import Venue, baseline, service
from test_v2_observed_entry_precision import anchor, inventory

from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core.account_inventory import persist_inventory
from v2_core.ledger import Ledger
from v2_core.state import BusinessState, StateKey

database = database_fixture
MODEL = "venue-reported-boundary-cash-adjustment-v1"
BALANCE = "999.91399999"


@pytest.fixture
def boundary_closed(database):
    _, runtime, signal, context = scenario(database)
    admitted = formal(runtime).consume(signal, context=context)
    episode, opening = admitted["intent_id"], admitted["order_id"]
    orders, ledger = runtime.data.orders, runtime.data.ledger
    rows = []

    def fill(oid, tid, price, fee, pnl, when):
        orders.transition(oid, expected_version=1, status="SUBMITTING", evidence={})
        ledger.record_fill(
            order_id=oid,
            exchange_fill_id=tid,
            quantity="1.25",
            price=price,
            fee=fee,
            fee_currency="USDT",
            occurred_at_ms=when,
            evidence={
                "source": "binance-userTrades",
                "trade_id": tid,
                "venue_realized_pnl": pnl,
            },
        )
        orders.transition(
            oid,
            expected_version=2,
            status="FILLED",
            exchange_order_id=tid,
            evidence={"fills_complete": True},
        )
        for kind, value in (("COMMISSION", "-" + fee), ("REALIZED_PNL", pnl)):
            if Decimal(value):
                rows.append(
                    {
                        "symbol": "BTCUSDT",
                        "asset": "USDT",
                        "incomeType": kind,
                        "income": value,
                        "tradeId": tid,
                        "tranId": len(rows) + 1,
                        "time": when,
                    }
                )

    fill(opening, "1", "100", ".05", "0", 3)
    observed = inventory(
        SCOPE,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1.25",
        entry="100",
        updated=3,
        start=4,
        end=5,
    )
    persist_inventory(database, scope=SCOPE, observation=observed)
    request, witness = anchor(SCOPE, episode, opening, observed, version=1)
    BusinessState(database).change(
        StateKey(**asdict(SCOPE), namespace="directional-exit-v1", key=episode),
        expected_version=0,
        request_key="pinned",
        payload=witness["exit_state"],
        reason="QA_BOUNDARY_WITNESS",
    )
    closing, _ = orders.prepare(
        episode,
        leg="CLOSE",
        quantity="1.25",
        request_key="close",
        evidence=request,
    )
    fill(closing, "2", "100.008", ".046", ".00999999", 10)
    baseline(database, runtime, episode)

    class Income:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __init__(self):
            self.rows = rows

        def __call__(self, method, path, params):
            assert method == "GET" and path == "/fapi/v1/income"
            return deepcopy(self.rows)

    return runtime, episode, Income()


def assert_once(runtime, episode, original):
    trace = runtime.data.trace(episode)
    assert trace["fills"] == original
    assert trace["episode"]["status"] == "SETTLED"
    assert len(trace["cash"]) == len(trace["settlements"]) == 1
    adjustment = trace["cash"][0]
    assert adjustment["kind"] == "CORRECTION"
    assert Decimal(adjustment["amount"]) == Decimal("-.00000001")
    proof = adjustment["evidence"]["precision"]
    assert proof["model"] == MODEL
    assert proof["algorithm_verified"] is False
    assert proof["requires_final_wallet_match"] is True
    final = trace["settlements"][0]["evidence"]
    assert final["exchange_flat"] and final["fills_complete"] and final["cash_complete"]
    assert (
        Decimal(final["net_pnl"])
        == Decimal(final["wallet_delta"])
        == Decimal("-.08600001")
    )


def test_boundary_full_settlement_and_retries(database, boundary_closed):
    runtime, episode, income = boundary_closed
    original = deepcopy(runtime.data.trace(episode)["fills"])
    for _ in range(3):
        assert (
            service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
        )
        assert_once(runtime, episode, original)


@pytest.mark.parametrize("defect", ["wallet", "missing_income", "trade_id", "fee"])
def test_cash_candidate_never_weakens_wallet_or_income(
    database, boundary_closed, defect
):
    runtime, episode, income = boundary_closed
    balance = BALANCE
    if defect == "wallet":
        balance = "999.914"
    elif defect == "missing_income":
        income.rows.pop()
    elif defect == "trade_id":
        income.rows[-1]["tradeId"] = "wrong-trade"
    else:
        income.rows[0]["income"] = "-.05000001"
    result = service(database, income, balance=balance).run_once()
    assert result["status"] == "BLOCKED"
    if defect == "wallet":
        assert result["settlements"][episode]["error_code"] == "WALLET_CASH_MISMATCH"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []
    assert trace["episode"]["status"] == "ACTIVE"


@pytest.mark.parametrize("fail_at", ["adjustment", "settle"])
def test_boundary_transaction_failure_rolls_back(
    database, boundary_closed, monkeypatch, fail_at
):
    runtime, episode, income = boundary_closed
    original_fills = deepcopy(runtime.data.trace(episode)["fills"])
    method = getattr(Ledger, fail_at)

    def crash(self, *args, **kwargs):
        method(self, *args, **kwargs)
        raise RuntimeError("QA simulated transactional failure")

    monkeypatch.setattr(Ledger, fail_at, crash)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "BLOCKED"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []
    monkeypatch.setattr(Ledger, fail_at, method)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
    assert_once(runtime, episode, original_fills)


def test_boundary_concurrent_stages_apply_once(database, boundary_closed):
    runtime, episode, income = boundary_closed
    original = deepcopy(runtime.data.trace(episode)["fills"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service(database, income, balance=BALANCE).run_once(),
                range(2),
            )
        )
    assert any(r["status"] == "CLEAR" for r in results)
    assert service(database, income, balance=BALANCE).run_once()["status"] == "CLEAR"
    assert_once(runtime, episode, original)


@pytest.mark.parametrize("fail_after", [None, "CORRECTION", "SETTLEMENT"])
def test_boundary_funding_and_external_cash_are_atomic_and_idempotent(
    database, boundary_closed, monkeypatch, fail_after
):
    runtime, episode, income = boundary_closed
    original = deepcopy(runtime.data.trace(episode))
    revision = original["episode"]["accounting_revision"]
    # An explicitly excluded external position's funding affects the wallet,
    # but cannot be allocated to this episode or advance its ledger revision.
    for symbol, value, transaction in (
        ("BTCUSDT", "-.07672130", 1001),
        ("QAEXTERNALUSDT", "-.02", 1002),
    ):
        income.rows.append(
            {
                "symbol": symbol,
                "asset": "USDT",
                "incomeType": "FUNDING_FEE",
                "income": value,
                "tradeId": "",
                "tranId": transaction,
                "time": 6,
            }
        )
    stage = DirectionalSettlementStage(
        database,
        Venue(income, final_balance="999.81727869"),
        scope=SCOPE,
        clock_ms=lambda: 70000,
        excluded_position_symbols=("QAEXTERNALUSDT",),
    )
    if fail_after:
        method_name = "adjustment" if fail_after == "CORRECTION" else "settle"
        method = getattr(Ledger, method_name)
        calls = []

        def crash_after_write(self, *args, **kwargs):
            result = method(self, *args, **kwargs)
            if fail_after == "SETTLEMENT" or kwargs.get("kind") == "CORRECTION":
                calls.append(fail_after)
                raise RuntimeError("QA failure after combined cash write")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(Ledger, method_name, crash_after_write)
            assert stage.run_once()["status"] == "BLOCKED"
        assert calls == [fail_after]
        rolled_back = runtime.data.trace(episode)
        assert rolled_back["episode"] == original["episode"]
        assert rolled_back["fills"] == original["fills"]
        assert (
            rolled_back["cash"]
            == rolled_back["income_allocations"]
            == rolled_back["settlements"]
            == []
        )

    for attempt in range(3):
        result = stage.run_once()
        assert result["status"] == "CLEAR"
        assert bool(result["settlements"]) is (attempt == 0)
        trace = runtime.data.trace(episode)
        assert trace["fills"] == original["fills"]
        assert trace["episode"] == {
            "status": "SETTLED",
            "accounting_revision": revision + 2,
        }
        assert len(trace["cash"]) == 2
        assert len(trace["settlements"]) == len(trace["income_allocations"]) == 1
        cash = {row["kind"]: row for row in trace["cash"]}
        assert Decimal(cash["FUNDING"]["amount"]) == Decimal("-.07672130")
        assert Decimal(cash["CORRECTION"]["amount"]) == Decimal("-.00000001")
        assert cash["CORRECTION"]["evidence"]["precision"]["model"] == MODEL
        settlement = trace["settlements"][0]
        final = settlement["evidence"]
        assert settlement["revision"] == final["ledger_revision"] == revision + 2
        assert final["exchange_flat"] and final["cash_complete"]
        assert Decimal(final["wallet_delta"]) == Decimal("-.18272131")
        assert (
            Decimal(final["attributed_wallet_delta"])
            == Decimal(final["net_pnl"])
            == Decimal("-.16272131")
        )
        assert len(final["funding_income_ids"]) == len(final["external_income"]) == 1
        external = final["external_income"][0]
        assert external["symbol"] == "QAEXTERNALUSDT"
        assert Decimal(external["amount"]) == Decimal("-.02")
        assert external["income_id"] not in final["funding_income_ids"]
