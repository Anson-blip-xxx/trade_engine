import json
from copy import deepcopy
from io import BytesIO

import pytest

from v2_core.ingress import ContextProvider, IntakeRejected, SignalIngress
from v2_core.signals import SignalConflict
from v2_core.webhook import TradingViewWebhook

SECRET = "qa-only-not-a-real-secret-123456789"


class MemorySignals:
    def __init__(self):
        self.rows = {}

    def lookup(self, *, source, environment, request_key):
        return deepcopy(self.rows.get((source, environment, request_key)))

    def admit(self, *, source, environment, request_key, snapshot):
        key = source, environment, request_key
        if key in self.rows and self.rows[key]["snapshot"] != snapshot:
            raise SignalConflict("conflict")
        self.rows[key] = {"signal_id": "qa-signal", "snapshot": deepcopy(snapshot)}
        return "qa-signal"


def alert(**changes):
    return {
        "secret": SECRET,
        "event_id": "tv:bar1:trend",
        "observed_at": 100,
        "expires_at_ms": 200,
        "signal": "TREND_UP_LONG",
        "symbol": "BINANCE:BTCUSDT.P",
        "price": "100.25",
        "strength": 70,
        **changes,
    }


def application(store=None, clock=None):
    store = MemorySignals() if store is None else store
    ingress = SignalIngress(
        store,
        source="tv_bridge",
        environment="SANDBOX",
        clock_ms=clock or (lambda: 120),
        max_age_ms=100,
        max_lifetime_ms=100,
    )
    return TradingViewWebhook(ingress, secret=SECRET), store


def call(app, payload=None, *, body=None, **changes):
    body = (
        json.dumps(alert() if payload is None else payload).encode()
        if body is None
        else body
    )
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v2/webhooks/tradingview",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
        **changes,
    }
    response = []
    output = b"".join(
        app(environ, lambda status, headers: response.append((status, dict(headers))))
    )
    return int(response[0][0].split()[0]), json.loads(output), response[0][1]


def test_webhook_records_normalized_event_without_secret():
    app, store = application()
    code, result, headers = call(app)
    assert code == 200 and result == {"status": "RECORDED", "signal_id": "qa-signal"}
    row = next(iter(store.rows.values()))["snapshot"]
    assert row["symbol"] == "BTCUSDT" and row["signal"] == "TREND_UP"
    assert row["observed_at"] == 100 and row["expires_at_ms"] == 200
    assert row["features"] == {
        "price": "100.25",
        "strength": 70,
        "tv_signal": "TREND_UP_LONG",
    }
    assert SECRET not in repr(store.rows)
    assert headers["Cache-Control"] == "no-store"


def test_webhook_preserves_directional_evidence_as_decimal_strings():
    app, store = application()
    code, _, _ = call(
        app,
        alert(chg_15m="8.125", chg_1h="12.5", vol_1h="18.75"),
    )
    assert code == 200
    features = next(iter(store.rows.values()))["snapshot"]["features"]
    assert features == {
        "price": "100.25",
        "strength": 70,
        "tv_signal": "TREND_UP_LONG",
        "chg_15m": "8.125",
        "chg_1h": "12.5",
        "vol_1h": "18.75",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 100.25},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "0"},
        {"strength": True},
        {"strength": 101},
        {"symbol": "EVIL:BTCUSDT.P"},
        {"symbol": "BTCUSDT/../../"},
        {"symbol": "BTC_PERP"},
        {"signal": "UNKNOWN"},
        {"signal": []},
        {"observed_at": True},
        {"observed_at": 121},
        {"observed_at": 1, "expires_at_ms": 101},
        {"expires_at_ms": 201},
        {"event_id": ""},
        {"event_id": "a" * 129},
        {"source": "s3"},
        {"environment": "LIVE"},
        {"comment": "do not persist arbitrary text"},
        {"taker_buy_ratio": "1.01"},
        {"orderflow_bias": "-1.1"},
        {"chg_15m": "-100.01"},
        {"chg_1h": "10000.01"},
        {"vol_1h": "-0.01"},
    ],
)
def test_webhook_rejects_invalid_input_without_persistence(changes):
    app, store = application()
    assert call(app, alert(**changes))[0] == 400
    assert store.rows == {}


