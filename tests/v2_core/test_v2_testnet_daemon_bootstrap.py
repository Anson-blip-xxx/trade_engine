import json

import pytest

from services.v2_testnet_daemon_bootstrap import (
    DaemonTelegramNotifier,
    TestnetDaemonConfig,
)
from v2_core.telegram import TelegramOperationalSink


def settings(**changes):
    values = {
        "V2_ACCOUNT_ID": "testnet-primary",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_INTERVAL_SECONDS": "10",
        "V2_ENABLE_ENTRIES": "false",
        "V2_ENABLE_PROTECTION_WRITES": "false",
        "V2_ENABLE_REDUCE_ONLY_EXITS": "false",
        "V2_TESTNET_WRITE_ACK": "",
    }
    values.update(changes)
    return values


def test_disabled_config_is_explicit_and_safe_to_report():
    config = TestnetDaemonConfig.from_mapping(settings())
    assert config.write_enabled is False
    assert config.public_summary() == {
        "account_id": "testnet-primary",
        "environment": "SANDBOX",
        "interval_seconds": 10,
        "enable_entries": False,
        "enable_protection_writes": False,
        "enable_reduce_only_exits": False,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"V2_ENVIRONMENT": "LIVE"},
        {"V2_INTERVAL_SECONDS": "0"},
        {"V2_INTERVAL_SECONDS": "61"},
        {"V2_INTERVAL_SECONDS": "+10"},
        {"V2_ENABLE_ENTRIES": "False"},
        {"V2_ENABLE_ENTRIES": "1"},
        {"V2_ACCOUNT_ID": "bad account"},
        {"V2_ACCOUNT_ID": ""},
        {"V2_ENABLE_ENTRIES": "true"},
        {
            "V2_ENABLE_PROTECTION_WRITES": "true",
            "V2_TESTNET_WRITE_ACK": "SANDBOX:someone-else",
        },
        {"V2_TESTNET_WRITE_ACK": "LIVE:testnet-primary"},
    ],
)
def test_invalid_or_unacknowledged_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        TestnetDaemonConfig.from_mapping(settings(**changes))


def test_each_write_capability_is_account_acknowledged_and_entries_are_protected():
    for flag in ("V2_ENABLE_PROTECTION_WRITES", "V2_ENABLE_REDUCE_ONLY_EXITS"):
        config = TestnetDaemonConfig.from_mapping(
            settings(
                **{flag: "true", "V2_TESTNET_WRITE_ACK": "SANDBOX:testnet-primary"}
            )
        )
        assert config.write_enabled is True
        assert config.enable_entries is False
    config = TestnetDaemonConfig.from_mapping(
        settings(
            V2_ENABLE_ENTRIES="true",
            V2_ENABLE_PROTECTION_WRITES="true",
            V2_TESTNET_WRITE_ACK="SANDBOX:testnet-primary",
        )
    )
    assert config.enable_entries is config.enable_protection_writes is True


class Response:
    status = 200

    def read(self, _):
        return b'{"ok":true,"result":{"message_id":7,"chat":{"id":-123}}}'


class Connection:
    def __init__(self, host, timeout):
        assert (host, timeout) == ("api.telegram.org", 10)
        self.requests = []

    def request(self, method, target, *, body, headers):
        self.requests.append((method, target, body, headers))
        connections.append(self)

    def getresponse(self):
        return Response()

    def close(self):
        pass


connections = []


def test_daemon_notification_is_allowlisted_and_contains_no_exception_text():
    connections.clear()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    notify = DaemonTelegramNotifier(
        sink, account_id="testnet-primary", clock_ms=lambda: 123456
    )
    assert notify(
        {
            "status": "UNAVAILABLE",
            "error_code": "ConnectionError",
            "account_scope": {
                "exchange": "BINANCE",
                "account_id": "testnet-primary",
                "environment": "SANDBOX",
                "product": "FUTURES",
            },
        }
    )
    _, target, body, _ = connections[0].requests[0]
    sent = json.loads(body)
    assert target.startswith("/bot123:")
    assert "TRADING_DAEMON" in sent["text"]
    assert "ConnectionError" in sent["text"]
    assert "account_scope" not in sent["text"]
    assert "secret" not in sent["text"]


def test_daemon_notification_rejects_wrong_scope_or_arbitrary_fields_without_io():
    connections.clear()
    sink = TelegramOperationalSink(
        token="123:abcdefghijklmnop",
        chat_id="-123",
        environment="SANDBOX",
        connection_factory=Connection,
    )
    notify = DaemonTelegramNotifier(
        sink, account_id="testnet-primary", clock_ms=lambda: 123456
    )
    base = {
        "status": "UNAVAILABLE",
        "error_code": "TimeoutError",
        "account_scope": {"account_id": "wrong", "environment": "SANDBOX"},
    }
    assert notify(base) is False
    assert notify({**base, "raw_error": "secret"}) is False
    assert notify({**base, "account_scope": None}) is False
    assert (
        notify(
            {
                **base,
                "account_scope": {
                    "exchange": "BINANCE",
                    "account_id": "testnet-primary",
                    "environment": "SANDBOX",
                    "product": "SPOT",
                },
            }
        )
        is False
    )
    assert connections == []
