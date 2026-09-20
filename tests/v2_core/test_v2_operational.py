from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_v2_intent_admission import (
    closed_candle_batch,
    durable_source,
)
from test_v2_intent_admission import (
    database as database_fixture,
)

from services.v2_market_pipeline import MarketSupervisor
from services.v2_s3_source import SourceQuarantined
from v2_core.operational import MarketAlerts

database = database_fixture

FAILURE = {"stage": "READ", "error_code": "ConnectionError", "status": "RETRY"}


def alerts(database, sink=lambda _: True, environment="SANDBOX"):
    return MarketAlerts(database, environment=environment, notify=sink)


def due(database):
    with database() as conn:
        conn.execute(
            "UPDATE v2_operational_attempts SET next_attempt_at=clock_timestamp()-interval '1 second', lease_until=NULL,lease_token=NULL"
        )


def test_concurrent_record_coalesces_and_drops_untrusted_fields(database):
    worker = alerts(database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(
            pool.map(
                lambda _: worker.record({**FAILURE, "secret": "do-not-store"}),
                range(16),
            )
        )
    with database() as conn:
        rows = conn.execute("SELECT payload FROM v2_operational_outbox").fetchall()
        assert len(rows) == 1
        assert set(rows[0][0]) == {"stage", "error_code", "bucket"}
        assert conn.execute("SELECT count(*) FROM v2_domain_outbox").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"stage": "secret"},
        {"error_code": "secret password"},
        {"error_code": "x" * 81},
        {"error_code": None},
    ],
)
def test_alert_validation(database, bad):
    with pytest.raises(ValueError):
        alerts(database).record({**FAILURE, **bad})


def test_retry_survives_restart_and_receipt_stops_delivery(database):
    worker = alerts(database, lambda _: False)
    worker.record(FAILURE)
    assert worker.flush()["failed"] == 1
    assert worker.flush()["claimed"] == 0
    due(database)
    received = []
    restarted = alerts(database, lambda event: received.append(event) or True)
    assert restarted.flush()["delivered"] == 1
    assert restarted.flush()["claimed"] == 0
    assert received[0]["kind"] == "MARKET_FAILURE"
    with database() as conn:
        assert (
            conn.execute("SELECT attempts FROM v2_operational_attempts").fetchone()[0]
            == 2
        )


def test_environments_never_consume_each_others_backlog(database):
    sandbox, live = [], []
    first = alerts(database, lambda event: sandbox.append(event) or True)
    second = alerts(database, lambda event: live.append(event) or True, "LIVE")
    first.record(FAILURE)
    second.record(FAILURE)
    assert first.flush()["delivered"] == 1
    assert first.flush()["claimed"] == 0
    assert second.flush()["delivered"] == 1
    assert sandbox[0]["environment"] == "SANDBOX"
    assert live[0]["environment"] == "LIVE"


def test_quarantine_crash_gap_is_recovered_from_evidence(database):
    clock = [86400001]
    source, _ = durable_source(database, now=lambda: clock[0])
    source.enqueue(closed_candle_batch())
    clock[0] += 1000000
    with pytest.raises(SourceQuarantined):
        source.read()
    worker = alerts(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: worker.scan_quarantines(), range(8))) == 1
    assert worker.flush()["delivered"] == 1
    with database() as conn:
        payload = conn.execute("SELECT payload FROM v2_operational_outbox").fetchone()[
            0
        ]
        assert payload["outcome"] == "EXPIRED_UNPUBLISHED"
        assert payload["frame_id"].startswith("s3-closed-1m-v1:")


def test_poison_notification_does_not_block_other_events(database):
    def send(event):
        if event["stage"] == "READ":
            raise ConnectionError("secret token must not persist")
        return True

    worker = alerts(database, send)
    worker.record(FAILURE)
    worker.record({**FAILURE, "stage": "ACK"})
    result = worker.flush()
    assert result["failed"] == result["delivered"] == 1
    with database() as conn:
        assert (
            conn.execute(
                "SELECT error_code FROM v2_operational_attempts WHERE error_code IS NOT NULL"
            ).fetchone()[0]
            == "ConnectionError"
        )


def test_expired_worker_cannot_ack_after_takeover(database):
    second = alerts(database)

    def delayed(event):
        due(database)
        assert second.flush()["delivered"] == 1
        return True

    first = alerts(database, delayed)
    first.record(FAILURE)
    assert first.flush()["superseded"] == 1
    assert second.flush()["claimed"] == 0


