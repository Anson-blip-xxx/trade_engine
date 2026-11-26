"""New immutable plans use real journal contracts and fake read-only venue I/O."""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_v2_strategy_deployment_execution as fixtures
from test_v2_account_coverage import Account

from services.v2_directional_cash import DirectionalCashAudit
from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import RiskReference
from v2_core.evidence import canonical
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.runtime_policy import PolicyStore
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_lifecycle import plan_provenance, strategy_plan_from_trace
from v2_core.strategy_outcomes import StrategyOutcomeJournal
from v2_core.strategy_plan_admission import StrategyPlanAdmission

database = database_fixture
SCOPE = fixtures.SCOPE


def closed_plan(
    database, *, side="BUY", partial=False, funding=True, maintenance=False
):
    selected = fixtures.binding()
    fixtures.publish(database, selected)
    fixtures.risk(database)
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"settlement.cash_basis_version": 2},
        expected_version=policy.read().version,
        reason="qa new strategy cash",
    )
    _, plan, _, _ = fixtures.material(
        selected=selected,
        plan_changes={"side": side, "stop_price": "90" if side == "BUY" else "110"},
    )
    accepted = StrategyPlanAdmission(database, fixtures.REGISTRY).admit(
        plan, selected, rationale="qa lifecycle"
    )
    episode = accepted["intent_id"]
    orders, ledger, data = Orders(database), Ledger(database), TradingData(database)
    opening, _ = orders.prepare(episode)
    tick = fixtures.now()
    orders.transition(
        opening,
        expected_version=1,
        status="SUBMITTING",
        evidence={"source": "qa"},
        risk_reference=RiskReference("SANDBOX", plan.symbol, "qa", "100", tick),
    )
    quantity = Decimal(".005" if partial else ".01")
    ledger.record_fill(
        order_id=opening,
        exchange_fill_id="1",
        quantity=str(quantity),
        price="100",
        fee=".001",
        fee_currency="USDT",
        occurred_at_ms=tick + 3,
        evidence={
            "source": "binance-userTrades",
            "trade_id": "1",
            "venue_realized_pnl": "0",
        },
    )
    if partial:
        orders.transition(
            opening,
            expected_version=2,
            status="ACKNOWLEDGED",
            exchange_order_id="10",
            evidence={"source": "qa-binance-order-query", "status": "PARTIALLY_FILLED"},
        )
        orders.transition(
            opening,
            expected_version=3,
            status="CANCELLED",
            exchange_order_id="10",
            evidence={"fills_complete": True},
        )
    else:
        orders.transition(
            opening,
            expected_version=2,
            status="FILLED",
            exchange_order_id="10",
            evidence={"fills_complete": True},
        )
    income = [
        {
            "incomeType": "COMMISSION",
            "income": "-.001",
            "tradeId": "1",
            "tranId": 1,
            "time": tick + 3,
        }
    ]
    for number, fraction in ((2, Decimal(".4")), (3, Decimal(".6"))):
        qty = quantity * fraction
        price = "108" if side == "BUY" else "92"
        gross = qty * Decimal(8)
        request_key = "qa-close-" + str(number)
        request_evidence = {"source": "strategy-exit-v1"}
        if maintenance:
            # A separately confirmed external close is adopted through the
            # actual maintenance-binding validator, not an origin-only bypass.
            case, _ = orders.prepare(
                episode,
                leg="CLOSE",
                quantity=str(qty),
                request_key="qa-maintenance-case-" + str(number),
            )
            orders.transition(
                case,
                expected_version=1,
                status="CANCELLED",
                evidence={"source": "qa-cancel-unsent-case"},
            )
            params = {
                "symbol": plan.symbol,
                "side": "SELL" if side == "BUY" else "BUY",
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": "true",
                "quantity": str(qty),
                "newClientOrderId": "qa-maintenance-" + str(number),
            }
            action = {"key": "close", "kind": "CLOSE", "params": params}
            raw = {
                "symbol": plan.symbol,
                "side": params["side"],
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": True,
                "status": "FILLED",
                "clientOrderId": params["newClientOrderId"],
                "orderId": 10 + number,
                "origQty": str(qty),
                "executedQty": str(qty),
            }
            for suffix, payload in (
                ("plan", {"symbol": plan.symbol, "actions": [action]}),
                (
                    "close",
                    {"status": "CONFIRMED", "action": action, "proof": {"order": raw}},
                ),
            ):
                BusinessState(database).change(
                    StateKey(
                        **asdict(SCOPE),
                        namespace="testnet-scoped-cleanup-v1",
                        key=case + ":" + suffix,
                    ),
                    expected_version=0,
                    request_key="fixture",
                    payload=payload,
                    reason="QA_CONFIRMED_MAINTENANCE",
                )
            request_key = "maintenance:" + case
            request_evidence = {
                "origin": "APPROVED_TESTNET_MAINTENANCE",
                "case_id": case,
                "exchange_order_id": str(raw["orderId"]),
                "venue_client_order_id": raw["clientOrderId"],
            }
        closing, _ = orders.prepare(
            episode,
            leg="CLOSE",
            quantity=str(qty),
            request_key=request_key,
            evidence=request_evidence,
        )
        orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
        ledger.record_fill(
            order_id=closing,
            exchange_fill_id=str(number),
            quantity=str(qty),
            price=price,
            fee=str(Decimal(".001") * fraction),
            fee_currency="USDT",
            occurred_at_ms=tick + number * 10,
            evidence={
                "source": "binance-userTrades",
                "trade_id": str(number),
                "venue_realized_pnl": str(gross),
            },
        )
        orders.transition(
            closing,
            expected_version=2,
            status="FILLED",
            exchange_order_id=str(10 + number),
            evidence={"fills_complete": True},
        )
        income.extend(
            [
                {
                    "incomeType": "COMMISSION",
                    "income": str(-Decimal(".001") * fraction),
                    "tradeId": str(number),
                    "tranId": number * 10,
                    "time": tick + number * 10,
                },
                {
                    "incomeType": "REALIZED_PNL",
                    "income": str(gross),
                    "tradeId": str(number),
                    "tranId": number * 10 + 1,
                    "time": tick + number * 10,
                },
            ]
        )
    if funding:
        income.append(
            {
                "incomeType": "FUNDING_FEE",
                "income": "-.0001",
                "tradeId": "",
                "tranId": 50,
                "time": tick + 5,
            }
        )
    net = quantity * Decimal(8) - Decimal(".002") - (Decimal(".0001") if funding else 0)

    class Venue(Account):
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __init__(self):
            super().__init__()
            self.income = deepcopy(income)
            self.income_calls = 0
            self.rows["/fapi/v3/account"] = {
                name: str(Decimal(1000) + net)
                for name in (
                    "totalWalletBalance",
                    "availableBalance",
                    "totalMarginBalance",
                )
            }

        def __call__(self, method, path, params):
            if path == "/fapi/v1/income":
                assert method == "GET"
                self.income_calls += 1
                return [
                    {"symbol": plan.symbol, "asset": "USDT", **row}
                    for row in deepcopy(self.income)
                ]
            return super().__call__(method, path, params)

    baseline = {
        "observation_id": str(uuid4()),
        "account_scope": asdict(SCOPE),
        "started_at_ms": tick - 2,
        "finished_at_ms": tick - 1,
        "status": "CLEAR_FOR_RECOVERY_CHECKS",
        "execution_authorized": False,
        "blockers": [],
        "failures": {},
        "summary": {"positions": [], "ordinary_orders": [], "conditional_orders": []},
        "responses": {
            "account": {"totalWalletBalance": "1000"},
            "positions_before": [],
            "positions_after": [],
            "ordinary_orders": [],
            "conditional_orders": [],
            "mode": {"dualSidePosition": False},
            "margin_mode": {"multiAssetsMargin": False},
        },
        "response_digests": {},
    }
    BusinessState(database).change(
        StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=opening),
        expected_version=0,
        request_key="qa-opening",
        reason="QA_OPENING_BASELINE",
        payload={
            "status": "READY_FOR_GUARDED_SUBMISSION",
            "blockers": [],
            "inventory": baseline,
        },
    )
    venue = Venue()
    stage = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: tick + 70000
    )
    return SimpleNamespace(
        episode=episode,
        plan=plan,
        selected=selected,
        venue=venue,
        stage=stage,
        data=data,
        net=net,
        tick=tick,
        quantity=quantity,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("partial", [False, True])
def test_new_plan_scanned_cash_settled_and_outcome_without_legacy_identity(
    database, side, partial
):
    case = closed_plan(database, side=side, partial=partial)
    result = case.stage.run_once()
    assert result["status"] == "CLEAR", result
    from services.v2_trading_pipeline import _stage_clear

    assert _stage_clear("settlement", case.stage, result)
    assert result["settlements"][case.episode]["status"] == "SETTLED", result
    trace = case.data.trace(case.episode)
    assert trace["account_risk"]["status"] == "RELEASED"
    assert trace["scope"]["producer"] == case.plan.instance_id
    proof = trace["settlements"][0]["evidence"]
    assert proof["source"] == "strategy-settlement-v1"
    assert proof["strategy_plan"] == plan_provenance(case.plan)
    assert Decimal(proof["net_pnl"]) == case.net
    assert "precision_reconciliation" not in proof
    assert (
        case.stage.strategy_outcomes.record(case.episode)
        == result["outcomes"][case.episode]
    )
    assert "quality_score" not in result["outcomes"][case.episode]
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_directional_outcomes"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_cash_adjustments WHERE kind='CORRECTION'"
        ).fetchone() == (0,)
    payload = json.loads(
        BusinessState(database)
        .read(
            StateKey(**asdict(SCOPE), namespace="strategy-outcome-v1", key=case.episode)
        )
        .payload_json
    )
    assert Decimal(payload["opening_quantity"]) == case.quantity
    assert payload["side"] == side and payload["strategy_learning_eligible"] is True
    assert case.stage.run_once()["settlements"] == {}


