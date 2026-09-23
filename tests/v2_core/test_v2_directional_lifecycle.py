from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_account_coverage import Account, inventory
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_lifecycle import DirectionalProtectionStage
from v2_core.account_coverage import coverage_from_facts

database = database_fixture


class Venue(Account):
    def __init__(self):
        super().__init__()
        self.writes, self.parent = [], None
        self.hide_stop = False
        self.child = None

    def __call__(self, method, path, params):
        if self.child is not None and path in {"/fapi/v1/order", "/fapi/v1/userTrades"}:
            assert method == "GET"
            if path.endswith("/order"):
                return deepcopy(self.child)
            return [
                {
                    "id": 2,
                    "orderId": 555,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "qty": "1.25",
                    "price": "92",
                    "commission": "0.046",
                    "commissionAsset": "USDT",
                    "time": 1001,
                    "realizedPnl": "-10",
                }
            ]
        if path == "/fapi/v1/algoOrder":
            if method == "POST":
                self.writes.append(dict(params))
                self.parent = {
                    **params,
                    "orderType": params["type"],
                    "algoId": 30,
                    "algoStatus": "NEW",
                    "closePosition": True,
                    "priceProtect": False,
                }
                self.rows["/fapi/v1/openAlgoOrders"] = (
                    [] if self.hide_stop else [deepcopy(self.parent)]
                )
            assert self.parent is not None
            return deepcopy(self.parent)
        return super().__call__(method, path, params)


def stage(database, venue, **options):
    return DirectionalProtectionStage(
        database,
        venue,
        scope=SCOPE,
        reference=lambda _: {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "mark_price": "100",
            "tick_size": "0.1",
            "observed_at_ms": 1000,
        },
        clock_ms=lambda: 1000,
        **options,
    )


def opening(database, strategy="S6", filled=True, symbol="BTCUSDT"):
    _, runtime, signal, context = scenario(database, strategy)
    if symbol != "BTCUSDT":
        with database() as conn:
            snapshot = conn.execute(
                "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal,)
            ).fetchone()[0]
        snapshot["symbol"] = symbol
        signal = runtime.data.signals.admit(
            source="s3",
            environment="SANDBOX",
            request_key="qa-" + symbol + strategy,
            snapshot=snapshot,
        )
        context["symbol"] = symbol
    result = formal(runtime, strategy).consume(signal, context=context)
    order = result["order_id"]
    if filled:
        runtime.data.orders.transition(
            order, expected_version=1, status="SUBMITTING", evidence={}
        )
        runtime.data.ledger.record_fill(
            order_id=order,
            exchange_fill_id="1",
            quantity="1.25",
            price="100",
            fee="0.05",
            fee_currency="USDT",
            occurred_at_ms=3,
            evidence={"source": "explicit-QA-fill"},
        )
        runtime.data.orders.transition(
            order,
            expected_version=2,
            status="FILLED",
            exchange_order_id="10",
            evidence={"fills_complete": True},
        )
    return runtime, result


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_actual_stage_installs_original_stop_despite_other_prepared_order(
    database, strategy
):
    _, result = opening(database, strategy)
    _, pending = opening(
        database, "S8" if strategy == "S6" else "S6", filled=False, symbol="ETHUSDT"
    )
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "1.25" if strategy == "S6" else "-1.25",
        }
    ]
    service = stage(database, venue, allow_writes=True)
    first = service.run_once()
    assert first["status"] == "CLEAR"
    assert first["stops"][result["order_id"]]["status"] == "NEW"
    assert first["coverage"]["status"] == "ACCOUNT_COVERAGE_CLEAR"
    assert first["execution_authorized"] is False
    # Reconstruct the stage: PG receipt and original strategy plan prevent duplicate POST.
    assert stage(database, venue, allow_writes=True).run_once()["status"] == "CLEAR"
    assert len(venue.writes) == 1
    assert venue.writes[0]["triggerPrice"] == ("92" if strategy == "S6" else "108")
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (pending["order_id"],)
        ).fetchone() == ("PREPARED",)


def test_stop_installer_honors_same_explicit_external_position_exclusion(database):
    _, result = opening(database)
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1.25"},
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"},
    ]
    result = stage(
        database,
        venue,
        allow_writes=True,
        excluded_position_symbols=("ZORAUSDT",),
    ).run_once()
    assert result["status"] == "CLEAR"
    assert len(venue.writes) == 1
    assert result["stops"][next(iter(result["stops"]))]["status"] == "NEW"


