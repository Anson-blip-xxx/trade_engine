"""Entry quality gates: no venue clients; all PG writes use isolated fixtures."""

import json
import time
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from test_v2_directional import inputs, sizing
from test_v2_directional_admission import formal
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE, scenario
from test_v2_directional_settlement import baseline, service
from test_v2_reentry import bar, history

from services.v2_reentry_gate import (
    StructuralReentryContext,
    assess,
    prior_close,
    structure_policy,
    validate,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.capital_model import portfolio_budget, rehearsal_profile
from v2_core.directional import evaluate_market, size_candidate
from v2_core.entry_price import validate_entry_price
from v2_core.errors import SubmissionNotSent
from v2_core.evidence import canonical, digest
from v2_core.runtime_policy import PolicyStore, resolve

database = database_fixture
closed = closed_fixture


@pytest.mark.parametrize(
    "side,mark,allowed",
    [
        ("BUY", "100.5", True),
        ("BUY", "100.500000000000000001", False),
        ("BUY", "99", True),
        ("BUY", "94", False),
        ("SELL", "99.5", True),
        ("SELL", "99.499999999999999999", False),
        ("SELL", "101", True),
        ("SELL", "106", False),
    ],
)
def test_exact_pre_submit_price_stop_risk_boundary(side, mark, allowed):
    p = resolve({"entry.price_guard_enabled": True})
    args = {
        "side": side,
        "original": "100",
        "mark": mark,
        "stop": "95" if side == "BUY" else "105",
        "frozen": p,
        "current": p,
    }
    with localcontext() as ctx:
        ctx.prec = 6
        if allowed:
            assert validate_entry_price(**args)["execution_authorized"] is False
        else:
            with pytest.raises(SubmissionNotSent):
                validate_entry_price(**args)


def test_price_guard_uses_tighter_policy_and_can_protect_old_intents():
    old = resolve({})
    active = resolve(
        {"entry.price_guard_enabled": True, "entry.max_adverse_price_fraction": "0.001"}
    )
    with pytest.raises(SubmissionNotSent, match="PRICE_MOVED"):
        validate_entry_price(
            side="BUY",
            original="100",
            mark="100.2",
            stop="95",
            frozen=old,
            current=active,
        )
    with pytest.raises(SubmissionNotSent, match="PRICE_MOVED"):
        validate_entry_price(
            side="BUY",
            original="100",
            mark="100.2",
            stop="95",
            frozen=active,
            current=old,
        )
    assert (
        validate_entry_price(
            side="BUY", original="bad", mark="bad", stop="bad", frozen=old, current=old
        )
        is None
    )


@pytest.mark.parametrize(
    "remaining", ["0", "0.000000000000000001", "1", "5", "10", "20"]
)
@pytest.mark.parametrize("factor", ["1", "0.25"])
def test_sizing_respects_remaining_risk_after_rounding(remaining, factor):
    policy = resolve({**rehearsal_profile("10000"), "sizing.risk_fraction": "0.02"})
    plan = evaluate_market(**inputs(), policy=policy)
    result = size_candidate(
        plan,
        **sizing(drawdown_factor=factor),
        policy=policy,
        remaining_portfolio_risk=remaining,
    )
    if result.reason == "SIZED":
        assert Fraction(result.notional) * (
            Fraction(plan.stop_fraction)
            + Fraction(policy["sizing.cost_buffer_fraction"])
        ) <= Fraction(remaining)
    else:
        assert result.reason == "INSUFFICIENT_BUDGET"


@pytest.mark.parametrize(
    "original,expected_held", [(("5", "10"), "15"), (("8", "12"), "20")]
)
def test_budget_snapshot_and_final_guard_use_same_original_reservations(
    original, expected_held
):
    class Conn:
        def execute(self, q, p):
            assert "r.status='HELD'" in q and "LEFT JOIN v2_decision_evidence" in q
            assert "proposed_stop_risk" in q and "stop_price" in q
            self.params = p
            return self

        def fetchall(self):
            # Query now carries both frozen original risk and the actual fixed
            # stop. Neither may be discarded when residual risk is computed.
            return [
                ("100", "0.048", original[0], "1", "95.2", "LONG", None, None, None),
                ("200", "0.048", original[1], "2", "95.2", "LONG", None, None, None),
            ]

    conn = Conn()
    settings = resolve(rehearsal_profile("10000"))
    with localcontext() as ctx:
        ctx.prec = 6
        result = portfolio_budget(
            conn, SCOPE, settings=settings, capital={"base": "1000", "factor": "1"}
        )
    assert Decimal(result["held_stop_risk"]) == Decimal(expected_held)
    assert Decimal(result["remaining_stop_risk"]) == 30 - Decimal(expected_held)
    assert conn.params == (SCOPE.key, None, None)
    assert (
        Decimal(
            portfolio_budget(
                conn,
                SCOPE,
                settings=settings,
                capital={"base": "1000", "factor": "0.25"},
            )["remaining_stop_risk"]
        )
        == 0
    )


def test_structure_assessment_ready_wait_unknown_no_time_unlock():
    policy = resolve({"entry.reentry_enabled": True})
    previous = {
        "previous_episode": "prior",
        "side": "BUY",
        "exit_reason": "PEAK_DRAWDOWN",
        "exit_known_ms": 25 * 60000,
        "exit_price": "100",
    }
    signal = {"symbol": "BTCUSDT", "observed_at": 26 * 60000}

    def read(bars):
        return lambda target, start, end: [
            b.payload() for b in reversed(bars) if start <= b.t < end
        ]

    kwargs = {
        "previous": previous,
        "missing": None,
        "side": "BUY",
        "now": 26 * 60000,
        "settings": policy,
    }
    ready = assess(
        signal, read_bars=read(history(bar(25, h="102", c="101.5", v="20"))), **kwargs
    )
    assert ready["status"] == "READY" and ready["opportunity_token"]
    assert (
        assess(signal, read_bars=read(history(bar(25))), **kwargs)["status"] == "WAIT"
    )
    assert assess(signal, read_bars=lambda *_: [], **kwargs)["status"] == "UNKNOWN"
    old = {**kwargs, "now": 1000 * 60000}
    assert (
        assess(signal, read_bars=lambda *_: pytest.fail("bounded query"), **old)[
            "reason"
        ]
        == "REENTRY_HISTORY_BUDGET_EXCEEDED"
    )


@pytest.fixture
def history_episode(database, monkeypatch, request):
    # Supply immutable provenance when preparing the fixture, never rewrite it.
    if request.param:
        from v2_core.orders import Orders

        original = Orders.prepare

        def prepare(self, *args, **kwargs):
            if kwargs.get("leg") == "CLOSE":
                kwargs.update(
                    request_key="native-algo:99",
                    evidence={
                        "origin": "BINANCE_ALGO_CHILD",
                        "parent_algo_id": 99,
                        "parent_client_algo_id": "qa-parent",
                        "exchange_order_id": "11",
                        "venue_client_order_id": "qa-child",
                    },
                )
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Orders, "prepare", prepare)
    return closed_fixture.__wrapped__(database), request.param


