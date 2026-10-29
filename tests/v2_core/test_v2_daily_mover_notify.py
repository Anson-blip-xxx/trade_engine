import json

import pytest

from services.v2_daily_mover_notify import (
    MoverNotificationError,
    PrivateMoverNotifier,
    make_notifier,
)

TOKEN = "123456:abcdefghijklmnopqrstuv"
ENV = {"V2_TG_ALERT_CHAT_ID": "12345", "CREDENTIALS_DIRECTORY": "/run/credentials/unit"}


class Response:
    status = 200

    def __init__(self, payload=None):
        self.payload = (
            payload
            if payload is not None
            else {"ok": True, "result": {"message_id": 456, "chat": {"id": 12345}}}
        )
        self.read_size = None

    def read(self, size):
        self.read_size = size
        return (
            self.payload
            if isinstance(self.payload, bytes)
            else json.dumps(self.payload).encode()
        )


class Connection:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def factory(connection, calls):
    def build(host, *, timeout):
        calls.append((host, timeout))
        return connection

    return build


def credentials(_):
    return {"TG_NOTIFY_TOKEN": TOKEN, "TG_NOTIFY_CHAT_ID": "-9999999"}


def test_protected_credentials_use_explicit_dm_never_existing_group():
    conn, builds = Connection(), []
    loader_calls = []

    def loader(path):
        loader_calls.append(path)
        return credentials(path)

    notify = make_notifier(
        ENV, credential_loader=loader, connection_factory=factory(conn, builds)
    )
    assert notify("每日榜单\n中文 😀") == 456
    assert loader_calls == [ENV["CREDENTIALS_DIRECTORY"]]
    assert builds == [("api.telegram.org", 10)]
    args, kwargs = conn.calls[0]
    assert args == ("POST", f"/bot{TOKEN}/sendMessage")
    assert json.loads(kwargs["body"]) == {
        "chat_id": "12345",
        "text": "每日榜单\n中文 😀",
    }
    assert "parse_mode" not in json.loads(kwargs["body"])
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    assert conn.response.read_size == 65537
    assert conn.closed


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"V2_TG_ALERT_CHAT_ID": "12345"},
        {"CREDENTIALS_DIRECTORY": "/run/credentials/unit"},
    ],
)
def test_missing_configuration_never_reads_credentials_or_falls_back_to_group(env):
    def forbidden(_):
        pytest.fail("credentials must not be read for unconfigured delivery")

    assert make_notifier(env, credential_loader=forbidden) is None


@pytest.mark.parametrize(
    "destination",
    ["-10012345", "0", "0012", "@name", "123 456", "https://bad", True, 12345],
)
def test_private_destination_validation_rejects_group_and_ambiguous_inputs(destination):
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        make_notifier(
            {**ENV, "V2_TG_ALERT_CHAT_ID": destination}, credential_loader=credentials
        )


@pytest.mark.parametrize("value", ["", "short", "token/../../foo", "123:wrong?path"])
def test_invalid_token_rejected_without_exposing_it(value):
    with pytest.raises(ValueError, match="CONFIG_INVALID") as caught:
        make_notifier(ENV, credential_loader=lambda _: {"TG_NOTIFY_TOKEN": value})
    if value:
        assert value not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False},
        {"ok": 1, "result": {"message_id": 456, "chat": {"id": 12345}}},
        {"ok": True, "result": {"message_id": True, "chat": {"id": 12345}}},
        {"ok": True, "result": {"message_id": 0, "chat": {"id": 12345}}},
        {"ok": True, "result": {"message_id": -1, "chat": {"id": 12345}}},
        {"ok": True, "result": {"message_id": "123", "chat": {"id": 12345}}},
        {"ok": True, "result": {"message_id": 456, "chat": {"id": -12345}}},
        {"ok": True, "result": {"message_id": 456, "chat": {"id": 999}}},
        {"ok": True, "result": {"message_id": 456}},
        [],
        b"not-json",
        b'{"ok":false,"ok":true,"result":{"message_id":456,"chat":{"id":12345}}}',
    ],
)
def test_unconfirmed_response_never_returns_receipt(payload):
    conn, builds = Connection(Response(payload)), []
    notify = make_notifier(
        ENV, credential_loader=credentials, connection_factory=factory(conn, builds)
    )
    with pytest.raises(MoverNotificationError, match="UNCONFIRMED"):
        notify("summary")
    assert len(builds) == len(conn.calls) == 1
    assert conn.closed


@pytest.mark.parametrize("status", [301, 429, 500])
def test_http_error_is_unknown_without_redirect_or_retry(status):
    conn, builds = Connection(), []
    conn.response.status = status
    notify = make_notifier(
        ENV, credential_loader=credentials, connection_factory=factory(conn, builds)
    )
    with pytest.raises(MoverNotificationError, match="UNCONFIRMED"):
        notify("summary")
    assert len(builds) == 1
    assert conn.closed


def test_timeout_does_not_retry_or_expose_token_or_message():
    class Broken(Connection):
        def getresponse(self):
            raise TimeoutError(f"{TOKEN} super-private-summary")

    conn, builds = Broken(), []
    notify = make_notifier(
        ENV, credential_loader=credentials, connection_factory=factory(conn, builds)
    )
    with pytest.raises(MoverNotificationError) as caught:
        notify("super-private-summary")
    assert str(caught.value) == "MOVER_PRIVATE_NOTIFICATION_UNCONFIRMED"
    assert len(builds) == len(conn.calls) == 1
    assert conn.closed


@pytest.mark.parametrize(
    "text", ["", "a" * 3501, "😀" * 1751, "\ud800", b"bytes", None]
)
def test_invalid_message_never_creates_connection(text):
    def forbidden(*_, **__):
        pytest.fail("invalid message must not reach network")

    notify = PrivateMoverNotifier(
        token=TOKEN, destination="12345", connection_factory=forbidden
    )
    with pytest.raises(ValueError, match="TEXT_INVALID"):
        notify(text)


def test_exact_message_bound_and_oversized_response():
    conn = Connection(Response(b"x" * 65537))
    notify = PrivateMoverNotifier(
        token=TOKEN, destination="12345", connection_factory=lambda *_a, **_k: conn
    )
    with pytest.raises(MoverNotificationError, match="UNCONFIRMED"):
        notify("中" * 3500)
    assert len(json.loads(conn.calls[0][1]["body"])["text"]) == 3500


def test_credential_loader_failure_has_only_safe_fixed_reason():
    def broken(_):
        raise PermissionError("private-path private-token")

    with pytest.raises(ValueError) as caught:
        make_notifier(ENV, credential_loader=broken)
    assert str(caught.value) == "MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID"
