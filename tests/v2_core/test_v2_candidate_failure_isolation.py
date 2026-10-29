"""Candidate data failures are not account failures; no venue/model calls.

Unit disposition tests require no database. Integration cases use only the
explicit isolated PostgreSQL fixture and the no-submit pipeline fixture.
"""

from contextlib import contextmanager

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_signal_context_isolation import (
    assert_no_signal_business,
    tv_schedulers,
    tv_signal,
)
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_trading_pipeline import failed
from v2_core.ingress import IntakeRejected
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import CANDIDATE_CONTEXT_WAITS, candidate_wait_reason
from v2_core.transport import ExchangeTransportError

database = database_fixture
case = pipeline_case
CAUSES = tuple(CANDIDATE_CONTEXT_WAITS)


@pytest.mark.parametrize("reason", CAUSES)
def test_disposition_accepts_only_known_candidate_context_failures(reason):
    assert (
        candidate_wait_reason(ValueError(reason), stage="CONTEXT")
        == (CANDIDATE_CONTEXT_WAITS[reason])
    )
    assert not failed({"candidate": "DEFERRED"})


@pytest.mark.parametrize("reason", CAUSES)
@pytest.mark.parametrize(
    "stage", ["DISCOVERY", "CLAIM", "READ", "CONTEXT_POLICY", "CONSUME", "ACK"]
)
def test_disposition_rejects_same_code_outside_context(stage, reason):
    assert candidate_wait_reason(ValueError(reason), stage=stage) is None


@pytest.mark.parametrize("reason", CAUSES)
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError, IntakeRejected])
def test_disposition_does_not_trust_matching_text_from_other_error_types(
    reason, error_type
):
    assert candidate_wait_reason(error_type(reason), stage="CONTEXT") is None


@pytest.mark.parametrize(
    "reason",
    [
        "SHORT_RATIO_SCOPE",
        "SHORT_RATIO_TIMESTAMP",
        "FUNDING_RATE_TIMESTAMP",
        "ACCOUNT_CONTEXT_STALE",
        "ACCOUNT_CONTEXT_DEADLINE",
        "ACCOUNT_CONTEXT_PERSIST_FAILED",
        "ACCOUNT_TRADE_PERMISSION_UNVERIFIED",
        "POSITIONS_CHANGED_DURING_CONTEXT",
        "ENTRY_CONTROLS_CHANGED_DURING_CONTEXT",
        "CACHE_DIGEST_MISMATCH",
        "UNKNOWN_CONTEXT_EXPIRED",
        "SHORT_RATIO_STALE ",
    ],
)
def test_disposition_does_not_generalize_from_names_or_value_errors(reason):
    assert candidate_wait_reason(ValueError(reason), stage="CONTEXT") is None
    assert failed({"candidate": "UNAVAILABLE"})


def failing(reason):
    def context(_):
        raise ValueError(reason)

    return context


@pytest.mark.parametrize("reason", CAUSES)
def test_candidate_data_wait_cannot_block_other_consumers(case, database, reason):
    pipeline, _, _, _, now = case
    television = tv_schedulers(pipeline, now, failing(reason))
    pipeline.schedulers += television
    identifier = tv_signal(pipeline, now)
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": True, "scheduler.evaluation_budget_ms": 16000},
        expected_version=0,
        reason="QA candidate data wait must not block independent consumers",
    )
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert result["phases"]["s6:tv"][identifier] == "DEFERRED"
    assert any(status == "PREPARED" for status in result["phases"]["s6"].values())
    assert result["scheduler_allocation"]["s6:s3"]["status"] == "FINISHED"
    wait = result["scheduler_diagnostics"]["s6:tv_bridge"]["candidate_waits"]
    assert wait == [
        {
            "signal_id": identifier,
            "symbol": "ACUUSDT",
            "scope": "CANDIDATE",
            "reason": CANDIDATE_CONTEXT_WAITS[reason],
            "error_code": reason,
            "retry_seconds": 5,
            "signal_expires_at_ms": now + 60000,
            "execution_authorized": False,
        }
    ]
    assert television[0].worker.decision(identifier) is None
    assert television[1].worker.decision(identifier)["action"] == "IGNORED"
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s",
            (identifier,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT error_code,completed_at IS NULL,lease_token IS NULL,"
            "next_attempt_at>clock_timestamp() FROM v2_strategy_tasks "
            "WHERE signal_id=%s AND consumer=%s",
            (identifier, television[0].worker.scope.consumer),
        ).fetchone() == (reason, True, True, True)


