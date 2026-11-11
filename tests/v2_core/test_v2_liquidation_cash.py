"""Insurance-clear must be owned by a verified forced close, never a time guess."""

from copy import deepcopy
from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service

from services.v2_directional_cash import DirectionalCashAudit
from tests.support.v2_admission import database as database_fixture
from v2_core.income import IncomeJournal
from v2_core.income_ownership import income_owner
from v2_core.liquidation_cash import insurance_clear_candidate
from v2_core.orders import Orders

database = database_fixture


def binding():
    return {
        "origin": "BINANCE_EXTERNAL_CLOSE",
        "kind": "LIQUIDATION",
        "exchange_order_id": "11",
        "venue_client_order_id": "autoclose-qa",
        "venue_order": {
            "orderId": 11,
            "clientOrderId": "autoclose-qa",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "status": "FILLED",
            "type": "LIMIT",
            "reduceOnly": True,
            "origQty": "1.25",
            "executedQty": "1.25",
            "updateTime": 10,
        },
    }


def income_row():
    return {
        "income_id": "11111111-1111-4111-8111-111111111111",
        "source_id": "99",
        "income_type": "INSURANCE_CLEAR",
        "symbol": "BTCUSDT",
        "currency": "USDT",
        "amount": "-0.84353267",
        "occurred_at_ms": 10,
        "evidence": {"source": "binance-income", "trade_id": ""},
    }


def forced_trace():
    return {
        "request": {"symbol": "BTCUSDT", "side": "BUY"},
        "orders": [
            {
                "order_id": "close",
                "leg": "CLOSE",
                "status": "FILLED",
                "quantity": "1.25",
                "request_evidence": binding(),
            }
        ],
        "fills": [
            {
                "order_id": "close",
                "quantity": "1.25",
                "fee_currency": "USDT",
                "occurred_at_ms": 10,
                "evidence": {"source": "binance-userTrades", "trade_id": "2"},
            }
        ],
    }


def test_verified_force_order_snapshot_owns_insurance():
    candidate = insurance_clear_candidate(forced_trace(), income_row())
    assert candidate["exchange_order_id"] == "11"
    assert candidate["amount"] == "-0.84353267"
    assert len(candidate["binding_digest"]) == 64


@pytest.mark.parametrize(
    "change",
    [
        "ordinary_close",
        "unproved_origin",
        "missing_snapshot",
        "not_autoclose",
        "different_symbol",
        "wrong_side",
        "not_reduce_only",
        "wrong_quantity",
        "missing_fills",
        "unverified_fill",
        "positive_insurance",
        "other_currency",
        "nonempty_trade_id",
        "remote_timestamp",
        "duplicate_candidate",
    ],
)
def test_ambiguous_or_unverified_insurance_fails_closed(change):
    trace, row = forced_trace(), income_row()
    proof = trace["orders"][0]["request_evidence"]
    raw = proof["venue_order"]
    if change == "ordinary_close":
        proof["kind"] = "EXTERNAL_REDUCE_ONLY"
    elif change == "unproved_origin":
        proof["origin"] = "QA"
    elif change == "missing_snapshot":
        proof.pop("venue_order")
    elif change == "not_autoclose":
        proof["venue_client_order_id"] = raw["clientOrderId"] = "manual"
    elif change == "different_symbol":
        raw["symbol"] = "ETHUSDT"
    elif change == "wrong_side":
        raw["side"] = "BUY"
    elif change == "not_reduce_only":
        raw["reduceOnly"] = False
    elif change == "wrong_quantity":
        raw["executedQty"] = "1.24"
    elif change == "missing_fills":
        trace["fills"] = []
    elif change == "unverified_fill":
        trace["fills"][0]["evidence"]["source"] = "manual"
    elif change == "positive_insurance":
        row["amount"] = "0.84353267"
    elif change == "other_currency":
        row["currency"] = "USDC"
    elif change == "nonempty_trade_id":
        row["evidence"]["trade_id"] = "2"
    elif change == "remote_timestamp":
        row["occurred_at_ms"] = 1010
    elif change == "duplicate_candidate":
        trace["orders"].append(deepcopy(trace["orders"][0]))
    with pytest.raises(ValueError):
        insurance_clear_candidate(trace, row)