def test_prepared_only_does_not_cancel_or_freeze_strategy_entry(database):
    _, result = opening(database, filled=False)
    venue = Venue()
    answer = stage(database, venue, allow_writes=True).run_once()
    assert answer["status"] == "CLEAR" and answer["stops"] == {}
    assert not venue.writes
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (result["order_id"],)
        ).fetchone() == ("PREPARED",)


@pytest.mark.parametrize("failure", ["disabled", "missing_remote_stop", "venue_error"])
def test_no_clear_without_verified_remote_protection(database, failure):
    opening(database)
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1.25"}
    ]
    venue.hide_stop = failure == "missing_remote_stop"
    if failure == "venue_error":
        venue.fail = "/fapi/v3/account"
    result = stage(database, venue, allow_writes=failure != "disabled").run_once()
    assert result["status"] == "BLOCKED"
    if failure != "missing_remote_stop":
        assert not venue.writes


@pytest.mark.parametrize(
    "defect", ["submitted", "close", "exchange_id", "fills", "unknown_fill_status"]
)
def test_only_proven_unsent_opening_is_exempt_from_nonfinal_coverage(database, defect):
    opening(database, filled=False)
    venue = Venue()
    service = stage(database, venue)
    facts = service.audit.facts()
    assert coverage_from_facts(SCOPE, facts, inventory(venue)) == []
    order = facts["orders"][0]
    if defect == "submitted":
        order["status"] = "SUBMITTING"
    elif defect == "close":
        order["leg"] = "CLOSE"
    elif defect == "exchange_id":
        order["exchange_order_id"] = "10"
    elif defect == "fills":
        order["has_fills"] = True
    else:
        del order["has_fills"]
    assert "LOCAL_ORDERS_NOT_FINAL" in coverage_from_facts(
        SCOPE, facts, inventory(venue)
    )


def test_duplicate_stage_is_busy_before_venue_io(database):
    venue = Venue()
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("directional-protection-stage:" + SCOPE.key,),
        )
        conn.commit()
        assert stage(database, venue).run_once() == {
            "status": "BLOCKED",
            "reason": "BUSY",
        }
    assert not venue.calls


def test_live_scope_rejected(database):
    with pytest.raises(ValueError):
        DirectionalProtectionStage(
            database,
            Venue(),
            scope=replace(SCOPE, environment="LIVE"),
            reference=lambda _: {},
            clock_ms=lambda: 1000,
        )


def test_registered_recovery_error_does_not_skip_original_stop_attempt(
    database, monkeypatch
):
    opening(database)
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1.25"}
    ]
    service = stage(database, venue, allow_writes=True)

    def fail(**_):
        raise TimeoutError("private account detail")

    monkeypatch.setattr(service.supervisor, "run_once", fail)
    result = service.run_once()
    assert result["status"] == "BLOCKED" and len(venue.writes) == 1
    assert result["recovery"] == {"status": "BLOCKED", "error_code": "TimeoutError"}


def test_stop_trigger_recovers_native_close_and_fills_without_another_post(database):
    runtime, result = opening(database)
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1.25"}
    ]
    service = stage(database, venue, allow_writes=True)
    assert service.run_once()["status"] == "CLEAR"
    venue.parent.update(algoStatus="FINISHED", actualOrderId="555", actualQty="1.25")
    venue.child = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "reduceOnly": True,
        "orderId": 555,
        "clientOrderId": "exchange-generated",
        "origQty": "1.25",
        "executedQty": "1.25",
        "status": "FILLED",
    }
    venue.rows["/fapi/v3/positionRisk"] = []
    venue.rows["/fapi/v1/openAlgoOrders"] = []
    for _ in range(2):
        final = stage(database, venue, allow_writes=True).run_once()
        assert final["status"] == "CLEAR", final
    assert len(venue.writes) == 1
    trace = runtime.data.trace(result["intent_id"])
    assert len(trace["orders"]) == len(trace["fills"]) == 2
    assert {o["leg"]: o["status"] for o in trace["orders"]} == {
        "OPEN": "FILLED",
        "CLOSE": "FILLED",
    }
    assert Decimal(service.audit.facts()["episodes"][0]["remaining"]) == 0


def test_durable_cursor_rotates_past_poison_order_after_restart(database, monkeypatch):
    _, first = opening(database)
    _, second = opening(database, symbol="ETHUSDT")
    attempted = []

    def fail(order_id):
        attempted.append(order_id)
        raise ValueError("private malformed episode")

    for _ in range(2):
        service = stage(database, Venue(), limit=1)
        monkeypatch.setattr(service.stop, "ensure", fail)
        assert service.run_once()["status"] == "BLOCKED"
    assert set(attempted) == {first["order_id"], second["order_id"]}
