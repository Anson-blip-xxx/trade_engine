import json
from dataclasses import replace

import pytest
from test_v2_account_risk import prepare
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent
from test_v2_maintenance_reconciliation import maintenance as maintenance_fixture
from test_v2_protection_child import SCOPE
from test_v2_protection_child import setup as setup_fixture

from v2_core.account_risk import AccountRiskDenied
from v2_core.maintenance_reconciliation import MaintenanceCloseReconciler
from v2_core.maintenance_resolution import MaintenanceResolution

database = database_fixture
setup = setup_fixture
maintenance = maintenance_fixture


class HistoryVenue:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, venue, cid):
        self.venue, self.cid = venue, cid
        self.position = []
        self.history = [
            {
                "id": 1,
                "orderId": 111,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "qty": "0.01",
                "price": "100",
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": 1,
            },
            *venue.trades,
        ]

    def __call__(self, method, path, params):
        assert method == "GET"
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("positionRisk"):
            return self.position
        if path.endswith(("openOrders", "openAlgoOrders")):
            return []
        if path.endswith("/order") and params.get("origClientOrderId") == self.cid:
            return {"code": -2013}
        if path.endswith("userTrades") and "startTime" in params:
            return self.history
        return self.venue(method, path, params)


def prepared(database, maintenance):
    data, spec, venue, case = maintenance
    MaintenanceCloseReconciler(database, venue, scope=SCOPE).reconcile(
        spec.episode, case
    )
    cid = next(
        o["client_order_id"]
        for o in data.trace(spec.episode)["orders"]
        if o["order_id"] == case
    )
    remote = HistoryVenue(venue, cid)
    worker = MaintenanceResolution(
        database, remote, scope=SCOPE, runner_stopped=lambda: True, clock_ms=lambda: 10
    )
    return data, spec, remote, case, worker


def test_local_resolution_preserves_unknown_event_and_quarantines_symbol(
    database, maintenance
):
    data, spec, _, case, worker = prepared(database, maintenance)
    result = worker.resolve(case)
    assert result["venue_outcome"] == "UNKNOWN"
    assert worker.resolve(case) == result
    trace = data.trace(spec.episode)
    assert (
        next(o for o in trace["orders"] if o["order_id"] == case)["status"]
        == "RECONCILED"
    )
    with database() as conn:
        history = conn.execute(
            "SELECT status FROM v2_order_events WHERE order_id=%s ORDER BY version",
            (case,),
        ).fetchall()
    assert [r[0] for r in history] == [
        "PREPARED",
        "SUBMITTING",
        "UNKNOWN",
        "RECONCILED",
    ]
    # Another intent cannot reuse the quarantined symbol, even before settlement.
    other = intent()
    data.accept(other, evidence())
    with pytest.raises(AccountRiskDenied, match="SYMBOL_OPENING_QUARANTINED"):
        data.orders.prepare(other.intent_id)
    # Other symbols are not quarantined.
    prepare(database, symbol="ETHUSDT")


def test_missing_history_keeps_unknown_and_rolls_back_resolution(database, maintenance):
    data, spec, remote, case, worker = prepared(database, maintenance)
    remote.history = remote.history[1:]
    with pytest.raises(ValueError, match="TRADE_HISTORY_LEDGER_MISMATCH"):
        worker.resolve(case)
    assert (
        next(o for o in data.trace(spec.episode)["orders"] if o["order_id"] == case)[
            "status"
        ]
        == "UNKNOWN"
    )


def test_nonflat_cannot_resolve(database, maintenance):
    _, _, remote, case, worker = prepared(database, maintenance)
    remote.position = [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]
    with pytest.raises(ValueError, match="NOT_FLAT"):
        worker.resolve(case)


def test_direct_transition_without_certificate_is_forbidden(database, maintenance):
    data, _, _, case = maintenance
    with pytest.raises(ValueError, match="EVIDENCE_MISSING"):
        data.orders.transition(
            case, expected_version=3, status="RECONCILED", evidence={"pretend": True}
        )


def test_running_daemon_forbids_resolution(database, maintenance):
    _, _, _, case, worker = prepared(database, maintenance)
    worker.runner_stopped = lambda: False
    with pytest.raises(ValueError, match="NOT_STOPPED"):
        worker.resolve(case)


def test_late_real_fill_rejected_as_unmatched_history(database, maintenance):
    _, _, remote, case, worker = prepared(database, maintenance)
    remote.history.append({**remote.history[-1], "id": 3, "orderId": 666})
    with pytest.raises(ValueError, match="TRADE_HISTORY_LEDGER_MISMATCH"):
        worker.resolve(case)


def test_quarantined_signal_is_durably_rejected_not_retried(database, maintenance):
    from v2_core.evidence import DecisionEvidence, canonical
    from v2_core.runtime import DataRuntime

    _, _, _, case, worker = prepared(database, maintenance)
    worker.resolve(case)
    proof = DecisionEvidence(
        "strategy-v1",
        evidence().config_json,
        canonical(dict(json.loads(evidence().snapshot_json), expires_at_ms=10)),
    )
    request = replace(
        intent(), evidence_ref=proof.evidence_ref, config_digest=proof.config_digest
    )
    runtime = DataRuntime(
        database,
        submit=lambda _: pytest.fail("must not submit"),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=lambda: 5,
    )
    result = runtime.accept_open(request, proof)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "SYMBOL_OPENING_QUARANTINED"
    assert runtime.accept_open(request, proof)["status"] == "REJECTED"
    assert runtime.data.trace(request.intent_id)["orders"] == []
