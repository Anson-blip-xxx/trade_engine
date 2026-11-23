"""Hot-health queries stay bounded while preserving unknown and old task lag."""

import json
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from services.v2_signal_queue_health import QUEUE_SQL, read_signal_queue
from services.v2_trading_health import retain_unknown_queue
from tests.support.v2_admission import database as database_fixture

database = database_fixture
CONSUMER = "bounded-health-qa"


def observe(connect, **options):
    return read_signal_queue(
        connect,
        **{
            "environment": "SANDBOX",
            "source": "s3",
            "consumer": CONSUMER,
            "now_ms": int(time.time() * 1000),
            "queue_seconds": 30,
            "reconcile_seconds": 60,
            "actionable_events": ("PULSE_UP",),
            "expected_waits": ("ADMISSION_BUDGET_UNAVAILABLE",),
            "admission_quota_codes": ("ADMISSION_BUDGET_UNAVAILABLE",),
            "capacity_codes": ("EXISTING_SYMBOL_POSITION",),
            "dependency_codes": ("SYMBOL_DATA_MISSING",),
            "reason_codes": (
                "ADMISSION_BUDGET_UNAVAILABLE",
                "EXISTING_SYMBOL_POSITION",
                "SYMBOL_DATA_MISSING",
            ),
            **options,
        },
    )


def seed(database, now, *, age=40000, expires=None, source="s3", signal="PULSE_UP"):
    identifier = uuid4()
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals "
            "(signal_id,source,environment,request_key,snapshot,received_at) "
            "VALUES (%s,%s,'SANDBOX',%s,%s::jsonb,to_timestamp(%s/1000.0))",
            (
                identifier,
                source,
                str(uuid4()),
                json.dumps(
                    {
                        "symbol": "BTCUSDT",
                        "signal": signal,
                        "observed_at": now - age,
                        "expires_at_ms": now + 10000 if expires is None else expires,
                    }
                ),
                now - age,
            ),
        )
    return identifier


def task(database, identifier, *, error=None, completed=False, consumer=CONSUMER):
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,error_code,completed_at) "
            "VALUES(%s,%s,%s,CASE WHEN %s THEN clock_timestamp() ELSE NULL END)",
            (consumer, identifier, error, completed),
        )


@pytest.mark.parametrize(
    "option,value",
    [
        ("limit", 0),
        ("limit", True),
        ("limit", 10001),
        ("budget_ms", 0),
        ("budget_ms", 5001),
        ("queue_seconds", 0),
        ("reconcile_seconds", -1),
        ("now_ms", -1),
    ],
)
def test_invalid_bound_never_opens_database(option, value):
    with pytest.raises(ValueError, match="QUEUE_OBSERVATION_BOUND_INVALID"):
        observe(lambda: pytest.fail("must validate before connect"), **{option: value})


def test_connection_failure_is_unknown_not_zero_and_never_leaks_text():
    def unavailable():
        raise RuntimeError("secret private account SQL")

    result = observe(unavailable)
    assert result["row"] is None
    assert result["diagnostics"]["status"] == "UNKNOWN"
    assert "secret" not in str(result)


def test_validated_empty_hot_queue_excludes_global_history_claim(database):
    result = observe(database)
    assert result["row"][0] == 0
    assert result["diagnostics"]["status"] == "COMPLETE"
    assert result["diagnostics"]["historical_undiscovered_coverage"] == (
        "COLD_MAINTENANCE_ONLY"
    )


def test_recent_and_ready_union_deduplicates_and_reads_old_unfinished(database):
    now = int(time.time() * 1000)
    recent = seed(database, now)
    old = seed(database, now, age=3_600_000, expires=now - 1)
    task(database, recent)
    task(database, old)
    result = observe(database, now_ms=now)
    assert result["diagnostics"]["status"] == "COMPLETE"
    row = result["row"]
    assert row[0] == 2
    assert row[1] == now - 3_600_000
    assert row[8:10] == (1, 1)