@pytest.mark.parametrize("supplied", [None, "", "wrong", 3, {"secret": SECRET}])
def test_webhook_authenticates_before_any_storage_access(supplied):
    class NoAccess:
        def lookup(self, **_):
            pytest.fail("unauthenticated DB access")

    app, _ = application(NoAccess())
    assert call(app, alert(secret=supplied))[0] == 401


@pytest.mark.parametrize(
    "raw",
    [
        b'{"secret":"one","secret":"two"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":"\\ud800"}',
        b"[]",
        b"null",
        b"\xff",
        b"{",
    ],
)
def test_webhook_rejects_ambiguous_or_invalid_json(raw):
    app, store = application()
    assert call(app, body=raw)[0] == 400
    assert not store.rows


@pytest.mark.parametrize(
    "change,code",
    [
        ({"REQUEST_METHOD": "GET"}, 405),
        ({"PATH_INFO": "/"}, 404),
        ({"CONTENT_TYPE": "text/plain"}, 415),
        ({"CONTENT_LENGTH": "-1"}, 400),
        ({"CONTENT_LENGTH": "9999999999999999999999"}, 400),
        ({"CONTENT_LENGTH": "16385"}, 413),
        ({"CONTENT_LENGTH": "0"}, 413),
        ({"CONTENT_LENGTH": ""}, 400),
        ({"CONTENT_LENGTH": "١"}, 400),
        ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
    ],
)
def test_webhook_http_guards_do_not_read_body(change, code):
    class Unreadable:
        def read(self, _):
            pytest.fail("body read for rejected HTTP request")

    app, _ = application()
    assert call(app, **{**change, "wsgi.input": Unreadable()})[0] == code


def test_webhook_truncated_body_and_storage_failure_never_ack_success():
    app, _ = application()
    assert call(app, CONTENT_LENGTH="1000")[0] == 400

    class Unavailable:
        def lookup(self, **_):
            raise PermissionError("sensitive database configuration")

    app, _ = application(Unavailable())
    code, result, headers = call(app)
    assert code == 503 and result == {"error": "INGRESS_UNAVAILABLE"}
    assert headers["Retry-After"] == "5"


def test_webhook_replay_does_not_renew_deadline_and_conflict_is_409():
    now = [120]
    app, store = application(clock=lambda: now[0])
    assert call(app)[0] == 200
    now[0] = 999
    assert call(app)[0] == 200
    assert len(store.rows) == 1
    assert call(app, alert(price="100.26"))[0] == 409
    assert call(app, alert(event_id="new-event"))[0] == 400


def ingress_config():
    return {
        "V2_SIGNAL_INGRESS_ENABLED": "YES",
        "V2_POSTGRES_DSN": "qa-only-no-connection",
        "V2_POSTGRES_SCHEMA": "v2_qa",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_TV_WEBHOOK_SECRET": SECRET,
        "V2_SIGNAL_MAX_AGE_MS": "100",
        "V2_SIGNAL_MAX_LIFETIME_MS": "100",
    }


@pytest.mark.parametrize("key", list(ingress_config()))
def test_service_factory_requires_explicit_configuration(key):
    from services.v2_signal_ingress import create_application

    config = ingress_config()
    config.pop(key)
    with pytest.raises(ValueError):
        create_application(environ=config)


@pytest.mark.parametrize(
    "changes",
    [
        {"V2_POSTGRES_SCHEMA": "public"},
        {"V2_ENVIRONMENT": "unknown"},
        {"V2_SIGNAL_MAX_AGE_MS": "0"},
        {"V2_SIGNAL_MAX_LIFETIME_MS": "86400001"},
        {"V2_SIGNAL_MAX_AGE_MS": "NaN"},
        {"V2_SIGNAL_MAX_AGE_MS": "١"},
        {"V2_TV_WEBHOOK_SECRET": "short"},
        {"V2_SIGNAL_INGRESS_ENABLED": "true"},
    ],
)
def test_service_factory_rejects_unsafe_configuration(changes):
    from services.v2_signal_ingress import create_application

    with pytest.raises(ValueError):
        create_application(environ={**ingress_config(), **changes})


