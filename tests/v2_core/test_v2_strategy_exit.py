"""Pure frozen exits and real journal/fake venue reduction integration."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from test_v2_strategy_deployment_execution import material
from test_v2_strategy_protection import SCOPE, Venue, new_opening, position
from test_v2_strategy_protection import service as protection_stage

from services.v2_directional_exit import DirectionalExitStage
from tests.support.v2_admission import database as database_fixture
from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_exit import (
    EXIT_HANDLER,
    EXIT_SCHEMA,
    StrategyExitFacts,
    evaluate_strategy_exit,
    exit_settings,
)
from v2_core.strategy_plan import PLAN_SCHEMA
from v2_core.strategy_registry import StrategyRegistry, StrategySpec

database = database_fixture
CONFIG = {
    "schema": EXIT_SCHEMA,
    "partial_take_profit_r": "2",
    "partial_fraction": "0.3",
    "trail_activation_r": "3",
    "trail_giveback_r": "1",
    "max_holding_ms": 0,
}
REGISTRY = StrategyRegistry(
    [StrategySpec("structure", "v1", PLAN_SCHEMA, "entry-v1", EXIT_HANDLER)],
    entry_handlers=["entry-v1"],
    exit_handlers=[EXIT_HANDLER],
    plan_schemas=[PLAN_SCHEMA],
)


def pure(*, side="BUY", **changes):
    _, plan, _, _ = material(
        plan_changes={
            "side": side,
            "stop_price": "90" if side == "BUY" else "110",
            "exit_handler": EXIT_HANDLER,
            "exit_config_json": canonical(CONFIG),
        }
    )
    facts = StrategyExitFacts(
        side,
        "100",
        "101" if side == "BUY" else "99",
        plan.stop_price,
        ".01",
        ".11",
        1000,
        "0",
        False,
        ".0004",
    )
    return plan, replace(facts, **changes)


@pytest.mark.parametrize(
    "side,mark,expected",
    [
        ("BUY", "101", "WAIT"),
        ("SELL", "99", "WAIT"),
        ("BUY", "123", "PARTIAL"),
        ("SELL", "77", "PARTIAL"),
        ("BUY", "90", "WAIT_NATIVE_STOP"),
        ("SELL", "110", "WAIT_NATIVE_STOP"),
    ],
)
def test_frozen_r_handler(side, mark, expected):
    plan, facts = pure(side=side, mark_price=mark)
    result = evaluate_strategy_exit(plan, facts)
    assert result.action == expected
    assert result.quantity_fraction == ("0.3" if expected == "PARTIAL" else "0")


@pytest.mark.parametrize("side,mark", [("BUY", "130"), ("SELL", "70")])
def test_trail_has_priority_over_new_partial_and_is_context_independent(side, mark):
    plan, facts = pure(side=side, mark_price=mark, peak_return_pct="50")
    expected = evaluate_strategy_exit(plan, facts)
    assert expected.reason == "PLAN_R_TRAIL"
    with localcontext() as context:
        context.prec = 3
        assert evaluate_strategy_exit(plan, facts) == expected


def test_partial_fill_risk_allocates_full_plan_not_filled_fraction():
    plan, facts = pure(remaining_quantity=".005", planned_loss=".055", mark_price="123")
    assert evaluate_strategy_exit(plan, facts).action == "PARTIAL"
    with pytest.raises(ValueError, match="RISK_BINDING"):
        evaluate_strategy_exit(plan, replace(facts, planned_loss=".11"))


def test_partial_dust_waits_without_invalid_order_and_max_duration_is_explicit():
    plan, facts = pure(remaining_quantity=".001", planned_loss=".011", mark_price="123")
    assert evaluate_strategy_exit(plan, facts).reason == "PARTIAL_BELOW_QUANTITY_STEP"
    plan = replace(plan, exit_config_json=canonical({**CONFIG, "max_holding_ms": 1000}))
    assert evaluate_strategy_exit(plan, facts).reason == "PLAN_MAX_HOLDING"
    plan = replace(plan, exit_config_json=canonical(CONFIG))
    assert (
        evaluate_strategy_exit(plan, replace(facts, held_ms=100000000)).action == "WAIT"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "unknown"},
        {"partial_fraction": "1"},
        {"partial_fraction": "0"},
        {"trail_giveback_r": "3"},
        {"trail_activation_r": "NaN"},
        {"max_holding_ms": True},
        {"max_holding_ms": -1},
        {"unknown": "1"},
    ],
)
def test_bad_frozen_config_rejected(changes):
    plan, _ = pure()
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        exit_settings(replace(plan, exit_config_json=canonical({**CONFIG, **changes})))


@pytest.mark.parametrize(
    "changes",
    [
        {"exit_handler": "unregistered"},
        {"exit_version": "v2"},
    ],
)
def test_exit_version_is_a_closed_allowlist(changes):
    plan, facts = pure()
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        evaluate_strategy_exit(replace(plan, **changes), facts)


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
        if path == "/fapi/v1/userTrades":
            return deepcopy(self.trades)
        return super().__call__(method, path, params)


def setup(
    database, *, side="BUY", filled=".01", mark="101", protected=True, changes=None
):
    selected, plan, episode, order, data = new_opening(
        database,
        side=side,
        filled=filled,
        registry=REGISTRY,
        plan_changes={"exit_config_json": canonical(CONFIG), **(changes or {})},
    )
    venue = ExitVenue()
    position(venue, side=side, quantity=filled)
    tick = plan.decided_at_ms + 300000
    if protected:
        assert (
            protection_stage(database, venue, tick=tick, allow_writes=True).run_once()[
                "status"
            ]
            == "CLEAR"
        )
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runtime = DataRuntime(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: True,
        clock_ms=lambda: tick,
        scope=SCOPE,
    )
    service = DirectionalExitStage(
        database,
        venue,
        runtime=runtime,
        scope=SCOPE,
        observe=lambda _: {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "observed_at_ms": tick,
            "mark_price": mark,
            "exit_fee_rate": ".0004",
        },
        clock_ms=lambda: tick,
        reference=lambda _: {},
        allow_writes=True,
    )
    return selected, plan, episode, order, data, venue, service


@pytest.mark.parametrize("side,mark", [("BUY", "123"), ("SELL", "77")])
@pytest.mark.parametrize("mode", ["OFF", "DRAINING"])
def test_actual_scan_dispatches_frozen_partial_after_entry_expiry_and_off(
    database, side, mark, mode
):
    selected, plan, episode, _, data, venue, service = setup(
        database, side=side, mark=mark
    )
    assert not plan.entry_valid_at(service.clock())
    StrategyDeployments(database, REGISTRY).publish(
        replace(selected, generation=2, mode=mode),
        expected_generation=1,
        request_id=str(uuid4()),
    )
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"exit.S6A.partial_pct": "99"},
        expected_version=policy.read().version,
        reason="irrelevant legacy change",
    )
    result = service.run_once()
    assert result["exits"][episode]["status"] == "DISPATCHED", result
    assert result["exits"][episode]["quantity"] == "0.003"
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == plan.exit_side
    close = next(r for r in data.trace(episode)["orders"] if r["leg"] == "CLOSE")
    assert close["request_evidence"]["source"] == "strategy-exit-v1"
    assert close["request_evidence"]["strategy_plan"]["plan_digest"] == plan.plan_digest
    service.run_once()
    assert len(venue.order_writes) == 1


def test_actual_partial_fill_uses_remaining_and_preserves_original_budget(database):
    _, plan, episode, _, _, venue, service = setup(database, filled=".005", mark="123")
    result = service.run_once()
    assert result["exits"][episode]["quantity"] == "0.001", result
    state = json.loads(BusinessState(database).read(service._key(episode)).payload_json)
    assert Decimal(state["facts"]["planned_loss"]) == Decimal(".055")
    assert state["exit_config"] == CONFIG
    assert state["strategy_plan"]["plan_digest"] == plan.plan_digest
    assert venue.order_writes[0]["quantity"] == "0.001"


def test_unprotected_escape_uses_new_plan_step_even_when_exit_version_unsupported(
    database,
):
    _, _, episode, _, _, venue, service = setup(
        database, protected=False, changes={"exit_version": "future"}
    )
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {
            "protection.emergency_exit_enabled": True,
            "protection.unconfirmed_grace_ms": 0,
        },
        expected_version=policy.read().version,
        reason="QA emergency reduction",
    )
    result = service.run_once()
    assert result["emergency_exits"][episode]["status"] == "DISPATCHED", result
    assert venue.order_writes[0]["quantity"] == "0.01"
    assert venue.order_writes[0]["reduceOnly"] == "true"
    service.run_once()
    assert len(venue.order_writes) == 1


def test_unsupported_normal_exit_does_not_remove_native_stop_or_post(database):
    _, _, episode, _, _, venue, service = setup(
        database, changes={"exit_version": "future"}
    )
    result = service.run_once()
    assert result["exits"][episode]["status"] == "BLOCKED", result
    assert len(venue.parents) == 1
    assert not venue.order_writes


def test_read_only_stage_records_wait_without_legacy_features(database):
    _, _, episode, _, _, venue, service = setup(database)
    service.allow_writes = False
    result = service.run_once()
    assert result["exits"][episode]["status"] == "WAIT", result
    assert not venue.order_writes
    assert service.run_once()["status"] == "CLEAR"


def test_stale_observation_blocks_software_exit_but_leaves_native_stop(database):
    _, _, episode, _, _, venue, service = setup(database)
    observe = service.observe
    service.observe = lambda symbol: {**observe(symbol), "observed_at_ms": 0}
    result = service.run_once()
    assert result["exits"][episode]["status"] == "BLOCKED"
    assert len(venue.parents) == 1
    assert not venue.order_writes


def test_partially_filled_take_profit_cancel_is_not_sold_again(database):
    _, plan, episode, _, _, venue, service = setup(database, mark="123")
    first = service.run_once()["exits"][episode]
    assert first["status"] == "DISPATCHED"
    venue.close.update(status="CANCELED", executedQty="0.001")
    venue.trades = [
        {
            "id": 901,
            "orderId": 900,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "qty": ".001",
            "price": "123",
            "commission": ".0000492",
            "commissionAsset": "USDT",
            "time": service.clock() - 1,
            "realizedPnl": ".023",
        }
    ]
    assert service.runtime.execution.recover(first["order_id"]) == "CANCELLED"
    position(venue, quantity=".009")
    tick = service.clock() + 1000
    old_observe = service.observe
    service.observe = lambda symbol: {**old_observe(symbol), "observed_at_ms": tick}
    service.clock = lambda: tick
    result = service.run_once()
    assert result["exits"][episode]["status"] == "WAIT", result
    assert len(venue.order_writes) == 1
    saved = json.loads(BusinessState(database).read(service._key(episode)).payload_json)
    assert saved["partial_done"] is True
    assert Decimal(saved["facts"]["remaining_quantity"]) == Decimal(".009")
    assert saved["strategy_plan"]["plan_digest"] == plan.plan_digest
