"""Late-settlement research scheduling; PG uses only the isolated QA fixture."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import services.v2_daily_account_review as account_review
from services.v2_daily_mover_report import seal_report
from services.v2_daily_mover_settlement_refresh import (
    INTERVAL_MS,
    PENDING_STATES,
    VERSION,
    WINDOW_MS,
    refresh_snapshot,
)
from services.v2_daily_mover_store import DailyMoverStore
from tests.support.v2_admission import database as database_fixture
from tests.v2_core import test_v2_daily_mover_job as job_qa
from v2_core.daily_movers import DAY
from v2_core.daily_movers import VERSION as METHOD_VERSION

database = database_fixture
SCOPE = job_qa.SCOPE
WINDOW = job_qa.WINDOW


def _day(offset=0):
    return (date.fromisoformat(WINDOW["date"]) + timedelta(days=offset)).isoformat()


def _window(day):
    delta = (date.fromisoformat(day) - date.fromisoformat(WINDOW["date"])).days
    return {
        **WINDOW,
        "date": day,
        "start_ms": WINDOW["start_ms"] + delta * DAY,
        "end_ms": WINDOW["end_ms"] + delta * DAY,
    }


def _report(*, day=None, scope=SCOPE, now=None, reasons=None, complete=False):
    window = _window(day or WINDOW["date"])
    return {
        "scope_key": DailyMoverStore.scope_key(scope),
        "account_environment": scope.environment,
        "window": window,
        "as_of_ms": window["end_ms"] + 6 * INTERVAL_MS if now is None else now,
        "account_day_review": {
            "status": "AVAILABLE" if complete else "UNKNOWN",
            "totals": {"settled_count": 0} if complete else None,
            "coverage": {
                "query_complete": True,
                "full_closed_settlement_population_proven": complete,
                "excluded_reasons": reasons
                if reasons is not None
                else {}
                if complete
                else {"AWAITING_SETTLEMENT": 1},
            },
        },
    }


def _with_snapshot(**kwargs):
    report = _report(**kwargs)
    report["settlement_refresh"] = refresh_snapshot(report)
    return report


@pytest.mark.parametrize("reason", sorted(PENDING_STATES))
def test_pending_settlement_is_hourly_with_fixed_day_deadline(reason):
    report = _report(reasons={reason: 2, "OPEN_OR_PARTIAL_EXIT": 7})
    before = deepcopy(report)
    snapshot = refresh_snapshot(report)
    assert snapshot["version"] == VERSION
    assert snapshot["status"] == "WAITING_SETTLEMENT"
    assert snapshot["pending_count"] == 2
    assert snapshot["pending_reasons"] == {reason: 2}
    assert snapshot["retry_after_ms"] == report["as_of_ms"] + INTERVAL_MS
    assert snapshot["deadline_ms"] == WINDOW["end_ms"] + 3 * DAY
    assert snapshot["execution_authorized"] is False
    assert snapshot["automatic_parameter_updates"] is False
    assert report == before


@pytest.mark.parametrize(
    ("offset", "status", "has_retry"),
    [
        (-INTERVAL_MS, "WAITING_SETTLEMENT", True),
        (-1, "WAITING_SETTLEMENT", True),
        (0, "WINDOW_CLOSED_UNRESOLVED", False),
        (1, "WINDOW_CLOSED_UNRESOLVED", False),
    ],
)
def test_deadline_is_inclusive_and_preserves_unknown(offset, status, has_retry):
    deadline = WINDOW["end_ms"] + WINDOW_MS
    report = _report(now=deadline + offset)
    snapshot = refresh_snapshot(report)
    assert snapshot["status"] == status
    assert snapshot["deadline_ms"] == deadline
    assert snapshot["retry_after_ms"] == (deadline if has_retry else None)
    assert snapshot["pending_count"] == 1
    assert report["account_day_review"]["totals"] is None


@pytest.mark.parametrize(
    "reasons",
    [
        {},
        {"OPEN_OR_PARTIAL_EXIT": 1},
        {"AWAITING_SETTLEMENT": True},
        {"AWAITING_SETTLEMENT": "1"},
        {"AWAITING_SETTLEMENT": 1.0},
        {"AWAITING_SETTLEMENT": 0},
        {"AWAITING_SETTLEMENT": -1},
        {"AWAITING_SETTLEMENT": 501},
        {"AWAITING_SETTLEMENT": 500, "AWAITING_TERMINAL_ORDERS": 1},
        ["AWAITING_SETTLEMENT"],
        "AWAITING_SETTLEMENT",
    ],
)
def test_unproven_or_malformed_pending_counts_do_not_schedule(reasons):
    snapshot = refresh_snapshot(_report(reasons=reasons))
    assert snapshot["status"] == "NOT_APPLICABLE"
    assert snapshot["pending_count"] == 0
    assert snapshot["retry_after_ms"] is None


def test_exact_population_bound_is_accepted():
    snapshot = refresh_snapshot(_report(reasons={"AWAITING_SETTLEMENT": 500}))
    assert snapshot["status"] == "WAITING_SETTLEMENT"
    assert snapshot["pending_count"] == 500


def test_failed_read_carries_last_known_pending_without_sliding_deadline():
    previous = _with_snapshot()
    original = deepcopy(previous)
    for attempt in range(1, 7):
        report = _report(now=original["as_of_ms"] + attempt * INTERVAL_MS)
        report["account_day_review"] = {
            "status": "UNKNOWN",
            "coverage": {"query_complete": False},
        }
        snapshot = refresh_snapshot(report, previous=previous)
        assert snapshot["status"] == "WAITING_EVIDENCE"
        assert snapshot["pending_reasons"] == {"AWAITING_SETTLEMENT": 1}
        assert snapshot["pending_count"] == 1
        assert snapshot["deadline_ms"] == original["settlement_refresh"]["deadline_ms"]
        assert snapshot["retry_after_ms"] == report["as_of_ms"] + INTERVAL_MS
        assert (
            snapshot["pending_reasons"]
            is not previous["settlement_refresh"]["pending_reasons"]
        )
        previous = {**report, "settlement_refresh": snapshot}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "old-version"),
        ("status", "COMPLETE"),
        ("scope_key", "another-account"),
        ("account_environment", "LIVE"),
        ("window_start_ms", WINDOW["start_ms"] - DAY),
        ("window_end_ms", WINDOW["end_ms"] - DAY),
        ("deadline_ms", WINDOW["end_ms"] + WINDOW_MS + 1),
        ("observed_at_ms", WINDOW["end_ms"] + DAY),
        ("observed_at_ms", True),
        ("pending_count", True),
        ("pending_count", 0),
        ("pending_count", 501),
        ("pending_count", 2),
        ("pending_reasons", {}),
        ("pending_reasons", {"AWAITING_SETTLEMENT": True}),
        ("pending_reasons", {"OPEN_OR_PARTIAL_EXIT": 1}),
        ("pending_reasons", ["AWAITING_SETTLEMENT"]),
    ],
)
def test_failed_read_never_carries_mismatched_or_invalid_snapshot(field, value):
    previous = _with_snapshot()
    previous["settlement_refresh"][field] = value
    report = _report(now=previous["as_of_ms"] + INTERVAL_MS)
    report["account_day_review"] = {"status": "UNKNOWN", "coverage": {}}
    snapshot = refresh_snapshot(report, previous=previous)
    assert snapshot["status"] == "NOT_APPLICABLE"
    assert snapshot["pending_count"] == 0
    assert snapshot["retry_after_ms"] is None


def test_failed_first_read_without_pending_labels_does_not_invent_pending():
    report = _report(reasons={"READ_UNAVAILABLE": 1})
    report["account_day_review"]["coverage"]["query_complete"] = False
    snapshot = refresh_snapshot(report)
    assert snapshot["status"] == "NOT_APPLICABLE"
    assert snapshot["pending_count"] == 0
    assert snapshot["pending_reasons"] == {}
    assert snapshot["retry_after_ms"] is None


@pytest.mark.parametrize(
    "reasons",
    [{"AWAITING_SETTLEMENT": 1}, {"AWAITING_SETTLEMENT": 1, "READ_UNAVAILABLE": 1}],
)
def test_first_partial_read_preserves_explicit_pending_labels(reasons):
    report = _report(reasons=reasons)
    report["account_day_review"]["coverage"]["query_complete"] = False
    snapshot = refresh_snapshot(report)
    assert snapshot["status"] == "WAITING_EVIDENCE"
    assert snapshot["pending_count"] == 1
    assert snapshot["pending_reasons"] == {"AWAITING_SETTLEMENT": 1}
    assert snapshot["retry_after_ms"] == report["as_of_ms"] + INTERVAL_MS
    assert snapshot["deadline_ms"] == WINDOW["end_ms"] + WINDOW_MS
    assert report["account_day_review"]["totals"] is None


def test_proven_complete_stops_known_wait_even_after_deadline():
    previous = _with_snapshot()
    report = _report(complete=True, now=WINDOW["end_ms"] + WINDOW_MS + 1)
    snapshot = refresh_snapshot(report, previous=previous)
    assert snapshot["status"] == "COMPLETE"
    assert snapshot["pending_count"] == 0
    assert snapshot["pending_reasons"] == {}
    assert snapshot["retry_after_ms"] is None


def _pending_loader(conn, scope, **kwargs):
    review = job_qa.account_day_loader(conn, scope, **kwargs)
    review.pop("content_digest")
    review.update(
        status="UNKNOWN", reason="ACCOUNT_SETTLEMENT_COVERAGE_INCOMPLETE", totals=None
    )
    review["coverage"].update(
        retryable=True,
        candidate_count=1,
        excluded_reasons={"AWAITING_SETTLEMENT": 1},
        full_closed_settlement_population_proven=False,
    )
    return account_review._seal(review)


@pytest.mark.parametrize("initial_partial", [False, True])
def test_known_pending_survives_read_retry_cap_then_stops_on_complete(initial_partial):
    messages, calls = [], []
    state = {"mode": "pending"}

    def loader(conn, scope, **kwargs):
        calls.append(kwargs["as_of_ms"])
        if state["mode"] == "failed":
            raise RuntimeError("PRIVATE_READ_FAILURE")
        review = (
            _pending_loader if state["mode"] == "pending" else job_qa.account_day_loader
        )(conn, scope, **kwargs)
        if initial_partial and state["mode"] == "pending":
            review.pop("content_digest")
            review["coverage"].update(
                query_complete=False,
                candidate_count=2,
                excluded_reasons={"AWAITING_SETTLEMENT": 1, "READ_UNAVAILABLE": 1},
            )
            return account_review._seal(review)
        return review

    worker, store, market = job_qa.job(
        account_day=loader, notifier=lambda message: messages.append(message) or 123
    )
    worker.catalog_loader = job_qa.observed_catalog
    clock = {"ms": WINDOW["end_ms"] + 6 * INTERVAL_MS}
    worker.clock = lambda: clock["ms"]
    worker.run_day(WINDOW)
    assert store.saved_reports[-1]["settlement_refresh"]["status"] == (
        "WAITING_EVIDENCE" if initial_partial else "WAITING_SETTLEMENT"
    )
    requests = market.requests
    state["mode"] = "failed"
    for attempt in range(2, 7):
        retry = store.saved_reports[-1]["settlement_refresh"]["retry_after_ms"]
        clock["ms"] = retry - 1
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
        clock["ms"] = retry
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
        report = store.saved_reports[-1]
        assert report["settlement_refresh"]["status"] == "WAITING_EVIDENCE"
        assert report["optional_review_snapshot"]["attempts"] == min(attempt, 3)
        assert (
            report["evidence_snapshot"]["retry_after_ms"] == clock["ms"] + INTERVAL_MS
        )
        assert report == seal_report(report)
    assert len(calls) == len(store.saved_reports) == 6
    state["mode"] = "complete"
    clock["ms"] += INTERVAL_MS
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["settlement_refresh"]["status"] == "COMPLETE"
    assert report["evidence_snapshot"]["retry_after_ms"] is None
    clock["ms"] += INTERVAL_MS
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(calls) == 7
    assert len(messages) == 1
    assert market.requests == requests


def test_job_closes_pending_window_without_inventing_zero_totals():
    worker, store, _ = job_qa.job(account_day=_pending_loader)
    worker.catalog_loader = job_qa.observed_catalog
    clock = {"ms": WINDOW["end_ms"] + 6 * INTERVAL_MS}
    worker.clock = lambda: clock["ms"]
    for _ in range(3):
        worker.run_day(WINDOW)
        clock["ms"] += INTERVAL_MS
    deadline = WINDOW["end_ms"] + WINDOW_MS
    clock["ms"] = deadline - 1
    worker.run_day(WINDOW)
    assert store.saved_reports[-1]["settlement_refresh"]["retry_after_ms"] == deadline
    clock["ms"] = deadline
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["settlement_refresh"]["status"] == "WINDOW_CLOSED_UNRESOLVED"
    assert report["settlement_refresh"]["pending_count"] == 1
    assert report["account_day_review"]["totals"] is None
    assert (
        report["account_day_review"]["coverage"][
            "full_closed_settlement_population_proven"
        ]
        is False
    )
    assert report["evidence_snapshot"]["retry_after_ms"] is None
    clock["ms"] += INTERVAL_MS
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"


@pytest.mark.parametrize("recover", [False, True])
def test_complete_settlement_with_retryable_jev_uses_only_bounded_read_retries(recover):
    calls, messages = [], []

    def loader(conn, scope, **kwargs):
        calls.append(kwargs["as_of_ms"])
        review = job_qa.account_day_loader(conn, scope, **kwargs)
        review.pop("content_digest")
        review["jev_participation"]["coverage"]["retryable"] = not (
            recover and len(calls) > 1
        )
        return account_review._seal(review)

    worker, store, market = job_qa.job(
        account_day=loader, notifier=lambda message: messages.append(message) or 123
    )
    worker.catalog_loader = job_qa.observed_catalog
    clock = {"ms": WINDOW["end_ms"] + 6 * INTERVAL_MS}
    worker.clock = lambda: clock["ms"]
    attempts = 2 if recover else 3
    for attempt in range(1, attempts + 1):
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
        report = store.saved_reports[-1]
        assert report["account_day_review"]["coverage"]["retryable"] is False
        assert report["settlement_refresh"]["status"] == "COMPLETE"
        assert report["settlement_refresh"]["retry_after_ms"] is None
        assert report["optional_review_snapshot"]["attempts"] == attempt
        if attempt < attempts:
            assert (
                report["evidence_snapshot"]["retry_after_ms"]
                == clock["ms"] + INTERVAL_MS
            )
        else:
            assert report["evidence_snapshot"]["retry_after_ms"] is None
        clock["ms"] += INTERVAL_MS
    requests = market.requests
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(calls) == len(store.saved_reports) == attempts
    assert len(messages) == 1
    assert market.requests == requests


def test_default_run_revisits_old_pending_after_new_day_checkpoint_exists():
    worker, store, _ = job_qa.job()
    store.days[WINDOW["date"]] = {"status": "COMPLETE"}
    lookups, windows = [], []
    store.pending_settlement_days = lambda day, now: (
        lookups.append((day, now)) or [_day(-3), _day(-1), _day(-1)]
    )
    worker.run_day = lambda window: windows.append(window) or window
    assert worker.run() == windows
    assert [row["date"] for row in windows] == [WINDOW["date"], _day(-1), _day(-3)]
    assert lookups == [(WINDOW["date"], job_qa.NOW)]
    assert windows == [_window(row["date"]) for row in windows]


def test_default_run_after_midnight_updates_old_account_report_without_resending():
    messages, lookups = [], []
    worker, store, market = job_qa.job(
        account_day=_pending_loader,
        notifier=lambda message: messages.append(message) or 123,
    )
    worker.catalog_loader = job_qa.observed_catalog
    clock = {"ms": WINDOW["end_ms"] + 6 * INTERVAL_MS}
    worker.clock = lambda: clock["ms"]
    worker.run_day(WINDOW)
    original_id, original_report = store.existing(SCOPE, WINDOW["date"])
    assert original_report["settlement_refresh"]["status"] == "WAITING_SETTLEMENT"

    next_window = _window(_day(1))
    clock["ms"] += DAY
    worker.run_day(next_window)
    requests = market.requests
    assert max(store.days) == next_window["date"]

    def pending(day, now):
        lookups.append((day, now))
        return [WINDOW["date"]]

    store.pending_settlement_days = pending
    result = worker.run()
    assert [row["date"] for row in result] == [next_window["date"], WINDOW["date"]]
    assert result[0]["accounts"][0]["status"] == "EXISTING"
    assert result[1]["accounts"][0]["status"] == "CREATED"
    identity, revised = store.existing(SCOPE, WINDOW["date"])
    assert identity != original_id
    assert revised["as_of_ms"] == clock["ms"]
    assert (
        revised["settlement_refresh"]["deadline_ms"]
        == original_report["settlement_refresh"]["deadline_ms"]
    )
    assert lookups == [(next_window["date"], clock["ms"])]
    assert len(messages) == 2
    assert market.requests == requests


@pytest.mark.parametrize(
    "pending",
    [
        None,
        {},
        "2026-10-25",
        [True],
        [1],
        [None],
        ["not-a-date"],
        [_day(-4)],
        [_day(0)],
        [_day(1)],
        [_day(-1)] * 4,
    ],
)
def test_invalid_pending_day_query_cannot_expand_or_block_new_day(pending, capsys):
    worker, store, _ = job_qa.job()
    store.days[WINDOW["date"]] = {}
    store.pending_settlement_days = lambda *_: pending
    worker.run_day = lambda window: window
    assert worker.run() == [WINDOW]
    assert "PENDING_DAY_READ_UNAVAILABLE" in capsys.readouterr().out


def test_failed_pending_day_query_does_not_abort_new_day(capsys):
    worker, store, _ = job_qa.job()
    store.days[WINDOW["date"]] = {}

    def unavailable(*_):
        raise RuntimeError("PRIVATE_PENDING_DAY_QUERY")

    store.pending_settlement_days = unavailable
    worker.run_day = lambda window: window
    assert worker.run() == [WINDOW]
    output = capsys.readouterr().out
    assert "PENDING_DAY_READ_UNAVAILABLE" in output
    assert "PRIVATE_PENDING_DAY_QUERY" not in output


@pytest.fixture
def pg_store(database):
    ddl = (
        Path(__file__).resolve().parents[2] / "db/migrations/20261026_daily_movers.sql"
    )
    with database() as conn:
        conn.execute(ddl.read_text())
    return DailyMoverStore(database)


def _insert_corrupt_report(pg_store, day, report):
    """Bypass canonical write validation to exercise damaged historical JSON."""
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    identity = uuid4()
    with pg_store.connect() as conn:
        conn.execute(
            """INSERT INTO v2_daily_mover_reports
            (report_id, day, method_version, scope_key, source_digest, report)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                identity,
                day,
                METHOD_VERSION,
                pg_store.scope_key(SCOPE),
                identity.hex * 2,
                Jsonb(report),
            ),
        )


