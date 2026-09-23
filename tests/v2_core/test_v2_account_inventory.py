import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture

from services.v2_testnet_inventory import config_fields, credentials
from services.v2_testnet_market import pilot_config
from v2_core.account_inventory import (
    AccountInventory,
    InventoryProjector,
    persist_inventory,
)
from v2_core.account_risk import AccountScope
from v2_core.operational import MarketAlerts
from v2_core.telegram import TelegramDeliveryError, TelegramOperationalSink

database = database_fixture
SCOPE = AccountScope("BINANCE", "inventory-test", "SANDBOX", "FUTURES")


class ReadAccount:
    account_id = SCOPE.account_id
    environment = SCOPE.environment

    def __init__(self):
        self.calls = []
        self.rows = {
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
            "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
            "/fapi/v3/positionRisk": [],
            "/fapi/v3/account": {
                "totalWalletBalance": "100",
                "availableBalance": "100",
                "totalMarginBalance": "100",
            },
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [],
        }
        self.fail = None

    def __call__(self, method, path, params):
        assert method == "GET" and params == {}
        self.calls.append(path)
        if path == self.fail:
            raise RuntimeError("sensitive-url-and-key")
        return copy.deepcopy(self.rows[path])


def collect(reader=None, clock=lambda: 1000):
    return AccountInventory(
        reader or ReadAccount(), scope=SCOPE, clock_ms=clock
    ).collect(str(uuid4()))


def test_empty_account_is_not_execution_permission():
    reader = ReadAccount()
    result = collect(reader)
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert result["execution_authorized"] is False
    assert result["summary"]["trade_permission_verified"] is False
    assert len(reader.calls) == 7
    assert len(result["response_digests"]) == 7


@pytest.mark.parametrize(
    "field,entry,blocker",
    [
        (
            "/fapi/v3/positionRisk",
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.1"},
            "EXISTING_POSITION_REQUIRES_RECOVERY",
        ),
        (
            "/fapi/v1/openOrders",
            {"symbol": "BTCUSDT", "orderId": 1},
            "EXISTING_ORDINARY_ORDERS",
        ),
        (
            "/fapi/v1/openAlgoOrders",
            {"symbol": "BTCUSDT", "algoId": 1},
            "EXISTING_CONDITIONAL_ORDERS",
        ),
    ],
)
def test_any_existing_exposure_blocks(field, entry, blocker):
    reader = ReadAccount()
    reader.rows[field] = [entry]
    result = collect(reader)
    assert result["status"] == "BLOCKED"
    assert blocker in result["blockers"]


def test_explicit_external_position_exclusion_is_quantity_only_and_audited():
    reader = ReadAccount()
    reader.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"}
    ]
    result = AccountInventory(
        reader,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        excluded_position_symbols=("ZORAUSDT",),
    ).collect(str(uuid4()))
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert result["excluded_position_symbols"] == ["ZORAUSDT"]
    assert result["summary"]["positions"] == []
    assert result["summary"]["excluded_positions"] == [
        {"symbol": "ZORAUSDT", "side": "BOTH", "quantity": "26399"}
    ]


def test_external_position_exclusion_never_hides_orders_or_movement():
    class MovingExcluded(ReadAccount):
        def __call__(self, method, path, params):
            result = super().__call__(method, path, params)
            if path == "/fapi/v3/positionRisk":
                quantity = "1" if self.calls.count(path) == 1 else "2"
                return [
                    {
                        "symbol": "ZORAUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": quantity,
                    }
                ]
            return result

    reader = MovingExcluded()
    reader.rows["/fapi/v1/openOrders"] = [{"symbol": "ZORAUSDT", "orderId": 1}]
    result = AccountInventory(
        reader,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        excluded_position_symbols=("ZORAUSDT",),
    ).collect(str(uuid4()))
    assert "POSITION_CHANGED_DURING_INVENTORY" in result["blockers"]
    assert "EXISTING_ORDINARY_ORDERS" in result["blockers"]


