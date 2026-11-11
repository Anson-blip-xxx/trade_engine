"""Capacity changes preserve late commits, audits, scopes and read boundaries."""

import threading
import time
from pathlib import Path

import pytest
from test_v2_directional_account_context import build, signal
from test_v2_directional_replay import SCOPE
from test_v2_trading_pipeline import case as pipeline_case

from tests.support.v2_admission import (
    database as database_fixture,
)
from tests.support.v2_admission import (
    scheduler_signal,
    strategy_worker,
)
from v2_core.runtime_policy import PolicyStore, resolve
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture
case = pipeline_case


def test_full_history_progress_is_cached_with_explicit_observation_time(
    case, monkeypatch
):
    pipeline, _, _, _, _ = case
    calls = []
    for scheduler in pipeline.schedulers:
        original = scheduler.progress
        monkeypatch.setattr(
            scheduler,
            "progress",
            lambda original=original: calls.append(1) or original(),
        )
    first = pipeline.run_once()
    assert calls
    original_count = len(calls)
    second = pipeline.run_once()
    assert len(calls) == original_count
    for name, value in first["phases"].items():
        if "progress" in value:
            assert (
                second["phases"][name]["progress_observed_at_ms"]
                == value["progress_observed_at_ms"]
            )
    pipeline._progress_cache = {
        name: (observed - 300001, result)
        for name, (observed, result) in pipeline._progress_cache.items()
    }
    pipeline.run_once()
    assert len(calls) > original_count


def test_hot_discovery_and_cold_reconciliation_do_not_use_received_cursor(database):
    now = [10000]
    worker, _ = strategy_worker(
        database,
        now=lambda: now[0],
        decide=lambda *_: StrategyDecision("IGNORED", "QA"),
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once() == {}
    # Committed after discovery with an older observation; still-valid inputs
    # must be consumed immediately, not wait for the historical scan.
    fresh = scheduler_signal(database, "late-live", observed=9900, expires=20000)
    old = scheduler_signal(database, "late-expired", observed=1, expires=2)
    assert scheduler.run_once() == {fresh: "IGNORED"}
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_strategy_tasks WHERE signal_id=%s", (old,)
            ).fetchone()[0]
            == 0
        )
    now[0] += 60000
    assert scheduler.run_once() == {old: "EXPIRED"}
    assert scheduler.run_once() == {}


def test_hot_discovery_still_isolates_strategy_consumers(database):
    schedulers = []
    for producer in ("s6", "s8"):
        worker, _ = strategy_worker(
            database,
            producer=producer,
            decide=lambda *_: StrategyDecision("IGNORED", "QA"),
        )
        schedulers.append(StrategyScheduler(worker, context_provider=lambda _: {}))
        schedulers[-1].run_once()
    sid = scheduler_signal(database, "per-consumer")
    assert all(s.run_once() == {sid: "IGNORED"} for s in schedulers)


@pytest.mark.parametrize(
    "setting,value",
    [
        ("context.read_workers", 0),
        ("context.read_workers", 5),
        ("scheduler.progress_seconds", 0),
        ("scheduler.discovery_reconcile_seconds", True),
    ],
)
def test_capacity_settings_are_bounded(setting, value):
    with pytest.raises((ValueError, TypeError)):
        resolve({setting: value})


@pytest.mark.parametrize("workers", [1, 4])
def test_parallel_reads_are_bounded_and_enclosed_by_position_checks(database, workers):
    provider, request, public, _, _ = build(database)
    PolicyStore(database, SCOPE).patch(
        {"context.read_workers": workers},
        expected_version=0,
        reason="isolated capacity QA",
    )
    lock = threading.Lock()
    active = peak = positions = 0

    def wrap(port, signed):
        def call(*args):
            nonlocal active, peak, positions
            path = args[1] if signed else args[0]
            with lock:
                if path == "/futures/data/globalLongShortAccountRatio":
                    assert not signed and active == 0 and positions == 0
                    return port(*args)
                if path == "/fapi/v3/positionRisk":
                    assert active == 0
                    positions += 1
                    return port(*args)
                assert positions == 1
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return port(*args)
            finally:
                with lock:
                    active -= 1

        call.environment = port.environment
        return call

    provider.request = wrap(request, True)
    provider.public = provider.sentiment = wrap(public, False)
    result = provider(signal())
    assert result["sizing"]["balance"] == "1000"
    assert positions == 2 and active == 0 and peak <= workers
    assert peak == workers


