import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent

from v2_core.account_risk import (
    AccountPolicy,
    AccountRisk,
    AccountRiskDenied,
    AccountScope,
    RiskReference,
)
from v2_core.errors import SubmissionNotSent
from v2_core.evidence import DecisionEvidence
from v2_core.runner import ExchangeObservation, ExecutionRunner
from v2_core.service import TradingData

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")
POLICY = AccountPolicy("USDT", "2", 2, 0, "test-mark", 60000)


def now():
    return time.time_ns() // 1000000


def prepare(
    database,
    *,
    symbol="BTCUSDT",
    account="test-account",
    environment="SANDBOX",
    quantity="0.01",
    child_quantity=None,
    expired=False,
    producer="s6",
):
    base = evidence()
    snapshot = json.loads(base.snapshot_json)
    snapshot.update(
        symbol=symbol,
        observed_at=now() - 1000,
        decided_at=now() - 500,
        expires_at_ms=now() + 60000 if not expired else now() - 1,
    )
    proof = DecisionEvidence(
        base.strategy_version, base.config_json, json.dumps(snapshot)
    )
    original = replace(
        intent(),
        symbol=symbol,
        account_id=account,
        environment=environment,
        quantity=quantity,
        producer=producer,
        evidence_ref=proof.evidence_ref,
    )
    data = TradingData(database)
    data.accept(original, proof)
    order, client = data.orders.prepare(original.intent_id, quantity=child_quantity)
    return original, data, order, client


def reference(request, **changes):
    return replace(
        RiskReference(
            request["environment"], request["symbol"], "test-mark", "100", now()
        ),
        **changes,
    )


def runner(database, *, submit=None, ref=reference):
    return ExecutionRunner(
        database,
        submit=submit
        or (
            lambda request: ExchangeObservation(
                request["client_order_id"],
                "ACKNOWLEDGED",
                request["order_id"],
                evidence={"source": "qa"},
            )
        ),
        query=lambda _: None,
        risk_check=lambda _: True,
        risk_reference=ref,
    )


def configured(database, **changes):
    risk = AccountRisk(database)
    risk.configure(SCOPE, replace(POLICY, **changes), expected_version=0)
    return risk


def test_concurrent_cross_strategy_dispatch_cannot_oversubscribe(database):
    risk = configured(database)
    orders = [
        prepare(database, symbol=f"ASSET{i}USDT", producer=("s6", "s7", "s8")[i % 3])[2]
        for i in range(8)
    ]
    sent = []

    def submit(request):
        sent.append(request["order_id"])
        usage = risk.usage(SCOPE)
        assert Decimal(usage["notional"]) <= 2 and usage["positions"] <= 2
        return ExchangeObservation(
            request["client_order_id"], "ACKNOWLEDGED", evidence={"source": "qa"}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(runner(database, submit=submit).dispatch, orders))
    assert results.count("ACKNOWLEDGED") == 2
    assert results.count("DENIED") == 6
    assert len(sent) == 2 and risk.usage(SCOPE)["positions"] == 2


def test_reservation_and_cas_commit_before_external_submit(database):
    configured(database)
    original, data, order, _ = prepare(database)

    def submit(request):
        trace = data.trace(original.intent_id)
        assert trace["orders"][0]["status"] == "SUBMITTING"
        assert trace["account_risk"]["status"] == "HELD"
        assert trace["account_risk"]["policy_version"] == 1
        assert trace["account_risk"]["policy"] == POLICY.__dict__
        return ExchangeObservation(
            request["client_order_id"], "ACKNOWLEDGED", evidence={"source": "qa"}
        )

    assert runner(database, submit=submit).dispatch(order) == "ACKNOWLEDGED"


def test_direct_orders_path_cannot_skip_configured_risk(database):
    risk = configured(database)
    _, data, order, _ = prepare(database)
    with pytest.raises(AccountRiskDenied, match="REFERENCE_REQUIRED"):
        data.orders.transition(
            order, expected_version=1, status="SUBMITTING", evidence={}
        )
    assert risk.usage(SCOPE)["positions"] == 0
    assert runner(database, ref=None).dispatch(order) == "DENIED"


