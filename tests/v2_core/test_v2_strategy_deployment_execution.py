"""Opening permits are fenced; historical management and recovery are not.

Only disposable PostgreSQL and fake exchange ports are used. These are core
contract tests, not activation or full new-strategy lifecycle acceptance.
"""

import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import opened
from v2_core.account_risk import (
    AccountPolicy,
    AccountRisk,
    AccountRiskDenied,
    AccountScope,
    RiskReference,
)
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.runner import ExchangeObservation, ExecutionRunner
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.service import TradingData
from v2_core.strategy_deployment import StrategyDeployments, deployment_snapshot
from v2_core.strategy_plan import PLAN_SCHEMA, TradePlan
from v2_core.strategy_plan_admission import StrategyPlanAdmission
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.strategy_registry import StrategyRegistry, StrategySpec

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-entry-fence", "SANDBOX", "FUTURES")
SPEC = StrategySpec("structure", "v1", PLAN_SCHEMA, "entry-v1", "exit-v1")
REGISTRY = StrategyRegistry(
    [SPEC],
    entry_handlers=["entry-v1"],
    exit_handlers=["exit-v1"],
    plan_schemas=[PLAN_SCHEMA],
)


def now():
    return time.time_ns() // 1000000


def binding(*, config=None):
    return REGISTRY.bind(
        scope=SCOPE,
        instance_id="structure-a",
        strategy_id="structure",
        code_version="v1",
        config=config
        if config is not None
        else {
            "risk": "0.01",
            "capital": {
                "schema": "strategy-capital-v1",
                "roundtrip_cost_fraction": "0.002",
                "price_drift_fraction": "0.01",
            },
        },
        generation=1,
        mode="ACTIVE",
    )


def publish(database, selected):
    return StrategyDeployments(database, REGISTRY).publish(
        selected, expected_generation=selected.generation - 1, request_id=str(uuid4())
    )


def material(
    *, selected=None, plan_changes=None, snapshot_changes=None, intent_changes=None
):
    selected = selected or binding()
    tick = now()
    plan = TradePlan(
        **{
            "scope": SCOPE,
            "instance_id": selected.instance_id,
            "strategy_id": SPEC.strategy_id,
            "code_version": SPEC.code_version,
            "deployment_generation": selected.generation,
            "config_digest": selected.config_digest,
            "opportunity_id": str(uuid4()),
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.01",
            "entry_price": "100",
            "stop_price": "90",
            "planned_loss": "0.11",
            "quantity_step": "0.001",
            "leverage": 2,
            "margin_mode": "ISOLATED",
            "observed_at_ms": tick - 100,
            "decided_at_ms": tick - 10,
            "expires_at_ms": tick + 60000,
            "exit_handler": "exit-v1",
            "exit_version": "v1",
            "exit_config_json": "{}",
            **(plan_changes or {}),
        }
    )
    evidence = DecisionEvidence(
        selected.spec.code_version,
        selected.config_json,
        canonical(
            {
                "observed_at": plan.observed_at_ms,
                "decided_at": plan.decided_at_ms,
                "expires_at_ms": plan.expires_at_ms,
                "source": "qa",
                "symbol": "BTCUSDT",
                "rationale": "core contract",
                "features": {},
                "strategy_plan": plan.payload(),
                "strategy_deployment": deployment_snapshot(selected),
                **(snapshot_changes or {}),
            }
        ),
    )
    intent = OpenIntent(
        str(uuid4()),
        SCOPE.exchange,
        SCOPE.account_id,
        SCOPE.environment,
        SCOPE.product,
        selected.instance_id,
        str(uuid4()),
        "BTCUSDT",
        "BUY",
        "0.01",
        SPEC.code_version,
        evidence.config_digest,
        evidence.evidence_ref,
    )
    return selected, plan, evidence, replace(intent, **(intent_changes or {}))


def stored(database, **changes):
    selected, plan, evidence, intent = material(**changes)
    assert TradingData(database).accept(intent, evidence).code is AdmissionCode.ACCEPTED
    return selected, plan, evidence, intent


