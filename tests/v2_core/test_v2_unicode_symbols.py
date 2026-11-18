"""Exact CJK venue identities through fake-only execution and producer ports.

No database, secrets, process changes, real HTTP or order capability is used.
"""

import hashlib
import hmac
import json
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
from test_v2_binance import Transport, adapter, fill, order, response
from test_v2_ingress import MemorySignals, alert, application
from test_v2_liquidity import evaluate, evaluate_quote, execution_quote, snapshot
from test_v2_transport import Connection, transport

from services.v2_s3_source import frame_digest
from services.v2_shared_market import IncrementalCandles
from services.v2_shared_risk_reference import SharedRiskReference
from services.v2_testnet_scoped_cleanup import ScopedCleanup
from v2_core.account_risk import AccountScope
from v2_core.ingress import IntakeRejected, SignalIngress, identity, symbol
from v2_core.jev_advisory import prepare_request
from v2_core.lifecycle import advance_events
from v2_core.producer import ProducerPublisher
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.reduction_fence import reduction_fence
from v2_core.shared_market import contracts
from v2_core.state import StateSnapshot
from v2_core.symbols import contract_symbol, is_supported_symbol, shared_frame_id
from v2_core.telegram import health_notification_details
from v2_core.transport import ExchangeTransportError

TARGET = "牛来USDT"
SCOPE = AccountScope("BINANCE", "qa", "SANDBOX", "FUTURES")


@pytest.mark.parametrize(
    "value",
    [
        TARGET,
        "币安人生USDT",
        "1000牛USDC",
        "BTCUSDT",
        "1000PEPEUSDT",
        "\U00020000USDT",
        "牛" * 32 + "USDT",
    ],
)
def test_contract_identity_is_bounded_and_returned_exactly(value):
    assert contract_symbol(value) == value
    assert symbol(value) == value
    assert is_supported_symbol(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "USDT",
        "牛" * 33 + "USDT",
        "牛来USD",
        "牛来usdt",
        "abcUSDT",
        "ABC_DEFUSDT",
        "牛来 USDT",
        "牛来USDT\n",
        "\t牛来USDT",
        "牛\x00来USDT",
        "牛\u200d来USDT",
        "牛\u202e来USDT",
        "牛\u0301USDT",
        "ＡUSDT",
        "АUSDT",
        "éUSDT",
        "ßUSDT",
        "١USDT",
        "牛/来USDT",
        "牛:来USDT",
        "牛%20USDT",
        "牛来.P",
        "\ud800USDT",
    ],
)
def test_invalid_unicode_and_injection_never_become_an_identity(value):
    assert not is_supported_symbol(value)
    with pytest.raises(ValueError, match="^INVALID_SYMBOL$"):
        contract_symbol(value)
    with pytest.raises(IntakeRejected, match="^INVALID_SYMBOL$"):
        symbol(value)


def test_catalog_admits_cjk_but_keeps_environment_listing_and_status_gates():
    def row(name, **changes):
        return {
            "symbol": name,
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "filters": [{"filterType": "LOT_SIZE"}],
            **changes,
        }

    catalog = contracts(
        {
            "symbols": [
                row(TARGET),
                row("éUSDT"),
                row("HALTUSDT", status="PENDING_TRADING"),
                row("OTHERUSDT", contractType="NEXT_QUARTER"),
            ]
        }
    )
    assert catalog[TARGET]["supported"] is True
    assert catalog["éUSDT"]["supported"] is False
    assert "HALTUSDT" not in catalog and "OTHERUSDT" not in catalog
    assert "MISSINGUSDT" not in catalog


def test_frame_identity_keeps_ascii_legacy_and_full_unicode_digest():
    stamp = 2**53 - 1
    assert (
        shared_frame_id("s3-shared-v6", "BTCUSDT", stamp)
        == f"s3-shared-v6:BTCUSDT:{stamp}"
    )
    for target in (TARGET, "牛莱USDT", "牛" * 32 + "USDT"):
        result = shared_frame_id("s3-shared-v6", target, stamp)
        assert (
            result
            == f"s3-shared-v6:u-{hashlib.sha256(target.encode()).hexdigest()}:{stamp}"
        )
        assert result.isascii() and len(result) <= 128 and identity(result) == result
    assert shared_frame_id("s3-shared-v6", TARGET, stamp) != shared_frame_id(
        "s3-shared-v6", "牛莱USDT", stamp
    )
    with pytest.raises(IntakeRejected, match="INVALID_EVENT_ID"):
        identity(f"s3-shared-v6:{TARGET}:{stamp}")