def test_explicit_guard_without_policy_fails_closed(database):
    _, _, order, _ = prepare(database)
    assert (
        runner(database, submit=lambda _: pytest.fail("unconfigured send")).dispatch(
            order
        )
        == "DENIED"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at_ms": 1},
        {"observed_at_ms": 2**53 - 1},
        {"source": "wrong"},
        {"environment": "LIVE"},
        {"symbol": "ETHUSDT"},
    ],
)
def test_reference_validation_blocks_send(database, changes):
    risk = configured(database)
    _, _, order, _ = prepare(database)
    assert (
        runner(
            database,
            ref=lambda request: reference(request, **changes),
            submit=lambda _: pytest.fail("bad quote sent"),
        ).dispatch(order)
        == "DENIED"
    )
    assert risk.usage(SCOPE)["positions"] == 0


def test_expired_decision_cannot_reserve(database):
    configured(database)
    _, _, order, _ = prepare(database, expired=True)
    assert runner(database).dispatch(order) == "DENIED"


def test_reference_provider_failure_does_not_claim_or_send(database):
    risk = configured(database)
    original, data, order, _ = prepare(database)

    def unavailable(_):
        raise ConnectionError("secret")

    with pytest.raises(ConnectionError):
        runner(database, ref=unavailable).dispatch(order)
    assert data.trace(original.intent_id)["orders"][0]["status"] == "PREPARED"
    assert risk.usage(SCOPE)["positions"] == 0


def test_unknown_and_restart_keep_capacity_and_never_resend(database):
    configured(database, max_positions=1)
    _, _, order, _ = prepare(database)

    def ambiguous(_):
        raise TimeoutError("ambiguous")

    assert runner(database, submit=ambiguous).dispatch(order) == "UNKNOWN"
    restarted = AccountRisk(database)
    assert restarted.sweep_releases() == 0
    assert restarted.usage(SCOPE)["positions"] == 1
    assert (
        runner(database, submit=lambda _: pytest.fail("resent")).dispatch(order)
        == "UNKNOWN"
    )
    _, _, second, _ = prepare(database, symbol="ETHUSDT")
    assert runner(database).dispatch(second) == "DENIED"


def test_known_unsent_rejection_releases_atomically(database):
    risk = configured(database)
    original, data, order, _ = prepare(database)

    def not_sent(_):
        raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")

    assert runner(database, submit=not_sent).dispatch(order) == "REJECTED"
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["account_risk"]["release_reason"] == "ABORTED"
    assert risk.sweep_releases() == 0


def test_cooldown_is_account_wide_and_survives_unsent_and_restart(database):
    configured(database, cooldown_ms=60000)
    _, _, first, _ = prepare(database)

    def not_sent(_):
        raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")

    assert runner(database, submit=not_sent).dispatch(first) == "REJECTED"
    _, _, second, _ = prepare(database, symbol="ETHUSDT")
    assert runner(database).dispatch(second) == "DENIED"
    assert AccountRisk(database).usage(SCOPE)["positions"] == 0


def test_policy_cas_and_immutable_version_history(database):
    risk = configured(database)
    assert risk.configure(SCOPE, POLICY, expected_version=0) == 1
    updated = replace(POLICY, max_notional="1")
    assert risk.configure(SCOPE, updated, expected_version=1) == 2
    with pytest.raises(ValueError, match="version conflict"):
        risk.configure(SCOPE, POLICY, expected_version=0)
    with pytest.raises(ValueError, match="currency"):
        risk.configure(SCOPE, replace(updated, currency="USDC"), expected_version=2)
    with pytest.raises(Exception, match="immutable"), database() as conn:
        conn.execute("DELETE FROM v2_risk_policies")


def test_policy_cannot_be_enabled_over_unreserved_active_episode(database):
    prepare(database)
    with pytest.raises(ValueError, match="unreserved"):
        configured(database)


