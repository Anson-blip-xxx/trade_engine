"""A stale symbol is a bounded wait, never account-wide trading authority.

All persistence uses the explicitly isolated QA fixture. No venue transport is
constructed; the pipeline fixture rejects every attempted order submission.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_directional_admission import create_directional_admission_scheduler
from services.v2_directional_context import DirectionalContext
from tests.support.v2_admission import database as database_fixture
from v2_core.ingress import ContextProvider, IntakeRejected
from v2_core.producer import market_envelope
from v2_core.runtime_policy import PolicyStore
from v2_core.transport import ExchangeTransportError

database = database_fixture
case = pipeline_case


def tv_schedulers(pipeline, now, context_provider):
    return tuple(
        create_directional_admission_scheduler(
            pipeline.runtime,
            context_provider=context_provider,
            strategy=strategy,
            analysis_mode="hard",
            max_delay_ms=30000,
            enable_admission=True,
            source="tv_bridge",
            allowed_symbols=("ACUUSDT", "BTCUSDT"),
            tv_start_ms=now,
        )
        for strategy in ("S6", "S8")
    )


def tv_signal(pipeline, now, *, symbol="ACUUSDT", ttl=60000):
    return pipeline.runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="context-isolation-" + symbol,
        snapshot={
            "symbol": symbol,
            "observed_at": now,
            "expires_at_ms": now + ttl,
            "signal": "TREND_UP",
            "features": {"tv_signal": "TREND_UP_LONG", "strength": 99, "price": "100"},
        },
    )


def stale_symbol_context(now, *, read_override=None):
    envelopes = {
        ("s0", "SANDBOX", "*"): market_envelope(
            "s0", "SANDBOX", "*", now, {"regime": "neutral"}
        ),
        ("s3", "SANDBOX", "ACUUSDT"): market_envelope(
            "s3", "SANDBOX", "ACUUSDT", now - 60001, {"stale": True}
        ),
    }
    return ContextProvider(
        environment="SANDBOX",
        policy={
            "s0": {"scope": "GLOBAL", "max_age_ms": 60000},
            "s3": {"scope": "SYMBOL", "max_age_ms": 60000},
        },
        read=read_override or (lambda *key: deepcopy(envelopes[key])),
        clock_ms=lambda: now,
    )


def assert_no_signal_business(database, identifier):
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions WHERE signal_id=%s",
            (identifier,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s", (identifier,)
        ).fetchone() == (0,)


def test_real_stale_tv_context_does_not_block_other_source_in_same_cycle(
    case, database
):
    pipeline, _, _, _, now = case
    television = tv_schedulers(pipeline, now, stale_symbol_context(now))
    pipeline.schedulers += television
    identifier = tv_signal(pipeline, now)
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": True, "scheduler.evaluation_budget_ms": 16000},
        expected_version=0,
        reason="QA stale TV must not block a fresh S3 candidate",
    )
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert result["phases"]["s6:tv"][identifier] == "DEFERRED"
    assert result["scheduler_diagnostics"]["s6:tv_bridge"]["failures"] == [
        {
            "signal_id": identifier,
            "attempt": 1,
            "stage": "CONTEXT",
            "error_code": "STALE_CONTEXT",
            "outcome": "DEFERRED",
        }
    ]
    assert any(status == "PREPARED" for status in result["phases"]["s6"].values())
    assert television[0].worker.decision(identifier) is None
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE payload->>'symbol'='ACUUSDT'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT error_code,completed_at IS NULL,lease_token IS NULL FROM v2_strategy_tasks "
            "WHERE signal_id=%s AND consumer=%s",
            (identifier, television[0].worker.scope.consumer),
        ).fetchone() == ("STALE_CONTEXT", True, True)


def test_stale_tv_does_not_stop_fresh_tv_peer_in_same_batch(case, database):
    pipeline, _, _, _, now = case
    pipeline.market.run_once()
    stale = stale_symbol_context(now)
    fresh = pipeline.schedulers[0].context_provider
    scheduler = tv_schedulers(
        pipeline,
        now,
        lambda signal: (
            stale(signal) if signal["symbol"] == "ACUUSDT" else fresh(signal)
        ),
    )[0]
    first = tv_signal(pipeline, now, ttl=50000)
    second = tv_signal(pipeline, now, symbol="BTCUSDT")
    result = scheduler.run_once(limit=2)
    assert result == {first: "DEFERRED", second: "PREPARED"}
    assert scheduler.worker.decision(first) is None
    assert scheduler.worker.decision(second)["action"] == "OPEN"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize(
    "expired", ["before-market", "before-signal", "after-market", "after-account", "after-signal"]
)
def test_assembled_context_expiry_does_not_stop_fresh_peer(case, database, expired):
    pipeline, _, _, _, now = case
    pipeline.market.run_once()
    fresh = pipeline.schedulers[0].context_provider
    first_clock = now + 60000 if expired == "before-signal" else now
    second_clock = now + 60000 if expired == "after-signal" else now + 10
    clocks = iter((first_clock, second_clock))
    market_end = now if expired == "before-market" else (
        now + 10 if expired == "after-market" else now + 90000
    )
    account_end = now + 10 if expired == "after-account" else now + 90000
    expiring = DirectionalContext(
        lambda signal: {
            "environment": "SANDBOX",
            "symbol": signal["symbol"],
            "assembled_at": now - 1,
            "valid_until_ms": market_end,
        },
        lambda signal: {
            "account_scope": asdict(pipeline.scope),
            "symbol": signal["symbol"],
            "evidence": {"source": "QA"},
            "observed_at_ms": now,
            "valid_until_ms": account_end,
        },
        scope=pipeline.scope,
        clock_ms=lambda: next(clocks),
    )
    scheduler = tv_schedulers(
        pipeline, now,
        lambda signal: expiring(signal) if signal["symbol"] == "ACUUSDT" else fresh(signal),
    )[0]
    first = tv_signal(pipeline, now, ttl=60000)
    second = tv_signal(pipeline, now, symbol="BTCUSDT", ttl=90000)
    assert scheduler.run_once(limit=2) == {first: "DEFERRED", second: "PREPARED"}
    assert_no_signal_business(database, first)
    assert scheduler.worker.decision(second)["action"] == "OPEN"
    failure = scheduler.last_run_diagnostics["failures"][0]
    assert failure["error_code"] == (
        "SIGNAL_EXPIRED_DURING_CONTEXT" if expired.endswith("signal") else "STALE_CONTEXT"
    )
    assert failure["outcome"] == "DEFERRED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize("reason", ["STALE_CONTEXT", "SIGNAL_EXPIRED_DURING_CONTEXT"])
def test_context_wait_expires_without_reloading_context_or_chasing_order(
    case, database, reason
):
    pipeline, _, _, _, now = case
    calls = []

    def context(_):
        calls.append(True)
        raise ValueError(reason)

    scheduler = tv_schedulers(pipeline, now, context)[0]
    identifier = tv_signal(pipeline, now)
    assert scheduler.run_once(limit=1) == {identifier: "DEFERRED"}
    assert_no_signal_business(database, identifier)
    pipeline.runtime.clock_ms = lambda: now + 60000
    assert scheduler.expire_pending() == 1
    assert scheduler.run_once(limit=1) == {}
    assert calls == [True]
    assert_no_signal_business(database, identifier)
    with database() as conn:
        assert conn.execute(
            "SELECT outcome,intent_id FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (scheduler.worker.scope.consumer, identifier),
        ).fetchone() == ("EXPIRED", None)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("STALE_CONTEXT"),
        ConnectionError("database unavailable"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=401, code=-2015),
        IntakeRejected("CONTEXT_SCOPE_MISMATCH"),
        IntakeRejected("INVALID_CONTEXT"),
        IntakeRejected("MISSING_CONTEXT"),
        ValueError("CACHE_DIGEST_MISMATCH"),
    ],
)
def test_infrastructure_auth_and_corruption_are_not_reclassified_as_symbol_wait(
    case, database, error
):
    pipeline, _, _, _, now = case

    def context(_):
        raise error

    scheduler = tv_schedulers(pipeline, now, context)[0]
    identifier = tv_signal(pipeline, now)
    assert scheduler.run_once(limit=1) == {identifier: "UNAVAILABLE"}
    failure = scheduler.last_run_diagnostics["failures"][0]
    assert failure["stage"] == "CONTEXT" and failure["outcome"] == "UNAVAILABLE"
    assert_no_signal_business(database, identifier)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )


@pytest.mark.parametrize("stage", ["CONTEXT_POLICY", "CONSUME"])
@pytest.mark.parametrize("reason", ["STALE_CONTEXT", "SIGNAL_EXPIRED_DURING_CONTEXT"])
def test_same_error_text_outside_context_acquisition_is_not_a_wait(
    case, database, monkeypatch, stage, reason
):
    pipeline, _, _, _, now = case
    scheduler = tv_schedulers(pipeline, now, lambda _: {})[0]
    identifier = tv_signal(pipeline, now)

    def fail(*_args, **_kwargs):
        raise ValueError(reason)

    if stage == "CONTEXT_POLICY":
        monkeypatch.setattr(scheduler, "context_required", fail)
    else:
        monkeypatch.setattr(scheduler.worker, "consume", fail)
    assert scheduler.run_once(limit=1) == {identifier: "UNAVAILABLE"}
    assert scheduler.last_run_diagnostics["failures"][0]["stage"] == stage
    assert_no_signal_business(database, identifier)


def test_ack_failure_after_stale_context_still_reports_unavailable(
    case, database, monkeypatch
):
    pipeline, _, _, _, now = case
    scheduler = tv_schedulers(pipeline, now, stale_symbol_context(now))[0]
    identifier = tv_signal(pipeline, now)

    @contextmanager
    def broken_ack():
        with database() as conn:

            class Proxy:
                def execute(self, query, params=None):
                    if "completed_at=CASE" in query:
                        raise ValueError("STALE_CONTEXT")
                    return conn.execute(query, params)

            yield Proxy()

    monkeypatch.setattr(scheduler, "_connect", broken_ack)
    assert scheduler.run_once(limit=1) == {identifier: "UNAVAILABLE"}
    assert scheduler.last_run_diagnostics["failures"][-1] == {
        "signal_id": identifier,
        "attempt": 1,
        "stage": "ACK",
        "error_code": "STALE_CONTEXT",
        "outcome": "UNAVAILABLE",
    }
    assert_no_signal_business(database, identifier)