def test_service_factory_constructs_without_database_or_listener(monkeypatch):
    from services import v2_signal_ingress

    bindings = []

    def factory(dsn, *, schema):
        bindings.append((dsn, schema))
        return lambda: pytest.fail("construction must not connect")

    monkeypatch.setattr(v2_signal_ingress, "connection_factory", factory)
    app = v2_signal_ingress.create_application(
        environ=ingress_config(), clock_ms=lambda: 120
    )
    assert app.ingress.environment == "SANDBOX" and app.ingress.source == "tv_bridge"
    assert bindings == [("qa-only-no-connection", "v2_qa")]
    assert call(app, alert(secret="wrong"))[0] == 401


def test_webhook_conforms_to_wsgi_without_opening_a_listener():
    from wsgiref.util import setup_testing_defaults
    from wsgiref.validate import validator

    app, _ = application()
    body = json.dumps(alert()).encode()
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        REQUEST_METHOD="POST",
        PATH_INFO="/v2/webhooks/tradingview",
        QUERY_STRING="",
        CONTENT_TYPE="application/json",
        CONTENT_LENGTH=str(len(body)),
    )
    environ["wsgi.input"] = BytesIO(body)
    statuses = []
    output = validator(app)(environ, lambda status, headers: statuses.append(status))
    try:
        assert json.loads(b"".join(output))["status"] == "RECORDED"
        assert statuses == ["200 OK"]
    finally:
        output.close()


def context_envelope(source, environment, symbol):
    return {
        "snapshot_id": source + ":100",
        "source": source,
        "environment": environment,
        "symbol": symbol,
        "observed_at": 100,
        "features": {"price": "100"},
    }


def context_provider(read=context_envelope, clock=lambda: 120):
    return ContextProvider(
        environment="SANDBOX",
        policy={
            "s0": {"scope": "GLOBAL", "max_age_ms": 50},
            "s3": {"scope": "SYMBOL", "max_age_ms": 30},
        },
        read=read,
        clock_ms=clock,
    )


def test_context_freezes_explicit_sources_with_scope_and_timestamp():
    cache = {}

    def read(source, env, symbol):
        cache[source] = context_envelope(source, env, symbol)
        return cache[source]

    context = context_provider(read)({"symbol": "BTCUSDT"})
    cache["s3"]["features"]["price"] = "999"
    assert context["sources"]["s0"]["symbol"] == "*"
    assert context["sources"]["s3"]["symbol"] == "BTCUSDT"
    assert context["sources"]["s3"]["features"]["price"] == "100"
    assert context["assembled_at"] == 120
    assert context["valid_until_ms"] == 131


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "s2"},
        {"environment": "LIVE"},
        {"symbol": "ETHUSDT"},
        {"observed_at": 121},
        {"observed_at": 0},
        {"observed_at": False},
        {"snapshot_id": ""},
        {"features": None},
        {"features": {"secret": "test"}},
        {"features": {"price": 100.1}},
        {"unexpected": 1},
    ],
)
def test_context_rejects_wrong_scope_missing_or_stale_facts(changes):
    def read(source, env, symbol):
        return {**context_envelope(source, env, symbol), **changes}

    with pytest.raises((IntakeRejected, ValueError)):
        context_provider(read)({"symbol": "BTCUSDT"})


def test_context_checks_freshness_after_all_reads_and_has_no_missing_fallback():
    now = [120]

    def slow_read(source, env, symbol):
        now[0] += 20
        return context_envelope(source, env, symbol)

    with pytest.raises(IntakeRejected, match="STALE_CONTEXT"):
        context_provider(slow_read, clock=lambda: now[0])({"symbol": "BTCUSDT"})
    with pytest.raises(IntakeRejected, match="MISSING_CONTEXT"):
        context_provider(lambda *_: None)({"symbol": "BTCUSDT"})


@pytest.mark.parametrize("source", ["s2", "s3"])
def test_internal_producer_uses_same_admission_contract(source):
    store = MemorySignals()
    ingress = SignalIngress(
        store,
        source=source,
        environment="SANDBOX",
        clock_ms=lambda: 120,
        max_age_ms=100,
        max_lifetime_ms=100,
    )
    event = {
        "event_id": "producer:1",
        "observed_at": 100,
        "expires_at_ms": 200,
        "symbol": "BTCUSDT",
        "signal": "TREND_UP",
        "features": {"strength": 70},
    }
    assert ingress.accept(event) == "qa-signal"
    event["features"]["strength"] = 90
    assert (
        store.rows[(source, "SANDBOX", "producer:1")]["snapshot"]["features"][
            "strength"
        ]
        == 70
    )
