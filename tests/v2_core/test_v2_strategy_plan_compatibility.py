"""Real isolated-PG admission evidence through the legacy plan projection.

The existing guarded fixture owns the database. Venue responses are explicit
scenario fakes; neither these tests nor the adapter contact an external service.
"""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_account_coverage import Account
from test_v2_directional_admission import formal
from test_v2_directional_exit_stage import observation, setup_exit
from test_v2_directional_lifecycle import opening
from test_v2_directional_replay import SCOPE, scenario

from services.v2_directional_protection import DirectionalStopRecovery
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.protection import ProtectionSpec
from v2_core.runtime_policy import PolicyStore
from v2_core.scoping import ScopeMismatch
from v2_core.state import BusinessState
from v2_core.strategy_plan import LegacyDirectionalPlan

database = database_fixture


def stored_evidence(database, order_id):
    """Read actual immutable admission records; never synthesize plan evidence."""
    with database() as conn:
        version, config, snapshot, producer, reference, config_hash = conn.execute(
            """SELECT e.strategy_version,e.config,e.snapshot,i.producer,
            e.evidence_ref,e.config_digest FROM v2_orders o
            JOIN v2_trade_intents i ON i.intent_id=o.episode_id
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE o.order_id=%s""",
            (order_id,),
        ).fetchone()
    proof = DecisionEvidence(version, canonical(config), canonical(snapshot))
    assert proof.evidence_ref == reference
    assert proof.config_digest == config_hash
    return proof, producer


def read_plan(proof, producer, *, scope=SCOPE):
    return LegacyDirectionalPlan.read(
        scope=scope,
        config=json.loads(proof.config_json),
        snapshot=json.loads(proof.snapshot_json),
        producer=producer,
        strategy_version=proof.strategy_version,
    )


def recovery(database, *, scope=SCOPE):
    venue = Account()
    venue.account_id = scope.account_id
    service = DirectionalStopRecovery(
        database,
        venue,
        scope=scope,
        reference=lambda _: pytest.fail("plan reads do not fetch new market data"),
        clock_ms=lambda: 1000,
    )
    return service, venue


@pytest.mark.parametrize(
    "strategy,side,stop", [("S6", "SELL", "92"), ("S8", "BUY", "108")]
)
def test_real_admission_plan_keeps_original_stop_after_expiry_and_policy_change(
    database, strategy, side, stop
):
    _, runtime, signal, context = scenario(database, strategy)
    admission = formal(runtime, strategy)
    result = admission.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    proof, producer = stored_evidence(database, result["order_id"])
    service, venue = recovery(database)
    order = service.recovery.cancel.snapshot(result["order_id"])
    expected = ProtectionSpec(result["intent_id"], "BTCUSDT", side, "STOP_MARKET", stop)

    snapshot = json.loads(proof.snapshot_json)
    assert snapshot["expires_at_ms"] < service.recovery.stop.clock()
    context["directional"]["price"] = "1"
    context["directional"]["sizing"]["available_margin"] = "0"
    runtime.clock_ms = lambda: 1000
    policy = PolicyStore(database, SCOPE)
    changed = policy.patch(
        {"stop.default": "0.04", "stop.max_default": "0.04"},
        expected_version=policy.read().version,
        reason="isolated QA changes current policy, not committed stop",
    )
    assert changed.values["stop.default"] == "0.04"

    restored_proof, restored_producer = stored_evidence(database, result["order_id"])
    projected = read_plan(restored_proof, restored_producer)
    assert restored_proof == proof
    assert restored_producer == producer == strategy.lower()
    assert (
        projected.protection_spec(order) == service.plan(result["order_id"]) == expected
    )
    assert projected.exit_policy_binding == "LEGACY_CURRENT_ACCOUNT_POLICY"
    assert projected.execution_authorized is False
    assert not venue.calls


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_partially_filled_opening_preserves_admitted_quantity_in_stop_identity(
    database, strategy
):
    runtime, result = opening(database, strategy, filled=False)
    order_id = result["order_id"]
    assert (
        runtime.data.orders.transition(
            order_id, expected_version=1, status="SUBMITTING", evidence={}
        )
        is True
    )
    runtime.data.ledger.record_fill(
        order_id=order_id,
        exchange_fill_id="partial-entry",
        quantity="0.5",
        price="100",
        fee="0.02",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={"source": "isolated-QA-confirmed-partial-fill"},
    )
    # The fake venue's final reconciliation confirms this single 0.5 fill is
    # its complete fill set; the unfilled 0.75 remainder was cancelled. A bare
    # cancellation response alone must never authorize this terminal transition.
    assert (
        runtime.data.orders.transition(
            order_id,
            expected_version=2,
            status="CANCELLED",
            exchange_order_id="10",
            evidence={
                "source": "isolated-QA-complete-fill-reconciliation",
                "fills_complete": True,
                "exchange_fill_ids": ["partial-entry"],
                "executed_quantity": "0.5",
                "unfilled_quantity_cancelled": "0.75",
            },
        )
        is True
    )
    service, venue = recovery(database)
    order = service.recovery.cancel.snapshot(order_id)
    assert (order["status"], order["version"]) == ("CANCELLED", 3)
    proof, producer = stored_evidence(database, order_id)
    plan = read_plan(proof, producer)
    trace = runtime.data.trace(result["intent_id"])
    assert sum(Decimal(fill["quantity"]) for fill in trace["fills"]) == Decimal("0.5")
    assert order["intent"]["quantity"] == "1.25"
    assert json.loads(plan.projection_json)["sizing"]["quantity"] == "1.25"
    assert plan.protection_spec(order) == service.plan(order_id)

    # Exposure sizing belongs to the ledger/installer. Replacing the admitted
    # quantity with the residual would change the immutable entry identity.
    confused = deepcopy(order)
    confused["intent"]["quantity"] = "0.5"
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_MISMATCH"):
        plan.protection_spec(confused)
    assert not venue.calls


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_unregistered_directional_version_is_not_adopted_from_real_evidence(
    database, strategy
):
    _, runtime, signal, context = scenario(database, strategy)
    admission = formal(runtime, strategy)
    # Use the real evaluator and persistence under an intentionally unsupported
    # label. The immutable record is never edited to fake the negative case.
    admission.strategy_version = "directional-admission-unknown"
    result = admission.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    proof, producer = stored_evidence(database, result["order_id"])
    service, venue = recovery(database)
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        read_plan(proof, producer)
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        service.plan(result["order_id"])
    assert not venue.calls


