import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_capital_income import baseline, income
from test_v2_capital_income import case as income_case
from test_v2_capital_journal import setup as journal_setup
from test_v2_capital_transfers import transfer_case as transfer_fixture
from test_v2_intent_admission import database as database_fixture
from test_v2_testnet_watchdog import Connection

from v2_core.capital_monitor import (
    CapitalMonitorTelegram,
    CapitalTransferMonitor,
    TransferMonitorPolicy,
)
from v2_core.telegram import TelegramOperationalSink

database = database_fixture
setup = journal_setup
case = income_case
transfer_case = transfer_fixture
POLICY = TransferMonitorPolicy(
    scan_ms=10,
    overdue_ms=20,
    confirm_ms=10,
    recovery_ms=20,
    reminder_ms=100,
    retry_ms=30,
)


@pytest.fixture
def monitor_case(transfer_case):
    transfers, projection, setup = transfer_case
    j, _, tenant, _, a, b, *_ = setup
    with j.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_capital_monitor.sql"
            ).read_text()
        )
    baseline(projection, tenant, a)
    baseline(projection, tenant, b)
    outgoing = income(j, tenant, a, kind="TRANSFER", value="-25", at=101)
    incoming = income(j, tenant, b, kind="TRANSFER", value="25", at=103)
    return CapitalTransferMonitor(j.connect), transfers, setup, outgoing, incoming


def scan(case, now, **kw):
    m, _, setup, *_ = case
    return m.scan(setup[2], setup[4], now_ms=now, policy=POLICY, **kw)


def resolve(case):
    _, transfers, setup, out, inc = case
    return transfers.match(
        setup[2], outgoing_id=out, incoming_id=inc, evidence_ref=uuid4()
    )


def test_confirm_restart_reminder_and_debounced_recovery(monitor_case):
    m, _, setup, *_ = monitor_case
    assert scan(monitor_case, 120)["overdue_count"] == 0
    assert scan(monitor_case, 130)["emitted"] is None
    assert (
        m.__class__(m.connect).scan(setup[2], setup[4], now_ms=140, policy=POLICY)[
            "emitted"
        ]
        == "OPEN"
    )
    assert scan(monitor_case, 141)["skipped"] is True
    assert scan(monitor_case, 150)["emitted"] is None
    assert scan(monitor_case, 240)["emitted"] == "REMINDER"
    resolve(monitor_case)
    assert scan(monitor_case, 250)["active"] is True
    assert scan(monitor_case, 260)["emitted"] is None
    assert scan(monitor_case, 270)["emitted"] == "RECOVERED"
    assert scan(monitor_case, 280)["emitted"] is None


def test_concurrent_scan_enqueues_once(monitor_case):
    scan(monitor_case, 130)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: scan(monitor_case, 140), range(8)))
    assert sum(not r["skipped"] for r in results) == 1
    with monitor_case[0].connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_monitor_events").fetchone()[0]
            == 1
        )


def test_state_and_event_rollback_together(monitor_case):
    m = monitor_case[0]
    scan(monitor_case, 130)
    with m.connect() as c:
        c.execute(
            "CREATE FUNCTION qa_fail_monitor() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fail'; END $$"
        )
        c.execute(
            "CREATE TRIGGER qa_fail BEFORE INSERT ON v2_capital_monitor_events FOR EACH ROW EXECUTE FUNCTION qa_fail_monitor()"
        )
    with pytest.raises(psycopg.Error):
        scan(monitor_case, 140)
    with m.connect() as c:
        assert (
            c.execute(
                "SELECT payload->>'active' FROM v2_capital_monitor_state"
            ).fetchone()[0]
            == "false"
        )
        c.execute("DROP TRIGGER qa_fail ON v2_capital_monitor_events")
    assert scan(monitor_case, 140)["emitted"] == "OPEN"


def test_policy_change_clock_and_scope(monitor_case):
    m, _, setup, *_ = monitor_case
    scan(monitor_case, 130)
    changed = replace(POLICY, confirm_ms=1)
    assert m.scan(setup[2], setup[4], now_ms=131, policy=changed)["emitted"] == "OPEN"
    with pytest.raises(ValueError, match="CLOCK_REGRESSION"):
        scan(monitor_case, 130)
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        m.scan(setup[3], setup[4], now_ms=140, policy=POLICY)
    assert m.scan(setup[2], setup[5], now_ms=130, policy=POLICY)["active"] is False


def test_failed_send_backoff_and_concurrent_ack(monitor_case):
    m, _, setup, *_ = monitor_case
    scan(monitor_case, 130)
    scan(monitor_case, 140)
    ref = str(uuid4())

    def deliver(now, send):
        return m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=ref,
            now_ms=now,
            policy=POLICY,
            send=send,
        )

    assert deliver(140, lambda _: False) == "RETRY"
    assert deliver(150, lambda _: pytest.fail("backoff ignored")) == "BACKOFF"
    calls = []

    def send(event):
        calls.append(event)
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: deliver(170, send), range(4)))
    assert results.count("SENT") == 1 and len(calls) == 1
    assert deliver(200, send) == "IDLE"
    with pytest.raises(ValueError, match="DESTINATION_CONFLICT"):
        m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=uuid4(),
            now_ms=200,
            policy=POLICY,
            send=send,
        )


