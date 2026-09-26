import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_v2_intent_admission import database as database_fixture

from services.v2_market_pipeline import MarketSupervisor
from v2_core.operational import MarketAlerts
from v2_core.public_market import PublicMarketError
from v2_core.telegram import TelegramDeliveryError, TelegramOperationalSink

database = database_fixture


def test_legacy_failure_has_no_invented_duration(database):
    with database() as c:
        c.execute(
            "INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload) VALUES (gen_random_uuid(),'SANDBOX','legacy','MARKET_FAILURE','{\"stage\":\"COLLECT\",\"error_code\":\"PublicMarketError\"}')"
        )
    worker = MarketAlerts(database, environment="SANDBOX", notify=lambda _: True)
    assert not worker.recovered()


def test_durable_recovery_and_reopen(database):
    received = []
    worker = MarketAlerts(
        database, environment="SANDBOX", notify=lambda e: received.append(e) or True
    )
    failure = {
        "stage": "COLLECT",
        "error_code": "PublicMarketError",
        "reason_code": "RATE_LIMITED",
    }
    assert not worker.recovered()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: worker.record(failure), range(8)))
    with database() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_operational_outbox").fetchone()[0] == 1
        )
    restarted = MarketAlerts(
        database, environment="SANDBOX", notify=lambda e: received.append(e) or True
    )
    assert restarted.recovered()
    assert not restarted.recovered()
    restarted.record(failure)
    assert restarted.flush()["delivered"] == 3
    assert [e["kind"] for e in received].count("MARKET_RECOVERED") == 1
    assert all(e["duration_ms"] >= 0 for e in received)
    assert all(e.get("reason_code", "RATE_LIMITED") == "RATE_LIMITED" for e in received)


@pytest.mark.parametrize("value", ["token=secret", {"secret": "value"}, None, 123])
def test_reason_allowlist(value):
    assert PublicMarketError(value).reason_code == "PUBLIC_UNKNOWN"


class Connection:
    def __init__(self, sent, fail=False):
        self.sent, self.fail = sent, fail

    def request(self, method, path, *, body, headers):
        self.body = json.loads(body)
        self.sent.append(self.body)

    def getresponse(self):
        return self

    @property
    def status(self):
        return 403 if self.fail else 200

    def read(self, _):
        return json.dumps(
            {
                "ok": True,
                "result": {"message_id": 1, "chat": {"id": int(self.body["chat_id"])}},
            }
        ).encode()

    def close(self):
        pass


def test_alert_dm_and_trade_group(monkeypatch):
    sent = []
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        alerts_chat_id="456",
        environment="SANDBOX",
        connection_factory=lambda *a, **kw: Connection(sent),
    )
    monkeypatch.setattr("v2_core.telegram._trade_text", lambda *args: "trade fixture")
    assert sink("e", "SANDBOX", "TRADE_OPENED", {})
    assert sink("e", "SANDBOX", "TRADE_CLOSED", {})
    assert sink(
        "e",
        "SANDBOX",
        "MARKET_FAILURE",
        {
            "stage": "COLLECT",
            "error_code": "PublicMarketError",
            "reason_code": "RATE_LIMITED",
            "started_at_ms": 0,
            "observed_at_ms": 2000,
            "duration_ms": 2000,
        },
    )
    assert [x["chat_id"] for x in sent] == ["-123", "-123", "456"]
    assert "交易所接口限流" in sent[-1]["text"]
    assert "UTC+8" in sent[-1]["text"]
    assert sink.pin_destination == "123:-123"
    assert not sink("e", "LIVE", "MARKET_FAILURE", {})


def test_dm_failure_never_falls_back_to_group():
    sent = []
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        alerts_chat_id="456",
        environment="SANDBOX",
        connection_factory=lambda *a, **kw: Connection(sent, fail=True),
    )
    with pytest.raises(TelegramDeliveryError):
        sink("e", "SANDBOX", "TRADING_DAEMON", {"status": "UNAVAILABLE"})
    assert [x["chat_id"] for x in sent] == ["456"]


def test_group_not_allowed_as_dm():
    with pytest.raises(ValueError):
        TelegramOperationalSink(
            token="123:abcdefghijklmnop",
            chat_id="-123",
            alerts_chat_id="-456",
            environment="SANDBOX",
        )


@pytest.mark.parametrize(
    "status,frame,expected",
    [
        ("ACKNOWLEDGED", True, 1),
        ("IDLE", True, 0),
        ("RETRY", True, 0),
        ("CURRENT", False, 0),
        ("WAITING_SOURCE", False, 0),
    ],
)
def test_recovery_requires_new_acknowledged_batch(status, frame, expected):
    calls = []
    service = object.__new__(MarketSupervisor)
    service.alerts = SimpleNamespace(recovered=lambda: calls.append(True), flush=dict)
    service._run_once = lambda: {
        "status": status,
        **({"collected_frame_id": "fixture"} if frame else {}),
    }
    service.run_once()
    assert len(calls) == expected
