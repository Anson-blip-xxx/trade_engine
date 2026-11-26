"""Real-PG reduction callback acceptance, not guarded-opening acceptance.

Fixtures deliberately record synthetic opening fills through the ledger APIs.
Only CLOSE travels through the real Runner and service-bound venue gate here;
the final submit/query ports are fakes and no venue request is permitted.
"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.v2_directional_execution import bind_directional_venue_gate
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import opened
from tests.v2_core import test_v2_strategy_deployment_execution as typed_fixtures
from v2_core.account_risk import AccountScope, RiskReference
from v2_core.ledger import Ledger
from v2_core.orders import Orders
from v2_core.runner import ExchangeObservation
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore

database = database_fixture


def reduction_case(database, kind, quantity, *, lost_response=False):
    """Build a legitimate close request against explicitly synthetic exposure."""
    if kind == "typed":
        selected, plan, _, original = typed_fixtures.ready(database)
        scope = selected.scope
        orders = Orders(database)
        opening, _ = orders.prepare(original.intent_id)
        reference = RiskReference(
            "SANDBOX", "BTCUSDT", "qa", "100", typed_fixtures.now()
        )
    else:
        original, orders, opening, _ = opened(database)
        scope = AccountScope(
            original.exchange,
            original.account_id,
            original.environment,
            original.product,
        )
        plan, reference = None, None
    assert orders.transition(
        opening,
        expected_version=1,
        status="SUBMITTING",
        evidence={"source": "synthetic-opening-for-reduction-callback-QA"},
        risk_reference=reference,
    )
    assert Ledger(database).record_fill(
        order_id=opening,
        exchange_fill_id="qa-synthetic-opening-fill",
        quantity=original.quantity,
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=typed_fixtures.now(),
        evidence={"source": "synthetic-opening-for-reduction-callback-QA"},
    )
    assert orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        evidence={"source": "synthetic-opening-for-reduction-callback-QA"},
    )
    if kind == "typed":
        typed_fixtures.publish(database, replace(selected, generation=2, mode="OFF"))
        policy = PolicyStore(database, scope)
        policy.patch(
            {"capital.enabled": False, "portfolio_risk.enabled": False},
            expected_version=policy.read().version,
            reason="QA disabled entry capital must not disable reductions",
        )
    closing, client_id = orders.prepare(
        original.intent_id,
        leg="CLOSE",
        quantity=quantity,
        request_key="qa-reduction-callback",
        evidence={"reason": "reduction identity QA"},
    )
    sent, queried = [], []

    def submit(order):
        sent.append(deepcopy(order))
        if lost_response:
            raise TimeoutError("QA close POST response unavailable")
        return ExchangeObservation(
            order["client_order_id"],
            "ACKNOWLEDGED",
            "qa-close",
            evidence={"source": "fake-close-submit"},
        )

    def query(order):
        queried.append(deepcopy(order))
        return ExchangeObservation(
            order["client_order_id"],
            "ACKNOWLEDGED",
            "qa-close",
            evidence={"source": "fake-close-query"},
        )

    def entry_port(*_args, **_kwargs):
        pytest.fail(
            "a reduction must not call an entry reference/settings/readiness port"
        )

    # The binder still constructs scoped inventory/settings adapters, even
    # though CLOSE must never call their request port.
    entry_port.account_id = scope.account_id
    entry_port.environment = scope.environment

    runtime = DataRuntime(
        database,
        scope=scope,
        submit=submit,
        query=query,
        risk_check=lambda _: True,
        risk_reference=entry_port,
        clock_ms=lambda: plan.expires_at_ms + 1 if plan is not None else 9999999999999,
    )
    gate = bind_directional_venue_gate(
        runtime,
        entry_port,
        mark_reference=entry_port,
        enabled=False,
        reduce_only_enabled=True,
    )
    snapshot = runtime.execution.snapshot(closing)
    trace = runtime.data.trace(original.intent_id)
    assert snapshot["status"] == "PREPARED" and snapshot["version"] == 1
    assert "execution" not in trace["decision"]["features"]
    if kind == "typed":
        assert not plan.entry_valid_at(runtime._now())
        assert trace["decision"]["strategy_plan"] == plan.payload()
    else:
        assert "strategy_plan" not in trace["decision"]
        assert "strategy_deployment" not in trace["decision"]
        assert "expires_at_ms" not in trace["decision"]
    return SimpleNamespace(
        runtime=runtime,
        gate=gate,
        closing=closing,
        client_id=client_id,
        episode=original.intent_id,
        sent=sent,
        queried=queried,
    )


@pytest.mark.parametrize("kind", ["legacy", "typed"])
@pytest.mark.parametrize("quantity", ["0.01", "0.005"], ids=["full", "partial"])
def test_bound_gate_accepts_committed_close_without_entry_authority(
    database, kind, quantity
):
    case = reduction_case(database, kind, quantity)
    assert case.runtime.execution.dispatch(case.closing) == "ACKNOWLEDGED"
    assert len(case.sent) == 1
    sent = case.sent[0]
    assert sent["order_id"] == case.closing
    assert sent["client_order_id"] == case.client_id
    assert sent["leg"] == "CLOSE" and sent["reduce_only"] is True
    assert sent["side"] == "SELL"
    assert sent["status"] == "PREPARED" and sent["version"] == 1
    assert Decimal(sent["quantity"]) == Decimal(quantity)
    current = case.runtime.execution.snapshot(case.closing)
    assert current["status"] == "ACKNOWLEDGED" and current["version"] == 3
    assert case.queried == []


@pytest.mark.parametrize("kind", ["legacy", "typed"])
@pytest.mark.parametrize(
    "change",
    [
        {"quantity": "0.006"},
        {"side": "BUY"},
        {"status": "SUBMITTING"},
        {"version": 2},
    ],
    ids=["quantity", "side", "status", "version"],
)
def test_tampered_close_is_definitely_unsent(database, kind, change):
    case = reduction_case(database, kind, "0.005")
    case.runtime.execution.submit = lambda order: case.gate({**order, **change})
    assert case.runtime.execution.dispatch(case.closing) == "REJECTED"
    assert case.sent == [] and case.queried == []
    with database() as conn:
        status, evidence = conn.execute(
            "SELECT o.status,e.evidence FROM v2_orders o JOIN v2_order_events e "
            "ON e.order_id=o.order_id AND e.version=o.version WHERE o.order_id=%s",
            (case.closing,),
        ).fetchone()
    assert status == "REJECTED"
    assert evidence["reason"] == "REDUCE_ONLY_REVALIDATION_FAILED"
    assert evidence["submission_sent"] is False


@pytest.mark.parametrize("kind", ["legacy", "typed"])
def test_close_post_failure_remains_unknown_and_recovers_without_resend(database, kind):
    case = reduction_case(database, kind, "0.005", lost_response=True)
    assert case.runtime.execution.dispatch(case.closing) == "UNKNOWN"
    current = case.runtime.execution.snapshot(case.closing)
    assert current["status"] == "UNKNOWN"
    assert current["client_order_id"] == case.client_id
    assert current["submission_failure"].get("submission_sent") is not False
    assert len(case.sent) == 1 and case.queried == []
    assert case.runtime.execution.dispatch(case.closing) == "ACKNOWLEDGED"
    assert len(case.sent) == len(case.queried) == 1
    assert case.queried[0]["status"] == "UNKNOWN"
    assert case.queried[0]["order_id"] == case.closing
    assert case.queried[0]["client_order_id"] == case.client_id
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE episode_id=%s AND leg='CLOSE'",
            (case.episode,),
        ).fetchone() == (1,)
