import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_directional_admission import formal
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE, scenario
from test_v2_reentry import bar

from services.v2_reentry_observation_report import observation_report
from services.v2_reentry_observer import (
    ObserverLimits,
    ReentryObserver,
    active_route,
    digest,
    discover,
    initialize,
    observe,
    persist,
    previous_close,
    read_only,
)
from services.v2_trade_review import trade_review
from tests.support.v2_admission import database as database_fixture
from v2_core.account_registry import AccountRegistry
from v2_core.reentry import VERSION, ReentryPolicy

database = database_fixture
closed = closed_fixture
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installed(database):
    with database() as conn:
        for name in (
            "20260925_tenant_registry.sql",
            "20260927_account_retirements.sql",
            "20260928_execution_routes.sql",
            "20261001_reentry_observer.sql",
        ):
            conn.execute((ROOT / "db/migrations" / name).read_text())
    registry = AccountRegistry(database)
    tenant = registry.create_tenant(uuid4(), "QA")
    enrolled = registry.enroll(
        tenant, SCOPE, display_name="QA", credential_ref=uuid4(), request_id=uuid4()
    )
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token) VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (tenant, enrolled["registry_id"], uuid4()),
        )
        route = active_route(conn)
        initialize(conn, 1)
    return database, route


def sample():
    candidate = {
        "decision_id": str(uuid4()),
        "decided_ms": 26 * 60000,
        "signal_observed_ms": str(26 * 60000),
        "symbol": "BTCUSDT",
        "side": "BUY",
        "source": "s3",
    }
    prev = {
        "previous_episode": str(uuid4()),
        "exit_reason": "PEAK_DRAWDOWN",
        "exit_known_ms": 25 * 60000,
        "exit_price": "100",
    }
    return candidate, prev


def observed(candidate=None, previous=None):
    c, p = sample()
    return observe(
        candidate or c,
        previous or p,
        None,
        read_bars=lambda _s, start, end: list(
            reversed(
                [
                    bar(i).payload()
                    if i < 25
                    else bar(i, h="102", c="101.5", v="20").payload()
                    for i in range(start // 60000, end // 60000)
                ]
            )
        ),
        now=26 * 60000 + 1000,
        policy=ReentryPolicy(),
        limits=ObserverLimits(),
    )


def test_frozen_evidence_lag_and_no_side_effects():
    result = observed()
    assert result["assessment"]["status"] == "READY"
    assert result["lag_ms"] == 1000 and not result["archive_arrival_at_decision_proven"]
    assert result["bars"] and not result["execution_authorized"]
    assert "net_pnl" not in str(result)
    assert result["inputs_digest"] == digest(
        {k: result[k] for k in ("candidate", "previous", "bars")}
    )


@pytest.mark.parametrize(
    "condition,reason",
    [
        (None, "ARCHIVE_OR_INPUT_INVALID"),
        ("error", "ARCHIVE_UNAVAILABLE"),
        ("late", "OBSERVATION_TOO_LATE"),
        ("old", "SIGNAL_PREDATES_EXIT"),
        ("future", "ARCHIVE_OR_INPUT_INVALID"),
    ],
)
def test_missing_late_old_future_signals_never_ready(condition, reason):
    c, p = sample()
    if condition == "old":
        c["signal_observed_ms"] = "1"
    if condition == "future":
        c["signal_observed_ms"] = str(c["decided_ms"] + 1)

    def read(*args):
        if condition == "error":
            raise OSError("SECRET MUST NOT BE RECORDED")
        if condition in ("old", "future"):
            return list(reversed([bar(i).payload() for i in range(3, 26)]))
        return []

    result = observe(
        c,
        p,
        None,
        read_bars=read,
        now=c["decided_ms"] + (120001 if condition == "late" else 1),
        policy=ReentryPolicy(),
        limits=ObserverLimits(),
    )
    assert result["assessment"]["reason"] == reason
    assert result["assessment"]["status"] != "READY"
    assert "SECRET" not in str(result)


def test_atomic_claims_retry_parameter_changes_and_account_isolation(installed):
    connect, route = installed
    original = observed()

    def write(record):
        with connect() as conn:
            return persist(conn, route, record, limits=ObserverLimits())

    copies = [deepcopy(original) for _ in range(4)]
    for item in copies:
        item["candidate"]["decision_id"] = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, copies))
    assert (
        results.count("RECORDED") == 1 and results.count("DUPLICATE_OPPORTUNITY") == 3
    )
    assert write(copies[0]) == "ALREADY_RECORDED"
    changed = deepcopy(original)
    changed["policy"]["reset_atr"] = "0.6"
    assert write(changed) == "DUPLICATE_OPPORTUNITY"
    with connect() as conn:
        read_only(conn)
        report = observation_report(
            conn, route["registry_id"], SCOPE, days=14, now=26 * 60000 + 1000
        )
        assert len(report["rows"]) == 5 and not report["execution_authorized"]
        foreign = observation_report(
            conn,
            route["registry_id"],
            replace(SCOPE, account_id="foreign"),
            days=14,
            now=26 * 60000 + 1000,
        )
        assert foreign["rows"] == []
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT sum(rows_used) FROM v2_reentry_observer_budget"
            ).fetchone()[0]
            == 5
        )


