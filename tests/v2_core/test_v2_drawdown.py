import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_v2_intent_admission import database as database_fixture

from v2_core.account_risk import AccountScope
from v2_core.drawdown import (
    RECOVERY_DELAY_MS,
    RETRY_DELAY_MS,
    BalanceObservation,
    DrawdownState,
    advance,
)
from v2_core.state import BusinessState

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


def store(database, scope=SCOPE, **changes):
    config = {"source": "balance-feed-v1", "currency": "USDT", "max_age_ms": 3600000}
    return DrawdownState(database, scope, **dict(config, **changes))


def observation(**changes):
    values = {
        "observation_id": "sample-1",
        "balance": "1000",
        "observed_at_ms": time.time_ns() // 1000000 - 1000,
        "evidence_digest": "a" * 64,
    }
    return BalanceObservation(**dict(values, **changes))


def initial():
    return advance(None, balance="1000", observed_at_ms=0)


@pytest.mark.parametrize(
    "balance,factor",
    [
        ("1001", "1"),
        ("920.000000000000000001", "1"),
        ("920", "0.5"),
        ("850.000000000000000001", "0.5"),
        ("850", "0"),
        ("0", "0"),
    ],
)
def test_exact_drawdown_thresholds(balance, factor):
    assert advance(initial(), balance=balance, observed_at_ms=1)["factor"] == factor


def test_recovery_requires_fresh_source_time_and_has_quarter_size():
    paused = advance(initial(), balance="850", observed_at_ms=1)
    waiting = advance(paused, balance="850", observed_at_ms=RECOVERY_DELAY_MS)
    assert waiting["factor"] == "0"
    recovered = advance(waiting, balance="850", observed_at_ms=RECOVERY_DELAY_MS + 1)
    assert (recovered["factor"], recovered["mode"]) == ("0.25", "recovery")
    assert recovered["pause_at_ms"] == 1


def test_recovery_loss_lock_and_retry_wait_are_persistent_transitions():
    paused = advance(initial(), balance="850", observed_at_ms=1)
    near = advance(paused, balance="833.000000000000000001", observed_at_ms=2)
    assert near["loss_locked"] is False
    locked = advance(near, balance="833", observed_at_ms=3)
    assert locked["loss_locked"] is True
    assert locked["pause_at_ms"] == 3
    waiting = advance(locked, balance="833", observed_at_ms=3 + RETRY_DELAY_MS - 1)
    assert waiting["loss_locked"] is True
    reset = advance(waiting, balance="833", observed_at_ms=3 + RETRY_DELAY_MS)
    assert reset["factor"] == "0"
    assert reset["loss_locked"] is False
    assert reset["pause_at_ms"] == 3 + RETRY_DELAY_MS
    recovered = advance(
        reset, balance="833", observed_at_ms=3 + RETRY_DELAY_MS + RECOVERY_DELAY_MS
    )
    assert recovered["factor"] == "0.25"


def test_new_peak_and_reduced_drawdown_clear_pause():
    paused = advance(initial(), balance="800", observed_at_ms=1)
    reduced = advance(paused, balance="900", observed_at_ms=2)
    assert reduced["factor"] == "0.5"
    assert reduced["pause_at_ms"] is None
    peak = advance(reduced, balance="1200", observed_at_ms=3)
    assert peak["peak_balance"] == "1200"
    assert peak["factor"] == "1"


def test_zero_balance_never_enters_recovery():
    state = advance(None, balance="0", observed_at_ms=0)
    for now in (1, RECOVERY_DELAY_MS, RETRY_DELAY_MS + 1, RETRY_DELAY_MS * 2):
        state = advance(state, balance="0", observed_at_ms=now)
        assert state["factor"] == "0"


def test_source_time_cannot_rewind():
    state = advance(initial(), balance="900", observed_at_ms=5)
    for now in (0, 4, 5):
        with pytest.raises(ValueError, match="advance source time"):
            advance(state, balance="1000", observed_at_ms=now)


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", True, 1000.0, "1e30"])
def test_bad_balance_rejected(bad):
    with pytest.raises(ValueError):
        observation(balance=bad)


def test_pg_state_restarts_without_peak_reset(database):
    service = store(database)
    first = observation()
    service.record(first)
    second = replace(
        first,
        observation_id="sample-2",
        balance="850",
        observed_at_ms=first.observed_at_ms + 1,
    )
    result = store(database).record(second)
    assert result.version == 2
    assert store(database).current() == result
    state = json.loads(result.payload_json)["state"]
    assert state["peak_balance"] == "1000"
    assert state["factor"] == "0"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0] == 2


def test_duplicate_observation_does_not_overwrite_newer_state(database):
    service = store(database)
    first = observation()
    original = service.record(first)
    latest = service.record(
        replace(
            first,
            observation_id="second",
            balance="850",
            observed_at_ms=first.observed_at_ms + 1,
        )
    )
    assert service.record(first) == original
    assert service.current() == latest
    with pytest.raises(ValueError, match="identity conflict"):
        service.record(replace(first, balance="1"))


def test_concurrent_duplicate_is_one_history_version(database):
    sample = observation()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store(database).record(sample), range(4)))
    assert all(result == results[0] for result in results)
    assert results[0].version == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0] == 1


def test_commit_acknowledgement_loss_is_recoverable(database):
    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        raise ConnectionError("simulated commit ack lost")

    sample = observation()
    with pytest.raises(ConnectionError):
        store(lost_ack).record(sample)
    assert store(database).record(sample).version == 1