@pytest.fixture
def liquidation(database, monkeypatch):
    original = Orders.prepare

    def prepare(self, *args, **kwargs):
        if kwargs.get("request_key") == "qa-close":
            kwargs["evidence"] = binding()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Orders, "prepare", prepare)
    runtime, episode, income = closed_fixture.__wrapped__(database)
    income.rows.append(
        {
            "incomeType": "INSURANCE_CLEAR",
            "income": "-0.84353267",
            "tradeId": "",
            "tranId": 99,
            "time": 10,
        }
    )
    return runtime, episode, income


def test_liquidation_final_settlement_exact_cash_and_retry(database, liquidation):
    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    stage = service(database, income, balance="989.05046733")
    result = stage.run_once()
    assert result["status"] == "CLEAR", result
    assert Decimal(result["settlements"][episode]["net_pnl"]) == Decimal("-10.94953267")
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] == "SETTLED"
    insurance = [
        c
        for c in trace["cash"]
        if c["evidence"].get("income_type") == "INSURANCE_CLEAR"
    ]
    assert len(insurance) == 1
    assert Decimal(insurance[0]["amount"]) == Decimal("-0.84353267")
    assert trace["settlements"][0]["evidence"]["insurance_clear_income_ids"] == [
        insurance[0]["evidence"]["income_id"]
    ]
    assert stage.run_once()["settlements"] == {}
    assert runtime.data.trace(episode)["cash"] == trace["cash"]
    with database() as conn:
        outcome = conn.execute(
            "SELECT evidence FROM v2_directional_outcomes WHERE episode_id=%s",
            (episode,),
        ).fetchone()[0]
    assert outcome["exit_reason"] == "EXCHANGE_LIQUIDATION"
    assert outcome["exit_origin"] == "BINANCE_EXTERNAL_CLOSE"
    assert "strategy_learning_eligible" not in outcome


def test_liquidation_tg_separates_funding_from_insurance(database, liquidation):
    from test_v2_account_inventory import TelegramConnection, sink
    from test_v2_trade_notifications import save_protection

    from v2_core.telegram import _trade_text
    from v2_core.trade_notifications import TradeLifecycleNotifications

    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    assert (
        service(database, income, balance="989.05046733").run_once()["status"]
        == "CLEAR"
    )
    save_protection(database, episode)
    notification = TradeLifecycleNotifications(
        database, sink(TelegramConnection()), scope=SCOPE
    )
    closing = notification._closing(episode)
    assert closing["funding"] == "-0.01"
    assert closing["insurance_clear"] == "-0.84353267"
    assert closing["exit_reason"].startswith("EXCHANGE_LIQUIDATION")
    text = _trade_text(SCOPE.key, "TRADE_CLOSED", "qa", closing)
    assert "资金费/现金调整：-0.01 USDT" in text
    assert "强平保险清算：-0.84353267 USDT" in text


def test_insurance_is_verified_for_external_account_cash(database, liquidation):
    _, episode, income = liquidation
    auditor = DirectionalCashAudit(
        database, income, scope=SCOPE, clock_ms=lambda: 70000
    )
    proof = auditor.audit(episode)
    assert proof["status"] == "CASH_MATCHED"
    with database() as conn:
        candidate = proof["insurance_candidates"][0]
        row = {**income_row(), "income_id": candidate["income_id"]}
        assert income_owner(conn, SCOPE, row) == episode


def test_wallet_mismatch_cannot_write_insurance_adjustment(database, liquidation):
    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    result = service(database, income, balance="989.05046734").run_once()
    assert result["status"] == "BLOCKED"
    assert runtime.data.trace(episode)["cash"] == []
    assert runtime.data.trace(episode)["settlements"] == []


def test_insurance_assignment_failure_rolls_back_funding_and_fee(
    database, liquidation, monkeypatch
):
    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    original = IncomeJournal.assign_insurance_clear

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("injected after insurance write")

    with monkeypatch.context() as patch:
        patch.setattr(IncomeJournal, "assign_insurance_clear", crash)
        result = service(database, income, balance="989.05046733").run_once()
    assert result["status"] == "BLOCKED"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["income_allocations"] == trace["settlements"] == []
    assert (
        service(database, income, balance="989.05046733").run_once()["status"]
        == "CLEAR"
    )