def test_old_deployment_and_expired_entry_do_not_prevent_settlement(database):
    case = closed_plan(database)
    StrategyDeployments(database, fixtures.REGISTRY).publish(
        replace(case.selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id=str(uuid4()),
    )
    assert case.stage.clock() > case.plan.expires_at_ms
    assert case.stage.run_once()["settlements"][case.episode]["status"] == "SETTLED"


def test_new_plan_requires_explicit_v2_cash_policy(database):
    case = closed_plan(database)
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"settlement.cash_basis_version": 1},
        expected_version=policy.read().version,
        reason="qa unsupported policy",
    )
    result = case.stage.run_once()
    assert (
        result["settlements"][case.episode]["error_code"]
        == "STRATEGY_CASH_BASIS_V2_REQUIRED"
    )
    assert case.data.trace(case.episode)["settlements"] == []


@pytest.mark.parametrize("defect", ["wallet", "income", "scope"])
def test_new_cash_uncertainty_never_releases_reserved_risk(database, defect):
    case = closed_plan(database)
    if defect == "wallet":
        case.venue.rows["/fapi/v3/account"]["totalWalletBalance"] = "999"
    elif defect == "income":
        case.venue.income.pop(0)
    else:
        case.stage.scope = replace(SCOPE, account_id="other-account")
        with pytest.raises(ValueError, match="SCOPE"):
            case.stage.settle(case.episode)
        return
    result = case.stage.run_once()
    assert result["status"] == "BLOCKED"
    trace = case.data.trace(case.episode)
    assert trace["account_risk"]["status"] == "HELD"
    assert trace["settlements"] == trace["cash"] == []


