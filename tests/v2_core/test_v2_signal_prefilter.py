"""Signal-only rejection shares the full evaluator's gate; never grants entry."""

import json

import pytest
from test_v2_directional_replay import scenario

from services.v2_directional_admission import (
    admission_decision,
    create_directional_admission_scheduler,
    signal_only_rejection,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.directional import below_minimum_strength
from v2_core.evidence import canonical

database = database_fixture


def signal(strength=40, kind="TREND_DOWN"):
    return {
        "symbol": "BTCUSDT",
        "observed_at": 2,
        "expires_at_ms": 10,
        "signal": kind,
        "features": {"type": kind, "strength": strength},
    }


def config(source="s3", policy=None):
    return {
        "strategy": "S8",
        "source": source,
        "policy": policy or {},
        "mode": "TESTNET_ADMISSION_ONLY",
        "dynamic_universe": True,
        "tv_start_ms": 1,
        "account_scope": {"environment": "SANDBOX"},
    }


@pytest.mark.parametrize("kind", ["TREND_DOWN", "VIOLENT_BEARISH", "PULSE_DOWN"])
@pytest.mark.parametrize("strength", [0, 19, 20, 40, 59, 60, 99, 100])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_source_threshold_matches_full_rule(kind, strength, source):
    policy = {"tv.trend_policy_enabled": True, "tv.trend_min_strength": "20"}
    threshold = 20 if source == "tv_bridge" and kind == "TREND_DOWN" else 60
    result = signal_only_rejection(signal(strength, kind), config(source, policy))
    assert (result is not None) == (strength < threshold)
    assert below_minimum_strength(
        "S8", kind, str(strength), policy={"entry.short_min_strength": str(threshold)}
    ) == (strength < threshold)
    if result:
        assert result.action == "IGNORED"
        proof = json.loads(result.features_json)
        assert proof["stage"] == "SIGNAL_ONLY_PREFILTER"
        assert proof["source_scoring"]["raw_strength"] == strength
        assert proof["execution_authorized"] is False


@pytest.mark.parametrize("kind", ["PUMP_DOWN", "PANIC_SELL", "TREND_UP", "HIGH_VOL"])
def test_other_events_never_inherit_the_short_strength_gate(kind):
    assert signal_only_rejection(signal(0, kind), config()) is None


def test_tv_opt_out_and_quant_keep_their_own_path():
    assert (
        signal_only_rejection(
            signal(), config("tv_bridge", {"tv.trend_policy_enabled": False})
        )
        is not None
    )
    assert signal_only_rejection(signal(), config("quant")) is None


@pytest.mark.parametrize("strength", ["NaN", "-1", "101", True, 0.2])
def test_malformed_strength_does_not_get_a_successful_rejection_receipt(strength):
    with pytest.raises((ValueError, TypeError)):
        signal_only_rejection(signal(strength), config())


def test_conflicting_event_type_is_not_hidden_by_prefilter():
    value = signal()
    value["features"]["type"] = "TREND_UP"
    with pytest.raises(ValueError, match="conflicting event type"):
        signal_only_rejection(value, config())


def test_passing_strength_does_not_bypass_contract_or_market_proof():
    with pytest.raises(ValueError, match="CONTRACT_ADMISSION_EVIDENCE_REQUIRED"):
        admission_decision(signal(99), {}, config())
    with pytest.raises(ValueError, match="CONTRACT_ADMISSION_EVIDENCE_REQUIRED"):
        admission_decision(
            signal(40), {}, config("tv_bridge", {"tv.trend_policy_enabled": True})
        )


@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_weak_signal_is_audited_once_without_context_or_orders(database, source):
    _, runtime, _, _ = scenario(database, "S8")
    runtime.execution.risk_reference = lambda _: pytest.fail("no dispatch")
    scheduler = create_directional_admission_scheduler(
        runtime,
        context_provider=lambda _: pytest.fail("weak input needs no external context"),
        strategy="S8",
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
        source=source,
        dynamic_universe=True,
        tv_start_ms=1,
    )
    with database() as c:
        for (sid,) in c.execute("SELECT signal_id FROM v2_inbound_signals").fetchall():
            c.execute(
                "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) VALUES(%s,%s,'IGNORED','QA fixture')",
                (scheduler.worker.scope.consumer, sid),
            )
    sid = runtime.data.signals.admit(
        source=source, environment="SANDBOX", request_key="weak", snapshot=signal(1)
    )
    assert scheduler.run_once()[sid] == "IGNORED"
    decision = scheduler.worker.decision(sid)
    assert decision["snapshot"]["rationale"] == "strength_below_minimum"
    assert decision["snapshot"]["features"]["context"] == {}
    assert scheduler.run_once() == {}
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
    # The prefilter must read the worker's newly selected policy, not closure state.
    scheduler.worker.config_json = canonical(
        {
            **json.loads(scheduler.worker.config_json),
            "policy": {
                "entry.short_min_strength": "30",
                "tv.trend_policy_enabled": False,
            },
        }
    )
    assert scheduler.context_required(signal(40)) is True
    scheduler.worker.config_json = canonical(
        {
            **json.loads(scheduler.worker.config_json),
            "policy": {
                "entry.short_min_strength": "60",
                "tv.trend_policy_enabled": False,
            },
        }
    )
    assert scheduler.context_required(signal(40)) is False
