"""Cold unbound refresh waits use real PG/worker, synthetic market and SDK only."""

import json
import time
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_directional import inputs
from test_v2_directional_replay import SCOPE
from test_v2_jev_entry_context import model_response
from test_v2_trading_pipeline import case as pipeline_fixture

from services.v2_jev_coordinator import JevCoordinator
from services.v2_jev_entry_context import JevEntryContext, policy_key
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from services.v2_s3_runtime import S3Runtime
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy
from v2_core.jev_advisory import AdvisoryPolicy, JevAdvisory
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.producer import market_envelope
from v2_core.state import BusinessState

database = database_fixture
pipeline_case = pipeline_fixture


def no_trade_records(database, *, decisions=0):
    with database() as conn:
        for table in ("v2_trade_intents", "v2_orders"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        for table in ("v2_strategy_decisions", "v2_signal_receipts"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (
                decisions,
            )


@pytest.mark.parametrize("finish", ["MODEL_PASS", "FRESH_RISK_REJECT", "EXPIRED"])
def test_unbound_stale_market_restart_fresh_source_and_original_expiry(
    database, pipeline_case, finish
):
    pipeline, _, account, envelopes, _ = pipeline_case
    # Produce the immutable signal and its referenced source at the SAME old
    # observation time. Both retain their original 90s lifetime; do not first
    # create a fresh signal then rewind its source, which would correctly fail
    # ContextProvider's producer-context monotonicity check before JEV.
    old_observed = time.time_ns() // 1000000 - 66000
    publisher = pipeline.market.source.publisher
    publisher.max_age_ms = publisher.lifetime_ms = 90000
    frames = inputs()["market"]
    frames["15m"].update(close="100", chg="0.5", vol_ratio="1")
    frames["1h"].update(chg="4")
    frames["4h"].update(chg="3")
    produced = S3Runtime(publisher).process(
        frame_id="qa-old-source-valid-signal",
        observed_at=old_observed,
        windows={"BTCUSDT": frames},
        raw_windows={
            "BTCUSDT": {
                "4h": [{"h": "110", "l": "90", "c": "100"}] * 3,
                "15m": [{"h": "101", "l": "99", "c": "100"}] * 3,
            }
        },
    )
    assert produced["market_status"] == "PROJECTED"
    with database() as conn:
        signal_id, signal = conn.execute(
            "SELECT signal_id::text,snapshot FROM v2_inbound_signals "
            "WHERE source='s3' AND snapshot->>'signal'='TREND_UP'"
        ).fetchone()
    original_signal = deepcopy(signal)
    scheduler = pipeline.schedulers[0]
    worker, runtime = scheduler.worker, pipeline.runtime
    clock = SimpleNamespace(offset=0)
    clock.now = lambda: time.time_ns() // 1000000 + clock.offset
    runtime.clock_ms = clock.now
    scheduler.context_provider.clock = clock.now
    scheduler.context_provider.market.clock_ms = clock.now
    runtime.account_risk.configure(
        SCOPE,
        AccountPolicy("USDT", "10000", 10, 0, "test-mark", 60000),
        expected_version=0,
    )
    queued, model_calls, evaluated = [], [], []
    reference = SimpleNamespace(submit=lambda value: queued.append(deepcopy(value)))
    provider_policy = AdvisoryPolicy(enabled=True)
    store = BusinessState(database)
    for key, policy in (
        (CONTROL_KEY, provider_policy),
        (policy_key(SCOPE), JevEntryGatePolicy(enabled=True, max_wait_ms=30000)),
    ):
        assert store.change(
            key,
            expected_version=0,
            request_key=str(uuid4()),
            payload=asdict(policy),
            reason="isolated QA market refresh only",
        ).code == "APPLIED"
    original_decide = worker.decide

    def decide(candidate, context, config):
        result = original_decide(candidate, context, config)
        evaluated.append((deepcopy(context["account_evidence"]), result.action))
        return result

    worker.decide = decide
    source_key = ("s3", "SANDBOX", "BTCUSDT")
    original_features = deepcopy(envelopes[source_key]["features"])
    assert envelopes[source_key]["observed_at"] == old_observed
    assert signal["observed_at"] == old_observed
    assert signal["expires_at_ms"] == old_observed + 90000

    def fresh_context():
        now = clock.now()
        account.update(observed_at_ms=now, valid_until_ms=now + 60000)
        account["evidence"]["qa_refresh_generation"] = len(evaluated) + 1
        return scheduler.context_provider(signal)

    binding = JevEntryContext(database, reference, scope=SCOPE).binding_key(
        worker.scope.consumer, signal_id
    )
    for _ in range(2):
        # Simulate a cold worker gate: no in-process cache grants authority.
        worker.entry_filter = JevEntryContext(database, reference, scope=SCOPE)
        result = worker.consume(signal_id, context=fresh_context())
        assert result["status"] == "DEFERRED", result
        assert result["reason"] == "JEV_MARKET_REFRESH_PENDING"
        assert store.read(binding) is None
        assert not queued and not model_calls
        assert envelopes[source_key]["observed_at"] == old_observed
        no_trade_records(database)
    assert len(evaluated) == 2 and all(action == "OPEN" for _, action in evaluated)
    assert evaluated[0][0] != evaluated[1][0]

    if finish == "EXPIRED":
        clock.offset = signal["expires_at_ms"] - clock.now() + 1
    # This is a newly observed synthetic producer envelope, not retiming the
    # old saved snapshot. The immutable original signal remains unchanged.
    envelopes[source_key] = market_envelope(
        "s3", "SANDBOX", "BTCUSDT", clock.now(), deepcopy(original_features)
    )
    if finish == "EXPIRED":
        assert worker.consume(signal_id, context={})["status"] == "EXPIRED"
        assert store.read(binding) is None and not queued
        no_trade_records(database, decisions=1)
    elif finish == "FRESH_RISK_REJECT":
        account["sizing"]["available_margin"] = "0"
        result = worker.consume(signal_id, context=fresh_context())
        assert result["status"] == "IGNORED", result
        assert evaluated[-1][1] != "OPEN"
        assert store.read(binding) is None and not queued
        no_trade_records(database, decisions=1)
    else:
        result = worker.consume(signal_id, context=fresh_context())
        assert result["status"] == "DEFERRED" and result["reason"] == "JEV_RESULT_PENDING"
        assert len(queued) == 1
        saved = store.read(binding)
        proof = json.loads(saved.payload_json)
        assert proof["public_features"]["observed_at_ms"] > old_observed
        assert proof["deadline_ms"] <= original_signal["expires_at_ms"]
        no_trade_records(database)

        def fake_sdk(request, *, timeout_ms):
            assert 0 < timeout_ms <= 1500
            model_calls.append(request)
            return model_response(trend="0.8", chase="0.2")

        result = JevAdvisory(
            JevCoordinator(database, scope=PUBLIC_SCOPE, enabled=True),
            fake_sdk,
            policy=provider_policy,
        ).evaluate(queued[0])
        assert result["status"] == "ADVISORY_READY"
        worker.entry_filter = JevEntryContext(database, reference, scope=SCOPE)
        result = worker.consume(signal_id, context=fresh_context())
        assert result["status"] == "PREPARED", result
        assert store.read(binding) == saved
        assert len(queued) == len(model_calls) == 1
        assert evaluated[-1][0] != evaluated[-2][0]
        with database() as conn:
            assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)
    with database() as conn:
        assert conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone() == (original_signal,)