def test_funding_and_settlement_rollback_then_retry(database, monkeypatch):
    case = closed_plan(database)
    original = Ledger.settle

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("qa settlement rollback")

    monkeypatch.setattr(Ledger, "settle", crash)
    assert case.stage.run_once()["status"] == "BLOCKED"
    trace = case.data.trace(case.episode)
    assert trace["settlements"] == trace["cash"] == trace["income_allocations"] == []
    assert trace["account_risk"]["status"] == "HELD"
    monkeypatch.setattr(Ledger, "settle", original)
    assert case.stage.run_once()["status"] == "CLEAR"
    assert len(case.data.trace(case.episode)["cash"]) == 1


def test_outcome_crash_is_retried_without_resettlement(database, monkeypatch):
    case = closed_plan(database)
    original = case.stage.strategy_outcomes.record
    monkeypatch.setattr(
        case.stage.strategy_outcomes,
        "record",
        lambda _: (_ for _ in ()).throw(ValueError("QA_ANALYSIS")),
    )
    first = case.stage.run_once()
    assert first["status"] == "CLEAR"
    assert first["outcomes"][case.episode]["status"] == "BLOCKED"
    assert case.data.trace(case.episode)["account_risk"]["status"] == "RELEASED"
    monkeypatch.setattr(case.stage.strategy_outcomes, "record", original)
    second = case.stage.run_once()
    assert second["settlements"] == {}
    assert second["outcomes"][case.episode]["outcome_schema"] == "strategy-outcome-v1"
    outcome = BusinessState(database).read(
        StateKey(**asdict(SCOPE), namespace="strategy-outcome-v1", key=case.episode)
    )
    assert json.loads(outcome.payload_json)["strategy_learning_eligible"] is True


