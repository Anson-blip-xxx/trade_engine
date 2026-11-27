"""Real guarded typed OPEN and management, using disposable PG and fake REST.

Admission, deployment, capital reservation, readiness, POST and fill recovery use
their actual adapters. No fabricated opening baseline or direct opening fill is
used, and no real transport or service activation is constructed.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_deployment_execution import binding, material, now
from test_v2_strategy_exit import CONFIG, REGISTRY
from test_v2_strategy_management_cycle import CycleVenue
from test_v2_strategy_protection import SCOPE, position
from test_v2_strategy_protection import service as protection_stage

from services.v2_directional_execution import bind_directional_venue_gate
from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core import entry_controls, liquidity_controls
from v2_core.account_risk import AccountPolicy, AccountRisk, RiskReference
from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.evidence import canonical
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import StrategyDeployments, deployment_snapshot
from v2_core.strategy_execution_context import execution_context
from v2_core.strategy_lifecycle import plan_provenance
from v2_core.strategy_plan_admission import (
    StrategyPlanAdmission,
    require_plan_admission,
)
from v2_core.venue_readiness import GuardedOpeningSubmit

database = database_fixture
PRICE_POLICY = (
    "entry.reentry_enabled",
    "entry.price_guard_enabled",
    "entry.max_adverse_price_fraction",
    "entry.max_stop_risk_increase",
)


@pytest.mark.parametrize("failure", ["validation", "submit"])
def test_reduction_validation_failure_is_unsent_but_post_failure_stays_ambiguous(
    failure,
):
    sent = []

    def validate(_):
        if failure == "validation":
            raise TimeoutError("private validation error")

    def submit(order):
        sent.append(order)
        raise TimeoutError("private submit error")

    gate = GuardedOpeningSubmit(
        lambda: pytest.fail("no database needed for callback boundary"),
        readiness=SimpleNamespace(scope=SCOPE),
        reference=lambda _: pytest.fail("no entry reference needed"),
        plan=lambda _: pytest.fail("no entry plan needed"),
        submit=submit,
        reduce_only_enabled=True,
        validate_reduction=validate,
    )
    order = {**asdict(SCOPE), "leg": "CLOSE", "reduce_only": True}
    if failure == "validation":
        with pytest.raises(SubmissionNotSent, match="REDUCE_ONLY_REVALIDATION_FAILED"):
            gate(order)
        assert not sent
    else:
        with pytest.raises(TimeoutError, match="private submit error"):
            gate(order)
        assert sent == [order]


class OpeningVenue(CycleVenue):
    """Single ordinary opening plus the existing native-stop/close fake."""

    def __init__(self):
        super().__init__()
        self.opening_posts, self.setting_posts = [], []
        self.opening, self.opening_fills = None, []
        self.lose_opening_response = False
        self.settings_outcome = "APPLY"
        self.before_opening = lambda: None
        self.rows["/fapi/v1/accountConfig"] = {
            "canTrade": True,
            "dualSidePosition": False,
            "multiAssetsMargin": False,
        }
        self.rows["/fapi/v1/symbolConfig"] = [
            {
                "symbol": "BTCUSDT",
                "leverage": 2,
                "marginType": "ISOLATED",
                "isAutoAddMargin": False,
                "maxNotionalValue": "1000",
            }
        ]

    def wallet(self, value):
        super().wallet(value)
        self.rows["/fapi/v3/account"].update(
            totalInitialMargin="0",
            assets=[
                {
                    "asset": "USDT",
                    "availableBalance": value,
                    "walletBalance": value,
                    "marginBalance": value,
                }
            ],
        )

    def __call__(self, method, path, params):
        if path == "/fapi/v1/symbolConfig":
            assert method == "GET" and params == {"symbol": "BTCUSDT"}
            self.calls.append(path)
            return deepcopy(self.rows[path])
        if method == "POST" and path in {
            "/fapi/v1/marginType",
            "/fapi/v1/leverage",
        }:
            self.setting_posts.append((path, deepcopy(params)))
            if self.settings_outcome != "IGNORE":
                field = "marginType" if path.endswith("marginType") else "leverage"
                self.rows["/fapi/v1/symbolConfig"][0][field] = params[field]
            if self.settings_outcome == "AMBIGUOUS":
                raise TimeoutError("fake settings response lost")
            return {"ok": True}
        if path == "/fapi/v1/order":
            if method == "POST" and params["reduceOnly"] == "false":
                self.before_opening()
                assert self.opening is None, "opening must never be sent twice"
                self.opening_posts.append(deepcopy(params))
                self.opening = {
                    "clientOrderId": params["newClientOrderId"],
                    "symbol": params["symbol"],
                    "side": params["side"],
                    "positionSide": "BOTH",
                    "type": params["type"],
                    "reduceOnly": False,
                    "origQty": params["quantity"],
                    "executedQty": "0",
                    "orderId": 10,
                    "status": "NEW",
                }
                if self.lose_opening_response:
                    raise TimeoutError("fake opening receipt lost after acceptance")
                return deepcopy(self.opening)
            if (
                method == "GET"
                and self.opening is not None
                and (
                    params.get("origClientOrderId") == self.opening["clientOrderId"]
                    or params.get("orderId") == self.opening["orderId"]
                )
            ):
                return deepcopy(self.opening)
        if path == "/fapi/v1/userTrades" and params["orderId"] == 10:
            assert method == "GET"
            return deepcopy(self.opening_fills)
        return super().__call__(method, path, params)


def readiness(database, opening):
    row = BusinessState(database).read(
        StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=opening)
    )
    return None if row is None else json.loads(row.payload_json)


def setup(
    database,
    *,
    side="BUY",
    policy_changes=None,
    plan_changes=None,
    context_change=None,
    enabled=True,
):
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {
            "capital.enabled": True,
            "portfolio_risk.enabled": True,
            "sizing.pool_fraction": "0.70",
            "settlement.cash_basis_version": 2,
            **(policy_changes or {}),
        },
        expected_version=0,
        reason="isolated typed opening QA",
    )
    original = binding()
    selected = replace(
        original,
        scope=SCOPE,
        instance_id="typed-opening",
        spec=REGISTRY.specs[original.spec.identity],
    )
    _, plan, _, _ = material(
        selected=selected,
        plan_changes={
            "scope": SCOPE,
            "side": side,
            "stop_price": "90" if side == "BUY" else "110",
            "exit_handler": selected.spec.exit_handler,
            "exit_config_json": canonical({**CONFIG, "max_holding_ms": 1000}),
            **(plan_changes or {}),
        },
    )
    context = execution_context(
        plan,
        entry_controls=entry_controls.read(database, SCOPE).snapshot(),
        liquidity_controls=liquidity_controls.read(database, SCOPE).snapshot(),
        policy={key: policy.read().values[key] for key in PRICE_POLICY},
    )
    features = {"execution": context}
    if context_change is not None:
        context_change(features)
    StrategyDeployments(database, REGISTRY).publish(
        selected, expected_generation=0, request_id="typed-opening-qa"
    )
    admitted = StrategyPlanAdmission(database, REGISTRY).admit(
        plan, selected, rationale="genuine guarded OPEN QA", features=features
    )
    AccountRisk(database).configure(
        SCOPE,
        AccountPolicy("USDT", "1000", 10, 0, "qa", 60000),
        expected_version=0,
    )
    tick = [now()]
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "1000",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=tick[0],
        deadline=tick[0] + 60000,
        observation_id=str(uuid4()),
    )
    venue = OpeningVenue()
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runtime = DataRuntime(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: True,
        risk_reference=lambda order: RiskReference(
            "SANDBOX", order["symbol"], "qa", "100", now()
        ),
        clock_ms=lambda: tick[0],
        scope=SCOPE,
    )
    opening, client = runtime.data.orders.prepare(admitted["intent_id"])
    reference = {
        "symbol": plan.symbol,
        "environment": "SANDBOX",
        "observed_at_ms": tick[0],
        "mark_price": "100",
    }
    gate = bind_directional_venue_gate(
        runtime,
        venue,
        mark_reference=lambda _: deepcopy(reference),
        enabled=enabled,
        reduce_only_enabled=True,
    )

    def before_opening():
        proof = readiness(database, opening)
        assert proof["status"] == "READY_FOR_GUARDED_SUBMISSION"
        assert proof["blockers"] == []
        assert proof["account_scope"] == asdict(SCOPE)
        assert proof["capital_budget"]["account_scope"] == asdict(SCOPE)
        assert proof["inventory"]["summary"]["positions"] == []
        assert proof["leverage"] == plan.leverage
        assert proof["margin_type"] == plan.margin_mode
        with database() as conn:
            assert conn.execute(
                "SELECT status FROM v2_orders WHERE order_id=%s", (opening,)
            ).fetchone() == ("SUBMITTING",)
            assert conn.execute(
                "SELECT status FROM v2_risk_reservations WHERE episode_id=%s",
                (admitted["intent_id"],),
            ).fetchone() == ("HELD",)
            assert (
                require_plan_admission(
                    conn,
                    admitted["intent_id"],
                    deployment=deployment_snapshot(selected),
                )["intent_id"]
                == admitted["intent_id"]
            )

    venue.before_opening = before_opening
    return SimpleNamespace(
        plan=plan,
        selected=selected,
        episode=admitted["intent_id"],
        opening=opening,
        client=client,
        venue=venue,
        runtime=runtime,
        gate=gate,
        reference=reference,
        tick=tick,
        policy=policy,
    )


def fill_opening(case):
    """Publish fake venue facts; only the actual query adapter records fills."""
    occurred = case.tick[0] + 1
    case.venue.opening.update(status="FILLED", executedQty=case.plan.quantity)
    case.venue.opening_fills = [
        {
            "id": 1,
            "orderId": 10,
            "symbol": case.plan.symbol,
            "side": case.plan.side,
            "positionSide": "BOTH",
            "qty": case.plan.quantity,
            "price": "100",
            "commission": ".001",
            "commissionAsset": "USDT",
            "time": occurred,
            "realizedPnl": "0",
        }
    ]
    position(case.venue, side=case.plan.side, quantity=case.plan.quantity)
    assert case.runtime.execution.recover(case.opening) == "FILLED"
    return occurred


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("mark", ["99.5", "100.5", "101"])
def test_typed_initial_reservation_covers_allowed_prepost_price_drift(
    database, side, mark
):
    case = setup(database, side=side)
    case.reference["mark_price"] = mark
    assert case.runtime.execution.dispatch(case.opening) == "ACKNOWLEDGED"
    assert len(case.venue.opening_posts) == 1
    with database() as conn:
        reserved = conn.execute(
            "SELECT reference->'capital'->>'reserved_margin' "
            "FROM v2_risk_reservations WHERE episode_id=%s",
            (case.episode,),
        ).fetchone()[0]
    assert Decimal(reserved) == Decimal("0.5555")
    assert Decimal(
        readiness(database, case.opening)["capital_budget"]["proposed_margin"]
    ) <= Decimal(reserved)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("mark", ["98.999999999999999999", "101.000000000000000001"])
def test_full_margin_reservation_does_not_authorize_outside_frozen_price_window(
    database, side, mark
):
    case = setup(database, side=side)
    case.reference["mark_price"] = mark
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts
    assert readiness(database, case.opening) is None


def test_typed_full_margin_envelope_must_fit_available_balance_before_post(database):
    case = setup(database)
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "0",
            "availableBalance": "0.552",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=case.tick[0],
        deadline=case.tick[0] + 60000,
        observation_id=str(uuid4()),
    )
    # The old reference-only .55 reservation fits; the complete .5555 does not.
    assert case.runtime.execution.dispatch(case.opening) == "DENIED"
    assert not case.venue.opening_posts
    assert not case.venue.setting_posts
    assert not case.venue.calls
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


def test_typed_full_margin_envelope_must_fit_seventy_percent_pool_before_post(database):
    case = setup(database)
    assert case.policy.read().values["capital.pool_fraction"] == "0.70"
    publish_capital_snapshot(
        database,
        SCOPE,
        account={
            "totalInitialMargin": "699.448",
            "availableBalance": "300.552",
            "totalWalletBalance": "1000",
            "totalMarginBalance": "1000",
        },
        started=case.tick[0],
        deadline=case.tick[0] + 60000,
        observation_id=str(uuid4()),
    )
    # Availability is ample. Only the full frozen envelope breaches the pool:
    # 699.448 + .55 <= 700, but 699.448 + .5555 > 700.
    assert case.runtime.execution.dispatch(case.opening) == "DENIED"
    assert not case.venue.opening_posts
    assert not case.venue.setting_posts
    assert not case.venue.calls
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


@pytest.mark.parametrize("side,close_price", [("BUY", "108"), ("SELL", "92")])
def test_guarded_open_recovery_management_and_financial_completion(
    database, side, close_price
):
    case = setup(database, side=side)
    venue, runtime, plan = case.venue, case.runtime, case.plan
    assert runtime.execution.dispatch(case.opening) == "ACKNOWLEDGED"
    assert venue.opening_posts[0]["reduceOnly"] == "false"
    assert venue.opening_posts[0]["side"] == side
    admitted_trace = runtime.data.trace(case.episode)
    assert admitted_trace["fills"] == []
    assert set(admitted_trace["decision"]["features"]) == {"execution"}
    opened_at = fill_opening(case)
    baseline = readiness(database, case.opening)
    assert baseline["inventory"]["finished_at_ms"] < opened_at
    assert runtime.data.trace(case.episode)["account_risk"]["status"] == "HELD"

    case.tick[0] = plan.decided_at_ms + 300000
    protected = protection_stage(
        database, venue, tick=case.tick[0], allow_writes=True
    ).run_once()
    assert protected["status"] == "CLEAR", protected
    assert protected["stops"][case.opening]["status"] == "NEW"
    assert venue.writes[0]["triggerPrice"] == plan.stop_price
    assert not plan.entry_valid_at(case.tick[0])
    StrategyDeployments(database, REGISTRY).publish(
        replace(case.selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="typed-opening-disable",
    )
    exits = DirectionalExitStage(
        database,
        venue,
        runtime=runtime,
        scope=SCOPE,
        observe=lambda _: {
            "symbol": plan.symbol,
            "environment": "SANDBOX",
            "observed_at_ms": case.tick[0],
            "mark_price": close_price,
            "exit_fee_rate": ".0004",
        },
        clock_ms=lambda: case.tick[0],
        reference=lambda _: {},
        allow_writes=True,
    )
    reduction = exits.run_once()["exits"][case.episode]
    assert reduction["status"] == "DISPATCHED", reduction
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == plan.exit_side
    assert venue.rows["/fapi/v1/openAlgoOrders"]
    closed_at = case.tick[0] + 1
    venue.close.update(status="FILLED", executedQty=plan.quantity)
    venue.trades = [
        {
            "id": 901,
            "orderId": 900,
            "symbol": plan.symbol,
            "side": plan.exit_side,
            "positionSide": "BOTH",
            "qty": plan.quantity,
            "price": close_price,
            "commission": ".001",
            "commissionAsset": "USDT",
            "time": closed_at,
            "realizedPnl": ".08",
        }
    ]
    assert runtime.execution.recover(reduction["order_id"]) == "FILLED"
    trace = runtime.data.trace(case.episode)
    closing = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    assert closing["request_evidence"]["strategy_plan"] == plan_provenance(plan)
    assert len(trace["fills"]) == 2
    assert trace["account_risk"]["status"] == "HELD"
    venue.rows["/fapi/v3/positionRisk"] = []
    cleanup = protection_stage(
        database, venue, tick=closed_at + 1, allow_writes=True
    ).run_once()
    assert cleanup["status"] == "CLEAR", cleanup
    assert len(venue.deletes) == 1
    assert venue.rows["/fapi/v1/openAlgoOrders"] == []
    venue.wallet("1000.078")
    venue.income = [
        {
            "symbol": plan.symbol,
            "asset": "USDT",
            "incomeType": kind,
            "income": value,
            "tradeId": trade,
            "tranId": number,
            "time": at,
        }
        for kind, value, trade, number, at in (
            ("COMMISSION", "-.001", "1", 1, opened_at),
            ("COMMISSION", "-.001", "901", 2, closed_at),
            ("REALIZED_PNL", ".08", "901", 3, closed_at),
        )
    ]
    settlement = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: closed_at + 70000
    )
    cash = settlement.cash.audit(case.episode)
    assert cash["status"] == "CASH_MATCHED", cash
    result = settlement.run_once()
    assert result["status"] == "CLEAR", result
    assert Decimal(result["settlements"][case.episode]["net_pnl"]) == Decimal(".078")
    assert result["outcomes"][case.episode]["plan_digest"] == plan.plan_digest
    assert runtime.data.trace(case.episode)["account_risk"]["status"] == "RELEASED"
    assert readiness(database, case.opening) == baseline
    assert settlement.run_once()["settlements"] == {}
    assert len(venue.opening_posts) == len(venue.order_writes) == len(venue.writes) == 1


def test_response_loss_recovers_original_open_after_deployment_off(database):
    case = setup(database)
    case.venue.lose_opening_response = True
    assert case.runtime.execution.dispatch(case.opening) == "UNKNOWN"
    baseline = readiness(database, case.opening)
    StrategyDeployments(database, REGISTRY).publish(
        replace(case.selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="off-after-lost-receipt",
    )
    assert case.runtime.data.orders.prepare(case.episode) == (case.opening, case.client)
    fill_opening(case)
    case.tick[0] = case.plan.expires_at_ms + 1
    assert case.runtime.execution.dispatch(case.opening) == "FILLED"
    assert len(case.venue.opening_posts) == 1
    assert readiness(database, case.opening) == baseline


@pytest.mark.parametrize(
    "side,mark",
    [("BUY", "101.01"), ("SELL", "98.99"), ("BUY", "98.99"), ("SELL", "101.01")],
)
def test_frozen_absolute_drift_is_mandatory_without_global_price_opt_in(
    database, side, mark
):
    case = setup(database, side=side)
    assert case.policy.read().values["entry.price_guard_enabled"] is False
    case.reference["mark_price"] = mark
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts
    assert readiness(database, case.opening) is None


@pytest.mark.parametrize("side,mark", [("BUY", "99.5"), ("SELL", "100.5")])
def test_crossing_frozen_stop_blocks_even_inside_price_drift(database, side, mark):
    case = setup(database, side=side, plan_changes={"stop_price": mark})
    case.reference["mark_price"] = mark
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts
    assert readiness(database, case.opening) is None


@pytest.mark.parametrize("frozen", [False, True])
def test_stricter_current_or_frozen_price_guard_is_preserved(database, frozen):
    tight = {
        "entry.price_guard_enabled": True,
        "entry.max_adverse_price_fraction": ".001",
    }
    case = setup(database, policy_changes=tight if frozen else None)
    case.policy.patch(
        {"entry.price_guard_enabled": False} if frozen else tight,
        expected_version=case.policy.read().version,
        reason="change current price policy",
    )
    case.reference["mark_price"] = "100.2"
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts
    # A later capital-budget rejection must not masquerade as a price guard.
    assert readiness(database, case.opening) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"mark_price": "0"},
        {"mark_price": "NaN"},
        {"symbol": "ETHUSDT"},
        {"environment": "PRODUCTION"},
        {"observed_at_ms": 0},
        {"observed_at_ms": True},
    ],
)
def test_invalid_fresh_reference_never_posts(database, changes):
    case = setup(database)
    case.reference.update(changes)
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts


@pytest.mark.parametrize("outcome", ["APPLY", "AMBIGUOUS", "IGNORE"])
def test_frozen_symbol_terms_require_observed_readback(database, outcome):
    case = setup(database)
    case.venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=20, marginType="CROSSED"
    )
    case.venue.settings_outcome = outcome
    result = case.runtime.execution.dispatch(case.opening)
    assert case.venue.setting_posts
    if outcome == "IGNORE":
        assert result == "REJECTED" and not case.venue.opening_posts
    else:
        assert result == "ACKNOWLEDGED"
        assert len(case.venue.opening_posts) == 1
        cfg = case.venue.rows["/fapi/v1/symbolConfig"][0]
        assert (cfg["leverage"], cfg["marginType"]) == (
            case.plan.leverage,
            case.plan.margin_mode,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"client_order_id": "v2unregistered"},
        {"quantity": ".005"},
        {"side": "SELL"},
        {"symbol": "ETHUSDT"},
        {"account_id": "other-account"},
        {"environment": "PRODUCTION"},
        {"leg": "CLOSE"},
        {"reduce_only": True},
        {"leg": "CLOSE", "reduce_only": True},
        {"order_type": "LIMIT", "limit_price": "99", "time_in_force": "GTC"},
        {"limit_price": "99"},
        {"time_in_force": "IOC"},
        {"request_evidence": {"reason": "forged request"}},
        {"exchange_order_id": "123"},
        {"order_id": "00000000-0000-0000-0000-000000000000"},
        {"intent": {"intent_id": "00000000-0000-0000-0000-000000000000"}},
    ],
)
def test_forged_order_is_rejected_before_settings_or_order_writes(database, changes):
    case = setup(database)
    snapshot = case.runtime.execution.snapshot(case.opening)
    case.runtime.data.orders.transition(
        case.opening,
        expected_version=1,
        status="SUBMITTING",
        risk_reference=RiskReference("SANDBOX", case.plan.symbol, "qa", "100", now()),
        evidence={"source": "honest CAS before injected snapshot mismatch"},
    )
    case.venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=20, marginType="CROSSED"
    )
    with pytest.raises(SubmissionNotSent):
        case.gate({**snapshot, **changes})
    assert not case.venue.opening_posts
    assert not case.venue.order_writes
    assert not case.venue.setting_posts
    assert readiness(database, case.opening) is None


def test_deployment_off_after_cas_denies_initial_post_before_settings(database):
    case = setup(database)
    snapshot = case.runtime.execution.snapshot(case.opening)
    case.runtime.data.orders.transition(
        case.opening,
        expected_version=1,
        status="SUBMITTING",
        risk_reference=RiskReference("SANDBOX", case.plan.symbol, "qa", "100", now()),
        evidence={"source": "committed permit before deployment stop"},
    )
    StrategyDeployments(database, REGISTRY).publish(
        replace(case.selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="off-before-initial-post",
    )
    case.venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=20, marginType="CROSSED"
    )
    with pytest.raises(SubmissionNotSent):
        case.gate(snapshot)
    assert not case.venue.opening_posts
    assert not case.venue.setting_posts
    assert readiness(database, case.opening) is None


@pytest.mark.parametrize("failure", ["write", "expiry"])
def test_readiness_failure_cannot_reach_opening_post(database, monkeypatch, failure):
    case = setup(database)
    if failure == "write":
        original = BusinessState.change

        def fail_readiness(self, key, **kwargs):
            if key.namespace == "opening-readiness-v1":
                raise RuntimeError("fake readiness commit failure")
            return original(self, key, **kwargs)

        monkeypatch.setattr(BusinessState, "change", fail_readiness)
    else:
        original_inspect = case.gate.readiness.inspect

        def expired(**kwargs):
            proof = original_inspect(**kwargs)
            case.tick[0] = proof["deadline_ms"]
            return proof

        case.gate.readiness.inspect = expired
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts


def test_disabled_open_gate_has_no_venue_reads_or_posts(database):
    case = setup(database, enabled=False)
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.calls
    assert not case.venue.opening_posts
    assert not case.venue.setting_posts


@pytest.mark.parametrize("defect", ["absent", "null", "digest", "account"])
def test_missing_or_unbound_execution_context_cannot_submit(database, defect):
    def change(features):
        if defect == "absent":
            features.clear()
        elif defect == "null":
            features["execution"] = None
        elif defect == "digest":
            features["execution"]["plan_digest"] = "0" * 64
        else:
            features["execution"]["account_scope"]["account_id"] = "wrong-account"

    case = setup(database, context_change=change)
    assert case.runtime.execution.dispatch(case.opening) == "REJECTED"
    assert not case.venue.opening_posts
    assert not case.venue.setting_posts
