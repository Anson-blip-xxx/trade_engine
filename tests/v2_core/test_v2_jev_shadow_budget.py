"""Observer metadata must not change baseline evidence admission limits."""

import json
from contextlib import nullcontext
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from v2_core import strategy as subject
from v2_core import trend_opportunities
from v2_core.evidence import canonical, digest
from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker


class _Captured(Exception):
    """Stop immediately after validated evidence reaches its persistence port."""


@pytest.fixture
def harness(monkeypatch):
    stored = []

    def capture(proof):
        stored.append(proof)
        raise _Captured()

    monkeypatch.setattr(
        subject, "EvidenceStore", lambda _: SimpleNamespace(put=capture)
    )
    monkeypatch.setattr(
        trend_opportunities, "claim_trend_opportunity", lambda *_args, **_kwargs: None
    )
    conn = SimpleNamespace(execute=lambda *_: None)
    runtime = SimpleNamespace(
        data=SimpleNamespace(_connect=lambda: nullcontext(conn)), _now=lambda: 10
    )
    signal_id = str(uuid4())
    signal = {
        "observed_at": 1,
        "expires_at_ms": 100,
        "symbol": "BTCUSDT",
        "signal": "PUMP_UP",
        "features": {},
    }
    baseline = StrategyDecision(
        "OPEN",
        "BASELINE_AUTHORIZED",
        side="BUY",
        quantity="1",
        features_json=canonical(
            {"admission_authorized": True, "execution_authorized": False}
        ),
    )
    calls = []

    def receipt(**request):
        calls.append(request)
        return {
            "schema": "jev-shadow-receipt-v1",
            "signal_id": request["signal_id"],
            "consumer": request["consumer"],
            "candidate_digest": digest(canonical(asdict(request["decision"]))),
            "decision_effect": "NONE",
            "execution_authorized": False,
            "risk_override": False,
            "admission_authorized": False,
            "status": "SUBMITTED_NOT_DURABLE",
            "coverage": "MISSING_UNTIL_DURABLE_RESULT",
        }

    def consume(padding, respond=None):
        worker = StrategyWorker(
            runtime,
            StrategyScope("BINANCE", "qa-shadow-budget", "SANDBOX", "FUTURES", "s6"),
            source="s3",
            strategy_version="qa-v1",
            config={"jev_mode": "SHADOW"},
            decide=lambda *_: baseline,
            max_delay_ms=50,
            entry_observer=None if respond is None else SimpleNamespace(submit=respond),
        )
        worker._read = lambda *_: (signal, None, None)
        with pytest.raises(_Captured):
            worker.consume(signal_id, context={"padding": "x" * padding})
        return stored[-1]

    return SimpleNamespace(
        consume=consume, receipt=receipt, calls=calls, stored=stored, baseline=baseline
    )


@pytest.mark.parametrize("target_bytes", [999999, 1000000])
def test_optional_receipt_overflow_preserves_exact_valid_baseline(
    harness, target_bytes
):
    empty = harness.consume(0)
    overhead = len((empty.config_json + empty.snapshot_json).encode())
    baseline = harness.consume(target_bytes - overhead)

    def receipt(**request):
        return {**harness.receipt(**request), "detail": "r" * 10000}

    observed = harness.consume(target_bytes - overhead, receipt)
    assert observed == baseline
    assert len((observed.config_json + observed.snapshot_json).encode()) == target_bytes
    snapshot = json.loads(observed.snapshot_json)
    assert snapshot["features"]["evaluation"] == json.loads(
        harness.baseline.features_json
    )
    assert snapshot["result"] == {"action": "OPEN", "side": "BUY", "quantity": "1"}
    assert json.loads(observed.config_json)["jev_mode"] == "SHADOW"
    assert len(harness.calls) == 1


@pytest.mark.parametrize("observer", [False, True])
def test_oversized_baseline_still_rejected_with_or_without_observer(harness, observer):
    empty = harness.consume(0)
    padding = 1000001 - len((empty.config_json + empty.snapshot_json).encode())
    with pytest.raises(ValueError, match="decision evidence exceeds size limit"):
        harness.consume(padding, harness.receipt if observer else None)
    assert harness.stored == [empty]


@pytest.mark.parametrize(
    "status", ["PASS", "VETO", "OBSERVED", "ADVISORY_READY", "UNKNOWN", None, True]
)
def test_nonreceipt_status_is_recorded_as_missing_without_changing_baseline(
    harness, status
):
    def receipt(**request):
        return {**harness.receipt(**request), "status": status}

    proof = harness.consume(0, receipt)
    snapshot = json.loads(proof.snapshot_json)
    evaluation = snapshot["features"]["evaluation"]
    shadow = evaluation.pop("jev_shadow")
    assert shadow["status"] == "OBSERVER_UNAVAILABLE"
    assert shadow["coverage"] == "MISSING"
    assert evaluation == json.loads(harness.baseline.features_json)
    assert snapshot["result"] == {"action": "OPEN", "side": "BUY", "quantity": "1"}


@pytest.mark.parametrize("status", sorted(subject._SHADOW_RECEIPT_STATUSES))
def test_production_receipt_statuses_remain_distinct(harness, status):
    def receipt(**request):
        return {**harness.receipt(**request), "status": status, "coverage": "MISSING"}

    proof = harness.consume(0, receipt)
    shadow = json.loads(proof.snapshot_json)["features"]["evaluation"]["jev_shadow"]
    assert shadow["status"] == status
    assert shadow["coverage"] == "MISSING"
