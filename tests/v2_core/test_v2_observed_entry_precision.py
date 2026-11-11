"""Pinned venue entry-price precision, never an adjacent-cash tolerance."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, Inexact, localcontext

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE as DB_SCOPE
from test_v2_directional_replay import scenario
from test_v2_directional_settlement import baseline, service
from test_v2_precision_contract import SCOPE, cash_case

from services.v2_directional_cash import _entry_observations, reconcile_cash
from tests.support.v2_admission import database as database_fixture
from v2_core.account_inventory import persist_inventory
from v2_core.evidence import canonical, digest
from v2_core.ledger import Ledger
from v2_core.settlement_precision import OBSERVED_ENTRY_MODEL
from v2_core.state import BusinessState, StateKey

database = database_fixture
OBSERVATION = "4febfed1-2082-43e2-9d4a-6f49c816efe5"


def inventory(scope, *, symbol, side, quantity, entry, updated, start, end):
    position = {
        "symbol": symbol,
        "positionSide": "BOTH",
        "positionAmt": quantity if side == "BUY" else "-" + quantity,
        "entryPrice": entry,
        "updateTime": updated,
    }
    raw = {
        "positions_before": [position],
        "positions_after": [deepcopy(position)],
        "mode": {"dualSidePosition": False},
        "margin_mode": {"multiAssetsMargin": False},
        "account": {},
        "ordinary_orders": [],
        "conditional_orders": [],
    }
    result = {
        "observation_id": OBSERVATION,
        "account_scope": asdict(scope),
        "started_at_ms": start,
        "finished_at_ms": end,
        "status": "BLOCKED",
        "execution_authorized": False,
        "blockers": ["EXISTING_POSITION_REQUIRES_RECOVERY"],
        "failures": {},
        "summary": {
            "positions": [position],
            "ordinary_orders": [],
            "conditional_orders": [],
        },
        "responses": raw,
    }
    result["response_digests"] = {
        name: digest(canonical({"response": value})) for name, value in raw.items()
    }
    return result


def anchor(
    scope, episode, opening, observation, *, source="directional-exit-v1", version=199
):
    saved = {
        "episode": episode,
        "account_scope": asdict(scope),
        "opening_order_id": opening,
        "execution_authorized": True,
        "preflight_inventory": OBSERVATION,
        "preflight_digest": digest(canonical(observation)),
    }
    request = {"source": source, "preflight_inventory": OBSERVATION}
    if source == "directional-exit-v1":
        key = StateKey(**asdict(scope), namespace="directional-exit-v1", key=episode)
        request.update(state_id=key.identity, state_version=version)
    else:
        request.update(
            opening_order_id=opening, preflight_digest=saved["preflight_digest"]
        )
    return request, {"inventory": observation, "exit_state": saved}


def actual_case(side="SELL", source="directional-exit-v1"):
    pnl = "-7.54692199" if side == "SELL" else "7.54692199"
    trace, report, rows = cash_case(
        side, [("701", ".007219"), ("25974", ".007216")], [("26675", ".007499", pnl)]
    )
    trace["request"]["symbol"] = "JCTUSDT"
    for index, fill in enumerate(trace["fills"]):
        fill["occurred_at_ms"] = 1793196677867 if index < 2 else 1793199754659
        fill["fee"] = ["0.00253025", "0.09371419", "0.10001791"][index]
    for row in rows:
        row["symbol"] = "JCTUSDT"
        if row["income_type"] == "COMMISSION":
            row["amount"] = "-" + trace["fills"][int(row["source_id"])]["fee"]
    report["fees"] = "0.19626235"
    observed = inventory(
        SCOPE,
        symbol="JCTUSDT",
        side=side,
        quantity="26675",
        entry="0.0072160788379",
        updated=1793196677894,
        start=1793199752242,
        end=1793199752623,
    )
    request, witness = anchor(
        SCOPE, trace["intent_id"], "open", observed, source=source
    )
    trace["orders"][-1]["request_evidence"] = request
    return trace, report, rows, {trace["orders"][-1]["order_id"]: witness}


@pytest.mark.parametrize("precision", [6, 28, 100])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("source", ["directional-exit-v1", "unprotected-emergency-v1"])
def test_actual_full_close_requires_pinned_entry_and_exact_income(
    precision, side, source
):
    trace, report, rows, witnesses = actual_case(side, source)
    original = deepcopy((trace, report, rows, witnesses))
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
            reconcile_cash(trace, report, rows, scope=SCOPE)
        proof = reconcile_cash(
            trace, report, rows, scope=SCOPE, entry_observations=witnesses
        )
    correction = proof["precision_reconciliation"]
    assert correction["model"] == OBSERVED_ENTRY_MODEL
    assert correction["amount"] == ("0.00000001" if side == "SELL" else "-0.00000001")
    assert correction["theoretical_gross_pnl"] == report["gross_pnl"]
    assert correction["fills"][0]["entry_observation"]["entry_decimal_places"] == 13
    assert proof["settlement_authorized"] is False
    if side == "SELL":
        assert proof["provisional_net_pnl"] == "-7.74318434"
    assert (trace, report, rows, witnesses) == original


@pytest.mark.parametrize(
    "defect",
    [
        "digest",
        "response_digest",
        "scope",
        "symbol",
        "hedge",
        "quantity",
        "entry",
        "disagreement",
        "updated_before_open",
        "updated_after_inventory",
        "inventory_after_close",
        "failures",
        "blocker",
        "state_id",
        "state_version",
        "state_episode",
        "state_opening",
        "state_authority",
        "observation_id",
        "coarse_entry",
        "tie",
        "pnl_neighbor",
        "income",
    ],
)
def test_observed_entry_never_accepts_unbound_or_inexact_evidence(defect):
    trace, report, rows, witnesses = actual_case()
    witness = next(iter(witnesses.values()))
    observation, saved = witness["inventory"], witness["exit_state"]
    request = trace["orders"][-1]["request_evidence"]
    before = observation["responses"]["positions_before"][0]
    if defect == "digest":
        saved["preflight_digest"] = "0" * 64
    elif defect == "response_digest":
        observation["response_digests"]["positions_before"] = "0" * 64
    elif defect == "scope":
        observation["account_scope"]["account_id"] = "other-account"
    elif defect == "symbol":
        before["symbol"] = "OTHERUSDT"
    elif defect == "hedge":
        before["positionSide"] = "SHORT"
    elif defect == "quantity":
        before["positionAmt"] = "-26674"
    elif defect in {"entry", "disagreement"}:
        before["entryPrice"] = "0.0072160788378"
    elif defect == "updated_before_open":
        before["updateTime"] = 1793196677866
    elif defect == "updated_after_inventory":
        before["updateTime"] = observation["started_at_ms"] + 1
    elif defect == "inventory_after_close":
        observation["finished_at_ms"] = trace["fills"][-1]["occurred_at_ms"]
    elif defect == "failures":
        observation["failures"] = {"account": "UNAVAILABLE"}
    elif defect == "blocker":
        observation["blockers"].append("POSITIONS_CHANGED_DURING_READ")
    elif defect == "state_id":
        request["state_id"] = OBSERVATION
    elif defect == "state_version":
        request["state_version"] = True
    elif defect == "state_episode":
        saved["episode"] = OBSERVATION
    elif defect == "state_opening":
        saved["opening_order_id"] = "other-open"
    elif defect == "state_authority":
        saved["execution_authorized"] = False
    elif defect == "observation_id":
        observation["observation_id"] = trace["intent_id"]
    elif defect == "coarse_entry":
        before["entryPrice"] = (
            "0.00721607884"  # nearest, but whole-position effect exceeds 1e-8
        )
    elif defect == "tie":
        for fill in trace["fills"][:2]:
            fill["price"] = "0.00721607883785"
    elif defect == "pnl_neighbor":
        trace["fills"][-1]["evidence"]["venue_realized_pnl"] = "-7.54692198"
        rows[-1]["amount"] = "-7.54692198"
    elif defect == "income":
        rows[-1]["amount"] = "-7.54692198"
    if defect != "disagreement":
        observation["responses"]["positions_after"][0] = deepcopy(before)
    if defect != "response_digest":
        observation["response_digests"] = {
            name: digest(canonical({"response": value}))
            for name, value in observation["responses"].items()
        }
    if defect != "digest":
        saved["preflight_digest"] = digest(canonical(observation))
    with pytest.raises(ValueError):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.fixture
def observed_closed(database):
    _, runtime, signal, context = scenario(database)
    admitted = formal(runtime).consume(signal, context=context)
    episode, opening = admitted["intent_id"], admitted["order_id"]
    orders, ledger = runtime.data.orders, runtime.data.ledger
    income_rows = []

    def fill(oid, tid, qty, price, fee, pnl, when):
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
        for kind, value in (("COMMISSION", "-" + fee), ("REALIZED_PNL", pnl)):
            if Decimal(value):
                income_rows.append(
                    {
                        "symbol": "BTCUSDT",
                        "asset": "USDT",
                        "incomeType": kind,
                        "income": value,
                        "tradeId": tid,
                        "tranId": len(income_rows) + 1,
                        "time": when,
                    }
                )

    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    fill(opening, "1", ".5", "100", ".03", "0", 3)
    fill(opening, "2", ".75", "100.00000001", ".02", "0", 3)
    orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        exchange_order_id="10",
        evidence={"fills_complete": True},
    )
    observed = inventory(
        DB_SCOPE,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1.25",
        entry="100.00000001",
        updated=3,
        start=4,
        end=5,
    )
    persist_inventory(database, scope=DB_SCOPE, observation=observed)
    request, witness = anchor(DB_SCOPE, episode, opening, observed, version=1)
    key = StateKey(**asdict(DB_SCOPE), namespace="directional-exit-v1", key=episode)
    state = BusinessState(database)
    state.change(
        key,
        expected_version=0,
        request_key="pinned",
        payload=witness["exit_state"],
        reason="QA",
    )
    closing, _ = orders.prepare(
        episode, leg="CLOSE", quantity="1.25", request_key="close", evidence=request
    )
    # A newer head is deliberately unusable: the order pins historical version 1.
    state.change(
        key,
        expected_version=1,
        request_key="new-head",
        payload={"status": "LATER"},
        reason="QA",
    )
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    fill(closing, "3", "1.25", "92", ".046", "-10.00000001", 10)
    orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        exchange_order_id="11",
        evidence={"fills_complete": True},
    )
    baseline(database, runtime, episode)

    class Income:
        account_id, environment = DB_SCOPE.account_id, DB_SCOPE.environment

        def __call__(self, method, path, params):
            assert method == "GET" and path == "/fapi/v1/income"
            return deepcopy(income_rows)

    return runtime, episode, Income()


def test_historical_preflight_settles_exact_cash_once(database, observed_closed):
    runtime, episode, income = observed_closed
    original = deepcopy(runtime.data.trace(episode)["fills"])
    stage = service(database, income, balance="989.90399999")
    assert stage.run_once()["status"] == "CLEAR"
    for _ in range(2):
        assert stage.run_once()["status"] == "CLEAR"
    trace = runtime.data.trace(episode)
    assert trace["episode"]["status"] == "SETTLED"
    assert len(trace["settlements"]) == len(trace["cash"]) == 1
    assert trace["cash"][0]["kind"] == "CORRECTION"
    assert Decimal(trace["cash"][0]["amount"]) == Decimal("-0.0000000025")
    assert trace["cash"][0]["evidence"]["precision"]["model"] == OBSERVED_ENTRY_MODEL
    assert Decimal(trace["settlements"][0]["evidence"]["net_pnl"]) == Decimal(
        "-10.09600001"
    )
    assert trace["fills"] == original


def test_observed_entry_still_requires_exact_wallet(database, observed_closed):
    runtime, episode, income = observed_closed
    result = service(database, income, balance="989.904").run_once()
    assert result["settlements"][episode]["error_code"] == "WALLET_CASH_MISMATCH"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []


@pytest.mark.parametrize(
    "defect", ["missing_history", "wrong_state", "missing_inventory"]
)
def test_entry_loader_requires_exact_immutable_references(
    database, observed_closed, defect
):
    runtime, episode, _ = observed_closed
    trace = runtime.data.trace(episode)
    request = next(
        o["request_evidence"] for o in trace["orders"] if o["leg"] == "CLOSE"
    )
    if defect == "missing_history":
        request["state_version"] = 999
    elif defect == "wrong_state":
        request["state_id"] = OBSERVATION
    else:
        request["preflight_inventory"] = episode
    with (
        database() as conn,
        pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"),
    ):
        _entry_observations(conn, DB_SCOPE, trace)


def test_observed_entry_correction_rolls_back_with_settlement(
    database, observed_closed, monkeypatch
):
    runtime, episode, income = observed_closed
    original = Ledger.settle

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("QA rollback")

    monkeypatch.setattr(Ledger, "settle", crash)
    stage = service(database, income, balance="989.90399999")
    assert stage.run_once()["settlements"][episode]["error_code"] == "RuntimeError"
    trace = runtime.data.trace(episode)
    assert trace["cash"] == trace["settlements"] == []
    monkeypatch.setattr(Ledger, "settle", original)
    assert stage.run_once()["status"] == "CLEAR"
