"""Real spawn/PG fault acceptance, with a fake GET-only settlement venue.

These exercise the job primitive, not a deployed/fenced settlement composition.
Opening fills/baseline are explicitly synthetic fixtures. No external network,
real credentials, controller activation or exchange mutations are permitted.
"""

import multiprocessing
import os
import re
import signal
import socket
import sys
import time
from contextlib import contextmanager
from copy import deepcopy

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from test_v2_strategy_settlement import SCOPE, closed_plan

from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core.bounded_job import BoundedJobPolicy, BoundedProcessJob
from v2_core.service import TradingData
from v2_core.session_lock import session_advisory_lock

database = database_fixture
LOCK = "testnet-protocol-account:" + SCOPE.key


def _deny_tcp(event, args):
    if event in {"socket.connect", "socket.sendto"} and args[0].family in {
        socket.AF_INET,
        socket.AF_INET6,
    }:
        raise RuntimeError("QA_EXTERNAL_NETWORK_DISABLED")


def _qa_connection(dsn, schema):
    # spawn does not inherit the parent's audit hooks. Reinstall before opening
    # any connection; libpq also receives an explicitly isolated Unix-socket DSN.
    sys.addaudithook(_deny_tcp)
    params = conninfo_to_dict(dsn)
    if (
        not re.fullmatch(
            r"/tmp/v2-data-qa\.[A-Za-z0-9]+/socket", params.get("host", "")
        )
        or any(params.get(key) for key in ("hostaddr", "service"))
        or any(
            os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
        )
        or not re.fullmatch(r"v2_core_test_[0-9a-f]{32}", schema)
    ):
        raise RuntimeError("QA_ISOLATED_DATABASE_REQUIRED")

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as conn:
            if conn.execute("SHOW cluster_name").fetchone() != ("v2_isolated_qa",):
                raise RuntimeError("QA_ISOLATED_DATABASE_REQUIRED")
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            # SET TRANSACTION in settlement must precede a transaction read.
            conn.commit()
            yield conn

    return connect


class _Venue:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, rows, income, symbol, mode="normal", entered=None):
        self.rows, self.income, self.symbol = rows, income, symbol
        self.mode, self.entered = mode, entered

    def __call__(self, method, path, params):
        if method != "GET":
            raise AssertionError("QA_SETTLEMENT_MUST_NOT_TRADE")
        if path == "/fapi/v1/income":
            if self.mode == "stuck-income":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                self.entered.set()
                while True:
                    time.sleep(0.1)
            return [
                {"symbol": self.symbol, "asset": "USDT", **item}
                for item in deepcopy(self.income)
            ]
        return deepcopy(self.rows[path])


def _settle_job(stop, dsn, schema, material, mode, entered):
    connect = _qa_connection(dsn, schema)
    venue = _Venue(
        material["rows"], material["income"], material["symbol"], mode, entered
    )
    if stop.is_set():
        return "BLOCKED"
    stage = DirectionalSettlementStage(
        connect, venue, scope=SCOPE, clock_ms=lambda: material["clock"], limit=1
    )
    result = stage.run_once()
    if mode == "lost-result" and result["status"] == "CLEAR":
        # Financial commit is real; no success frame reaches the supervisor.
        os._exit(0)
    return "COMPLETED" if result["status"] == "CLEAR" else "BLOCKED"


def _held_transaction(stop, dsn, schema, entered):
    connect = _qa_connection(dsn, schema)
    with session_advisory_lock(connect, LOCK) as acquired:
        assert acquired
        with connect() as conn:
            conn.execute("INSERT INTO qa_job_partial VALUES (1)")
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            entered.set()
            while True:
                time.sleep(0.1)


def _policy(*, work=5):
    return BoundedJobPolicy(
        work_seconds=work,
        stop_seconds=0.1,
        terminate_seconds=0.1,
        kill_seconds=1,
    )