@pytest.mark.parametrize("reason", CAUSES)
def test_fresh_symbol_proceeds_in_same_batch_without_using_bad_context(
    case, database, reason
):
    pipeline, _, _, _, now = case
    pipeline.market.run_once()
    fresh = pipeline.schedulers[0].context_provider
    scheduler = tv_schedulers(
        pipeline,
        now,
        lambda signal: (
            failing(reason)(signal) if signal["symbol"] == "ACUUSDT" else fresh(signal)
        ),
    )[0]
    first = tv_signal(pipeline, now, ttl=50000)
    second = tv_signal(pipeline, now, symbol="BTCUSDT")
    result = scheduler.run_once(limit=2)
    assert result == {first: "DEFERRED", second: "PREPARED"}
    assert scheduler.worker.decision(first) is None
    assert scheduler.worker.decision(second)["action"] == "OPEN"
    # Retry of an already prepared candidate is not an additional order.
    scheduler.run_once(limit=2)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize("reason", CAUSES)
def test_wait_expires_with_receipt_without_loading_data_or_chasing_order(
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
        TimeoutError("SHORT_RATIO_STALE"),
        ConnectionError("unavailable database"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=401, code=-2015),
        ValueError("SHORT_RATIO_SCOPE"),
        ValueError("ACCOUNT_TRADE_PERMISSION_UNVERIFIED"),
    ],
)
def test_infrastructure_auth_and_corruption_still_block_pipeline(case, database, error):
    pipeline, _, _, _, now = case

    def context(_):
        raise error

    pipeline.schedulers += tv_schedulers(pipeline, now, context)
    identifier = tv_signal(pipeline, now)
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": True},
        expected_version=0,
        reason="QA shared failures must still fail closed",
    )
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED", result
    assert result["phases"]["s6:tv"][identifier] == "UNAVAILABLE"
    assert "s6" not in result["phases"]
    assert_no_signal_business(database, identifier)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


@pytest.mark.parametrize("reason", CAUSES)
def test_ack_error_overrides_local_disposition_and_retains_unknown_state(
    case, database, monkeypatch, reason
):
    pipeline, _, _, _, now = case
    scheduler = tv_schedulers(pipeline, now, failing(reason))[0]
    identifier = tv_signal(pipeline, now)

    @contextmanager
    def broken_ack():
        with database() as conn:

            class Proxy:
                def execute(self, query, params=None):
                    if "completed_at=CASE" in query:
                        raise ConnectionError("QA acknowledgment unavailable")
                    return conn.execute(query, params)

            yield Proxy()

    monkeypatch.setattr(scheduler, "_connect", broken_ack)
    result = scheduler.run_once(limit=1)
    assert result == {identifier: "UNAVAILABLE"}
    assert failed(result)
    assert scheduler.last_run_diagnostics["failures"][-1]["stage"] == "ACK"
    assert_no_signal_business(database, identifier)


@pytest.mark.parametrize("reason", CAUSES)
def test_backoff_never_changes_signal_freshness_or_creates_receipts(
    case, database, reason
):
    pipeline, _, _, _, now = case
    scheduler = tv_schedulers(pipeline, now, failing(reason))[0]
    scheduler.context_failure_attempts = 5  # Explicitly permit four retry attempts.
    identifier = tv_signal(pipeline, now)
    for attempt in range(1, 5):
        if attempt > 1:
            with database() as conn:
                conn.execute(
                    "UPDATE v2_strategy_tasks SET next_attempt_at=clock_timestamp() "
                    "WHERE consumer=%s AND signal_id=%s",
                    (scheduler.worker.scope.consumer, identifier),
                )
        assert scheduler.run_once(limit=1) == {identifier: "DEFERRED"}
        wait = scheduler.last_run_diagnostics["candidate_waits"][0]
        assert wait["retry_seconds"] == max(5, 2**attempt)
        assert wait["signal_expires_at_ms"] == now + 60000
        assert wait["execution_authorized"] is False
    assert_no_signal_business(database, identifier)
    with database() as conn:
        assert conn.execute(
            "SELECT snapshot->>'expires_at_ms' FROM v2_inbound_signals WHERE signal_id=%s",
            (identifier,),
        ).fetchone() == (str(now + 60000),)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