def test_recent_undiscovered_and_non_actionable_remain_visible(database):
    now = int(time.time() * 1000)
    seed(database, now)
    seed(database, now, signal="HIGH_VOL")
    seed(database, now, age=-1)  # Future receipt is never evidence of lag.
    seed(database, now, age=1000)
    seed(database, now, source="tv_bridge")
    result = observe(database, now_ms=now)
    assert result["row"][0] == 1
    assert result["row"][3] == 1
    assert result["diagnostics"]["recent_keys"] == 2


def test_expected_wait_and_unknown_error_keep_original_classification(database):
    now = int(time.time() * 1000)
    expected = seed(database, now)
    unknown = seed(database, now, signal="HIGH_VOL")
    task(database, expected, error="ADMISSION_BUDGET_UNAVAILABLE")
    task(database, unknown, error="private unclassified SQL")
    result = observe(database, now_ms=now)
    assert result["row"][0] == 1
    assert result["row"][2] == 1
    assert result["row"][10] == 1
    assert result["row"][13] == {
        "ADMISSION_BUDGET_UNAVAILABLE": 1,
        "UNCLASSIFIED_TASK_ERROR": 1,
    }
    assert "private" not in str(result)


def test_terminal_receipt_clears_ready_and_recent_candidate(database):
    now = int(time.time() * 1000)
    identifier = seed(database, now)
    task(database, identifier)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "VALUES(%s,%s,'IGNORED','QA')",
            (CONSUMER, identifier),
        )
    result = observe(database, now_ms=now)
    assert result["row"][0] == 0
    assert result["diagnostics"]["status"] == "COMPLETE"


@pytest.mark.parametrize("mode", ["ready", "recent"])
def test_cap_plus_one_is_unknown_even_if_sample_counts_are_zero(database, mode):
    now = int(time.time() * 1000)
    for _ in range(3):
        identifier = seed(database, now, signal="HIGH_VOL")
        if mode == "ready":
            task(database, identifier)
    result = observe(database, now_ms=now, limit=2)
    assert result["row"][0] == 0
    assert result["diagnostics"]["status"] == "UNKNOWN"
    assert result["diagnostics"]["reason"] == "QUEUE_OBSERVATION_LIMIT_REACHED"
    if mode == "recent":
        assert result["diagnostics"]["tracking_overflow"] is True
        assert result["diagnostics"]["coverage_gap_reason"] == (
            "RECENT_INBOX_WINDOW_LIMIT_REACHED"
        )


def test_recent_cap_keeps_unseen_input_gap_after_sample_drains_and_window_moves(
    database,
):
    now = int(time.time() * 1000)
    identifiers = {str(seed(database, now)) for _ in range(4)}
    first = observe(database, now_ms=now, limit=2)
    tracked = first["diagnostics"]["lag_signal_ids"]
    assert len(tracked) == 3  # limit + 1, with one input not observed at all.
    unseen = identifiers - set(tracked)
    assert len(unseen) == 1
    assert first["diagnostics"]["tracking_overflow"] is True
    assert len(tracked) < 2 * 2 + 2  # No exact-key array truncation is needed.
    lane = "s6:s3"
    code = "SIGNAL_CONSUMPTION_LAG"
    findings = {code: {lane: {"count": first["row"][0]}}}
    old = {
        "observed_at_ms": now,
        "queue_diagnostics": {lane: first["diagnostics"]},
        "findings": findings,
    }
    assert retain_unknown_queue(old["queue_diagnostics"], {}, findings, now, {})
    assert (
        old["queue_diagnostics"][lane]["historical_recovery_unproven_since_ms"] == now
    )
    with database() as conn:
        for identifier in tracked:
            conn.execute(
                "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
                "VALUES(%s,%s,'EXPIRED','QA')",
                (CONSUMER, identifier),
            )
    later = observe(
        database,
        now_ms=now + 120_000,
        limit=2,
        previous_signal_ids=tracked,
    )
    # The bounded hot result is genuinely empty; its coverage is not the
    # missing input's historical coverage proof.
    assert later["diagnostics"]["status"] == "COMPLETE"
    assert later["row"][0] == 0
    current, findings = {lane: later["diagnostics"]}, {}
    assert retain_unknown_queue(current, old, findings, now + 120_000, {})
    assert current[lane]["status"] == "UNKNOWN"
    assert current[lane]["reason"] == "HISTORICAL_RECOVERY_NOT_PROVEN"
    assert current[lane]["historical_recovery_unproven_since_ms"] == now
    assert current[lane]["lag_signal_ids"] == []
    assert code in findings
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_inbound_signals s "
            "WHERE s.signal_id=%s AND NOT EXISTS "
            "(SELECT FROM v2_strategy_tasks t WHERE t.signal_id=s.signal_id) "
            "AND NOT EXISTS "
            "(SELECT FROM v2_signal_receipts r WHERE r.signal_id=s.signal_id)",
            (next(iter(unseen)),),
        ).fetchone() == (1,)


