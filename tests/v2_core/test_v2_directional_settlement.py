import json
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_account_coverage import Account
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture
from test_v2_trading_pipeline import case as pipeline_fixture

from services.v2_directional_settlement import DirectionalSettlementStage
from v2_core.ledger import Ledger
from v2_core.state import BusinessState, StateKey

database = database_fixture
closed = closed_fixture
pipeline_case = pipeline_fixture


class Venue(Account):
    def __init__(self, income, *, final_balance="989.894"):
        super().__init__()
        self.income = income
        self.rows["/fapi/v3/account"] = {
            k: final_balance
            for k in ("totalWalletBalance", "availableBalance", "totalMarginBalance")
        }

    def __call__(self, method, path, params):
        if path == "/fapi/v1/income":
            return self.income(method, path, params)
        return super().__call__(method, path, params)


def baseline(database, runtime, episode, *, wallet="1000", ready=True):
    order = next(o for o in runtime.data.trace(episode)["orders"] if o["leg"] == "OPEN")
    inventory = {
        "observation_id": "11111111-1111-4111-8111-111111111111",
        "account_scope": asdict(SCOPE),
        "started_at_ms": 1,
        "finished_at_ms": 2,
        "status": "CLEAR_FOR_RECOVERY_CHECKS",
        "execution_authorized": False,
        "blockers": [],
        "failures": {},
        "summary": {"positions": [], "ordinary_orders": [], "conditional_orders": []},
        "responses": {
            "account": {"totalWalletBalance": wallet},
            "positions_before": [],
            "positions_after": [],
            "ordinary_orders": [],
            "conditional_orders": [],
            "mode": {"dualSidePosition": False},
            "margin_mode": {"multiAssetsMargin": False},
        },
        "response_digests": {},
    }
    proof = {
        "status": "READY_FOR_GUARDED_SUBMISSION" if ready else "BLOCKED",
        "blockers": [] if ready else ["QA"],
        "inventory": inventory,
    }
    key = StateKey(
        **asdict(SCOPE), namespace="opening-readiness-v1", key=order["order_id"]
    )
    assert (
        BusinessState(database)
        .change(
            key,
            expected_version=0,
            request_key="pre-submit",
            payload=proof,
            reason="QA_OPENING_BASELINE",
        )
        .code
        == "APPLIED"
    )


def service(database, income, *, balance="989.894"):
    return DirectionalSettlementStage(
        database,
        Venue(income, final_balance=balance),
        scope=SCOPE,
        clock_ms=lambda: 70000,
    )


def test_final_account_cash_and_funding_settle_atomically(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    result = service(database, income).run_once()
    assert result["status"] == "CLEAR"
    assert result["settlements"][episode]["status"] == "SETTLED"
    assert Decimal(result["settlements"][episode]["net_pnl"]) == Decimal("-10.106")
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] == "SETTLED"
    assert len(trace["settlements"]) == len(trace["income_allocations"]) == 1
    assert trace["cash"][0]["kind"] == "FUNDING"
    assert Decimal(trace["cash"][0]["amount"]) == Decimal("-0.01")
    proof = trace["settlements"][0]["evidence"]
    assert proof["exchange_flat"] and proof["orders_terminal"]
    assert proof["fills_complete"] and proof["cash_complete"]
    assert (
        Decimal(proof["wallet_delta"])
        == Decimal(proof["net_pnl"])
        == Decimal("-10.106")
    )
    assert service(database, income).run_once() == {
        "status": "CLEAR",
        "settlements": {},
        "outcomes": {},
        "settlement_authorized": False,
    }
    assert len(runtime.data.trace(episode)["cash"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "wallet",
        "baseline_missing",
        "baseline_blocked",
        "venue_position",
        "venue_order",
        "income",
    ],
)
def test_incomplete_final_proof_never_settles(database, closed, failure):
    runtime, episode, income = closed
    if failure != "baseline_missing":
        baseline(database, runtime, episode, ready=failure != "baseline_blocked")
    venue = Venue(income, final_balance="989" if failure == "wallet" else "989.894")
    if failure == "venue_position":
        venue.rows["/fapi/v3/positionRisk"] = [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        ]
    elif failure == "venue_order":
        venue.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 99}]
    elif failure == "income":
        income.rows[0]["income"] = "-1"
    result = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: 70000
    ).run_once()
    assert result["status"] == "BLOCKED"
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] != "SETTLED"
    assert (
        trace["settlements"] == []
        and trace["cash"] == []
        and trace["income_allocations"] == []
    )


def test_explicit_external_position_does_not_block_final_settlement(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    venue = Venue(income)
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"}
    ]
    stage = DirectionalSettlementStage(
        database,
        venue,
        scope=SCOPE,
        clock_ms=lambda: 70000,
        excluded_position_symbols=("ZORAUSDT",),
    )
    result = stage.run_once()
    assert result["status"] == "CLEAR"
    assert result["settlements"][episode]["status"] == "SETTLED"
    with database() as conn:
        payload = conn.execute(
            """SELECT payload FROM v2_business_state
            WHERE scope->>'namespace'='account-inventory-v1'
            ORDER BY version DESC LIMIT 1"""
        ).fetchone()[0]
    assert payload["excluded_position_symbols"] == ["ZORAUSDT"]
    assert payload["summary"]["positions"] == []
    assert payload["summary"]["excluded_positions"][0]["symbol"] == "ZORAUSDT"


