from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_capital_income import case as income_case
from test_v2_capital_journal import setup as journal_setup
from test_v2_capital_monitor import POLICY
from test_v2_capital_monitor import monitor_case as monitor_fixture
from test_v2_capital_transfers import transfer_case as transfer_fixture
from test_v2_intent_admission import database as database_fixture

from v2_core.capital_monitor_runner import CapitalMonitorRunner

database = database_fixture
setup = journal_setup
case = income_case
transfer_case = transfer_fixture
monitor_case = monitor_fixture


@pytest.fixture
def runner_case(monitor_case):
    monitor, _, setup, *_ = monitor_case
    with monitor.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_capital_monitor_runner.sql"
            ).read_text()
        )
    return CapitalMonitorRunner(monitor.connect), setup


def enable(runner, setup, **overrides):
    args = {
        "enabled": True,
        "policy": POLICY,
        "expected_version": 0,
        "request_id": str(uuid4()),
    }
    args.update(overrides)
    return runner.configure(setup[2], setup[4], **args)


def test_disabled_by_default_and_versioned_configuration(runner_case):
    r, s = runner_case
    assert r.run_one(s[2], s[4], now_ms=130)["status"] == "DISABLED"
    request = str(uuid4())
    first = enable(r, s, request_id=request)
    assert enable(r, s, request_id=request) == first
    with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
        enable(r, s, request_id=request, enabled=False)
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        enable(r, s)
    assert r.run_one(s[2], s[4], now_ms=130)["status"] == "SCANNED"
    assert r.run_one(s[2], s[4], now_ms=131)["status"] == "NOT_DUE"
    opened = r.run_one(s[2], s[4], now_ms=140)
    assert opened["scan"]["emitted"] == "OPEN"
    enable(r, s, enabled=False, expected_version=1)
    assert r.run_one(s[2], s[4], now_ms=150)["status"] == "DISABLED"
    enable(r, s, expected_version=2)
    assert r.run_one(s[2], s[4], now_ms=160)["scan"]["emitted"] is None


def test_account_tenant_environment_isolation(runner_case):
    r, s = runner_case
    enable(r, s)
    assert len(r.run_due(s[2], now_ms=130)) == 1
    assert r.run_due(s[3], now_ms=130) == []
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        r.run_one(s[3], s[4], now_ms=140)
    with pytest.raises(ValueError, match="SANDBOX_MONITOR_ONLY"):
        r.configure(
            s[2],
            s[7],
            enabled=True,
            policy=POLICY,
            expected_version=0,
            request_id=uuid4(),
        )


def test_lock_contention_and_release_after_transaction_abort(runner_case):
    r, s = runner_case
    enable(r, s)
    with pytest.raises(RuntimeError), r.connect() as c:
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (r._key(s[2], s[4]),),
        )
        assert r.run_one(s[2], s[4], now_ms=130)["status"] == "BUSY"
        raise RuntimeError("simulate transaction abort")
    assert r.run_one(s[2], s[4], now_ms=130)["status"] == "SCANNED"


def test_concurrent_runners_create_one_committed_scan(runner_case):
    r, s = runner_case
    enable(r, s)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: r.run_one(s[2], s[4], now_ms=130), range(8)))
    assert sum(x["status"] == "SCANNED" for x in results) == 1
    assert {x["status"] for x in results} <= {"SCANNED", "BUSY", "NOT_DUE"}
    with r.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_monitor_runs").fetchone()[0] == 1
        )


def test_scan_failure_rolls_back_health_and_schedules_retry(runner_case):
    r, s = runner_case
    enable(r, s)
    r.run_one(s[2], s[4], now_ms=130)
    with r.connect() as c:
        c.execute(
            "CREATE FUNCTION qa_runner_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private error'; END $$"
        )
        c.execute(
            "CREATE TRIGGER qa_runner_fail BEFORE INSERT ON v2_capital_monitor_events FOR EACH ROW EXECUTE FUNCTION qa_runner_fail()"
        )
    failed = r.run_one(s[2], s[4], now_ms=140)
    assert failed["status"] == "SCAN_FAILED" and "private" not in str(failed)
    assert r.run_one(s[2], s[4], now_ms=150)["status"] == "NOT_DUE"
    with r.connect() as c:
        assert (
            c.execute(
                "SELECT payload->>'active' FROM v2_capital_monitor_state"
            ).fetchone()[0]
            == "false"
        )
        c.execute("DROP TRIGGER qa_runner_fail ON v2_capital_monitor_events")
    assert r.run_one(s[2], s[4], now_ms=170)["scan"]["emitted"] == "OPEN"


def test_outer_abort_rolls_back_scan_events_schedule_and_run(runner_case):
    r, s = runner_case
    enable(r, s)
    r.run_one(s[2], s[4], now_ms=130)
    with pytest.raises(RuntimeError), r.connect() as c:
        inside = CapitalMonitorRunner(lambda: nullcontext(c))
        assert inside.run_one(s[2], s[4], now_ms=140)["scan"]["emitted"] == "OPEN"
        raise RuntimeError("crash before commit")
    assert r.run_one(s[2], s[4], now_ms=140)["scan"]["emitted"] == "OPEN"
    with r.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_monitor_events").fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT count(*) FROM v2_capital_monitor_runs").fetchone()[0] == 2
        )


def test_policy_change_is_immediately_due_without_resetting_incident(runner_case):
    r, s = runner_case
    enable(r, s)
    r.run_one(s[2], s[4], now_ms=130)
    enable(r, s, expected_version=1, policy=replace(POLICY, confirm_ms=1))
    assert r.run_due(s[2], now_ms=131)[0]["scan"]["emitted"] == "OPEN"
    with pytest.raises(ValueError, match="CLOCK_REGRESSION"):
        r.run_one(s[2], s[4], now_ms=130)


def test_audit_immutability_and_bounded_batch(runner_case):
    r, s = runner_case
    enable(r, s)
    r.run_due(s[2], now_ms=130)
    for table in ("v2_capital_monitor_runs", "v2_capital_monitor_control_events"):
        with pytest.raises(psycopg.Error), r.connect() as c:
            c.execute("DELETE FROM " + table)
    for limit in (True, 0, 101):
        with pytest.raises(ValueError):
            r.run_due(s[2], now_ms=140, limit=limit)


def test_disable_waits_for_account_fence_then_blocks_new_work(runner_case):
    r, s = runner_case
    enable(r, s)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with r.connect() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (r._key(s[2], s[4]),),
            )
            disabling = pool.submit(enable, r, s, enabled=False, expected_version=1)
            with pytest.raises(TimeoutError):
                disabling.result(timeout=0.1)
        assert disabling.result(timeout=5)["enabled"] is False
    assert r.run_one(s[2], s[4], now_ms=130)["status"] == "DISABLED"


def test_concurrent_configuration_cas_and_cross_account_request_conflict(runner_case):
    r, s = runner_case
    enable(r, s)

    def change(_):
        try:
            enable(r, s, enabled=False, expected_version=1)
            return True
        except ValueError as exc:
            assert str(exc) == "MONITOR_CONFIG_VERSION_CONFLICT"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(change, range(4))) == 1
    request = str(uuid4())
    enable(r, s, expected_version=2, request_id=request)
    with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
        r.configure(
            s[2],
            s[5],
            enabled=True,
            policy=POLICY,
            expected_version=0,
            request_id=request,
        )