@pytest.mark.parametrize("history_episode", [False, True], indirect=True)
def test_prior_close_is_scoped_and_never_confuses_unsettled_with_no_history(
    database, history_episode
):
    (runtime, episode, income), native_child = history_episode
    now = time.time_ns() // 1000000 + 1000
    with database() as c:
        assert prior_close(c, SCOPE, "BTCUSDT", now)[1] == "PREVIOUS_EPISODE_UNRESOLVED"
        assert (
            prior_close(c, replace(SCOPE, account_id="other"), "BTCUSDT", now)[1]
            == "NO_PREVIOUS_FILLED_EPISODE"
        )
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as c:
        previous, missing = prior_close(
            c, SCOPE, "BTCUSDT", time.time_ns() // 1000000 + 1000
        )
        assert (
            missing is None
            and previous["previous_episode"] == episode
            and previous["exit_price"] == "92.000000000000000000"
        )
        assert prior_close(c, SCOPE, "BTCUSDT", 3)[1] == "PREVIOUS_EPISODE_UNRESOLVED"
        if native_child:
            assert previous["exit_reason"] == "VENUE_PROTECTION_CHILD"


def test_sync_proof_allows_first_entry_and_is_durable(database):
    _, runtime, signal_id, context = scenario(database)
    settings = (
        PolicyStore(database, SCOPE)
        .patch(
            {"entry.reentry_enabled": True}, expected_version=0, reason="isolated QA"
        )
        .values
    )
    with database() as c:
        signal = c.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    provider = StructuralReentryContext(
        database,
        lambda _: context,
        scope=SCOPE,
        strategy="S6",
        read_bars=lambda *_: pytest.fail("first entry needs no archive"),
        clock_ms=lambda: 3,
    )
    full = provider(signal)
    assert full["reentry"]["assessment"]["reason"] == "NO_PREVIOUS_FILLED_EPISODE"
    admission = formal(runtime)
    admission.config_json = canonical(
        {**json.loads(admission.config_json), "policy": settings}
    )
    result = admission.consume(signal_id, context=full)
    assert result["status"] == "PREPARED"
    saved = admission.decision(signal_id)["snapshot"]["features"]["evaluation"][
        "reentry"
    ]
    assert saved == full["reentry"]
    assert admission.consume(signal_id, context={})["intent_id"] == result["intent_id"]


