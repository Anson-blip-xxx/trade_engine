from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_lifecycle import opening
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from v2_core.runtime_policy import PolicyStore

database = database_fixture
closed = closed_fixture


@pytest.mark.parametrize(
    "exit_price,exit_pnl,exit_fee,eth_net,balance",
    [
        ("92", "10", "0.046", "9.904", "999.798"),
        ("80", "25", "0.04", "24.91", "1014.804"),
    ],
)
def test_two_overlapping_symbols_settle_with_separate_income_ownership(
    database, closed, exit_price, exit_pnl, exit_fee, eth_net, balance
):
    first, btc, income = closed
    second, opened = opening(database, "S8", filled=False, symbol="ETHUSDT")
    eth, order = opened["intent_id"], opened["order_id"]
    for leg, oid, price, when, tid, fee, pnl in [
        ("OPEN", order, "100", 4, "101", "0.05", "0"),
        ("CLOSE", None, exit_price, 9, "102", exit_fee, exit_pnl),
    ]:
        if oid is None:
            oid, _ = second.data.orders.prepare(
                eth,
                leg="CLOSE",
                quantity="1.25",
                request_key="qa-eth-close",
                evidence={"source": "qa"},
            )
        second.data.orders.transition(
            oid, expected_version=1, status="SUBMITTING", evidence={}
        )
        second.data.ledger.record_fill(
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
        second.data.orders.transition(
            oid,
            expected_version=2,
            status="FILLED",
            exchange_order_id=tid,
            evidence={"fills_complete": True},
        )
        income.rows.append(
            {
                "symbol": "ETHUSDT",
                "incomeType": "COMMISSION",
                "income": str(-Decimal(fee)),
                "tradeId": tid,
                "tranId": 100 + when,
                "time": when,
            }
        )
        if pnl != "0":
            income.rows.append(
                {
                    "symbol": "ETHUSDT",
                    "incomeType": "REALIZED_PNL",
                    "income": pnl,
                    "tradeId": tid,
                    "tranId": 200 + when,
                    "time": when,
                }
            )
    PolicyStore(database, SCOPE).patch(
        {"capital.enabled": True, "sizing.pool_fraction": "0.70"},
        expected_version=0,
        reason="QA multi cash",
    )
    baseline(database, first, btc)
    baseline(database, second, eth)
    # BTC loses 10.106, ETH earns 9.904: wallet delta is -0.202.
    stage = service(database, income, balance=balance)
    result = stage.settle(btc)
    assert result["status"] == "SETTLED", result
    result = stage.settle(eth)
    assert result["status"] == "SETTLED", result
    assert Decimal(
        first.data.ledger.report(btc, settlement_currency="USDT")["net_pnl"]
    ) == Decimal("-10.106")
    assert Decimal(
        second.data.ledger.report(eth, settlement_currency="USDT")["net_pnl"]
    ) == Decimal(eth_net)
    from v2_core.capital_model import current_health, rehearsal_profile

    profile = PolicyStore(database, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=1, reason="QA verified compounding"
    )
    wallet = str(Decimal(balance) + 500)
    capital = current_health(
        database,
        SCOPE,
        {
            "totalWalletBalance": wallet,
            "totalMarginBalance": wallet,
            "availableBalance": wallet,
            "totalInitialMargin": "0",
        },
        profile.values,
    )
    net = Decimal(eth_net) - Decimal("10.106")
    expected = Decimal(1000) + (net / 2 if net > 0 else net)
    assert Decimal(capital["base"]) == expected
