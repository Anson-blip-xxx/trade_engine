"""Real-PG cross-component acceptance for the position-safety deadlock."""

from dataclasses import asdict, replace

import pytest
from test_v2_external_close_recovery import (
    SCOPE,
    database,
    original_setup,
    setup,
)
from test_v2_unprotected_emergency import naked

from services.v2_directional_lifecycle import DirectionalProtectionStage
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.external_close_recovery import ExternalCloseRecovery
from v2_core.protection import TestnetProtection
from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.transport import ExchangeTransportError

__all__ = ["database", "original_setup", "setup"]


def test_full_stage_recovers_absent_unknown_parent_and_external_liquidation(
    database, setup
):
    data, spec, venue, _ = setup

    def request(method, path, params):
        if path == "/fapi/v1/algoOrder":
            assert method == "GET"
            raise ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2013)
        return venue(method, path, params)

    request.account_id, request.environment = SCOPE.account_id, SCOPE.environment
    stage = DirectionalProtectionStage(
        database, request, scope=SCOPE, clock_ms=lambda: 100,
        reference=lambda _: pytest.fail("External close recovery has no market dependency"),
        allow_writes=False,
    )
    journal = stage.supervisor.child.protection
    snapshot, payload = journal.read(spec)
    assert journal.save(spec, snapshot, {
        **payload, "status": "UNKNOWN",
        "submission_error": {"class": "ExchangeTransportError"},
    })
    result = stage.run_once()
    assert result["status"] == "CLEAR", result
    assert result["external_closes"]["results"][0]["status"] == "RECONCILED"
    assert all(
        item["status"] == "TERMINAL_FLAT_LEDGER"
        for item in result["recovery"]["results"].values()
    )
    assert stage.run_once()["status"] == "CLEAR"
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    close = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    assert close["request_evidence"]["origin"] == "BINANCE_EXTERNAL_CLOSE"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_liquidation_adoption_clears_protection_and_coverage_without_second_close(
    database, setup
):
    data, spec, venue, recovery = setup
    assert recovery.run_once()["results"][0]["status"] == "RECONCILED"
    supervisor = ProtectionSupervisor(database, venue, scope=SCOPE)
    state_id = supervisor.child.protection.key(spec).identity
    before = len(venue.calls)
    result = supervisor.run_once()
    assert result["results"][state_id]["status"] == "TERMINAL_FLAT_LEDGER"
    assert len(venue.calls) == before  # No futile GET of the missing parent.
    audit = AccountCoverageAudit(database, venue, scope=SCOPE, clock_ms=lambda: 100)
    assert audit.run_once()["status"] == "ACCOUNT_COVERAGE_CLEAR"
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    # Process restart preserves the same evidence, not a replacement CLOSE.
    assert ExternalCloseRecovery(
        database, venue, scope=SCOPE, clock_ms=lambda: 100
    ).run_once()["results"] == []
    assert supervisor.run_once()["results"][state_id]["status"] == "TERMINAL_FLAT_LEDGER"
    assert len(data.trace(spec.episode)["fills"]) == 2
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("strategy,close_side", [("S6", "SELL"), ("S8", "BUY")])
def test_emergency_fill_to_terminal_protection_then_both_stages_clear(
    database, strategy, close_side
):
    runtime, opened, venue, exits = naked(
        database, allow_writes=True, strategy=strategy
    )
    first = exits.run_once()
    emergency = first["emergency_exits"][opened["intent_id"]]
    assert first["status"] == "BLOCKED"
    assert emergency["outcome"] == "ACKNOWLEDGED"
    assert len(venue.order_writes) == 1
    venue.close.update(status="FILLED", executedQty="1.25")
    venue.trades = [{
        "id": 901, "orderId": 900, "symbol": "BTCUSDT", "side": close_side,
        "positionSide": "BOTH", "qty": "1.25", "price": "99",
        "commission": "0.0495", "commissionAsset": "USDT", "time": 600004,
        "realizedPnl": "-1.25" if close_side == "SELL" else "1.25",
    }]
    venue.rows["/fapi/v3/positionRisk"] = []
    assert runtime.execution.recover(emergency["order_id"]) == "FILLED"
    scope = exits.scope
    recovery = ExternalCloseRecovery(database, venue, scope=scope, clock_ms=lambda: 600005)
    assert recovery.run_once()["results"][0]["status"] == "RECONCILED"
    spec = exits.stop.plan(opened["order_id"])
    state_id = exits.protection.key(spec).identity
    recovered = ProtectionSupervisor(database, venue, scope=scope).run_once()
    assert recovered["results"][state_id]["status"] == "TERMINAL_FLAT_LEDGER"
    assert exits.run_once()["status"] == "CLEAR"
    # Rebuild the protection stage to catch orchestration / marker mismatch.
    protections = DirectionalProtectionStage(
        database, venue, scope=scope, clock_ms=lambda: 600005,
        reference=lambda _: pytest.fail("Flat cleanup must not need market data"),
        allow_writes=True,
    )
    assert protections.run_once()["status"] == "CLEAR"
    assert protections.run_once()["status"] == "CLEAR"
    assert len(venue.order_writes) == 1
    assert len(venue.writes) == 1  # Original stop only, never recreated.
    trace = runtime.data.trace(opened["intent_id"])
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2


def test_take_profit_is_not_substitute_for_missing_protective_stop(database):
    _, opened, venue, exits = naked(database, allow_writes=True)
    original = exits.stop.plan(opened["order_id"])
    spec = replace(original, kind="TAKE_PROFIT_MARKET", trigger_price="120")
    worker = TestnetProtection(exits.store, venue, scope=exits.scope)
    observation = {
        **worker.params(spec), "orderType": spec.kind, "algoId": 31,
        "algoStatus": "NEW", "closePosition": True, "priceProtect": False,
    }
    assert worker.save(spec, None, {
        "spec": asdict(spec), "status": "NEW", "observation": observation,
    })
    venue.rows["/fapi/v1/openAlgoOrders"] = [observation]
    result = exits.run_once()
    assert result["coverage"]["blockers"] == ["MISSING_CONFIRMED_STOP"]
    assert result["emergency_exits"][opened["intent_id"]]["outcome"] == "ACKNOWLEDGED"
    assert venue.order_writes[0]["reduceOnly"] == "true"