def test_failure_after_funding_allocation_rolls_back_whole_settlement(
    database, closed, monkeypatch
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    original = Ledger.settle
    monkeypatch.setattr(
        Ledger,
        "settle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("commit lost")),
    )
    assert service(database, income).run_once()["status"] == "BLOCKED"
    trace = runtime.data.trace(episode)
    assert (
        trace["cash"] == []
        and trace["income_allocations"] == []
        and trace["settlements"] == []
    )
    monkeypatch.setattr(Ledger, "settle", original)
    assert service(database, income).run_once()["status"] == "CLEAR"


def test_final_inventory_is_persisted_on_wallet_mismatch(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income, balance="900").run_once()["status"] == "BLOCKED"
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='account-inventory-v1'"
            ).fetchone()[0]
            == 1
        )
    assert runtime.data.trace(episode)["settlements"] == []


def test_episode_without_funding_settles_without_cash_adjustment(database, closed):
    runtime, episode, income = closed
    income.rows.pop()
    baseline(database, runtime, episode)
    result = service(database, income, balance="989.904").run_once()
    assert result["status"] == "CLEAR"
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] == "SETTLED"
    assert trace["cash"] == [] and trace["income_allocations"] == []
    assert Decimal(trace["settlements"][0]["evidence"]["net_pnl"]) == Decimal("-10.096")


def test_multiple_funding_rows_advance_revisions_in_one_transaction(database, closed):
    runtime, episode, income = closed
    income.rows.append(
        {
            "incomeType": "FUNDING_FEE",
            "income": "-0.02",
            "tradeId": "",
            "tranId": 5,
            "time": 6,
        }
    )
    baseline(database, runtime, episode)
    assert service(database, income, balance="989.874").run_once()["status"] == "CLEAR"
    trace = runtime.data.trace(episode)
    assert len(trace["cash"]) == len(trace["income_allocations"]) == 2
    assert Decimal(trace["settlements"][0]["evidence"]["net_pnl"]) == Decimal("-10.126")


def test_account_lock_blocks_final_inventory_and_commit(database, closed, monkeypatch):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    worker = service(database, income)
    cash = worker.cash.audit(episode)
    monkeypatch.setattr(worker.cash, "audit", lambda _: cash)
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert worker.settle(episode)["reason"] == "ACCOUNT_BUSY"
    assert runtime.data.trace(episode)["settlements"] == []


def test_superseded_cash_evidence_cannot_commit(database, closed, monkeypatch):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    worker = service(database, income)
    original = worker.coverage.inventory.collect

    def supersede(identity):
        result = original(identity)
        key = StateKey(
            **asdict(SCOPE), namespace="directional-cash-audit-v1", key=episode
        )
        store = BusinessState(database)
        previous = store.read(key)
        store.change(
            key,
            expected_version=previous.version,
            request_key="supersede",
            payload={"status": "BLOCKED", "settlement_authorized": False},
            reason="QA_SUPERSEDE",
        )
        return result

    monkeypatch.setattr(worker.coverage.inventory, "collect", supersede)
    assert worker.run_once()["status"] == "BLOCKED"
    assert runtime.data.trace(episode)["settlements"] == []


def test_unified_cycle_settles_old_episode_before_admitting_new_signals(
    database, closed, pipeline_case
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    pipeline, _, _, _, _ = pipeline_case
    pipeline.settlement = service(database, income)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert result["phases"]["settlement"]["settlements"][episode]["status"] == "SETTLED"
    assert "s6" in result["phases"] and "s8" in result["phases"]
    assert runtime.data.trace(episode)["episode"]["status"] == "SETTLED"


def test_baseline_after_first_fill_is_rejected(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    with database() as conn:
        order = conn.execute(
            "SELECT order_id::text FROM v2_orders WHERE episode_id=%s AND leg='OPEN'",
            (episode,),
        ).fetchone()[0]
    key = StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order)
    store = BusinessState(database)
    value = store.read(key)
    payload = json.loads(value.payload_json)
    payload["inventory"]["finished_at_ms"] = 4
    store.change(
        key,
        expected_version=value.version,
        request_key="late-baseline",
        payload=payload,
        reason="QA_LATE_BASELINE",
    )
    assert service(database, income).run_once()["status"] == "BLOCKED"
    assert runtime.data.trace(episode)["settlements"] == []


def test_baseline_changed_during_final_inventory_cannot_commit(
    database, closed, monkeypatch
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    worker = service(database, income)
    original = worker.coverage.inventory.collect

    def supersede(identity):
        observation = original(identity)
        with database() as conn:
            order = conn.execute(
                "SELECT order_id::text FROM v2_orders WHERE episode_id=%s AND leg='OPEN'",
                (episode,),
            ).fetchone()[0]
        key = StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order)
        store = BusinessState(database)
        value = store.read(key)
        payload = json.loads(value.payload_json)
        payload["status"] = "BLOCKED"
        store.change(
            key,
            expected_version=value.version,
            request_key="supersede",
            payload=payload,
            reason="QA_SUPERSEDE",
        )
        return observation

    monkeypatch.setattr(worker.coverage.inventory, "collect", supersede)
    assert worker.run_once()["status"] == "BLOCKED"
    assert runtime.data.trace(episode)["settlements"] == []