def test_pg_latest_complete_revision_suppresses_old_pending_per_scope(pg_store):
    day = _day(-1)
    first = _with_snapshot(day=day)
    pg_store.save_report(SCOPE, day, first)
    now = WINDOW["end_ms"] + INTERVAL_MS
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == [day]
    complete = _with_snapshot(day=day, complete=True, now=now)
    pg_store.save_report(SCOPE, day, complete)
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == []
    other = replace(SCOPE, account_id="qa-still-waiting")
    pg_store.save_report(other, day, _with_snapshot(day=day, scope=other))
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == [day]
    pg_store.save_report(
        other, day, _with_snapshot(day=day, scope=other, complete=True, now=now)
    )
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == []


def test_pg_pending_days_are_bounded_unique_and_due(pg_store):
    now = WINDOW["end_ms"] + INTERVAL_MS
    for offset in (-5, -4, -3, -2, -1, 0, 1):
        day = _day(offset)
        report = _with_snapshot(day=day)
        report["settlement_refresh"]["retry_after_ms"] = now
        pg_store.save_report(SCOPE, day, report)
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == [
        _day(-1),
        _day(-2),
        _day(-3),
    ]
    assert pg_store.pending_settlement_days(WINDOW["date"], now - 1) == []


@pytest.mark.parametrize(
    "retry", [None, True, "0", "not-a-number", [], {}, [0], -1, 0.5, 2**53]
)
def test_pg_malformed_retry_timestamp_is_ignored_without_sql_cast_failure(
    pg_store, retry
):
    day = _day(-1)
    report = _with_snapshot(day=day)
    report["settlement_refresh"]["retry_after_ms"] = retry
    _insert_corrupt_report(pg_store, day, report)
    assert pg_store.pending_settlement_days(WINDOW["date"], WINDOW["end_ms"]) == []