def test_stale_alerts_coalesce_to_latest_recovery(monitor_case):
    m, _, setup, *_ = monitor_case
    for now in (130, 140, 240):
        scan(monitor_case, now)
    resolve(monitor_case)
    scan(monitor_case, 250)
    scan(monitor_case, 270)
    seen = []

    def send(event):
        seen.append(event)
        return True

    assert (
        m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=uuid4(),
            now_ms=280,
            policy=POLICY,
            send=send,
        )
        == "SENT"
    )
    assert [e["kind"] for e in seen] == ["RECOVERED"]
    with m.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_monitor_events").fetchone()[0]
            == 3
        )


def test_reversal_reopens_monitor_after_recovery(monitor_case):
    _, _, setup, *_ = monitor_case
    scan(monitor_case, 130)
    scan(monitor_case, 140)
    matched = resolve(monitor_case)
    scan(monitor_case, 150)
    scan(monitor_case, 170)
    setup[0].reverse(
        setup[2],
        matched["incoming_journal"],
        source="qa",
        source_id="undo",
        occurred_at_ms=180,
    )
    assert scan(monitor_case, 180)["emitted"] is None
    assert scan(monitor_case, 190)["emitted"] == "OPEN"


@pytest.mark.parametrize(
    "changes",
    [
        {"scan_ms": True},
        {"retry_ms": 0},
        {"overdue_ms": -1},
        {"reminder_ms": 1},
        {"confirm_ms": 604800001},
    ],
)
def test_policy_validation(changes):
    with pytest.raises(ValueError):
        replace(POLICY, **changes)


def test_telegram_adapter_routes_only_bound_account(monitor_case):
    m, _, setup, *_ = monitor_case

    class Sink(TelegramOperationalSink):
        environment = "SANDBOX"

        def __init__(self):
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            return True

    sink, ref = Sink(), str(uuid4())
    sender = CapitalMonitorTelegram(
        m.connect,
        tenant_id=setup[2],
        registry_id=setup[4],
        destination_ref=ref,
        sink=sink,
    )
    event = {
        "event_id": str(uuid4()),
        "tenant_id": setup[2],
        "registry_id": setup[4],
        "destination_ref": ref,
        "kind": "OPEN",
        "payload": {"observed_at_ms": 140},
    }
    assert sender(event) is True
    assert sink.calls[0][3]["error_code"] == "CAPITAL_TRANSFER_OVERDUE"
    with pytest.raises(ValueError, match="ROUTE_MISMATCH"):
        sender(dict(event, registry_id=setup[5]))
    assert len(sink.calls) == 1


def test_real_telegram_template_uses_utc8_without_network(monitor_case):
    m, _, setup, *_ = monitor_case
    scan(monitor_case, 130)
    scan(monitor_case, 140)
    Connection.calls.clear()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    ref = str(uuid4())
    sender = CapitalMonitorTelegram(
        m.connect,
        tenant_id=setup[2],
        registry_id=setup[4],
        destination_ref=ref,
        sink=sink,
    )
    assert (
        m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=ref,
            now_ms=140,
            policy=POLICY,
            send=sender,
        )
        == "SENT"
    )
    message = json.loads(Connection.calls[0][2])["text"]
    assert "CAPITAL_TRANSFER_OVERDUE" in message and "UTC+8" in message
    assert "不会自动补造流水" in message


def test_transport_exception_is_retried_without_secret_persistence(monitor_case):
    m, _, setup, *_ = monitor_case
    scan(monitor_case, 130)
    scan(monitor_case, 140)

    def fail(_):
        raise RuntimeError("do-not-persist-transport-secret")

    ref = str(uuid4())
    assert (
        m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=ref,
            now_ms=140,
            policy=POLICY,
            send=fail,
        )
        == "RETRY"
    )
    with m.connect() as c:
        row = c.execute(
            "SELECT last_sequence,next_attempt_ms FROM v2_capital_monitor_delivery"
        ).fetchone()
        assert row == (0, 170)
    assert (
        m.deliver_latest(
            setup[2],
            setup[4],
            destination_ref=ref,
            now_ms=170,
            policy=POLICY,
            send=lambda _: True,
        )
        == "SENT"
    )


def test_new_unmatched_fact_cancels_recovery_window(monitor_case):
    scan(monitor_case, 130)
    scan(monitor_case, 140)
    resolve(monitor_case)
    assert scan(monitor_case, 150)["active"] is True
    setup = monitor_case[2]
    income(setup[0], setup[2], setup[4], kind="TRANSFER", value="-1", at=102)
    result = scan(monitor_case, 160)
    assert result["active"] is True and result["clear_since_ms"] is None
    assert scan(monitor_case, 170)["emitted"] is None


def test_monitor_event_history_cannot_be_deleted(monitor_case):
    scan(monitor_case, 130)
    scan(monitor_case, 140)
    with pytest.raises(psycopg.Error), monitor_case[0].connect() as c:
        c.execute("DELETE FROM v2_capital_monitor_events")