@pytest.mark.parametrize("path", list(ReadAccount().rows))
def test_failed_read_is_not_empty_account_and_never_leaks(path):
    reader = ReadAccount()
    reader.fail = path
    result = collect(reader)
    assert result["status"] == "BLOCKED"
    assert "sensitive" not in json.dumps(result)
    assert "INCOMPLETE_ACCOUNT_READ" in result["blockers"]


@pytest.mark.parametrize("value", [True, None, "false"])
def test_non_boolean_or_hedge_mode_blocks(value):
    reader = ReadAccount()
    reader.rows["/fapi/v1/positionSide/dual"]["dualSidePosition"] = value
    assert "ONE_WAY_MODE_REQUIRED" in collect(reader)["blockers"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("/fapi/v3/account", {}),
        ("/fapi/v1/openOrders", {}),
        (
            "/fapi/v3/positionRisk",
            [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "NaN"}],
        ),
        ("/fapi/v1/openAlgoOrders", [{"symbol": "BTCUSDT", "algoId": True}]),
    ],
)
def test_malformed_inventory_blocks(field, bad):
    reader = ReadAccount()
    reader.rows[field] = bad
    assert "INVALID_ACCOUNT_RESPONSE" in collect(reader)["blockers"]


def test_changed_position_during_reads_blocks():
    class Moving(ReadAccount):
        def __call__(self, method, path, params):
            result = super().__call__(method, path, params)
            if path == "/fapi/v3/positionRisk" and self.calls.count(path) == 2:
                return [
                    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
                ]
            return result

    assert "POSITION_CHANGED_DURING_INVENTORY" in collect(Moving())["blockers"]


def test_deadline_stops_additional_requests():
    values = iter([1000, 40000, 40000])
    reader = ReadAccount()
    result = collect(reader, clock=lambda: next(values))
    assert "ACQUISITION_DEADLINE" in result["blockers"]
    assert reader.calls == []


def test_transport_account_binding_required():
    reader = ReadAccount()
    reader.account_id = "other"
    with pytest.raises(ValueError, match="SCOPE"):
        collect(reader)


def test_snapshot_and_outbox_concurrent_idempotency(database):
    observation = collect()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: persist_inventory(
                    database, scope=SCOPE, observation=observation
                ),
                range(8),
            )
        )
    assert len({r["state_id"] for r in results}) == 1
    with database() as conn:
        for table in ("v2_business_state", "v2_state_history", "v2_operational_outbox"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 1
    changed = copy.deepcopy(observation)
    changed["finished_at_ms"] += 1
    with pytest.raises(ValueError, match="CONFLICT"):
        persist_inventory(database, scope=SCOPE, observation=changed)


def test_notification_insert_failure_rolls_back_snapshot(database):
    @contextmanager
    def broken():
        with database() as conn:

            class Connection:
                def execute(self, query, params):
                    if "INSERT INTO v2_operational_outbox" in query:
                        raise RuntimeError("injected failure")
                    return conn.execute(query, params)

            yield Connection()

    with pytest.raises(RuntimeError, match="injected"):
        persist_inventory(broken, scope=SCOPE, observation=collect())
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_business_state").fetchone()[0] == 0


def test_notifications_filter_before_claim_and_retry_after_restart(database):
    first = collect()
    persist_inventory(database, scope=SCOPE, observation=first)
    other = AccountScope("BINANCE", "other", "SANDBOX", "FUTURES")
    second = copy.deepcopy(first)
    second["observation_id"] = str(uuid4())
    second["account_scope"]["account_id"] = other.account_id
    persist_inventory(database, scope=other, observation=second)
    market = MarketAlerts(database, environment="SANDBOX", notify=lambda _: True)
    assert market.flush()["claimed"] == 0
    failed = InventoryProjector(database, scope=SCOPE, sink=lambda *_: False)
    assert failed.run_scheduled_batch()["failed"] == 1
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_operational_attempts").fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE v2_operational_attempts SET next_attempt_at=clock_timestamp()-interval '1 second'"
        )
    recovered = InventoryProjector(database, scope=SCOPE, sink=lambda *_: True)
    assert recovered.run_scheduled_batch()["delivered"] == 1
    assert recovered.run_scheduled_batch()["claimed"] == 0