def ready(database, *, features=None, selected=None):
    selected, plan, _, intent = material(selected=selected)
    publish(database, selected)
    result = StrategyPlanAdmission(database, REGISTRY).admit(
        plan,
        selected,
        rationale="core contract",
        features=features,
    )
    risk(database)
    return selected, plan, None, replace(intent, intent_id=result["intent_id"])


def executor(database, *, submit=None, query=None):
    return ExecutionRunner(
        database,
        submit=submit
        or (
            lambda r: ExchangeObservation(
                r["client_order_id"],
                "ACKNOWLEDGED",
                "qa-order",
                evidence={"source": "qa"},
            )
        ),
        query=query or (lambda _: None),
        risk_check=lambda _: True,
        risk_reference=lambda r: RiskReference(
            r["environment"], r["symbol"], "qa", "100", now()
        ),
    )


def risk(database):
    from v2_core.managed_portfolio import publish_capital_snapshot

    with database() as conn:
        if conn.execute(
            "SELECT 1 FROM v2_risk_accounts WHERE scope=%s", (SCOPE.key,)
        ).fetchone():
            return
    AccountRisk(database).configure(
        SCOPE, AccountPolicy("USDT", "100", 10, 0, "qa", 60000), expected_version=0
    )
    PolicyStore(database, SCOPE).patch(
        {
            "capital.enabled": True,
            "portfolio_risk.enabled": True,
            "sizing.pool_fraction": "0.70",
        },
        expected_version=0,
        reason="qa capital contract",
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


def test_reader_does_not_need_current_active_deployment_or_unexpired_entry(database):
    tick = now()
    selected, plan, _, intent = stored(
        database,
        plan_changes={
            "observed_at_ms": tick - 5000,
            "decided_at_ms": tick - 4000,
            "expires_at_ms": tick - 1000,
        },
    )
    publish(database, replace(selected, mode="OFF"))
    with database() as conn:
        assert read_trade_plan(conn, intent.intent_id) == plan
    with pytest.raises(AccountRiskDenied):
        Orders(database).prepare(intent.intent_id)


def test_legacy_reader_has_no_fabricated_plan(database):
    intent, _, _, _ = opened(database)
    with database() as conn:
        assert read_trade_plan(conn, intent.intent_id) is None
        with pytest.raises(ValueError, match="INTENT_REQUIRED"):
            read_trade_plan(conn, str(uuid4()))


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_plan": None},
        {"strategy_plan": {}},
        {"expires_at_ms": True},
        {"expires_at_ms": 1},
    ],
)
def test_present_invalid_plan_never_becomes_legacy(database, changes):
    _, _, _, intent = stored(database, snapshot_changes=changes)
    with database() as conn, pytest.raises(ValueError, match="INTENT_MISMATCH"):
        read_trade_plan(conn, intent.intent_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": replace(SCOPE, environment="LIVE")},
        {"instance_id": "wrong-instance"},
        {"code_version": "v2"},
        {"config_digest": "f" * 64},
        {"symbol": "ETHUSDT"},
        {"side": "SELL", "stop_price": "110"},
        {"quantity": "0.001"},
    ],
)
def test_plan_cannot_be_transplanted_to_other_intent(database, changes):
    _, _, _, intent = stored(database, plan_changes=changes)
    with database() as conn, pytest.raises(ValueError, match="INTENT_MISMATCH"):
        read_trade_plan(conn, intent.intent_id)


def test_equivalent_decimal_quantity_reads_without_float_comparison(database):
    _, plan, _, intent = stored(database, plan_changes={"quantity": "0.0100"})
    with database() as conn:
        assert read_trade_plan(conn, intent.intent_id) == plan