def test_cjk_s3_frame_producer_events_and_signal_intake_share_exact_symbol():
    frame_id = shared_frame_id("s3-shared-v6", TARGET, 100)
    _, events = advance_events(
        {},
        [{"symbol": TARGET, "type": "PULSE_UP", "strength": 75}],
        environment="SANDBOX",
        frame_id=frame_id,
        observed_at=100,
        covered={TARGET},
        removed=set(),
        cooldown_ms=30,
        absence_ms=50,
        strength_delta=20,
    )
    publisher = ProducerPublisher(
        lambda: pytest.fail("unexpected DB"),
        source="s3",
        environment="SANDBOX",
        market=None,
        clock_ms=lambda: 120,
        max_age_ms=100,
        lifetime_ms=100,
    )
    frozen, _ = publisher.prepare(
        frame_id=frame_id,
        observed_at=100,
        contexts={TARGET: {"price": "100"}},
        events=events,
    )
    event = frozen["events"][0]
    assert (
        event["symbol"] == TARGET and identity(event["event_id"]) == event["event_id"]
    )
    store = MemorySignals()
    ingress = SignalIngress(
        store,
        source="s3",
        environment="SANDBOX",
        clock_ms=lambda: 120,
        max_age_ms=100,
        max_lifetime_ms=100,
    )
    ingress.accept(event)
    assert next(iter(store.rows.values()))["snapshot"]["symbol"] == TARGET


def test_generated_incremental_frame_and_risk_reader_agree_on_unicode_id(monkeypatch):
    observed = 86400000

    def public(path, params):
        assert path == "/fapi/v1/klines" and params["symbol"] == TARGET
        return [
            [t, "100", "102", "99", "101", "10", t + 59999, "0", 1, "6", "0", "0"]
            for t in range(params["startTime"], params["endTime"] + 1, 60000)
        ]

    public.environment = "SANDBOX"
    source = IncrementalCandles(
        public,
        SimpleNamespace(hget=lambda *_: None),
        SimpleNamespace(environment="SANDBOX", read=lambda *_: []),
    )
    prepared = source.prepare(TARGET, observed)
    frame = prepared["frame"]
    assert frame["frame_id"] == shared_frame_id("s3-shared-v2", TARGET, observed)
    reader = SharedRiskReference(
        lambda: pytest.fail("unexpected DB"),
        scope=SCOPE,
        archive=SimpleNamespace(read=lambda *_: prepared["combined"]),
        clock_ms=lambda: observed + 100,
    )
    head = (observed, frame["frame_id"], frame_digest(frame))
    monkeypatch.setattr(reader, "_head", lambda _: head)
    request = {**asdict(SCOPE), "symbol": TARGET, "leg": "OPEN"}
    assert reader(request).symbol == TARGET
    monkeypatch.setattr(
        reader,
        "_head",
        lambda _: (
            observed,
            shared_frame_id("s3-shared-v2", "牛莱USDT", observed),
            head[2],
        ),
    )
    with pytest.raises(ValueError, match="REFERENCE_FEATURE_VERSION_UNSUPPORTED"):
        reader(request)


def test_pine_unicode_event_is_bound_encoded_idempotently_and_retains_symbol():
    app, store = application(clock=lambda: 900020)
    original = f"tv:BINANCE:{TARGET}.P:15:900000:TREND_UP_LONG"
    body = json.dumps(
        alert(
            symbol=f"BINANCE:{TARGET}.P",
            event_id=original,
            observed_at=900000,
            expires_at_ms=900100,
            producer={
                "version": "tv-causal-15m-v2",
                "setup": "TREND_BREAKOUT",
                "timeframe": "15",
                "bar_open_ms": 0,
                "bar_close_ms": 900000,
            },
        ),
        ensure_ascii=False,
    ).encode()
    assert app.receive(body) == app.receive(body) == "qa-signal"
    key, row = next(iter(store.rows.items()))
    assert key[2] == "tv-u:" + hashlib.sha256(original.encode()).hexdigest()
    assert identity(key[2]) == key[2]
    assert row["snapshot"]["symbol"] == TARGET
    assert row["snapshot"]["observed_at"] == 900000
    assert row["snapshot"]["expires_at_ms"] == 900100
    assert row["snapshot"]["features"]["tv_producer"]["setup"] == "TREND_BREAKOUT"


