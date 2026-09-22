from copy import deepcopy

import pytest
from test_v2_directional_lifecycle import SCOPE, Venue, opening
from test_v2_directional_lifecycle import stage as protection_stage
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_exit import DirectionalExitStage
from v2_core.binance import BinanceFutures
from v2_core.state import BusinessState, StateKey

database = database_fixture


class ExitVenue(Venue):
    def __init__(self):
        super().__init__()
        self.order_writes, self.close, self.trades = [], None, []

    def __call__(self, method, path, params):
        if path == "/fapi/v1/order":
            if method == "POST":
                self.order_writes.append(deepcopy(params))
                self.close = {
                    "clientOrderId": params["newClientOrderId"],
                    "symbol": params["symbol"],
                    "side": params["side"],
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "reduceOnly": True,
                    "origQty": params["quantity"],
                    "executedQty": "0",
                    "orderId": 900,
                    "status": "NEW",
                }
                return deepcopy(self.close)
            assert method == "GET" and self.close is not None
            return deepcopy(self.close)
        if path == "/fapi/v1/userTrades":
            assert method == "GET"
            return deepcopy(self.trades)
        return super().__call__(method, path, params)


def observation(now, **changes):
    return {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "observed_at_ms": now,
        "mark_price": "101",
        "funding_rate": "0",
        "ema9_1h": "101",
        "ema20_1h": "100",
        "momentum_closes_15m": ["100", "100", "100", "101"],
        "quantity_step": ".001",
        "exit_fee_rate": ".0004",
        **changes,
    }


def setup_exit(database, *, strategy="S6", allow_writes=False, now=600003, **observed):
    runtime, result = opening(database, strategy)
    venue = ExitVenue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "1.25" if strategy == "S6" else "-1.25",
        }
    ]
    assert (
        protection_stage(database, venue, allow_writes=True).run_once()["status"]
        == "CLEAR"
    )
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    runtime.execution.submit = adapter.submit
    runtime.execution.query = adapter.query
    runtime.risk_check = lambda _: True
    service = DirectionalExitStage(
        database,
        venue,
        runtime=runtime,
        scope=SCOPE,
        observe=lambda _: observation(now, **observed),
        clock_ms=lambda: now,
        reference=lambda _: {},
        allow_writes=allow_writes,
    )
    return runtime, result, venue, service


def test_wait_cycle_is_clear_and_persists_peak_evidence(database):
    _, result, venue, service = setup_exit(database)
    answer = service.run_once()
    episode = result["intent_id"]
    assert answer["status"] == "CLEAR", answer
    assert answer["exits"][episode]["status"] == "WAIT"
    assert not venue.order_writes
    saved = BusinessState(database).read(service._key(episode))
    assert saved is not None and saved.version == 1
    assert service.run_once()["status"] == "CLEAR"
    assert BusinessState(database).read(service._key(episode)).version == 1


def test_action_is_durable_but_never_posts_when_writes_disabled(database):
    _, result, venue, service = setup_exit(database, mark_price="105")
    answer = service.run_once()
    assert answer["status"] == "BLOCKED"
    assert answer["exits"][result["intent_id"]]["status"] == "ACTION_DISABLED", answer
    assert not venue.order_writes
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE leg='CLOSE'"
        ).fetchone() == (0,)


def test_partial_exit_dispatch_and_restart_recovery_never_duplicate_post(database):
    runtime, result, venue, service = setup_exit(
        database, allow_writes=True, mark_price="105"
    )
    first = service.run_once()
    episode = result["intent_id"]
    dispatched = first["exits"][episode]
    assert first["status"] == "BLOCKED"
    assert dispatched["status"] == "DISPATCHED", first
    assert dispatched["quantity"] == "0.625"
    assert dispatched["outcome"] == "ACKNOWLEDGED"
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == "SELL"
    assert venue.parent["algoStatus"] == "NEW"

    venue.close.update(status="FILLED", executedQty="0.625")
    venue.trades = [
        {
            "id": 901,
            "orderId": 900,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "qty": "0.625",
            "price": "105",
            "commission": "0.02625",
            "commissionAsset": "USDT",
            "time": 600004,
            "realizedPnl": "3.125",
        }
    ]
    venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "0.625"
    second = service.run_once()
    assert second["status"] == "CLEAR"
    assert second["exits"][episode]["status"] == "WAIT"
    assert len(venue.order_writes) == 1
    trace = runtime.data.trace(episode)
    close = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    assert close["status"] == "FILLED"
    assert close["request_key"] == "exit:partial-take-profit"
    assert (
        sum(
            float(fill["quantity"])
            for fill in trace["fills"]
            if fill["order_id"] == close["order_id"]
        )
        == 0.625
    )


@pytest.mark.parametrize(
    "strategy,now,observed,close_side,reason",
    [
        ("S6", 121 * 60000 + 3, {"mark_price": "99"}, "SELL", "TIME_STOP"),
        (
            "S8",
            600003,
            {"mark_price": "100", "funding_rate": "-.006"},
            "BUY",
            "ADVERSE_FUNDING",
        ),
    ],
)
def test_full_exit_uses_entire_remaining_quantity_and_inverse_side(
    database, strategy, now, observed, close_side, reason
):
    _, result, venue, service = setup_exit(
        database,
        strategy=strategy,
        allow_writes=True,
        now=now,
        **observed,
    )
    answer = service.run_once()["exits"][result["intent_id"]]
    assert answer["status"] == "DISPATCHED"
    assert answer["reason"] == reason
    assert answer["quantity"] == "1.250000000000000000"
    assert venue.order_writes[0]["side"] == close_side
    assert venue.order_writes[0]["reduceOnly"] == "true"


def test_stale_market_or_missing_remote_stop_blocks_without_close(database):
    _, _, venue, service = setup_exit(database)
    service.clock = lambda: 700003
    answer = service.run_once()
    assert answer["status"] == "BLOCKED"
    assert not venue.order_writes

    # A fresh cycle cannot treat an absent venue stop as permission to close.
    service.clock = lambda: 600003
    venue.rows["/fapi/v1/openAlgoOrders"] = []
    assert service.run_once()["status"] == "BLOCKED"
    assert not venue.order_writes


def test_busy_stage_makes_no_new_venue_calls(database):
    _, _, venue, service = setup_exit(database)
    venue.calls.clear()
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("directional-exit-stage:" + SCOPE.key,),
        )
        conn.commit()
        assert service.run_once() == {"status": "BLOCKED", "reason": "BUSY"}
    assert not venue.calls


def test_state_identity_is_account_and_episode_scoped(database):
    _, result, _, service = setup_exit(database)
    key = service._key(result["intent_id"])
    assert isinstance(key, StateKey)
    assert key.namespace == "directional-exit-v1"
    assert key.account_id == SCOPE.account_id


def test_live_scope_is_rejected_before_io(database):
    runtime, _, venue, _ = setup_exit(database)
    with pytest.raises(ValueError):
        DirectionalExitStage(
            database,
            venue,
            runtime=runtime,
            scope=SCOPE.__class__("BINANCE", SCOPE.account_id, "LIVE", "FUTURES"),
            observe=lambda _: {},
            clock_ms=lambda: 1,
            reference=lambda _: {},
        )