def test_claim_and_budget_rollback(installed):
    connect, route = installed
    result = observed()
    with pytest.raises(RuntimeError), connect() as conn:
        assert persist(conn, route, result, limits=ObserverLimits()) == "RECORDED"
        raise RuntimeError("rollback")
    with connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_reentry_observer_claims").fetchone()[
                0
            ]
            == 0
        )
        assert persist(conn, route, result, limits=ObserverLimits()) == "RECORDED"


def test_route_change_quotas_and_immutable_storage(installed):
    connect, route = installed
    with connect() as conn:
        assert (
            persist(conn, {**route, "epoch": 2}, observed(), limits=ObserverLimits())
            == "ROUTE_CHANGED"
        )
        assert (
            persist(conn, route, observed(), limits=ObserverLimits(total_bytes=1024))
            == "STORAGE_BUDGET_EXHAUSTED"
        )
        assert (
            persist(conn, route, observed(), limits=ObserverLimits(daily_rows=1))
            == "RECORDED"
        )
        assert (
            persist(conn, route, observed(), limits=ObserverLimits(daily_rows=1))
            == "STORAGE_BUDGET_EXHAUSTED"
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_reentry_observations").fetchone()[0]
            == 1
        )
    for statement in (
        "DELETE FROM v2_reentry_observations",
        "UPDATE v2_reentry_observations SET reason='oops'",
    ):
        with pytest.raises(psycopg.Error), connect() as conn:
            conn.execute(statement)


def test_no_backfill_on_first_start_restart_reuses_epoch(installed):
    connect, route = installed
    _, runtime, signal, context = scenario(connect)
    formal(runtime).consume(signal, context=context)
    observer = ReentryObserver(
        connect, lambda *a: pytest.fail("No prior close: no archive read")
    )
    before = time.time_ns() // 1000000
    first = observer.tick()
    assert first["processed"] == 1
    assert observer.tick()["processed"] == 0
    with connect() as conn:
        assert initialize(conn, before + 100000) == 1
        # Original stored decision age is explicit, not mistaken for timely observation.
        assert (
            conn.execute("SELECT reason FROM v2_reentry_observations").fetchone()[0]
            == "OBSERVATION_TOO_LATE"
        )
    with connect() as conn:
        read_only(conn)
        rows, _ = discover(
            conn,
            route,
            started=before + 100000,
            now=before + 100001,
            policy_digest=digest(
                {"version": VERSION, "policy": asdict(ReentryPolicy())}
            ),
            limits=ObserverLimits(),
        )
        assert rows == []