@pytest.mark.parametrize(
    "status", ["COMPLETE", "NOT_APPLICABLE", "WINDOW_CLOSED_UNRESOLVED"]
)
def test_pg_terminal_refresh_status_never_resurrects_account_pending_reasons(
    pg_store, status
):
    day = _day(-1)
    report = _with_snapshot(day=day)
    report["settlement_refresh"].update(status=status, retry_after_ms=0)
    pg_store.save_report(SCOPE, day, report)
    assert pg_store.pending_settlement_days(WINDOW["date"], WINDOW["end_ms"]) == []


def test_pg_legacy_pending_is_refreshed_once_then_latest_status_wins(pg_store):
    day = _day(-1)
    pg_store.save_report(SCOPE, day, _report(day=day))
    now = WINDOW["end_ms"] + INTERVAL_MS
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == [day]
    pg_store.save_report(SCOPE, day, _with_snapshot(day=day, complete=True, now=now))
    assert pg_store.pending_settlement_days(WINDOW["date"], now) == []


@pytest.mark.parametrize(
    "reasons",
    [
        {"AWAITING_SETTLEMENT": value}
        for value in (None, True, False, "1", 0, -1, 0.5, 501, [], {})
    ]
    + [["AWAITING_SETTLEMENT"], "AWAITING_SETTLEMENT", {}],
)
def test_pg_legacy_malformed_pending_counts_are_not_selected(pg_store, reasons):
    day = _day(-1)
    _insert_corrupt_report(pg_store, day, _report(day=day, reasons=reasons))
    assert pg_store.pending_settlement_days(WINDOW["date"], WINDOW["end_ms"]) == []


def test_pg_pending_from_other_method_is_not_selected(pg_store, database):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    day = _day(-1)
    with database() as conn:
        conn.execute(
            """INSERT INTO v2_daily_mover_reports
            (report_id, day, method_version, scope_key, source_digest, report)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                day,
                METHOD_VERSION + "-incompatible",
                pg_store.scope_key(SCOPE),
                "f" * 64,
                Jsonb(_with_snapshot(day=day)),
            ),
        )
    assert pg_store.pending_settlement_days(WINDOW["date"], WINDOW["end_ms"]) == []


@pytest.mark.parametrize("now", [None, True, -1, 1.5, "1", 2**53])
def test_store_rejects_invalid_clock_before_any_connection(now):
    def forbidden_connection():
        pytest.fail("invalid refresh clock must not reach PostgreSQL")

    store = DailyMoverStore(forbidden_connection)
    with pytest.raises(ValueError, match="INVALID_REFRESH_CLOCK"):
        store.pending_settlement_days(WINDOW["date"], now)
