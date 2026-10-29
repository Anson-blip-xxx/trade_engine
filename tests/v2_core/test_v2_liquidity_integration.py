"""Real replay, admission and submission fences against isolated PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from threading import Event

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario, worker
from test_v2_runtime_policy import database
from test_v2_venue_readiness import Venue, case

from services.v2_directional_replay import create_directional_replay_worker
from v2_core.capital_model import rehearsal_profile
from v2_core.liquidity_controls import configure, read
from v2_core.runner import ExchangeObservation, ExecutionRunner
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness

__all__ = ["case", "database"]


@pytest.mark.parametrize("stamp", [4, True, 2])
def test_strategy_worker_cannot_freeze_a_future_or_pre_context_liquidity_clock(
    database, stamp
):
    replay, _, signal, context = scenario(database)
    context["liquidity_as_of_ms"] = stamp
    with pytest.raises(ValueError, match="context is stale"):
        replay.consume(signal, context=context)
    assert replay.decision(signal) is None


def book(*, now=3, kind="good"):
    result = {
        "version": "binance-depth-v1",
        "symbol": "BTCUSDT",
        "source_environment": "LIVE",
        "source_host": "fapi.binance.com",
        "observed_at_ms": now - 1,
        "received_at_ms": now,
        "last_update_id": 1,
        "bids": [["99.99", "10"]],
        "asks": [["100.01", "10"]],
    }
    if kind == "missing":
        return None
    if kind == "shallow":
        result["bids"][0][1] = result["asks"][0][1] = "0.01"
    elif kind == "wide":
        result["bids"][0][0], result["asks"][0][0] = "99", "101"
    return result


def scene(
    database,
    *,
    mode="ENFORCE",
    strategy="S6",
    now=3,
    kind="good",
    age=5000,
    quote_age=30000,
):
    replay, runtime, signal, context = scenario(database, strategy)
    if now != 3:
        with database() as conn:
            original = conn.execute(
                "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s",
                (signal,),
            ).fetchone()[0]
        signal = runtime.data.signals.admit(
            source="s3",
            environment="SANDBOX",
            request_key="liquidity-long-deadline-" + strategy,
            snapshot={
                **original,
                "observed_at": now - 2,
                "expires_at_ms": now + 10000,
            },
        )
        runtime.clock_ms = lambda: now
        context.update(assembled_at=now, valid_until_ms=now + 10000)
        replay = create_directional_replay_worker(
            runtime, strategy=strategy, analysis_mode="hard", max_delay_ms=10000
        )
    if mode is not None:
        if mode == "OFF":
            controls = read(database, SCOPE)
        else:
            # Configuration requires the original capital guards. The strategy
            # worker retains its existing frozen policy and sizing baseline.
            PolicyStore(database, SCOPE).patch(
                rehearsal_profile("1000"), expected_version=0, reason="QA capital"
            )
            controls = configure(
                database,
                SCOPE,
                {
                    "mode": mode,
                    "max_age_ms": age,
                    "max_execution_quote_age_ms": quote_age,
                },
                expected_version=0,
                reason="QA liquidity integration",
            )
        context["liquidity_controls"] = controls.snapshot()
        context["liquidity_evidence"] = book(now=now, kind=kind)
        context["directional"]["leverage_venue"] = {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "source_host": "demo-fapi.binance.com",
            "book_at_ms": now - 1,
            "spread_fraction": "0.0002",
        }
    return replay, runtime, signal, context


def readiness_proof(database, order):
    state = BusinessState(database).read(
        StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order)
    )
    return json.loads(state.payload_json)


def guarded(database, *, clock, plan):
    venue, sent = Venue(), []
    terms = {"leverage": plan["leverage"], "margin_type": plan["margin_mode"]}
    venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=terms["leverage"], marginType=terms["margin_type"]
    )
    reference_at = clock()

    def submit(snapshot):
        proof = readiness_proof(database, snapshot["order_id"])
        assert proof["status"] == "READY_FOR_GUARDED_SUBMISSION"
        assert not proof["blockers"]
        sent.append(deepcopy(snapshot))
        return ExchangeObservation(
            snapshot["client_order_id"], "ACKNOWLEDGED", "10", evidence={"source": "qa"}
        )

    gate = GuardedOpeningSubmit(
        database,
        readiness=TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=clock),
        reference=lambda _: {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "mark_price": "100",
            "observed_at_ms": reference_at,
        },
        plan=lambda _: terms,
        submit=submit,
        enabled=True,
    )
    runner = ExecutionRunner(
        database,
        submit=gate,
        query=lambda _: pytest.fail("fresh submission must not query recovery"),
        risk_check=lambda _: True,
        scope=SCOPE,
    )
    return venue, gate, runner, sent


def admitted(database, *, age=5000, quote_age=30000, strategy="S6"):
    _, runtime, signal, context = scene(
        database, now=1000, age=age, quote_age=quote_age, strategy=strategy
    )
    admission = formal(runtime, strategy, max_delay_ms=10000)
    result = admission.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    evaluation = admission.decision(signal)["snapshot"]["features"]["evaluation"]
    return result["order_id"], evaluation, context


@pytest.mark.parametrize("strategy,side", [("S6", "BUY"), ("S8", "SELL")])
def test_enforced_depth_flows_through_real_replay_admission_and_submit(
    database, strategy, side
):
    replay, runtime, signal, context = scene(database, strategy=strategy)
    unused_side = "bids" if side == "BUY" else "asks"
    context["liquidity_evidence"][unused_side][0][1] = "0.01"
    original = deepcopy(context)
    assert (
        replay.consume(signal, context=context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    frozen = replay.decision(signal)["snapshot"]["features"]
    assert frozen["evaluation"]["liquidity_gate"]["reason"] == "LIQUIDITY_PASSED"
    assert frozen["context"] == original == context
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
    admission = formal(runtime, strategy)
    result = admission.consume(signal, context=context)
    decision = admission.decision(signal)
    assert result["status"] == "PREPARED" and decision["action"] == "OPEN"
    assert decision["side"] == side and Decimal(decision["quantity"]) == Decimal("1.25")
    evaluation = decision["snapshot"]["features"]["evaluation"]
    assert evaluation["liquidity_gate"] == frozen["evaluation"]["liquidity_gate"]
    assert evaluation["execution_authorized"] is False
    _, _, runner, sent = guarded(
        database, clock=lambda: 3, plan=evaluation["market_plan"]
    )
    assert runner.dispatch(result["order_id"]) == "ACKNOWLEDGED"
    assert len(sent) == 1 and sent[0]["side"] == side
    assert readiness_proof(database, result["order_id"])["deadline_ms"] == 8


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing", "LIQUIDITY_EVIDENCE_MISSING"),
        ("shallow", "LIQUIDITY_INSUFFICIENT_DEPTH"),
        ("wide", "LIQUIDITY_SPREAD_EXCEEDED"),
    ],
)
def test_enforcement_rejects_before_any_order_is_prepared(database, kind, reason):
    replay, runtime, signal, context = scene(database, kind=kind)
    for consumer in (replay, formal(runtime)):
        assert consumer.consume(signal, context=context)["reason"] == reason
        evaluation = consumer.decision(signal)["snapshot"]["features"]["evaluation"]
        assert evaluation["liquidity_gate"]["blocked"] is True
        assert evaluation["sizing"]["quantity"] == "1.25"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_shadow_records_rejection_without_changing_admitted_quantity(database):
    replay, runtime, signal, context = scene(database, mode="SHADOW", kind="shallow")
    assert (
        replay.consume(signal, context=context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    admission = formal(runtime)
    assert admission.consume(signal, context=context)["status"] == "PREPARED"
    decision = admission.decision(signal)
    evaluation = decision["snapshot"]["features"]["evaluation"]
    assessment = evaluation["liquidity_gate"]
    assert assessment["reason"] == "LIQUIDITY_INSUFFICIENT_DEPTH"
    assert assessment["would_block"] is True
    assert assessment["blocked"] is assessment["authorized"] is False
    assert evaluation["sizing"]["quantity"] == "1.25"
    assert Decimal(decision["quantity"]) == Decimal("1.25")


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing", "LIQUIDITY_EXECUTION_EVIDENCE_MISSING"),
        ("wide", "LIQUIDITY_EXECUTION_SPREAD_EXCEEDED"),
        ("stale", "LIQUIDITY_EXECUTION_STALE"),
    ],
)
def test_passing_live_depth_cannot_override_bad_execution_quote(
    database, fault, reason
):
    replay, runtime, signal, context = scene(database, now=1000, quote_age=100)
    quote = context["directional"]["leverage_venue"]
    if fault == "missing":
        context["directional"].pop("leverage_venue")
    elif fault == "wide":
        quote["spread_fraction"] = "0.03"
    else:
        quote["book_at_ms"] = 899
    for consumer in (replay, formal(runtime)):
        assert consumer.consume(signal, context=context)["reason"] == reason
        evaluation = consumer.decision(signal)["snapshot"]["features"]["evaluation"]
        assert evaluation["liquidity_gate"]["reason"] == "LIQUIDITY_PASSED"
        assert evaluation["liquidity_execution_quote"]["blocked"] is True
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


@pytest.mark.parametrize("mode", [None, "OFF"])
def test_legacy_and_off_context_preserve_admission_without_depth(database, mode):
    replay, runtime, signal, context = scene(database, mode=mode, kind="missing")
    assert (
        replay.consume(signal, context=context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    admission = formal(runtime)
    assert admission.consume(signal, context=context)["status"] == "PREPARED"
    evaluation = admission.decision(signal)["snapshot"]["features"]["evaluation"]
    assert evaluation["sizing"]["quantity"] == "1.25"
    if mode is None:
        assert "liquidity_gate" not in evaluation
    else:
        assert evaluation["liquidity_gate"]["reason"] == "LIQUIDITY_DISABLED"


@pytest.mark.parametrize(
    "stage,reason",
    [
        ("market", "entry_mode_unconfirmed"),
        ("size", "INSUFFICIENT_BUDGET"),
        ("funding", "ADVERSE_FUNDING"),
    ],
)
def test_prior_market_and_capital_rejections_keep_their_reason(database, stage, reason):
    replay, runtime, signal, context = scene(database, kind="missing")
    if stage == "market":
        context["directional"]["price"] = "98"
    elif stage == "size":
        context["directional"]["sizing"]["available_margin"] = "0"
    else:
        context["directional"]["funding_rate"] = "0.002"
    for consumer in (replay, formal(runtime)):
        assert consumer.consume(signal, context=context)["reason"] == reason
        evaluation = consumer.decision(signal)["snapshot"]["features"]["evaluation"]
        assert "liquidity_gate" not in evaluation
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_restart_reuses_complete_frozen_book_after_configuration_changes(database):
    replay, _, signal, context = scene(database)
    first = replay.consume(signal, context=context)
    frozen = replay.decision(signal)
    configure(
        database, SCOPE, {"mode": "OFF"}, expected_version=1, reason="QA changed mode"
    )
    context["liquidity_evidence"]["asks"][0][1] = "0.001"
    restarted, _ = worker(database, now=4)
    assert restarted.consume(signal, context={}) == dict(first, intent_id=None)
    assert restarted.decision(signal) == frozen
    saved = frozen["snapshot"]["features"]["context"]
    assert saved["liquidity_controls"]["values"]["mode"] == "ENFORCE"
    assert saved["liquidity_evidence"]["asks"] == [["100.01", "10"]]


def test_enabling_after_legacy_prepare_rejects_with_zero_sends(case):
    data, order, _, _, runner, sent = case
    PolicyStore(data._connect, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA capital"
    )
    configure(
        data._connect,
        SCOPE,
        {"mode": "ENFORCE"},
        expected_version=0,
        reason="QA enable after preparation",
    )
    assert runner.dispatch(order) == "REJECTED"
    assert sent == []
    assert (
        "LIQUIDITY_CONTROLS_CHANGED_REPROPOSE"
        in readiness_proof(data._connect, order)["blockers"]
    )


def test_threshold_change_after_admission_requires_reproposal(database):
    order, evaluation, _ = admitted(database)
    configure(
        database,
        SCOPE,
        {"max_impact_bps": "10"},
        expected_version=1,
        reason="QA tighten after admission",
    )
    _, _, runner, sent = guarded(
        database, clock=lambda: 1000, plan=evaluation["market_plan"]
    )
    assert runner.dispatch(order) == "REJECTED" and sent == []
    assert (
        "LIQUIDITY_CONTROLS_CHANGED_REPROPOSE"
        in readiness_proof(database, order)["blockers"]
    )


def test_submission_recomputes_quantity_instead_of_reusing_passed_assessment(database):
    from v2_core.liquidity_submission import validate_opening_liquidity

    order, evaluation, _ = admitted(database)
    assert evaluation["liquidity_gate"]["reason"] == "LIQUIDITY_PASSED"
    _, _, runner, sent = guarded(
        database, clock=lambda: 1000, plan=evaluation["market_plan"]
    )
    snapshot = runner.snapshot(order)
    with database() as conn:
        assert validate_opening_liquidity(conn, SCOPE, snapshot, now_ms=1000) == 5999
        with pytest.raises(ValueError, match="LIQUIDITY_INSUFFICIENT_DEPTH"):
            validate_opening_liquidity(
                conn, SCOPE, {**snapshot, "quantity": "20"}, now_ms=1000
            )
    assert sent == []


@pytest.mark.parametrize(
    "age,quote_age,reason",
    [
        (100, 30000, "LIQUIDITY_STALE"),
        (5000, 100, "LIQUIDITY_EXECUTION_STALE"),
    ],
)
def test_waiting_after_admission_does_not_refresh_frozen_book(
    database, age, quote_age, reason
):
    order, evaluation, _ = admitted(database, age=age, quote_age=quote_age)
    _, _, runner, sent = guarded(
        database, clock=lambda: 1100, plan=evaluation["market_plan"]
    )
    assert runner.dispatch(order) == "REJECTED" and sent == []
    assert reason in readiness_proof(database, order)["blockers"]


@pytest.mark.parametrize("age,quote_age", [(100, 30000), (5000, 100)])
def test_book_expiry_after_readiness_is_a_strict_send_deadline(
    database, age, quote_age
):
    order, evaluation, _ = admitted(database, age=age, quote_age=quote_age)
    clock = [1000]
    _, gate, runner, sent = guarded(
        database, clock=lambda: clock[0], plan=evaluation["market_plan"]
    )
    inspect = gate.readiness.inspect

    def advancing_clock(**kwargs):
        result = inspect(**kwargs)
        clock[0] = 1099
        return result

    gate.readiness.inspect = advancing_clock
    assert runner.dispatch(order) == "REJECTED" and sent == []
    proof = readiness_proof(database, order)
    assert proof["deadline_ms"] == 1099
    assert proof["finished_at_ms"] == 1000 and proof["blockers"] == []


def test_configuration_cannot_change_while_guarded_send_holds_account_lock(database):
    from psycopg.errors import LockNotAvailable

    order, evaluation, _ = admitted(database)
    _, gate, runner, sent = guarded(
        database, clock=lambda: 1000, plan=evaluation["market_plan"]
    )
    entered, release = Event(), Event()
    submit = gate.submit

    def held_submit(snapshot):
        entered.set()
        assert release.wait(timeout=5), "test must release the fake venue"
        return submit(snapshot)

    @contextmanager
    def bounded_configuration_connection():
        with database() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute("SET LOCAL statement_timeout='2s'")
            yield conn

    gate.submit = held_submit
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatch = pool.submit(runner.dispatch, order)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(LockNotAvailable):
                configure(
                    bounded_configuration_connection,
                    SCOPE,
                    {"mode": "OFF"},
                    expected_version=1,
                    reason="QA concurrent configuration",
                )
            assert read(database, SCOPE).version == 1
            assert sent == []
        finally:
            release.set()
        assert dispatch.result(timeout=5) == "ACKNOWLEDGED"
    assert len(sent) == 1