def test_previous_close_uses_known_time_and_rejects_ambiguity():
    c, _ = sample()
    base = {
        "id": str(uuid4()),
        "symbol": c["symbol"],
        "side": "BUY",
        "closed_ms": 25 * 60000,
        "close_known_ms": 25 * 60000,
        "maintenance": False,
        "exit_reason": "PEAK_DRAWDOWN",
        "net_pnl": "99999",
    }

    class Conn:
        rows = (("100",),)

        def execute(self, *_):
            return self

        def fetchall(self):
            return self.rows

    conn = Conn()
    route = {"scope": asdict(SCOPE)}
    prev, missing = previous_close(conn, route, c, [base])
    assert not missing and "net_pnl" not in prev and prev["exit_price"] == "100"
    assert (
        previous_close(
            conn, route, c, [{**base, "close_known_ms": c["decided_ms"] + 1}]
        )[1]
        == "NO_PRIOR_CLOSE_IN_BOUNDED_HISTORY"
    )
    assert (
        previous_close(conn, route, c, [base], truncated=True)[1]
        == "PRIOR_HISTORY_TRUNCATED"
    )
    conn.rows = [("100",), ("101",)]
    assert (
        previous_close(conn, route, c, [base])[1]
        == "EXIT_FILL_PRICE_AMBIGUOUS_OR_MISSING"
    )


def test_restricted_role_cannot_write_business_or_read_ungranted_state(installed):
    connect, _ = installed
    role = "qa_shadow_" + uuid4().hex
    with connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        grants = (ROOT / "deploy/v2-testnet/reentry-observer-grants.sql").read_text()
        conn.execute(
            grants.replace("trade_v2_testnet", sql.Identifier(dbname).as_string(conn))
            .replace("trade_v2", sql.Identifier(schema).as_string(conn))
            .replace("tradev2shadow", sql.Identifier(role).as_string(conn))
            .replace("tradev2accounts", sql.Identifier(role).as_string(conn))
        )
    try:
        for query in (
            "SELECT * FROM v2_business_state",
            "INSERT INTO v2_orders DEFAULT VALUES",
            "UPDATE v2_trade_intents SET producer='other'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connect() as conn:
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                conn.execute(query)
        with connect() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            assert (
                conn.execute("SELECT count(*) FROM v2_reentry_observations").fetchone()[
                    0
                ]
                == 0
            )
    finally:
        with connect() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch": 0},
        {"poll_seconds": True},
        {"max_lag_ms": 300000, "lookback_ms": 60000},
        {"total_bytes": 0},
    ],
)
def test_limits_validated(kwargs):
    with pytest.raises(ValueError):
        ObserverLimits(**kwargs)


def test_prior_close_actual_sql_with_fills(installed, closed):
    connect, route = installed
    now = time.time_ns() // 1000000 + 1
    with connect() as conn:
        read_only(conn)
        review = trade_review(conn, SCOPE, now_ms=now)
        assert len(review["trades"]) == 1
        candidate = {"symbol": "BTCUSDT", "side": "BUY", "decided_ms": now}
        prev, missing = previous_close(conn, route, candidate, review["trades"])
        assert missing is None and Decimal(prev["exit_price"]) == Decimal(92)
        assert prev["exit_known_ms"] <= now and "net_pnl" not in prev


def test_empty_no_active_and_live_report(installed):
    connect, route = installed
    with connect() as conn:
        conn.execute("UPDATE v2_execution_routes SET phase='STOPPED',worker_token=NULL")
    observer = ReentryObserver(connect, lambda *_: pytest.fail("No route: no bars"))
    assert observer.tick()["status"] == "NO_ACTIVE_SANDBOX"
    with connect() as conn:
        read_only(conn)
        assert (
            observation_report(
                conn,
                route["registry_id"],
                replace(SCOPE, environment="LIVE"),
                days=7,
                now=1,
            )["health"]["status"]
            == "SANDBOX_ONLY"
        )