class TelegramConnection:
    status = 200
    raw = b'{"ok":true,"result":{"message_id":1,"chat":{"id":-123}}}'

    def __init__(self):
        self.calls = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        self.closed = True


def sink(conn):
    def factory(host, timeout):
        assert host == "api.telegram.org" and timeout == 10
        return conn

    return TelegramOperationalSink(
        token="12345:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=factory,
    )


def test_telegram_only_selected_fields_and_scoped_environment():
    conn = TelegramConnection()
    notify = sink(conn)
    assert not notify("1", "LIVE", "ACCOUNT_INVENTORY", {})
    assert notify(
        "1",
        "SANDBOX",
        "ACCOUNT_INVENTORY",
        {"status": "BLOCKED", "secret": "never-forward"},
    )
    assert len(conn.calls) == 1 and conn.closed
    assert b"never-forward" not in conn.calls[0][1]["body"]


@pytest.mark.parametrize(
    "status,raw",
    [
        (301, b"token"),
        (429, b"token"),
        (200, b'{"ok":false}'),
        (200, b'{"ok":true,"result":{"message_id":1,"chat":{"id":999}}}'),
    ],
)
def test_telegram_unconfirmed_is_redacted_without_retry(status, raw):
    conn = TelegramConnection()
    conn.status, conn.raw = status, raw
    with pytest.raises(TelegramDeliveryError, match="UNCONFIRMED"):
        sink(conn)("1", "SANDBOX", "ACCOUNT_INVENTORY", {})
    assert len(conn.calls) == 1 and conn.closed


def test_credentials_never_fall_back_to_live_keys(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("BINANCE_API_KEY=live-key\nBINANCE_API_SECRET=live-secret\n")
    with pytest.raises(ValueError, match="MISSING_TESTNET"):
        credentials(path, notify=False)
    path.write_text(
        "BINANCE_API_KEY=live-key\nBINANCE_TESTNET_API_KEY=test-key\nBINANCE_TESTNET_API_SECRET=test-secret\n"
    )
    assert credentials(path, notify=False) == {
        "BINANCE_TESTNET_API_KEY": "test-key",
        "BINANCE_TESTNET_API_SECRET": "test-secret",
    }


def test_market_config_has_no_execution_or_live_environment_switch():
    first = pilot_config()
    assert first["environment"] == "SANDBOX"
    assert first["symbols"] == ["BTCUSDT", "ETHUSDT"]
    first["symbols"].append("UNKNOWNUSDT")
    assert pilot_config()["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert not {"api_key", "api_secret", "enable_trading"}.intersection(first)


def test_telegram_config_does_not_select_exchange_keys(tmp_path):
    path = tmp_path / "tg.env"
    path.write_text(
        "TG_NOTIFY_TOKEN=123:abcdefghijklmnop\nTG_NOTIFY_CHAT_ID=-123\nBINANCE_TESTNET_API_KEY=not-selected\n"
    )
    assert config_fields(path, {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}) == {
        "TG_NOTIFY_TOKEN": "123:abcdefghijklmnop",
        "TG_NOTIFY_CHAT_ID": "-123",
    }


def test_duplicate_secret_fields_reject_without_echoing_value(tmp_path):
    path = tmp_path / "duplicate.env"
    path.write_text("TG_NOTIFY_TOKEN=first-secret\nTG_NOTIFY_TOKEN=second-secret\n")
    with pytest.raises(ValueError, match="DUPLICATE") as caught:
        config_fields(path, {"TG_NOTIFY_TOKEN"})
    assert "secret" not in str(caught.value)