def test_foreign_account_cannot_adopt_a_valid_persisted_directional_plan(database):
    _, result = opening(database, filled=False)
    proof, producer = stored_evidence(database, result["order_id"])
    other = replace(SCOPE, account_id="another-test-account")
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        read_plan(proof, producer, scope=other)
    service, venue = recovery(database, scope=other)
    with pytest.raises(ScopeMismatch, match="ACCOUNT_SCOPE_MISMATCH"):
        service.plan(result["order_id"])
    assert not venue.calls


@pytest.mark.parametrize(
    "strategy,mark,funding", [("S6", "101", "0.001"), ("S8", "99", "-0.001")]
)
def test_same_exit_instance_uses_current_policy_without_changing_original_stop(
    database, strategy, mark, funding
):
    _, result, venue, exits = setup_exit(
        database, strategy=strategy, mark_price=mark, funding_rate=funding
    )
    episode = result["intent_id"]
    proof, producer = stored_evidence(database, result["order_id"])
    plan = read_plan(proof, producer)
    order = exits.stop.recovery.cancel.snapshot(result["order_id"])
    original_stop = plan.protection_spec(order)
    first = exits.run_once()
    assert first["status"] == "CLEAR", first
    assert first["exits"][episode]["status"] == "WAIT"

    policies = PolicyStore(database, SCOPE)
    changed = policies.patch(
        {"exit.adverse_funding": "0.0005", "stop.default": "0.04"},
        expected_version=policies.read().version,
        reason="isolated QA preserves legacy dynamic software exit policy",
    )
    second = exits.run_once()
    assert second["exits"][episode]["status"] == "ACTION_DISABLED", second
    saved = json.loads(BusinessState(database).read(exits._key(episode)).payload_json)
    assert saved["decision"]["reason"] == "ADVERSE_FUNDING"
    assert saved["policy_version"] == changed.version
    assert saved["policy_digest"] == changed.digest
    assert exits.stop.plan(result["order_id"]) == original_stop
    assert stored_evidence(database, result["order_id"])[0] == proof
    assert not venue.order_writes


def test_partial_close_recovery_keeps_original_plan_and_uses_remaining_exposure(
    database,
):
    runtime, result, venue, exits = setup_exit(
        database, allow_writes=True, mark_price="105.1"
    )
    episode, opening_order = result["intent_id"], result["order_id"]
    proof, producer = stored_evidence(database, opening_order)
    plan = read_plan(proof, producer)
    original_stop = plan.protection_spec(
        exits.stop.recovery.cancel.snapshot(opening_order)
    )
    first = exits.run_once()
    assert first["exits"][episode]["status"] == "DISPATCHED", first
    assert first["exits"][episode]["quantity"] == "0.625"

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
    exits.clock = lambda: 600005
    exits.observe = lambda _: observation(600005, mark_price="105.1")
    second = exits.run_once()
    assert second["status"] == "CLEAR", second
    assert second["exits"][episode]["status"] == "WAIT"
    assert len(venue.order_writes) == 1

    saved = json.loads(BusinessState(database).read(exits._key(episode)).payload_json)
    assert Decimal(saved["facts"]["remaining_quantity"]) == Decimal("0.625")
    assert json.loads(plan.projection_json)["sizing"]["quantity"] == "1.25"
    assert exits.stop.plan(opening_order) == original_stop
    assert stored_evidence(database, opening_order)[0] == proof
    trace = runtime.data.trace(episode)
    close = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    assert close["status"] == "FILLED"