def test_child_orders_share_full_intent_reservation_not_child_quantity(database):
    risk = configured(database)
    original, data, first, _ = prepare(database, child_quantity="0.004")
    second, _ = data.orders.prepare(
        original.intent_id,
        quantity="0.006",
        request_key="child-2",
        evidence={"reason": "split"},
    )
    assert runner(database).dispatch(first) == "ACKNOWLEDGED"
    assert runner(database).dispatch(second) == "ACKNOWLEDGED"
    assert Decimal(risk.usage(SCOPE)["notional"]) == 1
    assert risk.usage(SCOPE)["positions"] == 1


def test_child_cannot_increase_reference_price_or_evade_lowered_policy(database):
    risk = configured(database)
    original, data, first, _ = prepare(database, child_quantity="0.004")
    assert runner(database).dispatch(first) == "ACKNOWLEDGED"
    second, _ = data.orders.prepare(
        original.intent_id,
        quantity="0.003",
        request_key="child-2",
        evidence={"reason": "split"},
    )
    assert (
        runner(database, ref=lambda request: reference(request, price="101")).dispatch(
            second
        )
        == "DENIED"
    )
    risk.configure(SCOPE, replace(POLICY, max_notional="0.5"), expected_version=1)
    third, _ = data.orders.prepare(
        original.intent_id,
        quantity="0.003",
        request_key="child-3",
        evidence={"reason": "split"},
    )
    assert runner(database).dispatch(third) == "DENIED"
    assert Decimal(risk.usage(SCOPE)["notional"]) == 1


@pytest.mark.parametrize(
    "field,value", [("account_id", "other-account"), ("environment", "LIVE")]
)
def test_account_and_environment_budgets_are_isolated(database, field, value):
    risk = configured(database, max_positions=1)
    scope = replace(SCOPE, **{field: value})
    risk.configure(scope, replace(POLICY, max_positions=1), expected_version=0)
    _, _, first, _ = prepare(database)
    kwargs = {"account" if field == "account_id" else field: value}
    _, _, second, _ = prepare(database, **kwargs)
    assert runner(database).dispatch(first) == "ACKNOWLEDGED"
    assert runner(database).dispatch(second) == "ACKNOWLEDGED"
    assert risk.usage(SCOPE)["positions"] == risk.usage(scope)["positions"] == 1


def test_mixed_quote_currency_is_rejected_not_implicitly_converted(database):
    configured(database)
    _, _, order, _ = prepare(database, symbol="BTCUSDC")
    assert runner(database).dispatch(order) == "DENIED"


def test_commit_response_loss_preserves_reservation_without_send(database):
    risk = configured(database)
    _, _, order, _ = prepare(database)
    calls = [0]

    @contextmanager
    def lost_commit():
        calls[0] += 1
        with database() as conn:
            yield conn
        if calls[0] == 2:
            raise ConnectionError("commit acknowledgement lost")

    with pytest.raises(ConnectionError):
        runner(lost_commit, submit=lambda _: pytest.fail("must not send")).dispatch(
            order
        )
    assert risk.usage(SCOPE)["positions"] == 1
    assert (
        runner(database, submit=lambda _: pytest.fail("must not replay")).dispatch(
            order
        )
        == "UNKNOWN"
    )


def test_transaction_rollback_leaves_no_reservation_or_submit(database):
    risk = configured(database)
    original, data, order, _ = prepare(database)

    @contextmanager
    def rollback():
        with database() as conn:
            yield conn
            raise ConnectionError("rollback")

    request = runner(database).snapshot(order)
    from v2_core.orders import Orders

    with pytest.raises(ConnectionError):
        Orders(rollback).transition(
            order,
            expected_version=1,
            status="SUBMITTING",
            evidence={},
            risk_reference=reference(request),
            require_account_risk=True,
        )
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["orders"][0]["status"] == "PREPARED"