def test_confirmed_maintenance_close_is_not_strategy_learning(database):
    case = closed_plan(database, maintenance=True)
    result = case.stage.run_once()
    assert result["status"] == "CLEAR", result
    assert result["outcomes"][case.episode]["outcome_schema"] == "strategy-outcome-v1"
    outcome = BusinessState(database).read(
        StateKey(**asdict(SCOPE), namespace="strategy-outcome-v1", key=case.episode)
    )
    assert json.loads(outcome.payload_json)["strategy_learning_eligible"] is False


@pytest.mark.parametrize(
    "reason,forged_digest",
    [
        ("QA_UNVERIFIED", False),
        ("VERIFIED_STRATEGY_OUTCOME", False),
        ("VERIFIED_STRATEGY_OUTCOME", True),
    ],
)
def test_invalid_version_one_outcome_is_diagnosed_not_skipped(
    database, monkeypatch, reason, forged_digest
):
    case = closed_plan(database)
    original = case.stage.strategy_outcomes.record
    monkeypatch.setattr(
        case.stage.strategy_outcomes,
        "record",
        lambda _: (_ for _ in ()).throw(ValueError("QA_CRASH")),
    )
    assert case.stage.run_once()["status"] == "CLEAR"
    monkeypatch.setattr(case.stage.strategy_outcomes, "record", original)
    key = StateKey(**asdict(SCOPE), namespace="strategy-outcome-v1", key=case.episode)
    payload = {"episode_id": case.episode}
    if forged_digest:
        payload.update(
            source="strategy-outcome-v1",
            outcome_schema="strategy-outcome-v1",
            settlement_revision=case.data.trace(case.episode)["settlements"][0][
                "revision"
            ],
            plan_digest="0" * 64,
            strategy_plan={**plan_provenance(case.plan), "plan_digest": "0" * 64},
        )
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="qa-unverified-outcome",
        payload=payload,
        reason=reason,
    )
    attempted = case.stage.run_once()
    assert attempted["settlements"] == {}
    assert attempted["outcomes"][case.episode] == {
        "status": "BLOCKED",
        "error_code": "STRATEGY_OUTCOME_CONFLICT",
    }
    assert len(case.data.trace(case.episode)["settlements"]) == 1


@pytest.mark.parametrize(
    "defect", ["source", "plan_digest", "instance", "legacy_basis"]
)
def test_ledger_rejects_forged_new_plan_settlement_provenance(database, defect):
    case = closed_plan(database)
    assert case.stage.run_once()["status"] == "CLEAR"
    proof = deepcopy(case.data.trace(case.episode)["settlements"][0]["evidence"])
    if defect == "source":
        proof["source"] = "directional-settlement-v1"
    elif defect == "legacy_basis":
        proof.pop("cash_basis")
    else:
        proof["strategy_plan"][
            "plan_digest" if defect == "plan_digest" else "instance_id"
        ] = "forged"
    with pytest.raises(ValueError, match="STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH"):
        Ledger(database).settle(case.episode, currency="USDT", evidence=proof)