def test_ready_only_cap_does_not_invent_recent_window_coverage_loss(database):
    now = int(time.time() * 1000)
    for _ in range(4):
        identifier = seed(database, now, age=120_000)
        task(database, identifier)
    result = observe(database, now_ms=now, limit=2)
    assert result["diagnostics"]["status"] == "UNKNOWN"
    assert result["diagnostics"]["ready_keys"] == 3
    assert result["diagnostics"]["recent_keys"] == 0
    assert "tracking_overflow" not in result["diagnostics"]
    assert "coverage_gap_reason" not in result["diagnostics"]


def test_exact_cap_is_complete(database):
    now = int(time.time() * 1000)
    for _ in range(2):
        seed(database, now)
    result = observe(database, now_ms=now, limit=2)
    assert result["diagnostics"]["status"] == "COMPLETE"
    assert result["row"][0] == 2


@pytest.mark.parametrize("defect", ["disabled", "trigger", "schema", "mismatch"])
def test_unvalidated_or_inconsistent_projection_is_unknown(database, defect):
    now = int(time.time() * 1000)
    if defect == "mismatch":
        identifier = seed(database, now)
        task(database, identifier, completed=True)
    with database() as conn:
        if defect == "disabled":
            conn.execute("UPDATE v2_strategy_ready_control SET enabled=false")
        elif defect == "trigger":
            conn.execute(
                "ALTER TABLE v2_strategy_tasks DISABLE TRIGGER v2_strategy_ready_sync"
            )
        elif defect == "schema":
            conn.execute(
                "ALTER TABLE v2_strategy_ready_control RENAME TO qa_missing_control"
            )
        else:
            conn.execute(
                "INSERT INTO v2_strategy_ready(consumer,signal_id) VALUES(%s,%s)",
                (CONSUMER, identifier),
            )
    result = observe(database, now_ms=now)
    assert result["row"] is None
    assert result["diagnostics"]["status"] == "UNKNOWN"


def test_query_failure_rolls_back_and_does_not_poison_other_health_reads(database):
    class Failing:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            if sql == QUEUE_SQL:
                return self.conn.execute("SELECT 1/0")
            return self.conn.execute(sql, args)

    @contextmanager
    def failure():
        with database() as conn:
            yield Failing(conn)

    result = observe(failure)
    assert result["row"] is None
    assert result["diagnostics"]["status"] == "UNKNOWN"
    with database() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_recent_window_covers_queue_threshold_and_reconciliation(database):
    now = int(time.time() * 1000)
    result = observe(database, now_ms=now, queue_seconds=30, reconcile_seconds=300)
    assert result["diagnostics"]["recent_from_ms"] == now - 300_000
    assert result["diagnostics"]["recent_to_ms"] == now - 30_000