@pytest.mark.parametrize("offset", [-3600001, 60000])
def test_stale_and_future_samples_cannot_initialize_state(database, offset):
    service = store(database)
    with pytest.raises(ValueError, match="stale or future"):
        service.record(observation(observed_at_ms=time.time_ns() // 1000000 + offset))
    assert BusinessState(database).read(service.key) is None


def test_out_of_order_distinct_sample_cannot_reset_peak(database):
    service = store(database)
    sample = observation()
    first = service.record(sample)
    for timestamp in (sample.observed_at_ms - 1, sample.observed_at_ms):
        with pytest.raises(ValueError, match="advance source time"):
            service.record(
                replace(
                    sample, observation_id="late", balance="1", observed_at_ms=timestamp
                )
            )
    assert service.current() == first


@pytest.mark.parametrize("change", [{"account_id": "other"}, {"environment": "LIVE"}])
def test_account_environment_isolation(database, change):
    service = store(database)
    service.record(observation())
    other = store(database, replace(SCOPE, **change))
    with pytest.raises(ValueError, match="unavailable"):
        other.current()
    other.record(observation(balance="1"))
    assert json.loads(service.current().payload_json)["state"]["peak_balance"] == "1000"


@pytest.mark.parametrize(
    "changes", [{"source": "other"}, {"currency": "USDC"}, {"max_age_ms": 1}]
)
def test_configuration_change_cannot_reset_state(database, changes):
    store(database).record(observation())
    with pytest.raises(ValueError, match="configuration conflict"):
        store(database, **changes).current()


def test_tombstone_is_not_a_new_account(database):
    service = store(database)
    service.record(observation())
    BusinessState(database).change(
        service.key,
        expected_version=1,
        request_key="delete",
        payload={},
        reason="explicit QA tombstone",
        deleted=True,
    )
    with pytest.raises(ValueError, match="tombstoned"):
        service.record(observation(observation_id="after-delete"))


def test_current_checks_freshness_again(database):
    service = store(database, max_age_ms=2000)
    sample = observation(observed_at_ms=time.time_ns() // 1000000)
    historical = service.record(sample)
    with database() as conn:
        conn.execute("SELECT pg_sleep(2.1)")
    assert service.record(sample) == historical  # audit replay, not current permit
    with pytest.raises(ValueError, match="stale or future"):
        service.current()


def context_setup(database, balance="900"):
    from test_v2_directional_replay import scenario, worker

    from services.v2_drawdown_context import DrawdownContext

    service = store(database, max_age_ms=10000)
    now = time.time_ns() // 1000000
    service.record(observation(observed_at_ms=now - 2000))
    service.record(
        observation(
            observation_id="sample-2", balance=balance, observed_at_ms=now - 1000
        )
    )
    _, _, _, context = scenario(database)
    context.update(assembled_at=now, valid_until_ms=now + 60000)
    replay, runtime = worker(database, now=now)
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": now - 1000,
        "expires_at_ms": now + 60000,
        "signal": "TREND_UP",
        "features": {"type": "TREND_UP", "strength": 70},
    }
    signal_id = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key="drawdown-context",
        snapshot=signal,
    )
    provider = DrawdownContext(lambda _: context, service)
    return provider, context, replay, signal_id, signal


def test_context_uses_pg_balance_factor_and_original_deadline(database):
    provider, original, replay, signal_id, signal = context_setup(database)
    context = provider(signal)
    assert original["directional"]["sizing"]["drawdown_factor"] == "1"
    assert context["directional"]["sizing"]["drawdown_factor"] == "0.5"
    assert context["directional"]["sizing"]["balance"] == "900"
    assert context["valid_until_ms"] == signal["observed_at"] + 10001
    assert context["drawdown"]["version"] == 2
    assert (
        replay.consume(signal_id, context=context)["reason"]
        == "DIRECTIONAL_REPLAY_ONLY"
    )
    saved = replay.decision(signal_id)["snapshot"]["features"]
    assert saved["evaluation"]["sizing"]["quantity"] == "0.562"
    assert saved["context"]["drawdown"] == context["drawdown"]


def test_pg_halt_prevents_even_sized_replay_candidate(database):
    provider, _, replay, signal_id, signal = context_setup(database, balance="850")
    assert (
        replay.consume(signal_id, context=provider(signal))["reason"]
        == "INSUFFICIENT_BUDGET"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_scope", {}),
        ("environment", "LIVE"),
        ("symbol", "ETHUSDT"),
        ("assembled_at", 0),
        ("valid_until_ms", 0),
    ],
)
def test_context_rejects_wrong_scope_or_time(database, field, value):
    provider, context, _, _, signal = context_setup(database)
    context[field] = value
    with pytest.raises(ValueError):
        provider(signal)


def test_context_never_extends_market_deadline(database):
    provider, original, _, _, signal = context_setup(database)
    original["valid_until_ms"] = original["assembled_at"] + 1
    assert provider(signal)["valid_until_ms"] == original["valid_until_ms"]


def test_state_and_history_rollback_together(database):
    @contextmanager
    def fail_before_commit():
        with database() as conn:
            yield conn
            raise ConnectionError("rollback requested")

    with pytest.raises(ConnectionError):
        store(fail_before_commit).record(observation())
    assert BusinessState(database).read(store(database).key) is None