def test_new_cash_stage_scans_and_keeps_match_nonterminal(database):
    case = closed_plan(database)
    result = DirectionalCashAudit(
        database, case.venue, scope=SCOPE, clock_ms=case.stage.clock
    ).run_once()
    assert result["status"] == "BLOCKED"
    assert result["audits"][case.episode]["source"] == "strategy-cash-audit-v1"
    assert case.data.trace(case.episode)["settlements"] == []


def test_pure_trace_binding_cannot_fall_back_from_malformed_marker(database):
    case = closed_plan(database)
    trace = case.data.trace(case.episode)
    assert strategy_plan_from_trace(trace) == case.plan
    trace["scope"]["producer"] = "s6"
    trace["configuration"] = {"mode": "TESTNET_ADMISSION_ONLY"}
    trace["decision"]["strategy_plan"] = None
    with pytest.raises(ValueError, match="STRATEGY_LIFECYCLE_PLAN_MISMATCH"):
        strategy_plan_from_trace(trace)


def test_outcome_is_exact_under_low_ambient_precision(database):
    case = closed_plan(database)
    assert case.stage.run_once()["status"] == "CLEAR"
    with localcontext() as context:
        context.prec = 6
        result = StrategyOutcomeJournal(database, scope=SCOPE).record(case.episode)
    assert Decimal(result["return_pct"]) == case.net * 100
    canonical(result)


def test_strategy_preflight_witness_is_bound_not_skipped(database):
    from test_v2_strategy_exit import setup

    from services.v2_directional_cash import _entry_observations

    _, plan, episode, _, data, _, exits = setup(database, mark="123")
    assert exits.run_once()["exits"][episode]["status"] == "DISPATCHED"
    trace = data.trace(episode)
    close = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    with database() as conn:
        observations = _entry_observations(conn, plan.scope, trace)
    witness = observations[close["order_id"]]
    assert witness["exit_state"]["source"] == "strategy-exit-v1"
    assert witness["exit_state"]["strategy_plan"] == plan_provenance(plan)
    assert (
        witness["inventory"]["observation_id"]
        == close["request_evidence"]["preflight_inventory"]
    )
    close["request_evidence"]["strategy_plan"]["plan_digest"] = "0" * 64
    with (
        database() as conn,
        pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"),
    ):
        _entry_observations(conn, plan.scope, trace)


def test_outcome_scan_rotates_past_poison_without_authoritative_cursor(monkeypatch):
    import services.v2_directional_settlement as module

    identifiers = ["00000000-0000-4000-8000-00000000000" + str(n) for n in (1, 2, 3)]

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params):
            if "AND e.status='SETTLED'" in sql:
                after, limit = params[-2:]
                return Result(
                    [
                        (item,)
                        for item in sorted(identifiers, key=lambda i: (i <= after, i))[
                            :limit
                        ]
                    ]
                )
            return Result([])

    stage = DirectionalSettlementStage.__new__(DirectionalSettlementStage)
    stage.connect = lambda: nullcontext(Connection())
    stage.scope, stage.limit, stage._outcome_after = SCOPE, 1, ""
    stage._settlement_after = ""
    seen = []

    def outcome(episode):
        seen.append(episode)
        if episode != identifiers[-1]:
            raise ValueError("QA_POISON_OUTCOME")
        return {"episode_id": episode}

    stage._record_outcome = outcome
    monkeypatch.setattr(
        module, "session_advisory_lock", lambda *args: nullcontext(True)
    )
    for _ in range(3):
        assert stage.run_once()["settlements"] == {}
    assert seen == identifiers
    stage.run_once()
    assert seen[-1] == identifiers[0]