def test_failed_parallel_read_never_publishes_partial_account_evidence(database):
    provider, request, _, _, _ = build(database)

    def fail(method, path, params):
        if path == "/fapi/v3/account":
            raise TimeoutError("QA_TIMEOUT")
        return request(method, path, params)

    provider.request = fail
    with pytest.raises(TimeoutError):
        provider(signal())
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='directional-account-context-v1'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_live_index_migration_is_repeatable(database):
    sql = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20260930_signal_hot_discovery.sql"
    ).read_text()
    with database() as conn:
        conn.execute(sql)
        conn.execute(sql)
        assert conn.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid='v2_signals_live_discovery'::regclass"
        ).fetchone()[0]


def test_demand_excludes_expired_future_receipted_and_backoff_inputs(database):
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: {}, priority_events=("TREND_UP",)
    )
    scheduler_signal(database, "ready")
    scheduler_signal(database, "expired", expires=2)
    scheduler_signal(database, "future", observed=5)
    done = scheduler_signal(database, "receipted")
    worker.consume(done, context={})
    retry = scheduler_signal(database, "backoff")
    lease = scheduler_signal(database, "lease")
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,next_attempt_at) VALUES(%s,%s,clock_timestamp()+interval '1 minute')",
            (worker.scope.consumer, retry),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) VALUES(%s,%s,'00000000-0000-0000-0000-000000000001',clock_timestamp()+interval '1 minute')",
            (worker.scope.consumer, lease),
        )
    assert scheduler.demand() == 1


def test_budget_follows_live_demand_without_starving_other_consumer(case, monkeypatch):
    pipeline, _, _, _, _ = case
    allocations = []
    for index, scheduler in enumerate(pipeline.schedulers):
        monkeypatch.setattr(
            scheduler, "demand", lambda index=index, **_: 100 if index == 0 else 0
        )

        def run(scheduler=scheduler, **kwargs):
            allocations.append(kwargs["budget_ms"])
            scheduler.last_run_metrics = {"processed": 0}
            return {}

        monkeypatch.setattr(scheduler, "run_once", run)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE"
    assert allocations[0] > 0 and allocations[1] > 0
    assert result["scheduler_demand"]["s6:s3"]["weight"] == 16
    assert result["scheduler_demand"]["s8:s3"]["weight"] == 1


def test_adaptive_leverage_reads_share_position_bracket_and_original_validator(
    database,
):
    provider, request, public, clock, _ = build(database)
    request.account["totalMarginBalance"] = "1000"
    PolicyStore(database, SCOPE).patch(
        {
            "leverage.adaptive_enabled": True,
            "entry.force_isolated": True,
            "portfolio_risk.enabled": True,
            "capital.enabled": True,
            "sizing.pool_fraction": "0.70",
        },
        expected_version=0,
        reason="QA adaptive reads",
    )
    bracket_reads = []

    def signed(method, path, params):
        if path == "/fapi/v1/leverageBracket":
            assert sum(p == "/fapi/v3/positionRisk" for _, p, _ in request.calls) == 1
            bracket_reads.append(path)
            return [
                {
                    "symbol": "BTCUSDT",
                    "brackets": [
                        {
                            "notionalFloor": 0,
                            "notionalCap": 100000,
                            "initialLeverage": 20,
                            "maintMarginRatio": "0.005",
                        }
                    ],
                }
            ]
        return request(method, path, params)

    def market(path, params):
        if path == "/fapi/v1/ticker/bookTicker":
            assert sum(p == "/fapi/v3/positionRisk" for _, p, _ in request.calls) == 1
            bracket_reads.append(path)
            return {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "100.01",
                "bidQty": "1000",
                "askQty": "1000",
                "time": clock.now,
            }
        return public(path, params)

    market.environment = "SANDBOX"
    provider.request, provider.public = signed, market
    result = provider(signal())
    assert len(bracket_reads) == 2
    assert result["leverage_venue"]["symbol"] == "BTCUSDT"
    assert result["leverage_venue"]["tiers"][0]["max_leverage"] == 20
