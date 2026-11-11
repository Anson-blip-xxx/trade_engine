"""Cooperative stop preserves receipts, lease ownership and uncertain orders."""

import json
import multiprocessing
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_managed_testnet import ChildWorker
from services.v2_testnet_daemon import TestnetTradingDaemon
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal, strategy_worker
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture
case = pipeline_case


def ignored_worker(connect):
    return strategy_worker(
        connect, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )[0]


def test_already_stopping_does_not_connect_or_claim(database, monkeypatch):
    scheduler = StrategyScheduler(
        ignored_worker(database), context_provider=lambda _: {}
    )
    monkeypatch.setattr(scheduler, "_connect", lambda: pytest.fail("must not connect"))
    assert scheduler.run_once(stop_requested=lambda: True) == {}
    assert scheduler.last_run_diagnostics["stop_reason"] == "STOP_REQUESTED"
    assert all(type(v) is int for v in scheduler.last_run_metrics.values())


@pytest.mark.parametrize("at", ["read", "policy", "context", "consume"])
def test_stop_around_attempt_releases_only_own_lease_and_resumes(
    database, monkeypatch, at
):
    sid = scheduler_signal(database, "drain-at-" + at)
    worker = ignored_worker(database)
    stop = threading.Event()
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    obj, name = {
        "read": (worker, "_read"),
        "policy": (scheduler, "context_required"),
        "context": (scheduler, "context_provider"),
        "consume": (worker, "consume"),
    }[at]
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(obj, name, wrapped)
    result = scheduler.run_once(stop_requested=stop.is_set)
    assert result == {sid: "IGNORED" if at == "consume" else "DRAINED"}
    with database() as c:
        assert c.execute(
            "SELECT lease_token,lease_until FROM v2_strategy_tasks WHERE signal_id=%s",
            (sid,),
        ).fetchone() == (None, None)
        assert c.execute("SELECT count(*) FROM v2_signal_receipts").fetchone()[
            0
        ] == int(at == "consume")
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
    monkeypatch.setattr(obj, name, original)
    assert scheduler.run_once() == ({} if at == "consume" else {sid: "IGNORED"})


def test_stop_never_releases_a_successor_lease(database):
    sid = scheduler_signal(database, "successor-lease")
    worker = ignored_worker(database)
    stop = threading.Event()
    successor = str(uuid4())

    def context(_):
        # Simulate ownership change before a late ACK; never touch an online lease.
        with database() as c:
            c.execute(
                "UPDATE v2_strategy_tasks SET lease_token=%s,lease_until=clock_timestamp()+interval '1 minute' WHERE consumer=%s AND signal_id=%s",
                (successor, worker.scope.consumer, sid),
            )
        stop.set()
        return {}

    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(stop_requested=stop.is_set) == {sid: "DRAINED"}
    with database() as c:
        assert c.execute(
            "SELECT lease_token::text,completed_at FROM v2_strategy_tasks WHERE signal_id=%s",
            (sid,),
        ).fetchone() == (successor, None)
        assert c.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (0,)


def test_stop_after_first_receipt_does_not_claim_next_task(database, monkeypatch):
    scheduler_signal(database, "one")
    scheduler_signal(database, "two")
    worker = ignored_worker(database)
    stop = threading.Event()
    original = worker.consume

    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(worker, "consume", consume)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert len(scheduler.run_once(stop_requested=stop.is_set)) == 1
    with database() as c:
        assert c.execute("SELECT sum(attempts) FROM v2_strategy_tasks").fetchone() == (
            1,
        )


@pytest.mark.parametrize("value", [True, 1, "stop"])
def test_invalid_stop_port_is_rejected_before_claim(database, value):
    scheduler = StrategyScheduler(
        ignored_worker(database), context_provider=lambda _: {}
    )
    with pytest.raises(TypeError):
        scheduler.run_once(stop_requested=value)
    with pytest.raises(TypeError):
        scheduler.run_once(stop_requested=lambda: "not a boolean")


def test_pipeline_stop_finishes_safety_but_skips_new_work(case, monkeypatch):
    pipeline, calls, *_ = case
    stop = threading.Event()
    original = pipeline.protection.run_once

    def protect():
        result = original()
        stop.set()
        return result

    monkeypatch.setattr(pipeline.protection, "run_once", protect)
    result = pipeline.run_once(stop_requested=stop.is_set)
    assert result["drain_requested"] and result["status"] == "ENTRY_BLOCKED"
    assert all(n in calls for n in ("protection", "exits", "settlement", "followups"))
    assert "market" not in calls and "signal_maintenance" not in result["phases"]


def test_stop_after_prepare_never_dispatches_and_keeps_recoverable_order(
    case, database, monkeypatch
):
    pipeline, *_ = case
    stop = threading.Event()
    pipeline.enable_entries = True
    original = pipeline.schedulers[0].run_once

    def prepare(**kwargs):
        result = original(**kwargs)
        assert "PREPARED" in result.values()
        stop.set()
        return result

    monkeypatch.setattr(pipeline.schedulers[0], "run_once", prepare)
    monkeypatch.setattr(
        pipeline.runtime.execution,
        "dispatch",
        lambda _: pytest.fail("stopping must not dispatch"),
    )
    result = pipeline.run_once(stop_requested=stop.is_set)
    assert result["drain_requested"] and result["entries"] == {}
    with database() as c:
        assert c.execute("SELECT status FROM v2_orders").fetchall() == [("PREPARED",)]