def test_prepare_and_direct_submission_cannot_skip_version_fence(database):
    selected, _, _, intent = ready(database)
    orders = Orders(database)
    order, _ = orders.prepare(intent.intent_id)
    publish(database, replace(selected, generation=2))
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISMATCH"):
        orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    with database() as conn:
        assert conn.execute(
            "SELECT status,version FROM v2_orders WHERE order_id=%s", (order,)
        ).fetchone() == ("PREPARED", 1)
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )
    _, _, _, new_intent = stored(database)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISMATCH"):
        orders.prepare(new_intent.intent_id)


def test_raw_intent_cannot_bypass_permanent_opportunity_receipt(database):
    selected, _, _, intent = stored(database)
    publish(database, selected)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_OPPORTUNITY_RECEIPT_INVALID"):
        Orders(database).prepare(intent.intent_id)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_runtime_missing_opportunity_receipt_is_terminal_not_retried(database):
    selected, _, evidence, intent = material()
    publish(database, selected)
    runtime = DataRuntime(
        database,
        submit=lambda _: pytest.fail("unexpected submit"),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=now,
    )
    result = runtime.accept_open(intent, evidence)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "STRATEGY_OPPORTUNITY_RECEIPT_INVALID"
    assert runtime.accept_open(intent, evidence)["status"] == "REJECTED"


def test_prepared_plan_expiry_is_checked_even_without_optional_risk_provider(database):
    _, plan, _, intent = ready(database)
    order, _ = Orders(database).prepare(intent.intent_id)

    class LaterDatabaseClock:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, query, *args):
            if query.startswith("SELECT floor(extract(epoch FROM clock_timestamp())"):
                return SimpleNamespace(fetchone=lambda: (plan.expires_at_ms,))
            return self.conn.execute(query, *args)

    @contextmanager
    def later():
        with database() as conn:
            yield LaterDatabaseClock(conn)

    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_EXPIRED"):
        Orders(later).transition(
            order, expected_version=1, status="SUBMITTING", evidence={}
        )
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (order,)
        ).fetchone() == ("PREPARED",)


def test_committed_permit_is_not_retroactively_revoked_by_later_publication(database):
    selected, _, _, intent = ready(database)
    risk(database)
    order, _ = Orders(database).prepare(intent.intent_id)
    submitted = []

    def submit(request):
        publish(database, replace(selected, generation=2, mode="OFF"))
        submitted.append(request["client_order_id"])
        return ExchangeObservation(
            request["client_order_id"],
            "ACKNOWLEDGED",
            "issued-before-stop",
            evidence={"source": "qa"},
        )

    assert executor(database, submit=submit).dispatch(order) == "ACKNOWLEDGED"
    assert len(submitted) == 1
    assert (
        executor(database, submit=lambda _: pytest.fail("no resend")).dispatch(order)
        == "UNKNOWN"
    )


def test_runner_terminally_cancels_stale_open_without_venue_call(database):
    selected, _, _, intent = ready(database)
    order, _ = Orders(database).prepare(intent.intent_id)
    publish(database, replace(selected, generation=2, mode="OFF"))
    runner = executor(database, submit=lambda _: pytest.fail("stale submission"))
    assert runner.dispatch(order) == "DENIED"
    assert runner.dispatch(order) == "CANCELLED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


def test_current_dispatch_records_frozen_binding_and_risk_in_same_event(database):
    selected, _, _, intent = ready(database)
    risk(database)
    order, _ = Orders(database).prepare(intent.intent_id)
    assert executor(database).dispatch(order) == "ACKNOWLEDGED"
    with database() as conn:
        event = conn.execute(
            "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='SUBMITTING'",
            (order,),
        ).fetchone()[0]
        assert event["strategy_deployment"] == deployment_snapshot(selected)
        assert event["account_risk"]["notional"] == "1.000000000000000000"
        assert conn.execute(
            "SELECT status FROM v2_risk_reservations WHERE episode_id=%s",
            (intent.intent_id,),
        ).fetchone() == ("HELD",)


