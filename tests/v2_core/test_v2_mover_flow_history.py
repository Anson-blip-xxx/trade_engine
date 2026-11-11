"""Historical projections use only read-only, bounded disposable-PG reads."""

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

import services.v2_mover_flow_evaluation as adapter
from services.v2_mover_flow_evaluation import FlowHistoryEvaluation
from tests.support.v2_admission import database as database_fixture
from v2_core.daily_movers import VERSION as MARKET_VERSION
from v2_core.evidence import canonical, digest

database = database_fixture
DAY = "2026-10-28"
PREVIOUS = date(2026, 10, 27)
CREATED = datetime(2026, 10, 28, tzinfo=timezone.utc)


@pytest.fixture
def history(database):
    ddl = (
        Path(__file__).resolve().parents[2] / "db/migrations/20261026_daily_movers.sql"
    ).read_text()
    with database() as connection:
        connection.execute(ddl)
    return FlowHistoryEvaluation(database)


def insert_report(
    database,
    day=PREVIOUS,
    report=None,
    *,
    report_id=None,
    created=CREATED,
    method=MARKET_VERSION,
    scope=None,
):
    identity = report_id or uuid4()
    with database() as connection:
        connection.execute(
            """INSERT INTO v2_daily_mover_reports
            (report_id,day,method_version,scope_key,source_digest,report,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                identity,
                day,
                method,
                scope or uuid4().hex,
                uuid4().hex,
                Jsonb({} if report is None else report),
                created,
            ),
        )
    return identity


def projection(marker="selected"):
    return {
        "precursor_review": {"marker": marker},
        "flow_review": {"marker": marker},
    }


def no_database():
    pytest.fail("invalid configuration must not open the database")


@pytest.mark.parametrize(
    "day",
    [
        None,
        True,
        date(2026, 10, 28),
        "20261028",
        "2026-02-30",
        "1969-10-28",
        "2026-10-28' OR TRUE",
    ],
)
def test_invalid_history_date_does_not_open_connection(day):
    with pytest.raises(ValueError):
        FlowHistoryEvaluation(no_database).history(day)


def test_latest_revision_is_deduplicated_across_accounts_with_uuid_tie_break(
    history, database
):
    insert_report(
        database, report=projection("older"), created=CREATED - timedelta(hours=1)
    )
    insert_report(database, report=projection("tie-lower"), report_id=UUID(int=1))
    chosen = projection("tie-higher")
    insert_report(
        database,
        report={**chosen, "account_evidence": {"secret_marker": "MUST_NOT_LEAVE_DB"}},
        report_id=UUID(int=2),
    )
    insert_report(
        database,
        report=projection("wrong-method"),
        method="incompatible-method",
        created=CREATED + timedelta(hours=1),
    )
    result = history.history(DAY)
    assert result == [{"day": PREVIOUS.isoformat(), **chosen}]
    assert "MUST_NOT_LEAVE_DB" not in json.dumps(result)


def test_calendar_bounds_exclude_current_future_and_31st_prior_day(history, database):
    day = date.fromisoformat(DAY)
    for offset in range(-35, 3):
        insert_report(database, day + timedelta(days=offset), projection(str(offset)))
    result = history.history(DAY)
    assert len(result) == 30
    assert [item["day"] for item in result] == [
        (day - timedelta(days=offset)).isoformat() for offset in range(30, 0, -1)
    ]
    assert result[0]["precursor_review"]["marker"] == "-30"
    assert result[-1]["precursor_review"]["marker"] == "-1"


def test_latest_missing_sections_are_retained_instead_of_falling_back(
    history, database
):
    insert_report(
        database,
        report=projection("older-observed"),
        created=CREATED - timedelta(hours=1),
    )
    insert_report(database, report={"account_evidence": {"unrelated": True}})
    assert history.history(DAY) == [
        {"day": PREVIOUS.isoformat(), "precursor_review": None, "flow_review": None}
    ]


def test_operational_failure_notifications_do_not_replace_daily_research(
    history, database
):
    insert_report(database, report=projection())
    failure = {"kind": "DAILY_MOVER_JOB_FAILURE", "error_code": "PUBLIC_SOURCE_FAILURE"}
    insert_report(database, report=failure, created=CREATED + timedelta(hours=1))
    failure_only_day = PREVIOUS - timedelta(days=1)
    insert_report(database, failure_only_day, failure)
    missing_research_day = PREVIOUS - timedelta(days=2)
    insert_report(database, missing_research_day, {"version": MARKET_VERSION})

    assert history.history(DAY) == [
        {
            "day": missing_research_day.isoformat(),
            "precursor_review": None,
            "flow_review": None,
        },
        {"day": PREVIOUS.isoformat(), **projection()},
    ]


def test_malformed_research_sections_are_retained_for_evaluator_validation(
    history, database
):
    report = {"precursor_review": ["invalid-shape"], "flow_review": "invalid-shape"}
    insert_report(database, report=report)
    assert history.history(DAY) == [{"day": PREVIOUS.isoformat(), **report}]


def test_projection_size_excludes_account_material_and_uses_utf8_bytes(
    history, database
):
    report = {
        **projection(),
        "account_evidence": {"padding": "x" * (2 * adapter.MAX_DAY_BYTES)},
    }
    insert_report(database, report=report)
    assert history.history(DAY) == [{"day": PREVIOUS.isoformat(), **projection()}]
    oversized_day = PREVIOUS - timedelta(days=1)
    insert_report(
        database,
        oversized_day,
        {"precursor_review": {"padding": "界" * (adapter.MAX_DAY_BYTES // 3)}},
    )
    result = history.history(DAY)
    assert result[0] == {
        "day": oversized_day.isoformat(),
        "precursor_review": None,
        "flow_review": None,
        "history_error": "DAY_PROJECTION_TOO_LARGE",
    }
    assert result[1] == {"day": PREVIOUS.isoformat(), **projection()}


def test_actual_read_transaction_rejects_writes_and_sets_local_timeouts(
    history, database
):
    insert_report(database, report=projection())
    checked = []

    @contextmanager
    def connect():
        with database() as connection:

            class CheckedConnection:
                def execute(self, query, params=None):
                    if query.lstrip().startswith("WITH latest"):
                        settings = connection.execute(
                            "SELECT current_setting('transaction_read_only'), "
                            "current_setting('transaction_isolation'), "
                            "current_setting('statement_timeout'), current_setting('lock_timeout')"
                        ).fetchone()
                        checked.append(settings)
                        with (
                            pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
                            connection.transaction(),
                        ):
                            connection.execute(
                                "DELETE FROM v2_daily_mover_reports WHERE false"
                            )
                    return connection.execute(query, params)

            yield CheckedConnection()

    assert FlowHistoryEvaluation(connect).history(DAY) == history.history(DAY)
    assert checked == [("on", "repeatable read", "3s", "100ms")]
    with database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM v2_daily_mover_reports"
            ).fetchone()[0]
            == 1
        )


def fake_history(rows):
    @contextmanager
    def connect():
        class Connection:
            def execute(self, query, params=None):
                return rows if query.lstrip().startswith("WITH latest") else None

        yield Connection()

    return FlowHistoryEvaluation(connect)


def encoded_row(day=PREVIOUS, value=None):
    encoded = json.dumps(projection() if value is None else value)
    return day, len(encoded.encode()), encoded


@pytest.mark.parametrize(
    "row",
    [
        (PREVIOUS.isoformat(), 0, ""),
        (date.fromisoformat(DAY), 0, ""),
        (date.fromisoformat(DAY) - timedelta(days=31), 0, ""),
        (PREVIOUS, True, ""),
        (PREVIOUS, -1, ""),
        (PREVIOUS, 3, "{}"),
        (PREVIOUS, 2, b"{}"),
        (PREVIOUS, 2, "[]"),
        (PREVIOUS, 2, "{}"),
        (PREVIOUS, adapter.MAX_DAY_BYTES + 1, "x" * (adapter.MAX_DAY_BYTES + 1)),
    ],
)
def test_corrupt_projection_metadata_or_shape_fails_explicitly(row):
    with pytest.raises(ValueError, match="FLOW_EVALUATION_"):
        fake_history([row]).history(DAY)


def test_fake_total_bytes_bound_is_enforced(monkeypatch):
    first = encoded_row()
    second = encoded_row(PREVIOUS - timedelta(days=1))
    monkeypatch.setattr(adapter, "MAX_HISTORY_BYTES", first[1] + second[1] - 1)
    with pytest.raises(ValueError, match="FLOW_EVALUATION_HISTORY_TOO_LARGE"):
        fake_history([first, second]).history(DAY)


def test_fake_row_limit_is_enforced():
    with pytest.raises(ValueError, match="FLOW_EVALUATION_HISTORY_INVALID"):
        fake_history([encoded_row()] * 31).history(DAY)


def window():
    start = int(datetime(2026, 10, 28, tzinfo=adapter.UTC8).timestamp()) * 1000
    return {"date": DAY, "start_ms": start, "end_ms": start + adapter.DAY}


@pytest.mark.parametrize("field", ["start_ms", "end_ms", "as_of_ms"])
def test_build_rejects_incomplete_or_misaligned_day_before_database(field):
    supplied = window()
    as_of = supplied["end_ms"]
    if field == "as_of_ms":
        as_of -= 1
    else:
        supplied[field] += 1
    with pytest.raises(ValueError, match="FLOW_EVALUATION_WINDOW_INVALID"):
        FlowHistoryEvaluation(no_database).build(
            window=supplied, precursor=None, flow_review=None, as_of_ms=as_of
        )


def test_build_current_size_is_checked_before_database():
    supplied = window()
    with pytest.raises(ValueError, match="FLOW_EVALUATION_CURRENT_TOO_LARGE"):
        FlowHistoryEvaluation(no_database).build(
            window=supplied,
            precursor={"padding": "x" * adapter.MAX_DAY_BYTES},
            flow_review=None,
            as_of_ms=supplied["end_ms"],
        )


def test_build_passes_missing_history_and_current_to_pure_evaluator(monkeypatch):
    observed = []

    def evaluate(reports, *, as_of_ms):
        observed.append((reports, as_of_ms))
        return {"version": "test", "status": "INSUFFICIENT", "content_digest": "stale"}

    monkeypatch.setattr(adapter, "evaluate_flow_history", evaluate)
    supplied = window()
    report = fake_history(
        [encoded_row(value={"precursor_review": None, "flow_review": None})]
    ).build(
        window=supplied,
        precursor={"current": True},
        flow_review=None,
        as_of_ms=supplied["end_ms"],
    )
    assert observed == [
        (
            [
                {
                    "day": PREVIOUS.isoformat(),
                    "precursor_review": None,
                    "flow_review": None,
                },
                {
                    "day": DAY,
                    "precursor_review": {"current": True},
                    "flow_review": None,
                },
            ],
            supplied["end_ms"],
        )
    ]
    assert report["history_read"]["prior_calendar_days"] == 30
    assert report["history_read"]["current_day_included"] is True
    assert report["history_read"]["account_reports_deduplicated_by_day"] is True
    assert report["content_digest"] == digest(
        canonical(
            {key: value for key, value in report.items() if key != "content_digest"}
        )
    )