def _wait(job, *, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = job.poll()
        if result["stop_confirmed"]:
            return result
        time.sleep(0.01)
    pytest.fail("QA_JOB_NOT_STOPPED_WITHIN_BOUND")


def _entered(job, event):
    end = time.monotonic() + 10
    while time.monotonic() < end and not event.is_set():
        result = job.poll()
        assert not result["stop_confirmed"], result
        time.sleep(0.01)
    assert event.is_set(), "child did not reach the locked operation"


def _cleanup(job):
    job.stop()
    _wait(job)


def _lock_available(database):
    with session_advisory_lock(database, LOCK) as acquired:
        return acquired


def _confirmed_lock_release(database):
    # Process exit is NOT proof that the server has dropped its session lock.
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if _lock_available(database):
            return
        time.sleep(0.01)
    pytest.fail("QA_BACKEND_LOCK_STILL_HELD_AFTER_PROCESS_EXIT")


def _confirmed_write_rollback(database):
    # The writer uses a different connection from the account session lock.
    # MVCC count=0 alone also holds while an uncommitted INSERT is still alive.
    # An exclusive table lock proves that writer's transaction has ended.
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            with database() as conn:
                conn.execute(
                    "LOCK TABLE qa_job_partial IN ACCESS EXCLUSIVE MODE NOWAIT"
                )
                assert conn.execute(
                    "SELECT count(*) FROM qa_job_partial"
                ).fetchone() == (0,)
                return
        except psycopg.errors.LockNotAvailable:
            time.sleep(0.01)
    pytest.fail("QA_WRITE_TRANSACTION_STILL_ALIVE_AFTER_PROCESS_EXIT")


def _material(database, case):
    with database() as conn:
        dsn, schema = (
            conn.info.dsn,
            conn.execute("SELECT current_schema()").fetchone()[0],
        )
    return (
        dsn,
        schema,
        {
            "rows": case.venue.rows,
            "income": case.venue.income,
            "symbol": case.plan.symbol,
            "clock": case.stage.clock(),
        },
    )


@pytest.mark.parametrize("mode", ["normal", "lost-result"])
def test_real_settlement_commit_and_lost_result_recover_from_journal(database, mode):
    case = closed_plan(database)
    dsn, schema, material = _material(database, case)
    job = BoundedProcessJob(_settle_job, policy=_policy(work=15))
    try:
        job.start(dsn, schema, material, mode, None)
        result = _wait(job, seconds=20)
        assert result["status"] == ("COMPLETED" if mode == "normal" else "FAILED")
        assert result["execution_authorized"] is False
        assert result["outcome_requires_reconciliation"] is True
        first = TradingData(database).trace(case.episode)
        assert len(first["settlements"]) == 1
        assert first["account_risk"]["status"] == "RELEASED"
        _confirmed_lock_release(database)
        previous_job = result["job_id"]
        job.start(dsn, schema, material, "normal", None)
        repeated = _wait(job, seconds=20)
        assert repeated["job_id"] != previous_job
        assert repeated["status"] == "COMPLETED"
        # Replay rescans committed facts; no second settlement or risk release.
        assert TradingData(database).trace(case.episode) == first
    finally:
        _cleanup(job)


def test_stuck_income_is_killed_then_account_protection_and_settlement_can_resume(
    database,
):
    case = closed_plan(database)
    dsn, schema, material = _material(database, case)
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    job = BoundedProcessJob(_settle_job, policy=_policy(), context=ctx)
    try:
        job.start(dsn, schema, material, "stuck-income", entered)
        _entered(job, entered)
        assert not _lock_available(database)
        before = TradingData(database).trace(case.episode)
        assert not before["settlements"]
        assert before["account_risk"]["status"] == "HELD"
        result = _wait(job)
        assert result["status"] == "TIMEOUT"
        assert result["outcome_requires_reconciliation"] is True
        _confirmed_lock_release(database)
        after = TradingData(database).trace(case.episode)
        assert not after["settlements"] and after["account_risk"]["status"] == "HELD"
        protection = DirectionalProtectionStage(
            database,
            _Venue(material["rows"], material["income"], material["symbol"]),
            scope=SCOPE,
            reference=lambda _symbol: None,
            clock_ms=lambda: material["clock"],
            allow_writes=False,
        )
        # Real orchestration against a flat synthetic account; this proves
        # continued processing, not coverage of a real open position.
        assert protection.run_once()["status"] == "CLEAR"
        job.start(dsn, schema, material, "normal", None)
        assert _wait(job)["status"] == "COMPLETED"
        assert len(TradingData(database).trace(case.episode)["settlements"]) == 1
    finally:
        _cleanup(job)


def test_force_kill_rolls_back_inflight_pg_write_and_requires_real_lock_reacquisition(
    database,
):
    with database() as conn:
        conn.execute("CREATE TABLE qa_job_partial(value integer)")
        dsn, schema = (
            conn.info.dsn,
            conn.execute("SELECT current_schema()").fetchone()[0],
        )
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    job = BoundedProcessJob(_held_transaction, policy=_policy(), context=ctx)
    try:
        job.start(dsn, schema, entered)
        _entered(job, entered)
        assert not _lock_available(database)
        # Negative control: invisibility is not rollback while writer is alive.
        with database() as conn:
            assert conn.execute("SELECT count(*) FROM qa_job_partial").fetchone() == (
                0,
            )
        with pytest.raises(psycopg.errors.LockNotAvailable), database() as conn:
            conn.execute("LOCK TABLE qa_job_partial IN ACCESS EXCLUSIVE MODE NOWAIT")
        result = _wait(job)
        assert result["status"] == "TIMEOUT"
        _confirmed_lock_release(database)
        _confirmed_write_rollback(database)
    finally:
        _cleanup(job)
