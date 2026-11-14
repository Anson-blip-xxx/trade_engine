"""Admission -> fills -> income -> wallet -> correction -> outcome integration."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import scenario
from test_v2_directional_settlement import baseline, service

from tests.support.v2_admission import database as database_fixture
from v2_core.directional_outcomes import DirectionalOutcomeJournal
from v2_core.ledger import Ledger

database = database_fixture


@pytest.fixture
def rounded_episode(database):
    _, runtime, signal, context = scenario(database)
    admitted = formal(runtime).consume(signal, context=context)
    episode, opening = admitted["intent_id"], admitted["order_id"]
    orders, ledger = runtime.data.orders, runtime.data.ledger
    rows = []
    groups = [
        (
            opening,
            [(".5", "100", ".03", "0", 3), (".75", "100.00000001", ".02", "0", 3)],
        ),
    ]
    for key, qty, fee, pnl, when in [
        ("partial", ".5", ".02", "-4", 10),
        ("final", ".75", ".026", "-6", 20),
    ]:
        groups.append((key, [(qty, "92", fee, pnl, when)]))
    for group_index, (oid, fills) in enumerate(groups):
        if group_index:
            oid, _ = orders.prepare(
                episode,
                leg="CLOSE",
                quantity=fills[0][0],
                request_key=oid,
                evidence={"source": "qa"},
            )
        orders.transition(oid, expected_version=1, status="SUBMITTING", evidence={})
        for index, (qty, price, fee, pnl, when) in enumerate(fills):
            tid = str(group_index * 10 + index + 1)
            ledger.record_fill(
                order_id=oid,
                exchange_fill_id=tid,
                quantity=qty,
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
            for kind, value in (
                ("COMMISSION", str(-Decimal(fee))),
                ("REALIZED_PNL", pnl),
            ):
                if Decimal(value):
                    rows.append(
                        {
                            "incomeType": kind,
                            "income": value,
                            "tradeId": tid,
                            "tranId": len(rows) + 1,
                            "time": when,
                        }
                    )
        orders.transition(
            oid,
            expected_version=2,
            status="FILLED",
            exchange_order_id=str(100 + group_index),
            evidence={"fills_complete": True},
        )
    rows.append(
        {
            "incomeType": "FUNDING_FEE",
            "income": "-.01",
            "tradeId": "",
            "tranId": 99,
            "time": 5,
        }
    )

    class Income:
        account_id, environment = runtime.scope.account_id, runtime.scope.environment

        def __call__(self, method, path, params):
            assert method == "GET" and path == "/fapi/v1/income"
            return [{"symbol": "BTCUSDT", "asset": "USDT", **r} for r in deepcopy(rows)]

    baseline(database, runtime, episode)
    return runtime, episode, Income()


def assert_settled(database, runtime, episode):
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] == "SETTLED"
    assert len(trace["settlements"]) == 1
    assert len(trace["cash"]) == 2
    correction = next(c for c in trace["cash"] if c["kind"] == "CORRECTION")
    assert Decimal(correction["amount"]) == Decimal("0.0000000075")
    assert (
        correction["evidence"]["precision"]["model"]
        == "binance-usdt-per-fill-truncate-8-v1"
    )
    report = runtime.data.ledger.report(episode, settlement_currency="USDT")
    assert Decimal(report["gross_pnl"]) == Decimal("-10.0000000075")
    assert Decimal(report["net_pnl"]) == Decimal("-10.106")
    with database() as conn:
        assert conn.execute(
            "SELECT count(*),min(net_pnl) FROM v2_directional_outcomes WHERE episode_id=%s",
            (episode,),
        ).fetchone() == (1, Decimal("-10.106"))


def test_multifill_rounding_funding_settlement_and_retries(database, rounded_episode):
    runtime, episode, income = rounded_episode
    original = deepcopy(runtime.data.trace(episode)["fills"])
    assert service(database, income).run_once()["status"] == "CLEAR"
    assert_settled(database, runtime, episode)
    for _ in range(3):
        assert service(database, income).run_once()["status"] == "CLEAR"
    assert_settled(database, runtime, episode)
    assert runtime.data.trace(episode)["fills"] == original


@pytest.mark.parametrize("fail_at", ["adjustment", "settle"])
def test_crash_after_correction_or_settlement_write_rolls_back(
    database, rounded_episode, monkeypatch, fail_at
):
    runtime, episode, income = rounded_episode
    original = getattr(Ledger, fail_at)

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("private diagnostic must not leak")

    monkeypatch.setattr(Ledger, fail_at, crash)
    result = service(database, income).run_once()
    assert result["settlements"][episode]["error_code"] == "RuntimeError"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == trace["income_allocations"] == []
    monkeypatch.setattr(Ledger, fail_at, original)
    assert service(database, income).run_once()["status"] == "CLEAR"
    assert_settled(database, runtime, episode)


def test_outcome_failure_after_commit_backfills_without_reapplying_cash(
    database, rounded_episode, monkeypatch
):
    runtime, episode, income = rounded_episode
    original = DirectionalOutcomeJournal.record
    monkeypatch.setattr(
        DirectionalOutcomeJournal,
        "record",
        lambda *_: (_ for _ in ()).throw(ValueError("OUTCOME_RETRY_REQUIRED")),
    )
    attempted = service(database, income).run_once()
    assert attempted["status"] == "CLEAR"
    assert attempted["settlements"][episode]["status"] == "SETTLED"
    assert attempted["outcomes"][episode]["error_code"] == "OUTCOME_RETRY_REQUIRED"
    assert runtime.data.trace(episode)["episode"]["status"] == "SETTLED"
    monkeypatch.setattr(DirectionalOutcomeJournal, "record", original)
    assert service(database, income).run_once()["status"] == "CLEAR"
    assert_settled(database, runtime, episode)


def test_rounding_never_hides_wallet_difference(database, rounded_episode):
    runtime, episode, income = rounded_episode
    result = service(database, income, balance="989.89400001").run_once()
    assert result["settlements"][episode]["error_code"] == "WALLET_CASH_MISMATCH"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []


def test_concurrent_stages_cannot_double_apply_correction(database, rounded_episode):
    runtime, episode, income = rounded_episode
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: service(database, income).run_once(), range(2))
        )
    assert any(r["status"] == "CLEAR" for r in results)
    assert service(database, income).run_once()["status"] == "CLEAR"
    assert_settled(database, runtime, episode)