def test_runtime_stale_admission_is_durable_rejection_not_retry_poison(database):
    selected, _, evidence, intent = material()
    publish(database, replace(selected, mode="OFF"))
    runtime = DataRuntime(
        database,
        submit=lambda _: pytest.fail("unexpected submit"),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=now,
    )
    result = runtime.accept_open(intent, evidence)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "STRATEGY_ENTRY_DISABLED"
    assert runtime.accept_open(intent, evidence)["status"] == "REJECTED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize("status", ["SUBMITTING", "UNKNOWN"])
def test_issued_permit_recovers_original_identity_after_off(database, status):
    selected, _, _, intent = ready(database)
    orders = Orders(database)
    order, _ = orders.prepare(intent.intent_id)
    orders.transition(
        order,
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference("SANDBOX", "BTCUSDT", "qa", "100", now()),
    )
    if status == "UNKNOWN":
        orders.transition(order, expected_version=2, status="UNKNOWN", evidence={})
    publish(database, replace(selected, generation=2, mode="OFF"))
    runner = executor(
        database,
        submit=lambda _: pytest.fail("must never resend"),
        query=lambda r: ExchangeObservation(
            r["client_order_id"],
            "ACKNOWLEDGED",
            "same-order",
            evidence={"source": "qa"},
        ),
    )
    assert runner.dispatch(order) == "ACKNOWLEDGED"


def test_filled_open_can_close_after_off_but_cannot_add_open_allocation(database):
    selected, _, _, intent = ready(database)
    orders = Orders(database)
    order, _ = orders.prepare(intent.intent_id)
    orders.transition(
        order,
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference("SANDBOX", "BTCUSDT", "qa", "100", now()),
    )
    Ledger(database).record_fill(
        order_id=order,
        exchange_fill_id="qa-open",
        quantity="0.01",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=now(),
        evidence={"source": "qa"},
    )
    orders.transition(
        order, expected_version=2, status="FILLED", evidence={"source": "qa"}
    )
    publish(database, replace(selected, generation=2, mode="OFF"))
    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_DISABLED"):
        orders.prepare(
            intent.intent_id,
            request_key="remainder",
            quantity="0.005",
            evidence={"reason": "qa additional allocation"},
        )
    closing, _ = orders.prepare(intent.intent_id, leg="CLOSE", quantity="0.005")
    assert executor(database).dispatch(closing) == "ACKNOWLEDGED"


def test_missing_capital_contract_cannot_be_bypassed_by_legacy_looking_features(
    database,
):
    _, _, _, intent = ready(
        database,
        features={"evaluation": {"market_plan": {"leverage": 125}}},
        selected=binding(config={"risk": "0.01"}),
    )
    order, _ = Orders(database).prepare(intent.intent_id)
    assert (
        executor(
            database, submit=lambda _: pytest.fail("capital contract absent")
        ).dispatch(order)
        == "DENIED"
    )
    with database() as conn:
        event = conn.execute(
            "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='CANCELLED'",
            (order,),
        ).fetchone()[0]
        assert event["account_risk_code"] == "STRATEGY_CAPITAL_CONFIG_REQUIRED"
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )


def test_failed_permit_transaction_rolls_back_proof_and_reservation_without_submit(
    database,
):
    _, _, _, intent = ready(database)
    risk(database)
    order, _ = Orders(database).prepare(intent.intent_id)

    @contextmanager
    def broken_commit():
        with database() as conn:
            yield conn
            if conn.execute(
                "SELECT 1 FROM v2_orders WHERE order_id=%s AND status='SUBMITTING'",
                (order,),
            ).fetchone():
                raise RuntimeError("qa before commit")

    with pytest.raises(RuntimeError, match="qa before commit"):
        executor(
            broken_commit, submit=lambda _: pytest.fail("uncommitted permit")
        ).dispatch(order)
    with database() as conn:
        assert conn.execute(
            "SELECT status FROM v2_orders WHERE order_id=%s", (order,)
        ).fetchone() == ("PREPARED",)
        assert conn.execute("SELECT count(*) FROM v2_risk_reservations").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT count(*) FROM v2_order_events WHERE status='SUBMITTING'"
        ).fetchone() == (0,)