def test_sync_wait_is_terminal_receipt_not_retried_queue_failure(database, monkeypatch):
    from services import v2_reentry_gate as module

    _, runtime, signal_id, context = scenario(database)
    settings = (
        PolicyStore(database, SCOPE)
        .patch(
            {"entry.reentry_enabled": True}, expected_version=0, reason="isolated QA"
        )
        .values
    )
    monkeypatch.setattr(
        module, "prior_close", lambda *_: (None, "PREVIOUS_EPISODE_UNRESOLVED")
    )
    with database() as c:
        signal = c.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    full = StructuralReentryContext(
        database,
        lambda _: context,
        scope=SCOPE,
        strategy="S6",
        read_bars=lambda *_: pytest.fail("no evidence"),
        clock_ms=lambda: 3,
    )(signal)
    admission = formal(runtime)
    admission.config_json = canonical(
        {**json.loads(admission.config_json), "policy": settings}
    )
    assert admission.consume(signal_id, context=full)["reason"] == "REENTRY_UNKNOWN"
    assert admission.consume(signal_id, context={})["status"] == "IGNORED"
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM v2_signal_receipts").fetchone()[0] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"valid_until_ms": 3},
        {"observed_at_ms": 4},
        {"source": "ASYNCHRONOUS_DECISION_SHADOW"},
    ],
)
def test_proof_rejects_stale_foreign_and_shadow_facts(mutation):
    proof = {
        "source": "synchronous-reentry-v1",
        "account_scope": asdict(SCOPE),
        "symbol": "BTCUSDT",
        "side": "BUY",
        "signal_observed_ms": 1,
        "observed_at_ms": 2,
        "finished_at_ms": 2,
        "valid_until_ms": 4,
        "assessment": {
            "status": "NOT_APPLICABLE",
            "reason": "NO_PREVIOUS_FILLED_EPISODE",
        },
        "previous_episode": None,
        "execution_authorized": False,
        "policy": asdict(structure_policy(resolve({}))),
    }
    proof.update(mutation)
    proof["digest"] = digest(canonical(proof))
    with pytest.raises(ValueError, match="REENTRY_PROOF"):
        validate(
            proof, scope=asdict(SCOPE), target="BTCUSDT", side="BUY", observed=1, now=3
        )


def test_missing_portfolio_context_cannot_authorize_admission(database):
    _, runtime, signal, context = scenario(database)
    admission = formal(runtime)
    admission.config_json = canonical(
        {**json.loads(admission.config_json), "policy": rehearsal_profile("10000")}
    )
    with pytest.raises(ValueError, match="PORTFOLIO_SIZING_EVIDENCE_REQUIRED"):
        admission.consume(signal, context=context)