def test_partial_fill_cancellation_holds_until_flat_reconciled_settlement(database):
    risk = configured(database)
    original, data, opening, _ = prepare(database)
    run = runner(database)
    assert run.dispatch(opening) == "ACKNOWLEDGED"

    def fill(order):
        data.ledger.record_fill(
            order_id=order,
            exchange_fill_id=str(uuid4()),
            quantity="0.004",
            price="100",
            fee="0",
            fee_currency="USDT",
            occurred_at_ms=now(),
            evidence={"source": "qa"},
        )

    fill(opening)
    data.orders.transition(
        opening,
        expected_version=3,
        status="CANCELLED",
        evidence={"fills_complete": True},
    )
    assert risk.usage(SCOPE)["positions"] == 1
    risk.configure(SCOPE, replace(POLICY, max_notional="0.1"), expected_version=1)
    closing, _ = data.orders.prepare(original.intent_id, leg="CLOSE", quantity="0.004")
    # A reduced policy and broken market quote port cannot block the CLOSE path.
    assert (
        runner(database, ref=lambda _: pytest.fail("close requested quote")).dispatch(
            closing
        )
        == "ACKNOWLEDGED"
    )
    fill(closing)
    data.orders.transition(
        closing, expected_version=3, status="FILLED", evidence={"source": "qa"}
    )
    assert risk.sweep_releases() == 0
    assert risk.usage(SCOPE)["positions"] == 1
    revision = data.ledger.report(original.intent_id, settlement_currency="USDT")[
        "accounting_revision"
    ]
    proof = {
        "exchange_flat": True,
        "orders_terminal": True,
        "fills_complete": True,
        "cash_complete": True,
        "source": "qa-reconciler",
        "observed_at_ms": now(),
        "ledger_revision": revision,
    }
    assert data.ledger.settle(original.intent_id, currency="USDT", evidence=proof)
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["account_risk"]["release_reason"] == "SETTLED"


@pytest.mark.parametrize("amount", ["0", "NaN", "-1", "1e20", 1.0])
def test_bad_policy_amounts_are_rejected(amount):
    with pytest.raises(ValueError):
        replace(POLICY, max_notional=amount)


def test_precision_rounds_reservation_up_not_down(database):
    risk = configured(database)
    _, _, order, _ = prepare(database, quantity="0.000000000000000001")
    assert (
        runner(database, ref=lambda request: reference(request, price="0.1")).dispatch(
            order
        )
        == "ACKNOWLEDGED"
    )
    assert Decimal(risk.usage(SCOPE)["notional"]) == Decimal("1e-18")


def test_duplicate_dispatch_reserves_once(database):
    risk = configured(database)
    _, _, order, _ = prepare(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runner(database).dispatch(order), range(4)))
    assert "ACKNOWLEDGED" in results
    assert risk.usage(SCOPE)["positions"] == 1
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_domain_outbox WHERE event_type='ACCOUNT_RISK_RESERVED'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"max_notional": "0.5", "max_positions": 100}, "RISK_NOTIONAL_LIMIT"),
        ({"max_notional": "100", "max_positions": 1}, "RISK_POSITION_LIMIT"),
    ],
)
def test_notional_and_position_limits_are_independent(database, changes, expected):
    configured(database, **changes)
    if expected == "RISK_POSITION_LIMIT":
        first = prepare(database)[2]
        assert runner(database).dispatch(first) == "ACKNOWLEDGED"
    original, data, order, _ = prepare(database, symbol="ETHUSDT")
    assert runner(database).dispatch(order) == "DENIED"
    with database() as conn:
        proof = conn.execute(
            "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='CANCELLED'",
            (order,),
        ).fetchone()[0]
        assert proof["account_risk_code"] == expected
        assert proof["account_risk_evidence"]["policy_version"] == 1
        assert (
            proof["account_risk_evidence"]["policy"]
            == replace(POLICY, **changes).__dict__
        )
    assert data.trace(original.intent_id)["account_risk"] is None


def test_policy_change_between_reference_fetch_and_cas_is_observed(database):
    risk = configured(database)
    _, _, order, _ = prepare(database)

    def quote(request):
        risk.configure(SCOPE, replace(POLICY, max_notional="0.5"), expected_version=1)
        return reference(request)

    assert runner(database, ref=quote).dispatch(order) == "DENIED"
    assert risk.usage(SCOPE)["positions"] == 0


def test_future_last_reservation_cannot_reset_cooldown(database):
    configured(database)
    with database() as conn:
        conn.execute(
            "UPDATE v2_risk_accounts SET last_reserved_at=clock_timestamp()+interval '1 hour'"
        )
    _, _, order, _ = prepare(database)
    assert runner(database).dispatch(order) == "DENIED"


