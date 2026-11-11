"""Isolated real-PG integration; every venue port below is an in-memory fake.

Run with the existing disposable QA database fixture, never the trading DB.
This proves provider/evidence/admission/prepared fencing, not real exchange PnL.
"""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_directional_replay import SCOPE
from test_v2_layered_history import row as history_row
from test_v2_layered_history import seed as seed_history
from test_v2_venue_readiness import Venue

from services.v2_directional_admission import create_directional_admission_worker
from services.v2_directional_replay import create_directional_replay_worker, market_plan
from services.v2_entry_research_context import EntryResearchContext
from services.v2_entry_structure import EntryStructureContext
from tests.support.v2_admission import database
from v2_core.capital_model import rehearsal_profile
from v2_core.entry_controls import configure
from v2_core.evidence import canonical
from v2_core.runner import ExchangeObservation, ExecutionRunner
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness

__all__ = ["database"]
NOW = 1_800_000_001_000
CUTOFF = NOW // 60000 * 60000


def build(database, *, mode="ENFORCE", seeded=False):
    policy = PolicyStore(database, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA base capital"
    )
    controls = configure(
        database,
        SCOPE,
        {
            "history.mode": "SHADOW",
            "reentry.mode": mode,
            "sizing.remaining_budget_enabled": True,
        },
        expected_version=0,
        reason="QA narrow reentry and history shadow",
    )
    facts = inputs()
    event = {**facts["event"], "strength": 70}
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": NOW - 10,
        "expires_at_ms": NOW + 60000,
        "signal": event["type"],
        "features": event,
    }
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    limits.update(
        remaining_portfolio_risk="30",
        sizing_mark_price="100",
        price_drift_fraction="0.015",
    )
    base = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": NOW,
        "valid_until_ms": NOW + 60000,
        "account_scope": asdict(SCOPE),
        "sources": {"s3": {"snapshot_id": "qa-full-flow", "observed_at": NOW}},
        "account_evidence": {"entry_controls": controls.snapshot()},
        "directional": {
            "market": facts["market"],
            "price": facts["price"],
            "regime": facts["regime"],
            "short_ratio": facts["short_ratio"],
            "history": history(),
            "sizing": limits,
            "expected_move_pct": "8",
            "funding_rate": "0",
        },
    }
    if seeded:
        plan, _ = market_plan(
            signal, base, {"strategy": "S6", "source": "s3", "policy": policy.values}
        )
        sample = history_row(scope=SCOPE)
        sample.update(
            plan=asdict(plan),
            closed_at_ms=CUTOFF - 120000,
            latest_fill_ms=CUTOFF - 120000,
            latest_fill_known_at_ms=CUTOFF - 120000,
            close_reason="PEAK_DRAWDOWN",
        )
        seed_history(database, sample)
    archive_calls = []

    def archive(target, start, end):
        archive_calls.append((target, start, end))
        result = []
        for opened in range(start, end, 60000):
            bar = {
                "t": opened,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100",
                "v": "10",
            }
            if opened == CUTOFF - 120000:
                bar.update(h="102", c="101.5", v="20")
            if opened == CUTOFF - 60000:
                bar.update(o="101.5", h="111", c="110", v="20")
            result.append(bar)
        return list(reversed(result))

    structural = EntryStructureContext(
        database,
        lambda _: deepcopy(base),
        scope=SCOPE,
        strategy="S6",
        read_bars=archive,
        clock_ms=lambda: NOW,
        archive_clock_ms=lambda: NOW,
    )
    context = EntryResearchContext(
        database,
        structural,
        scope=SCOPE,
        strategy="S6",
        source="s3",
        clock_ms=lambda: NOW,
    )(signal)
    runtime = DataRuntime(
        database,
        scope=SCOPE,
        clock_ms=lambda: NOW,
        submit=lambda _: pytest.fail("no real submission port"),
        query=lambda _: pytest.fail("no real query port"),
        risk_check=lambda _: True,
        risk_reference=lambda _: pytest.fail("admission must not submit"),
    )
    signal_id = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key="full-flow",
        snapshot=signal,
    )
    replay = create_directional_replay_worker(
        runtime, strategy="S6", analysis_mode="hard", max_delay_ms=30000
    )
    admission = create_directional_admission_worker(
        runtime,
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=30000,
        enable_admission=True,
    )
    # Mirror TradingPipeline.load_policy rather than silently use worker defaults.
    for worker in (replay, admission):
        worker.config_json = canonical(
            {
                **json.loads(worker.config_json),
                "policy": policy.values,
                "policy_version": policy.version,
                "policy_digest": policy.digest,
            }
        )
    return runtime, replay, admission, signal_id, context, controls, archive_calls