@pytest.mark.parametrize(
    "case", ["ready", "price", "expired", "policy", "prior", "slow_readiness"]
)
def test_real_submission_binding_rechecks_and_never_sends_rejected_entries(
    database, monkeypatch, case
):
    from test_v2_venue_readiness import REF, Venue

    from services import v2_reentry_gate as module
    from services.v2_directional_execution import bind_directional_venue_gate

    _, runtime, signal_id, context = scenario(database)
    settings = (
        PolicyStore(database, SCOPE)
        .patch(
            {"entry.reentry_enabled": True, "entry.price_guard_enabled": True},
            expected_version=0,
            reason="isolated QA",
        )
        .values
    )
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    full = StructuralReentryContext(
        database,
        lambda _: context,
        scope=SCOPE,
        strategy="S6",
        read_bars=lambda *_: pytest.fail("first entry"),
        clock_ms=lambda: 3,
    )(signal)
    admission = formal(runtime)
    admission.config_json = canonical(
        {**json.loads(admission.config_json), "policy": settings}
    )
    result = admission.consume(signal_id, context=full)
    desired = admission.decision(signal_id)["snapshot"]["features"]["evaluation"][
        "market_plan"
    ]
    venue, sent = Venue(), []
    venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=desired["leverage"],
        marginType=desired["margin_mode"],
    )
    runtime.execution.submit = lambda order: sent.append(order) or "sent"
    if case == "expired":
        runtime.clock_ms = lambda: 9
    if case == "policy":
        PolicyStore(database, SCOPE).patch(
            {"entry.reentry_reset_atr": "0.6"},
            expected_version=1,
            reason="isolated QA",
        )
    if case == "prior":
        monkeypatch.setattr(
            module, "prior_close", lambda *_: (None, "PREVIOUS_EPISODE_UNRESOLVED")
        )
    gate = bind_directional_venue_gate(
        runtime,
        venue,
        enabled=True,
        reduce_only_enabled=True,
        mark_reference=lambda _: {
            **REF,
            "observed_at_ms": 3,
            "mark_price": "102" if case == "price" else "100",
        },
    )
    if case == "slow_readiness":
        original_inspect = gate.readiness.inspect

        def slow_inspect(**kwargs):
            proof = original_inspect(**kwargs)
            gate.readiness.clock = lambda: 9
            return proof

        gate.readiness.inspect = slow_inspect
    runtime.data.orders.transition(
        result["order_id"], expected_version=1, status="SUBMITTING", evidence={}
    )
    order = runtime.execution.snapshot(result["order_id"])
    if case == "ready":
        assert gate(order) == "sent" and len(sent) == 1
    else:
        with pytest.raises(
            SubmissionNotSent,
            match={
                "price": "DIRECTIONAL_SUBMISSION_PRICE_MOVED",
                "slow_readiness": "VENUE_READINESS_BLOCKED",
            }.get(case, "REENTRY_SUBMISSION_BLOCKED"),
        ):
            gate(order)
        assert not sent
    sent.clear()
    # A reduction needs its own durable identity, even with closing enabled.
    with pytest.raises(SubmissionNotSent, match="REDUCE_ONLY_REVALIDATION_FAILED"):
        gate({**order, "leg": "CLOSE", "reduce_only": True})
    assert not sent


def test_account_context_persists_remaining_stop_risk_for_sizing(database):
    from test_v2_directional_account_context import build, signal

    provider, request, _, _, _ = build(database)
    request.account["totalMarginBalance"] = "1000"
    PolicyStore(database, SCOPE).patch(
        {
            "capital.enabled": True,
            "portfolio_risk.enabled": True,
            "sizing.pool_fraction": "0.70",
        },
        expected_version=0,
        reason="isolated QA",
    )
    result = provider(signal())
    assert Decimal(result["sizing"]["remaining_portfolio_risk"]) == 24
    with database() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (result["evidence"]["state_id"],),
        ).fetchone()[0]
    assert Decimal(payload["portfolio_stop_budget"]["remaining_stop_risk"]) == 24
