"""Frozen new-plan protection through isolated PG and explicit fake venue ports.

No deployment activation, credentials, external requests or real account state
are used. Entry fences stay active; tests issue a valid opening before disabling
its strategy, then exercise only existing-exposure protection and reduction.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from test_v2_account_coverage import Account
from test_v2_directional_replay import SCOPE
from test_v2_strategy_deployment_execution import REGISTRY, binding, material, now

from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_protection import DirectionalStopRecovery
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy, AccountRisk, RiskReference
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.protection import ProtectionSpec
from v2_core.runtime_policy import PolicyStore
from v2_core.scoping import ScopeMismatch
from v2_core.service import TradingData
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_lifecycle import LIFECYCLE_EPISODE_SQL
from v2_core.strategy_plan_admission import StrategyPlanAdmission

database = database_fixture


class Venue(Account):
    def __init__(self):
        super().__init__()
        self.parents, self.writes = {}, []
        self.lose_response = False

    def __call__(self, method, path, params):
        if path == "/fapi/v1/algoOrder":
            identity = params["clientAlgoId"]
            if method == "POST":
                self.writes.append(deepcopy(params))
                self.parents[identity] = {
                    **params,
                    "orderType": params["type"],
                    "closePosition": True,
                    "priceProtect": False,
                    "algoId": len(self.parents) + 100,
                    "algoStatus": "NEW",
                }
                self.rows["/fapi/v1/openAlgoOrders"] = list(self.parents.values())
                if self.lose_response:
                    raise TimeoutError("private request details")
            assert method in {"GET", "POST"}
            return deepcopy(self.parents[identity])
        return super().__call__(method, path, params)


def new_opening(
    database,
    *,
    side="BUY",
    filled="0.01",
    symbol="BTCUSDT",
    registry=None,
    plan_changes=None,
):
    registry = REGISTRY if registry is None else registry
    original = binding()
    selected = replace(
        original,
        scope=SCOPE,
        instance_id="typed-" + symbol.lower(),
        spec=registry.specs[original.spec.identity],
    )
    _, plan, _, _ = material(
        selected=selected,
        plan_changes={
            "scope": SCOPE,
            "symbol": symbol,
            "side": side,
            "stop_price": "90" if side == "BUY" else "110",
            "exit_handler": selected.spec.exit_handler,
            **(plan_changes or {}),
        },
    )
    assert (
        StrategyDeployments(database, registry).publish(
            selected, expected_generation=0, request_id=str(uuid4())
        )["status"]
        == "APPLIED"
    )
    admitted = StrategyPlanAdmission(database, registry).admit(
        plan, selected, rationale="isolated protection lifecycle QA"
    )
    with database() as conn:
        configured = conn.execute(
            "SELECT 1 FROM v2_risk_accounts WHERE scope=%s", (SCOPE.key,)
        ).fetchone()
    if not configured:
        AccountRisk(database).configure(
            SCOPE,
            AccountPolicy("USDT", "1000", 10, 0, "qa", 60000),
            expected_version=0,
        )
        policy = PolicyStore(database, SCOPE)
        policy.patch(
            {
                "capital.enabled": True,
                "portfolio_risk.enabled": True,
                "sizing.pool_fraction": "0.70",
            },
            expected_version=policy.read().version,
            reason="isolated QA capital",
        )
    tick = now()
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "1000",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=tick,
        deadline=tick + 60000,
        observation_id=str(uuid4()),
    )
    data = TradingData(database)
    order, _ = data.orders.prepare(admitted["intent_id"])
    if filled is None:
        return selected, plan, admitted["intent_id"], order, data
    data.orders.transition(
        order,
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference("SANDBOX", symbol, "qa", "100", now()),
    )
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="entry",
        quantity=filled,
        price="100",
        fee="0.001",
        fee_currency="USDT",
        occurred_at_ms=plan.decided_at_ms + 1,
        evidence={"source": "explicit-QA-complete-fill-reconciliation"},
    )
    data.orders.transition(
        order,
        expected_version=2,
        status="FILLED" if Decimal(filled) == Decimal(plan.quantity) else "CANCELLED",
        exchange_order_id="10",
        evidence={"fills_complete": True, "executed_quantity": filled},
    )
    return selected, plan, admitted["intent_id"], order, data


def position(venue, *, symbol="BTCUSDT", side="BUY", quantity="0.01"):
    venue.rows["/fapi/v3/positionRisk"] = [
        {
            "symbol": symbol,
            "positionSide": "BOTH",
            "positionAmt": quantity if side == "BUY" else "-" + quantity,
        }
    ]


def service(database, venue, *, tick=None, allow_writes=False, limit=20):
    tick = now() if tick is None else tick
    return DirectionalProtectionStage(
        database,
        venue,
        scope=SCOPE,
        reference=lambda symbol: {
            "symbol": symbol,
            "environment": "SANDBOX",
            "mark_price": "100",
            "tick_size": "0.1",
            "observed_at_ms": tick,
        },
        clock_ms=lambda: tick,
        allow_writes=allow_writes,
        limit=limit,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("mode", ["OFF", "DRAINING"])
def test_original_stop_survives_entry_expiry_deployment_and_policy_changes(
    database, side, mode
):
    selected, plan, episode, order, _ = new_opening(database, side=side)
    StrategyDeployments(database, REGISTRY).publish(
        replace(selected, generation=2, mode=mode),
        expected_generation=1,
        request_id="disable-entry",
    )
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"stop.default": "0.04", "stop.max_default": "0.04"},
        expected_version=policy.read().version,
        reason="new entry config only",
    )
    venue = Venue()
    position(venue, side=side)
    worker = service(database, venue, tick=plan.expires_at_ms + 1, allow_writes=True)
    expected = ProtectionSpec(
        episode, plan.symbol, plan.exit_side, "STOP_MARKET", plan.stop_price
    )
    assert worker.stop.plan(order) == expected
    assert venue.calls == [] and venue.writes == []
    result = worker.run_once()
    assert result["status"] == "CLEAR", result
    assert result["stops"][order]["status"] == "NEW"
    assert result["execution_authorized"] is False
    assert venue.writes[0]["triggerPrice"] == plan.stop_price
    assert service(database, venue, allow_writes=True).run_once()["status"] == "CLEAR"
    assert len(venue.writes) == 1


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_partial_open_uses_full_plan_binding_but_protects_ledger_residual(
    database, side
):
    _, plan, _, order, _ = new_opening(database, side=side, filled="0.004")
    venue = Venue()
    position(venue, side=side, quantity="0.004")
    worker = service(database, venue, allow_writes=True)
    snapshot = worker.stop.recovery.cancel.snapshot(order)
    assert Decimal(snapshot["intent"]["quantity"]) == Decimal(plan.quantity)
    assert worker.run_once()["stops"][order]["status"] == "NEW"
    assert venue.writes[0]["closePosition"] == "true"
    assert "quantity" not in venue.writes[0]
    assert venue.writes[0]["triggerPrice"] == plan.stop_price


def test_partial_close_retains_original_stop_without_duplicate_post(database):
    _, plan, episode, order, data = new_opening(database)
    venue = Venue()
    position(venue)
    worker = service(database, venue, allow_writes=True)
    assert worker.run_once()["status"] == "CLEAR"
    closing, _ = data.orders.prepare(
        episode, leg="CLOSE", quantity="0.004", request_key="qa-reduction"
    )
    data.orders.transition(
        closing, expected_version=1, status="SUBMITTING", evidence={}
    )
    data.ledger.record_fill(
        order_id=closing,
        exchange_fill_id="partial-close",
        quantity="0.004",
        price="105",
        fee="0.001",
        fee_currency="USDT",
        occurred_at_ms=now(),
        evidence={"source": "QA"},
    )
    data.orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        exchange_order_id="11",
        evidence={"fills_complete": True},
    )
    position(venue, quantity="0.006")
    result = worker.run_once()
    assert result["status"] == "CLEAR", result
    assert result["stops"][order]["status"] == "NEW"
    assert worker.stop.plan(order).trigger_price == plan.stop_price
    assert len(venue.writes) == 1


@pytest.mark.parametrize(
    "field",
    [
        "scope",
        "side",
        "symbol",
        "intent_side",
        "intent_symbol",
        "intent_quantity",
        "order_quantity",
        "leg",
    ],
)
def test_scoped_order_projection_cannot_change_frozen_plan_binding(
    database, monkeypatch, field
):
    _, _, _, order, _ = new_opening(database)
    venue = Venue()
    worker = service(database, venue)
    original = worker.stop.recovery.cancel.snapshot(order)
    changed = deepcopy(original)
    if field == "scope":
        changed["account_id"] = "foreign"
    elif field == "intent_quantity":
        changed["intent"]["quantity"] = "0.004"
    elif field == "order_quantity":
        changed["quantity"] = "0.004"
    elif field.startswith("intent_"):
        changed["intent"][field.removeprefix("intent_")] = (
            "SELL" if field == "intent_side" else "ETHUSDT"
        )
    else:
        changed[field] = {"side": "SELL", "symbol": "ETHUSDT", "leg": "CLOSE"}[field]
    monkeypatch.setattr(worker.stop.recovery.cancel, "snapshot", lambda _: changed)
    with pytest.raises(ValueError, match="STRATEGY_STOP_EVIDENCE_MISMATCH"):
        worker.stop.plan(order)
    assert not venue.calls and not venue.writes


def test_foreign_service_rejected_before_market_or_protection_io(database):
    _, _, _, order, _ = new_opening(database)
    venue = Venue()
    other = replace(SCOPE, account_id="foreign")
    venue.account_id = other.account_id
    worker = DirectionalStopRecovery(
        database,
        venue,
        scope=other,
        reference=lambda _: pytest.fail("no reference"),
        clock_ms=now,
    )
    with pytest.raises(ScopeMismatch, match="ACCOUNT_SCOPE_MISMATCH"):
        worker.plan(order)
    assert not venue.calls and not venue.writes


@pytest.mark.parametrize("marker", [None, {}, {"schema_version": "bad"}])
def test_malformed_marker_never_falls_back_and_is_lifecycle_eligible(
    database, monkeypatch, marker
):
    # Persist immutable malformed input, not an illegal historical order edit.
    # The scoped order read is mocked because current admission correctly will
    # not create an OPEN for this corrupted historical evidence.
    _, _, evidence, intent = material(snapshot_changes={"strategy_plan": marker})
    evidence = DecisionEvidence(
        evidence.strategy_version, evidence.config_json, evidence.snapshot_json
    )
    assert TradingData(database).accept(intent, evidence).code is AdmissionCode.ACCEPTED
    with database() as conn:
        assert conn.execute(
            f"SELECT i.intent_id::text FROM v2_trade_intents i WHERE i.intent_id=%s AND {LIFECYCLE_EPISODE_SQL}",
            (intent.intent_id,),
        ).fetchone() == (intent.intent_id,)
    venue = Venue()
    worker = service(database, venue)
    monkeypatch.setattr(
        worker.stop.recovery.cancel,
        "snapshot",
        lambda _: {
            "episode_id": intent.intent_id,
            "leg": "OPEN",
            "symbol": "BTCUSDT",
            "side": "BUY",
            **asdict(SCOPE),
            "intent": {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01"},
        },
    )
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
        worker.stop.plan(str(uuid4()))
    assert not venue.calls and not venue.writes


def test_prepared_only_is_not_frozen_or_canceled_by_lifecycle_scan(database):
    _, _, _, order, _ = new_opening(database, filled=None)
    venue = Venue()
    result = service(database, venue, allow_writes=True).run_once()
    assert result["status"] == "CLEAR" and result["stops"] == {}
    assert not venue.writes
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (order,)
        ).fetchone() == ("PREPARED",)


def test_new_and_legacy_scan_share_cursor_and_one_poison_does_not_starve_other(
    database, monkeypatch
):
    _, _, _, typed, data = new_opening(database)
    # The account risk model is configured before either filled episode. This
    # explicit legacy fixture uses the same current capital reservation path;
    # it must not activate risk over a pre-existing unreserved legacy position.
    tick = now()
    proof = DecisionEvidence(
        "directional-admission-v2-1",
        canonical(
            {
                "mode": "TESTNET_ADMISSION_ONLY",
                "account_scope": asdict(SCOPE),
                "strategy": "S6",
            }
        ),
        canonical(
            {
                "symbol": "ETHUSDT",
                "observed_at": tick - 100,
                "decided_at": tick - 10,
                "expires_at_ms": tick + 60000,
                "source": "qa",
                "rationale": "explicit legacy scan fixture",
                "features": {
                    "evaluation": {
                        "market_plan": {
                            "leverage": 2,
                            "stop_fraction": "0.1",
                            "margin_mode": "ISOLATED",
                            "side": "LONG",
                        },
                        "sizing": {
                            "reason": "SIZED",
                            "quantity": "0.01",
                            "stop_price": "90",
                        },
                    }
                },
            }
        ),
    )
    legacy = OpenIntent(
        str(uuid4()),
        SCOPE.exchange,
        SCOPE.account_id,
        SCOPE.environment,
        SCOPE.product,
        "s6",
        str(uuid4()),
        "ETHUSDT",
        "BUY",
        "0.01",
        proof.strategy_version,
        proof.config_digest,
        proof.evidence_ref,
    )
    assert data.accept(legacy, proof).code is AdmissionCode.ACCEPTED
    legacy_order, _ = data.orders.prepare(legacy.intent_id)
    data.orders.transition(
        legacy_order,
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference("SANDBOX", "ETHUSDT", "qa", "100", tick),
    )
    data.ledger.record_fill(
        order_id=legacy_order,
        exchange_fill_id="legacy-entry",
        quantity="0.01",
        price="100",
        fee="0.001",
        fee_currency="USDT",
        occurred_at_ms=tick,
        evidence={"source": "explicit-QA"},
    )
    data.orders.transition(
        legacy_order,
        expected_version=2,
        status="FILLED",
        evidence={"fills_complete": True},
    )
    venue = Venue()
    worker = service(database, venue, limit=1)
    # Isolate scan fairness from deliberately unavailable account inventory.
    monkeypatch.setattr(
        worker.supervisor, "run_once", lambda **_: {"status": "SCANNED", "results": {}}
    )
    monkeypatch.setattr(
        worker.external_close,
        "run_once",
        lambda **_: {"status": "CLEAR", "results": []},
    )
    monkeypatch.setattr(
        worker.audit, "run_once", lambda: {"status": "ACCOUNT_COVERAGE_CLEAR"}
    )
    seen = []

    def ensure(order):
        seen.append(order)
        if order == typed:
            raise ValueError("STRATEGY_PLAN_INTENT_MISMATCH")
        return {"status": "NEW"}

    monkeypatch.setattr(worker.stop, "ensure", ensure)
    results = [worker.run_once() for _ in range(3)]
    assert set(seen[:2]) == {typed, legacy_order}
    assert seen[2] == seen[0]
    assert any(
        result["stops"].get(typed, {}).get("status") == "BLOCKED" for result in results
    )
    assert any(
        result["stops"].get(legacy_order, {}).get("status") == "NEW"
        for result in results
    )
    assert not venue.calls and not venue.writes


def test_ambiguous_stop_response_keeps_single_post_identity(database):
    _, _, _, order, _ = new_opening(database)
    venue = Venue()
    venue.lose_response = True
    position(venue)
    worker = service(database, venue, allow_writes=True)
    assert worker.run_once()["stops"][order]["status"] == "NEW"
    assert worker.run_once()["stops"][order]["status"] == "NEW"
    assert len(venue.writes) == 1


def test_flat_cleanup_exact_sum_is_independent_of_ambient_precision(
    database, monkeypatch
):
    _, _, episode, order, data = new_opening(database)
    venue = Venue()
    position(venue)
    worker = service(database, venue, allow_writes=True)
    assert worker.run_once()["status"] == "CLEAR"
    spec = worker.stop.plan(order)
    state_id = worker.stop.recovery.stop.protection.key(spec).identity
    trace = data.trace(episode)
    # A bounded fake read probes summation without bypassing immutable ledger
    # guards to insert corrupt real accounting facts. Both exact totals match;
    # ambient precision 2 would otherwise round different fill partitions.
    trace["orders"].append({"order_id": "qa-close", "leg": "CLOSE", "status": "FILLED"})
    trace["fills"] = [
        {"order_id": order, "quantity": "0.00333"},
        {"order_id": order, "quantity": "0.00333"},
        {"order_id": order, "quantity": "0.00334"},
        {"order_id": "qa-close", "quantity": "0.01"},
    ]
    monkeypatch.setattr(TradingData, "trace", lambda *_args, **_kwargs: deepcopy(trace))
    from v2_core.protection import TestnetProtection

    cancellations = []
    monkeypatch.setattr(
        TestnetProtection,
        "cancel_flat_once",
        lambda _self, target, **_kwargs: (
            cancellations.append(target) or {"status": "CANCELED"}
        ),
    )
    with localcontext() as ctx:
        ctx.prec = 2
        assert worker._cleanup_flat(state_id)["status"] == "CLEANED"
    assert cancellations == [spec]