@pytest.mark.parametrize(
    "event_id",
    [
        f"tv:BINANCE:{TARGET}.P:15:101:TREND_UP_LONG",
        f"tv:BINANCE:{TARGET}.P:15:100:TREND_DOWN_SHORT",
        "tv:BINANCE:牛莱USDT.P:15:100:TREND_UP_LONG",
        f"tv:BINANCE:{TARGET}.P:5:100:TREND_UP_LONG",
        "任意中文事件",
    ],
)
def test_unicode_tv_identity_must_match_every_pine_field(event_id):
    app, store = application()
    with pytest.raises(IntakeRejected, match="INVALID_EVENT_ID"):
        app.receive(
            json.dumps(alert(symbol=f"BINANCE:{TARGET}.P", event_id=event_id)).encode()
        )
    assert not store.rows


@pytest.mark.parametrize(
    "raw",
    [
        "BINANCE:ßUSDT.P",
        f" BINANCE:{TARGET}.P",
        f"BINANCE:{TARGET}.P ",
        "BINANCE:牛\u0301USDT.P",
    ],
)
def test_tv_does_not_casefold_or_strip_unicode_identities(raw):
    app, store = application()
    with pytest.raises(IntakeRejected, match="INVALID_SYMBOL"):
        app.receive(json.dumps(alert(symbol=raw)).encode())
    assert not store.rows


def test_jev_and_both_liquidity_gates_accept_exact_cjk_symbol():
    request = prepare_request(
        {
            "symbol": TARGET,
            "observed_at_ms": 100000,
            "timeframe": "15m",
            "direction": "LONG",
            "price": "100",
            "atr_pct": "1.2",
            "return_1h_pct": "2.5",
        },
        now_ms=100001,
        max_age_ms=60000,
    )
    assert request["request"]["state"]["symbol"] == TARGET
    assert (
        evaluate(symbol=TARGET, evidence={**snapshot(), "symbol": TARGET})["reason"]
        == "LIQUIDITY_PASSED"
    )
    assert (
        evaluate_quote(symbol=TARGET, evidence={**execution_quote(), "symbol": TARGET})[
            "reason"
        ]
        == "LIQUIDITY_EXECUTION_PASSED"
    )


@pytest.mark.parametrize("closing", [False, True])
def test_orders_and_complete_fill_receipts_preserve_exact_cjk_identity(closing):
    side = "SELL" if closing else "BUY"
    item = {
        **order(),
        "symbol": TARGET,
        "side": side,
        "leg": "CLOSE" if closing else "OPEN",
        "reduce_only": closing,
    }
    venue = Transport(
        raw={**response(), "symbol": TARGET, "side": side, "reduceOnly": closing},
        pages=[[{**fill(), "symbol": TARGET, "side": side}]],
    )
    client = adapter(venue)
    if closing:
        checks = []
        with reduction_fence(item, lambda request: checks.append(request) is None):
            assert client.submit(item).status == "ACKNOWLEDGED"
        assert checks[0]["symbol"] == TARGET
    else:
        assert client.submit(item).status == "ACKNOWLEDGED"
    result = client.query(item)
    assert result.status == "FILLED" and result.evidence["fills_complete"] is True
    assert venue.calls[1][2]["symbol"] == TARGET
    assert venue.calls[1][2]["reduceOnly"] == ("true" if closing else "false")
    assert result.fills[0]["fee"] == "0.01"


def test_wrong_cjk_receipt_identity_is_rejected_before_fills():
    venue = Transport(raw={**response(), "symbol": "牛莱USDT"})
    with pytest.raises(ValueError, match="identity mismatch"):
        adapter(venue).query({**order(), "symbol": TARGET})
    assert len(venue.calls) == 1