def test_undiscovered_lag_is_pinned_after_recent_window_until_receipt(database):
    now = int(time.time() * 1000)
    identifier = seed(database, now)
    first = observe(database, now_ms=now)
    assert first["diagnostics"]["lag_signal_ids"] == [str(identifier)]
    later = observe(
        database,
        now_ms=now + 120_000,
        previous_signal_ids=first["diagnostics"]["lag_signal_ids"],
    )
    assert later["diagnostics"]["status"] == "COMPLETE"
    assert later["diagnostics"]["ready_keys"] == 0
    assert later["diagnostics"]["recent_keys"] == 0
    assert later["row"][0] == 1
    assert later["diagnostics"]["lag_signal_ids"] == [str(identifier)]
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "VALUES(%s,%s,'EXPIRED','QA')",
            (CONSUMER, identifier),
        )
    cleared = observe(
        database,
        now_ms=now + 120_001,
        previous_signal_ids=later["diagnostics"]["lag_signal_ids"],
    )
    assert cleared["diagnostics"]["status"] == "COMPLETE"
    assert cleared["row"][0] == 0
    assert cleared["diagnostics"]["lag_signal_ids"] == []


def test_expected_wait_reclassifies_a_pinned_signal(database):
    now = int(time.time() * 1000)
    identifier = seed(database, now, age=120_000)
    task(database, identifier, error="ADMISSION_BUDGET_UNAVAILABLE")
    result = observe(database, now_ms=now, previous_signal_ids=[str(identifier)])
    assert result["row"][0] == 0
    assert result["row"][2] == 1
    assert result["diagnostics"]["lag_signal_ids"] == []


def test_pinned_error_signal_does_not_disappear_when_error_clears(database):
    now = int(time.time() * 1000)
    identifier = seed(database, now, age=120_000, signal="HIGH_VOL")
    # A previously observed processing error can be cleared by a worker retry;
    # that is not a terminal receipt for its already recorded lag incident.
    result = observe(database, now_ms=now, previous_signal_ids=[str(identifier)])
    assert result["row"][0] == 1
    assert result["diagnostics"]["lag_signal_ids"] == [str(identifier)]


@pytest.mark.parametrize("failure", ["cap", "query"])
def test_unknown_observation_preserves_previous_ids(database, failure):
    now = int(time.time() * 1000)
    identifier = seed(database, now, age=120_000)
    previous = [str(identifier)]
    if failure == "cap":
        for _ in range(3):
            seed(database, now)
        result = observe(database, now_ms=now, limit=2, previous_signal_ids=previous)
    else:

        def unavailable():
            raise TimeoutError("QA timeout")

        result = observe(unavailable, now_ms=now, previous_signal_ids=previous)
    assert result["diagnostics"]["status"] == "UNKNOWN"
    assert str(identifier) in result["diagnostics"]["lag_signal_ids"]


@pytest.mark.parametrize(
    "identifiers", ["not-an-array", ["not-a-uuid"], [None], [str(uuid4())] * 5]
)
def test_invalid_or_excessive_tracking_keys_are_unknown_before_db(identifiers):
    result = observe(
        lambda: pytest.fail("invalid tracking keys must not read DB"),
        previous_signal_ids=identifiers,
        limit=1,
    )
    assert result["row"] is None
    assert result["diagnostics"]["status"] == "UNKNOWN"
    assert result["diagnostics"]["reason"] == "QUEUE_TRACKING_KEYS_INVALID"


@pytest.mark.parametrize("mode", ["query_cap", "tracking_cap"])
def test_tracking_truncation_always_reports_overflow(database, mode):
    now = int(time.time() * 1000)
    # limit=1 allows four tracked IDs. Four prior IDs outside the recent window
    # must survive even when new candidates can no longer fit the same report.
    previous = [str(seed(database, now, age=120_000)) for _ in range(4)]
    for _ in range(2 if mode == "query_cap" else 1):
        seed(database, now)
    result = observe(database, now_ms=now, limit=1, previous_signal_ids=previous)
    diagnostics = result["diagnostics"]
    assert diagnostics["status"] == "UNKNOWN"
    assert diagnostics["tracking_overflow"] is True
    assert set(diagnostics["lag_signal_ids"]) == set(previous)
    assert len(diagnostics["lag_signal_ids"]) == 4
    assert diagnostics["reason"] == (
        "QUEUE_OBSERVATION_LIMIT_REACHED"
        if mode == "query_cap"
        else "QUEUE_TRACKING_LIMIT_REACHED"
    )