def fake_dispatch(database, admission, signal_id, order_id):
    evaluation = admission.decision(signal_id)["snapshot"]["features"]["evaluation"]
    plan = evaluation["market_plan"]
    venue = Venue()
    venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=plan["leverage"], marginType=plan["margin_mode"]
    )
    sent = []

    def submit(order):
        sent.append(order)
        return ExchangeObservation(
            order["client_order_id"],
            "ACKNOWLEDGED",
            "qa-local-only",
            evidence={"source": "in-memory-qa"},
        )

    gate = GuardedOpeningSubmit(
        database,
        readiness=TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=lambda: NOW),
        reference=lambda target: {
            "symbol": target,
            "environment": "SANDBOX",
            "mark_price": "100",
            "observed_at_ms": NOW,
        },
        plan=lambda _: {
            "leverage": plan["leverage"],
            "margin_type": plan["margin_mode"],
        },
        submit=submit,
        enabled=True,
    )
    runner = ExecutionRunner(
        database,
        submit=gate,
        query=lambda _: None,
        risk_check=lambda _: True,
        scope=SCOPE,
    )
    result = runner.dispatch(order_id)
    proof = json.loads(
        BusinessState(database)
        .read(StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order_id))
        .payload_json
    )
    return result, sent, proof


def test_real_pg_provider_replay_prepare_guard_preserves_both_experiments(database):
    _, replay, admission, signal_id, context, controls, calls = build(database)
    assert (
        context["entry_structure"]["assessment"]["reason"]
        == "NO_PREVIOUS_FILLED_EPISODE"
    )
    assert context["layered_history"]["read_error"] is None
    assert context["layered_history"]["mode"] == "SHADOW"
    assert context["layered_history"]["applied_factor"] == "1"
    assert not calls
    assert (
        replay.consume(signal_id, context=context)["reason"]
        == "DIRECTIONAL_REPLAY_ONLY"
    )
    prepared = admission.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    saved = admission.decision(signal_id)["snapshot"]
    assert saved["features"]["context"]["entry_controls"] == controls.snapshot()
    assert (
        saved["features"]["evaluation"]["sizing_budget"]["version"]
        == "remaining-risk-sizing-v1"
    )
    assert (
        saved["features"]["evaluation"]["layered_history"] == context["layered_history"]
    )
    assert (
        saved["features"]["evaluation"]["entry_structure"] == context["entry_structure"]
    )
    assert saved["expires_at_ms"] <= context["valid_until_ms"]
    status, sent, proof = fake_dispatch(
        database, admission, signal_id, prepared["order_id"]
    )
    assert status == "ACKNOWLEDGED" and len(sent) == 1
    assert proof["status"] == "READY_FOR_GUARDED_SUBMISSION"
    assert proof["execution_authorized"] is False


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("SHADOW", "DIRECTIONAL_REPLAY_ONLY"),
        ("ENFORCE", "REENTRY_STRUCTURE_CHASE_TOO_FAR"),
    ],
)
def test_actual_pg_settlement_anchor_chase_veto_is_narrow_and_auditable(
    database, mode, reason
):
    _, replay, admission, signal_id, context, _, calls = build(
        database, mode=mode, seeded=True
    )
    assert (
        context["entry_structure"]["assessment"]["reason"] == "STRUCTURE_CHASE_TOO_FAR"
    ), context["entry_structure"]["assessment"]
    assert (
        context["entry_structure"]["archive_read_finished_ms"]
        <= context["entry_structure"]["as_of_ms"]
    )
    assert context["layered_history"]["queried_count"] == 1
    assert context["layered_history"]["read_error"] is None
    assert len(calls) == 1
    assert replay.consume(signal_id, context=context)["reason"] == reason
    result = admission.consume(signal_id, context=context)
    assert result["status"] == ("IGNORED" if mode == "ENFORCE" else "PREPARED")
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == (
            2 if mode == "ENFORCE" else 3
        )
    frozen = replay.decision(signal_id)["snapshot"]["features"]["context"]
    assert frozen["layered_history"] == context["layered_history"]
    assert frozen["entry_structure"] == context["entry_structure"]


def test_prepared_after_new_providers_rejected_if_control_changes_before_send(database):
    _, _, admission, signal_id, context, controls, _ = build(database)
    prepared = admission.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    configure(
        database,
        SCOPE,
        {"history.mode": "SOFT"},
        expected_version=controls.version,
        reason="QA changed after preparation",
    )
    status, sent, proof = fake_dispatch(
        database, admission, signal_id, prepared["order_id"]
    )
    assert status == "REJECTED" and sent == []
    assert "ENTRY_CONTROLS_CHANGED_REPROPOSE" in proof["blockers"]