def test_reservation_identity_and_amount_cannot_be_rewritten(database):
    configured(database)
    _, _, order, _ = prepare(database)
    runner(database).dispatch(order)
    for sql in (
        "UPDATE v2_risk_reservations SET notional=0.5",
        "DELETE FROM v2_risk_reservations",
    ):
        with pytest.raises(Exception, match="reservation"), database() as conn:
            conn.execute(sql)


def test_pg_failure_before_cas_never_sends(database):
    configured(database)
    _, _, order, _ = prepare(database)
    calls = [0]

    @contextmanager
    def unavailable():
        calls[0] += 1
        if calls[0] == 2:
            raise ConnectionError("storage unavailable")
        with database() as conn:
            yield conn

    with pytest.raises(ConnectionError):
        runner(
            unavailable, submit=lambda _: pytest.fail("PG failure must not send")
        ).dispatch(order)
    assert AccountRisk(database).usage(SCOPE)["positions"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_positions", True),
        ("max_positions", 0),
        ("cooldown_ms", -1),
        ("max_reference_age_ms", 0),
        ("reference_source", "secret source"),
    ],
)
def test_policy_bounds_fail_before_storage(field, value):
    with pytest.raises(ValueError):
        replace(POLICY, **{field: value})


def test_terminal_transition_and_release_rollback_together(database, monkeypatch):
    import v2_core.account_risk as module

    risk = configured(database)
    original, data, order, _ = prepare(database)
    assert runner(database).dispatch(order) == "ACKNOWLEDGED"
    actual_release = module.release_terminal

    def broken(conn, episode_id):
        assert actual_release(conn, episode_id)
        raise ConnectionError("failure before terminal transaction commits")

    with monkeypatch.context() as patch:
        patch.setattr(module, "release_terminal", broken)
        with pytest.raises(ConnectionError):
            data.orders.transition(
                order,
                expected_version=3,
                status="CANCELLED",
                evidence={"fills_complete": True},
            )
    trace = data.trace(original.intent_id)
    assert trace["orders"][0]["status"] == "ACKNOWLEDGED"
    assert trace["account_risk"]["status"] == "HELD"
    assert risk.usage(SCOPE)["positions"] == 1
    data.orders.transition(
        order, expected_version=3, status="CANCELLED", evidence={"fills_complete": True}
    )
    assert risk.usage(SCOPE)["positions"] == 0


def test_concurrent_sweep_releases_old_terminal_reservation_once(database, monkeypatch):
    import v2_core.account_risk as module

    risk = configured(database)
    _, data, order, _ = prepare(database)
    assert runner(database).dispatch(order) == "ACKNOWLEDGED"
    # Simulate a terminal record left by an older writer lacking the release hook.
    with monkeypatch.context() as patch:
        patch.setattr(module, "release_terminal", lambda *_: False)
        data.orders.transition(
            order,
            expected_version=3,
            status="CANCELLED",
            evidence={"fills_complete": True},
        )
    assert risk.usage(SCOPE)["positions"] == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(
            pool.map(lambda _: AccountRisk(database).sweep_releases(), range(8))
        )
    assert sum(counts) == 1 and risk.usage(SCOPE)["positions"] == 0
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_domain_outbox WHERE event_type='ACCOUNT_RISK_RELEASED'"
            ).fetchone()[0]
            == 1
        )


def test_runtime_wires_guard_trace_and_release_supervision(database):
    from v2_core.runtime import DataRuntime

    configured(database)
    original, _, order, _ = prepare(database)
    runtime = DataRuntime(
        database,
        submit=lambda request: ExchangeObservation(
            request["client_order_id"], "ACKNOWLEDGED", evidence={"source": "qa"}
        ),
        query=lambda _: None,
        risk_check=lambda _: True,
        risk_reference=reference,
        clock_ms=now,
    )
    assert runtime.execution.dispatch(order) == "ACKNOWLEDGED"
    assert runtime.data.trace(original.intent_id)["account_risk"]["status"] == "HELD"
    result = runtime.tick(overdue_ms=60000)
    assert result["risk_releases"] == 0
    assert result["expiry"] == {}