def test_success_before_receipt_failure_replays_same_event_id(database):
    received = []
    first = alerts(database)
    first.record(FAILURE)
    calls = [0]

    @contextmanager
    def failing_connection():
        calls[0] += 1
        if calls[0] == 2:
            raise ConnectionError("receipt connection unavailable")
        with database() as conn:
            yield conn

    first.projector._connect = failing_connection
    first.projector.sink = lambda event_id, *_: received.append(event_id) or True
    with pytest.raises(ConnectionError):
        first.projector.run_scheduled_batch()
    due(database)
    restarted = alerts(
        database, lambda event: received.append(event["event_id"]) or True
    )
    assert restarted.flush()["delivered"] == 1
    assert len(received) == 2 and received[0] == received[1]


def test_concurrent_deliverers_claim_once(database):
    worker = alerts(database)
    worker.record(FAILURE)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: alerts(database).flush(), range(12)))
    assert sum(result["delivered"] for result in results) == 1


def test_outbox_and_receipts_are_immutable(database):
    worker = alerts(database)
    worker.record(FAILURE)
    worker.flush()
    for table in ("v2_operational_outbox", "v2_operational_receipts"):
        with pytest.raises(Exception, match="immutable"), database() as conn:
            conn.execute(f"DELETE FROM {table}")


def supervisor(worker):
    publisher = SimpleNamespace(environment="SANDBOX")
    service = MarketSupervisor(
        source=SimpleNamespace(publisher=publisher, has_pending=lambda: False),
        runtime=SimpleNamespace(processor=SimpleNamespace(publisher=publisher)),
        collector=SimpleNamespace(environment="SANDBOX"),
        notify=lambda _: pytest.fail("must use durable queue"),
        alerts=worker,
    )
    service.runner = SimpleNamespace(run_once=lambda: dict(FAILURE))
    return service


def test_supervisor_distinguishes_queued_from_delivered(database):
    result = supervisor(alerts(database, lambda _: False)).run_once()
    assert result["alert_status"] == "QUEUED"
    assert result["alert_delivery"]["failed"] == 1


def test_pg_outage_is_visible_and_never_claims_durability():
    def unavailable():
        raise ConnectionError("secret dsn")

    result = supervisor(alerts(unavailable)).run_once()
    assert result["alert_status"] == "UNAVAILABLE"
    assert result["alert_delivery"] == {
        "status": "UNAVAILABLE",
        "error_code": "ConnectionError",
    }


@pytest.mark.parametrize("limit", [True, 0, 1001, -1])
def test_scan_is_bounded(database, limit):
    with pytest.raises(ValueError):
        alerts(database).scan_quarantines(limit)


def test_unconfirmed_projection_without_exception_is_durably_reported(database):
    received = []
    service = supervisor(alerts(database, lambda event: received.append(event) or True))
    service.runner.run_once = lambda: {
        "status": "RETRY",
        "stage": "PROJECT",
        "frame_id": "frame",
    }
    result = service.run_once()
    assert result["alert_status"] == "QUEUED"
    assert result["alert_delivery"]["delivered"] == 1
    assert received[0]["error_code"] == "ProjectionUnconfirmed"


def test_healthy_supervisor_drains_pending_notifications(database):
    worker = alerts(database, lambda _: False)
    worker.record(FAILURE)
    worker.flush()
    due(database)
    service = supervisor(alerts(database))
    service.runner.run_once = lambda: {"status": "IDLE"}
    service.source.has_pending = lambda: True
    result = service.run_once()
    assert result["status"] == "WAITING_SOURCE"
    assert result["alert_delivery"]["delivered"] == 1


def test_supervisor_rejects_mixed_alert_environment(database):
    with pytest.raises(ValueError, match="environment"):
        supervisor(alerts(database, environment="LIVE"))


def test_delivery_batch_limit_and_backlog_progress(database):
    worker = alerts(database)
    for stage in ("READ", "ACK", "COLLECT"):
        worker.record({**FAILURE, "stage": stage})
    assert worker.flush(limit=1)["delivered"] == 1
    assert worker.flush(limit=1)["delivered"] == 1
    assert worker.flush(limit=1)["delivered"] == 1
    assert worker.flush(limit=1)["claimed"] == 0