def test_stop_during_acknowledged_submit_still_recovers_and_protects(case, monkeypatch):
    pipeline, calls, *_ = case
    stop = threading.Event()
    pipeline.enable_entries = True

    def dispatch(_):
        calls.append("mock-submit")
        stop.set()
        return "ACKNOWLEDGED"

    monkeypatch.setattr(pipeline.runtime.execution, "dispatch", dispatch)
    monkeypatch.setattr(
        pipeline.runtime.execution,
        "recover",
        lambda _: calls.append("mock-recover") or {},
    )
    result = pipeline.run_once(stop_requested=stop.is_set)
    assert result["drain_requested"]
    assert calls.index("mock-submit") < calls.index("mock-recover")
    assert "protection" in calls[calls.index("mock-recover") + 1 :]
    assert len(result["entries"]) == 1


def test_daemon_wires_stop_and_skips_notification_io_on_shutdown(case, monkeypatch):
    pipeline, *_ = case
    stop = threading.Event()

    def run(**kwargs):
        assert not kwargs["stop_requested"]()
        stop.set()
        assert kwargs["stop_requested"]()
        return {"status": "ENTRY_BLOCKED"}

    monkeypatch.setattr(pipeline, "run_once", run)
    daemon = TestnetTradingDaemon(
        pipeline,
        stop=stop,
        notify=lambda _: None,
        trade_notifications=SimpleNamespace(
            run_once=lambda: pytest.fail("notifications are durable and can wait")
        ),
    )
    assert daemon.run_once()["status"] == "ENTRY_BLOCKED"


@pytest.mark.parametrize(
    "deaths,expected,forced",
    [
        ([False, False, False], True, False),
        ([True, False, False], True, True),
        ([True, True, False], True, True),
        ([True, True, True], False, True),
    ],
)
def test_child_stop_is_bounded_and_reports_forced_fallback(
    deaths, expected, forced, capsys
):
    worker = ChildWorker.__new__(ChildWorker)
    worker.stop_event = threading.Event()
    worker.stop_grace = 30
    calls = []
    alive = iter(deaths)
    worker.process = SimpleNamespace(
        is_alive=lambda: next(alive),
        join=lambda seconds: calls.append(("join", seconds)),
        terminate=lambda: calls.append(("terminate",)),
        kill=lambda: calls.append(("kill",)),
    )
    assert worker.stop() is expected
    assert worker.stop_event.is_set() and calls[0] == ("join", 30)
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "component": "execution-worker-stop",
        "forced": forced,
        "stopped": expected,
        "grace_seconds": 30,
    }
    assert sum(c[1] for c in calls if c[0] == "join") <= 40


@pytest.mark.parametrize("grace", ["0", "61", "true", "1.5", "private-value", True, 30])
def test_grace_validation_happens_before_process_start(grace):
    with pytest.raises(ValueError, match="^BOUNDED_STOP_GRACE_REQUIRED$"):
        ChildWorker("qa", {}, {"V2_CONTROLLER_STOP_GRACE_SECONDS": grace})


def test_unit_signals_parent_first_but_keeps_cgroup_kill():
    text = (
        Path(__file__).resolve().parents[2]
        / "deploy/v2-testnet/trade-v2-execution-controller.service"
    ).read_text()
    assert "KillMode=mixed" in text and "TimeoutStopSec=90" in text
    assert "SendSIGKILL=no" not in text


def _scheduler_child(dsn, schema, stop, entered, release):
    @contextmanager
    def connect():
        with psycopg.connect(dsn) as c:
            c.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield c

    def context(_):
        entered.set()
        assert release.wait(10)
        return {}

    scheduler = StrategyScheduler(ignored_worker(connect), context_provider=context)
    result = scheduler.run_once(stop_requested=stop.is_set)
    assert list(result.values()) == ["DRAINED"]


def test_real_spawned_child_drains_lease_and_new_owner_resumes(database):
    sid = scheduler_signal(database, "real-child-drain")
    with database() as c:
        dsn, schema = c.info.dsn, c.execute("SELECT current_schema()").fetchone()[0]
    ctx = multiprocessing.get_context("spawn")
    stop, entered, release = ctx.Event(), ctx.Event(), ctx.Event()
    worker = ChildWorker.__new__(ChildWorker)
    worker.stop_event, worker.stop_grace = stop, 5
    worker.process = ctx.Process(
        target=_scheduler_child, args=(dsn, schema, stop, entered, release)
    )
    worker.process.start()
    try:
        assert entered.wait(15)
        stop.set()
        release.set()
        assert worker.stop() and worker.process.exitcode == 0
        with database() as c:
            assert c.execute(
                "SELECT lease_token,completed_at FROM v2_strategy_tasks WHERE signal_id=%s",
                (sid,),
            ).fetchone() == (None, None)
        scheduler = StrategyScheduler(
            ignored_worker(database), context_provider=lambda _: {}
        )
        assert scheduler.run_once() == {sid: "IGNORED"}
    finally:
        release.set()
        if worker.process.is_alive():
            worker.process.kill()
        worker.process.join(5)
