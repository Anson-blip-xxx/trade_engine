import json
import threading
from pathlib import Path

from test_v2_intent_admission import database, scheduler_signal, strategy_worker
from test_v2_trading_pipeline import case

from services.v2_signal_maintenance import SignalMaintenance
from v2_core.evidence import canonical
from v2_core.runtime_policy import resolve
from v2_core.scheduling import StrategyScheduler

__all__ = ["case", "database"]


def test_pipeline_latest_index_repeatable_and_used(database):
    sql = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20261008_pipeline_latest.sql"
    ).read_text()
    with database() as c:
        c.execute(sql)
        c.execute(sql)
        c.execute("SET LOCAL enable_seqscan=off")
        plan = c.execute(
            "EXPLAIN SELECT payload FROM v2_business_state WHERE NOT deleted "
            "AND scope->>'namespace'='trading-pipeline-cycle-v1' "
            "AND scope->>'account_id'='qa' AND payload->>'status' IN ('CYCLE_COMPLETE','ENTRY_BLOCKED') "
            "ORDER BY (payload->>'started_at_ms')::bigint DESC LIMIT 1"
        ).fetchall()
        assert "v2_pipeline_latest_account" in str(plan)


def test_background_discovers_and_expires_but_never_evaluates(database):
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(
        worker,
        context_provider=lambda _: (_ for _ in ()).throw(AssertionError("no context")),
    )
    fresh = scheduler_signal(database, "fresh-background")
    expired = scheduler_signal(database, "old-background", expires=2)
    stop = threading.Event()
    maintenance = SignalMaintenance([scheduler], settings=resolve, stop=stop)
    maintenance.run_once()
    report = maintenance.snapshot(scheduler)
    canonical(report)  # Must satisfy the real durable audit serializer.
    assert report["status"] == "CURRENT", report
    assert report["expired"] == 1
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone() == (
            0,
        )
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert c.execute(
            "SELECT attempts,lease_token FROM v2_strategy_tasks WHERE signal_id=%s",
            (fresh,),
        ).fetchone() == (0, None)
        assert c.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE signal_id=%s", (expired,)
        ).fetchone() == ("EXPIRED",)


def test_hot_path_never_waits_for_cold_discovery(database):
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    fresh = scheduler_signal(database, "hot")
    expired = scheduler_signal(database, "cold", expires=2)
    scheduler.run_once(hot_only=True, discover_only=True)
    with database() as c:
        assert c.execute(
            "SELECT signal_id::text FROM v2_strategy_tasks"
        ).fetchall() == [(fresh,)]
    scheduler.run_once(discover_only=True)
    with database() as c:
        assert c.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE signal_id=%s", (expired,)
        ).fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone() == (
            0,
        )


def test_hot_contract_skips_unsupported_events_without_losing_expiry(database):
    worker, _ = strategy_worker(database)
    signal = scheduler_signal(database, "unsupported")
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: {}, actionable_events=("PUMP_DOWN",)
    )
    assert scheduler.run_once(hot_only=True) == {}
    with database() as c:
        assert c.execute(
            "SELECT attempts FROM v2_strategy_tasks WHERE signal_id=%s", (signal,)
        ).fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone() == (
            0,
        )
    worker.runtime.clock_ms = lambda: 20
    assert scheduler.expire_pending() == 1
    with database() as c:
        assert c.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE signal_id=%s", (signal,)
        ).fetchone() == ("EXPIRED",)


def test_real_background_snapshot_is_persistable_by_pipeline(case):
    pipeline, *_ = case
    maintenance = SignalMaintenance(
        pipeline.schedulers,
        settings=resolve,
        stop=threading.Event(),
        monotonic=lambda: 123.456,
    )
    maintenance.run_once()
    pipeline.background_maintenance = maintenance
    result = pipeline.run_once()
    assert result["status"] in {"CYCLE_COMPLETE", "ENTRY_BLOCKED"}
    assert result["background_maintenance"]
    canonical(result)
    assert all(
        r["progress_monotonic_ms"] == 123456
        for r in result["background_maintenance"].values()
    )


def test_pipeline_background_diagnostics_do_not_call_full_progress(case, monkeypatch):
    pipeline, *_ = case

    class Background:
        def snapshot(self, _):
            return {
                "status": "CURRENT",
                "progress": {"caught_up": False},
                "progress_observed_at_ms": 123,
            }

    pipeline.background_maintenance = Background()
    for scheduler in pipeline.schedulers:
        monkeypatch.setattr(
            scheduler,
            "progress",
            lambda: (_ for _ in ()).throw(AssertionError("blocking progress")),
        )
    result = pipeline.run_once()
    assert result["background_maintenance"]
    for name in result["background_maintenance"]:
        assert result["phases"][name]["progress_observed_at_ms"] == 123
    assert all(
        not value["full_reconciliation"] for value in result["expiry_metrics"].values()
    )


def test_background_failure_is_visible_and_preserves_cached_timestamp(
    database, monkeypatch
):
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    now = [0]
    maintenance = SignalMaintenance(
        [scheduler], settings=resolve, stop=threading.Event(), monotonic=lambda: now[0]
    )
    maintenance.run_once()
    before = maintenance.snapshot(scheduler)
    now[0] = 1000

    def fail(**_):
        raise ValueError("secret-not-for-diagnostics")

    monkeypatch.setattr(maintenance.work[0][1], "expire_pending", fail)
    maintenance.run_once()
    after = maintenance.snapshot(scheduler)
    assert after["status"] == "UNAVAILABLE"
    assert after["progress_observed_at_ms"] == before["progress_observed_at_ms"]
    assert "secret" not in json.dumps(after)