@pytest.mark.parametrize(
    "method,path,params,flag",
    [
        ("POST", "/fapi/v1/leverage", {"leverage": 2}, "enable_testnet_settings"),
        (
            "POST",
            "/fapi/v1/marginType",
            {"marginType": "ISOLATED"},
            "enable_testnet_settings",
        ),
        (
            "DELETE",
            "/fapi/v1/order",
            {"origClientOrderId": "v2" + "a" * 32},
            "enable_testnet_order_cancellation",
        ),
    ],
)
def test_signed_settings_and_cancel_encode_same_unicode_bytes(
    method, path, params, flag
):
    conn = Connection()
    request = {"symbol": TARGET, **params}
    transport(conn, **{flag: True})(method, path, request)
    args, kwargs = conn.calls[0]
    signed = kwargs["body"] if method == "POST" else args[1].split("?", 1)[1]
    payload, signature = signed.rsplit("&signature=", 1)
    assert signed.isascii() and parse_qs(payload)["symbol"] == [TARGET]
    assert (
        signature
        == hmac.new(b"dummy-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    assert request["symbol"] == TARGET
    denied = Connection()
    with pytest.raises(ValueError):
        transport(denied, **{flag: True})(
            method, path, {**request, "symbol": "牛/来USDT"}
        )
    assert denied.calls == []


class MemoryState:
    def __init__(self):
        self.rows = {}

    def read(self, key):
        return self.rows.get(key.identity)

    def change(self, key, *, expected_version, payload, **_):
        old = self.read(key)
        assert (old.version if old else 0) == expected_version
        self.rows[key.identity] = StateSnapshot(
            expected_version + 1, json.dumps(payload), False
        )
        return SimpleNamespace(code="APPLIED")


def test_protection_submits_cjk_once_and_checks_venue_receipt():
    calls, row = [], {}

    def venue(method, path, params):
        calls.append((method, path, params))
        if method == "POST":
            row.update(
                {
                    **params,
                    "orderType": params["type"],
                    "closePosition": True,
                    "priceProtect": False,
                    "algoId": 42,
                    "algoStatus": "NEW",
                }
            )
        return dict(row)

    venue.account_id, venue.environment = "qa", "SANDBOX"
    worker = TestnetProtection(MemoryState(), venue, scope=SCOPE)
    spec = ProtectionSpec("episode-1", TARGET, "SELL", "STOP_MARKET", "99")
    assert worker.submit_once(spec)["status"] == "NEW"
    assert worker.submit_once(spec)["status"] == "NEW"
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert calls[0][2]["symbol"] == TARGET
    conn = Connection()
    transport(conn, enable_testnet_protection=True)(
        "POST", "/fapi/v1/algoOrder", worker.params(spec)
    )
    assert parse_qs(conn.calls[0][1]["body"])["symbol"] == [TARGET]
    rejected = Connection()
    with pytest.raises(ExchangeTransportError, match="PROTECTION_CLOSE_ALL_REQUIRED"):
        transport(rejected, enable_testnet_protection=True)(
            "POST", "/fapi/v1/algoOrder", {**worker.params(spec), "symbol": "牛/来USDT"}
        )
    assert rejected.calls == []
    row["symbol"] = "牛莱USDT"
    with pytest.raises(ValueError, match="identity mismatch"):
        worker.query(spec)


def test_cleanup_and_health_projection_keep_cjk_positions_visible():
    venue = SimpleNamespace(account_id="qa", environment="SANDBOX")
    worker = ScopedCleanup(
        lambda: pytest.fail("unexpected DB"),
        venue,
        scope=SCOPE,
        case_id=uuid4(),
        symbol=TARGET,
        approved_algos=[42],
        runner_stopped=lambda: True,
        pause=lambda _: None,
    )
    assert worker.symbol == TARGET
    detail = health_notification_details(
        "POSITION_SAFETY_BLOCKED",
        {
            "status": "CURRENT",
            "observed_at_ms": 100,
            "episodes": [
                {"symbol": TARGET, "status": "BLOCKED", "remaining_quantity": "1"},
                {"symbol": "牛/来USDT", "status": "BLOCKED"},
            ],
        },
    )
    assert detail["episodes"] == [
        {"symbol": TARGET, "status": "BLOCKED", "remaining_quantity": "1"}
    ]
