"""Shared V2 test database, builders and deterministic scenario fakes.

Importing this module performs no database, venue or credential-file access.
Only the explicit database fixture can create a schema, and it retains the
isolated Unix-socket DSN guard. No test_* functions belong in this support module.
"""

import os
import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from v2_core.evidence import DecisionEvidence, EvidenceStore
from v2_core.intents import AdmissionCode as Code
from v2_core.intents import IntentStore, OpenIntent


def evidence():
    return DecisionEvidence(
        "strategy-v1",
        '{"size":"0.01"}',
        '{"observed_at":1,"decided_at":2,"source":"test",'
        '"symbol":"BTCUSDT","rationale":"momentum","features":{}}',
    )


def intent():
    return OpenIntent(
        str(uuid4()),
        "BINANCE",
        "test-account",
        "SANDBOX",
        "FUTURES",
        "s6",
        str(uuid4()),
        "BTCUSDT",
        "BUY",
        "0.0100",
        "strategy-v1",
        evidence().config_digest,
        evidence().evidence_ref,
    )


@pytest.fixture
def database():
    dsn = os.environ.get("V2_CORE_TEST_DSN")
    if not dsn or os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires explicit isolated QA database")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    parameters = conninfo_to_dict(dsn)
    host = parameters.get("host", "")
    if (
        not re.fullmatch(r"/tmp/(?:codex-)?v2-data-qa\.[A-Za-z0-9]+/socket", host)
        or str(Path(host).resolve()) != host
        or any(parameters.get(key) for key in ("hostaddr", "service"))
        or any(
            os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
        )
    ):
        pytest.fail("V2 database QA requires a dedicated temporary Unix socket")

    name = "v2_core_test_" + uuid4().hex
    ddl = (
        Path(__file__).resolve().parents[2] / "db/postgres_v2_core_schema.sql"
    ).read_text()
    with psycopg.connect(dsn) as conn:
        if conn.execute("SHOW cluster_name").fetchone() != ("v2_isolated_qa",):
            pytest.fail("V2 database QA requires the isolated QA cluster identity")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
        conn.execute(ddl)

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
            yield conn

    try:
        EvidenceStore(connect).put(evidence())
        yield connect
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def counts(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM v2_trade_intents), "
            "(SELECT count(*) FROM v2_domain_outbox)"
        ).fetchone()


def opened(database):
    from v2_core.orders import Orders

    original = intent()
    assert IntentStore(database).admit(original).code is Code.ACCEPTED
    orders = Orders(database)
    order_id, client_id = orders.prepare(original.intent_id)
    return original, orders, order_id, client_id


def record(ledger, order_id, fill_id, price, *, fee="0.001", currency="USDT"):
    return ledger.record_fill(
        order_id=order_id,
        exchange_fill_id=fill_id,
        quantity="0.01",
        price=price,
        fee=fee,
        fee_currency=currency,
        occurred_at_ms=100,
        evidence={"source": "exchange-query"},
    )


def income_fact(**changes):
    return {
        "income_type": "FUNDING_FEE",
        "source_id": "123",
        "symbol": "BTCUSDT",
        "amount_text": "-0.003",
        "currency": "USDT",
        "occurred_at_ms": 150,
        "evidence": {"source": "binance-income", "trade_id": ""},
        **changes,
    }


def income_scope(account="test-account"):
    from v2_core.income import IncomeScope

    return IncomeScope("BINANCE", account, "SANDBOX", "FUTURES")


def closed_for_income(database):
    from v2_core.ledger import Ledger

    original, orders, opening, _client = opened(database)
    ledger = Ledger(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    record(ledger, opening, "entry", "100", fee="0")
    orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "test"}
    )
    closing, _ = orders.prepare(original.intent_id, leg="CLOSE")
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id="exit",
        quantity="0.01",
        price="110",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=200,
        evidence={"source": "test"},
    )
    orders.transition(
        closing, expected_version=2, status="FILLED", evidence={"source": "test"}
    )
    return original


def income_row(identity=123, **changes):
    return {
        "tranId": identity,
        "incomeType": "FUNDING_FEE",
        "symbol": "BTCUSDT",
        "income": "-0.003",
        "asset": "USDT",
        "time": 150,
        "tradeId": "",
        **changes,
    }


def strategy_worker(database, *, decide=None, now=None, producer="s6", config=None):
    from v2_core.runtime import DataRuntime
    from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker

    runtime = DataRuntime(
        database,
        submit=lambda _: pytest.fail("strategy admission must never submit"),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=now or (lambda: 3),
    )
    worker = StrategyWorker(
        runtime,
        StrategyScope("BINANCE", "test-account", "SANDBOX", "FUTURES", producer),
        source="tv_bridge",
        strategy_version="rule-v1",
        config={"size": "0.01"} if config is None else config,
        decide=decide
        or (
            lambda signal, context, config: StrategyDecision(
                "OPEN", "momentum", "BUY", config["size"]
            )
        ),
        max_delay_ms=5,
    )
    return worker, runtime


def scheduler_signal(database, key, symbol="ETHUSDT", *, observed=1, expires=10):
    from v2_core.signals import Signals

    return Signals(database).admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key=key,
        snapshot={
            "observed_at": observed,
            "expires_at_ms": expires,
            "symbol": symbol,
            "signal": "TREND_UP",
            "features": {},
        },
    )


def scheduler_due(database):
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET next_attempt_at=clock_timestamp()-interval '1 second'"
        )


def pg_webhook(database, *, now=lambda: 120):
    from v2_core.ingress import SignalIngress
    from v2_core.signals import Signals
    from v2_core.webhook import TradingViewWebhook

    return TradingViewWebhook(
        SignalIngress(
            Signals(database),
            source="tv_bridge",
            environment="SANDBOX",
            clock_ms=now,
            max_age_ms=100,
            max_lifetime_ms=100,
        ),
        secret="qa-webhook-secret-never-production",
    )


def pg_webhook_call(app, **changes):
    import json
    from io import BytesIO

    payload = {
        "secret": "qa-webhook-secret-never-production",
        "event_id": "tv:test:bar1",
        "observed_at": 100,
        "expires_at_ms": 200,
        "symbol": "BINANCE:BTCUSDT.P",
        "signal": "TREND_UP_LONG",
        "strength": 70,
        "price": "100",
        **changes,
    }
    body = json.dumps(payload).encode()
    response = []
    output = b"".join(
        app(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/v2/webhooks/tradingview",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": BytesIO(body),
            },
            lambda status, headers: response.append(status),
        )
    )
    return int(response[0].split()[0]), json.loads(output)


def lifecycle_frame(time=100, **changes):
    return {
        "frame_id": f"s3:{time}",
        "observed_at": time,
        "contexts": {"BTCUSDT": {"strength": 60}},
        "raw_windows": {},
        **changes,
    }


def lifecycle_processor(database, *, now=None, evaluate=None):
    from v2_core.lifecycle import S3FrameProcessor

    publisher, cache = producer(database, now=now)

    def detect(contexts, raw, previous, observed):
        return (
            [
                {"symbol": target, "type": "TREND_UP", "strength": features["strength"]}
                for target, features in contexts.items()
                if "strength" in features
            ],
            previous,
        )

    return S3FrameProcessor(
        publisher,
        evaluate=evaluate or detect,
        detector_version="test-v1",
        cooldown_ms=30,
        absence_ms=300,
    ), cache


def closed_candle_batch():
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": 86400000,
        "candles": {
            "BTCUSDT": [
                {
                    "t": 86400000 - (i + 1) * 60000,
                    "o": "100",
                    "h": "102",
                    "l": "99",
                    "c": "101",
                    "v": "10",
                    "tbv": "6",
                }
                for i in range(1440)
            ]
        },
    }


def durable_source(database, *, archive=None, now=None):
    from services.v2_s3_source import DurableCandleSource

    class Archive:
        def __init__(self):
            self.rows = {}

        def put(self, key, encoded):
            self.rows[key] = encoded
            return True

        def get(self, key):
            return self.rows.get(key)

    publisher, cache = producer(database, now=now or (lambda: 86400001))
    source = DurableCandleSource(publisher, archive=archive or Archive())
    return source, cache


def expire_source_lease(database):
    with database() as conn:
        conn.execute(
            "UPDATE v2_candle_deliveries SET lease_until=clock_timestamp()-interval '1 second' WHERE status='PENDING' AND lease_token IS NOT NULL"
        )


def producer_frame(**changes):
    return {
        "frame_id": "s3:frame:100",
        "observed_at": 100,
        "contexts": {"BTCUSDT": {"price": "100"}, "ETHUSDT": {"price": "200"}},
        "events": [
            {
                "event_id": "s3:btc:100",
                "symbol": "BTCUSDT",
                "signal": "TREND_UP",
                "features": {"strength": 70},
            },
            {
                "event_id": "s3:eth:100",
                "symbol": "ETHUSDT",
                "signal": "TREND_UP",
                "features": {"strength": 80},
            },
        ],
        **changes,
    }


def producer(database, *, market=None, source="s3", now=None):
    from v2_core.producer import ProducerPublisher

    class Cache:
        def __init__(self):
            self.records = []

        def put(self, envelope):
            self.records.append(envelope)
            return True

    market = Cache() if market is None else market
    return ProducerPublisher(
        database,
        source=source,
        environment="SANDBOX",
        market=market,
        clock_ms=now or (lambda: 120),
        max_age_ms=100,
        lifetime_ms=100,
    ), market


def signal_request(database):
    import json

    from v2_core.evidence import canonical
    from v2_core.service import TradingData

    service = TradingData(database)
    args = {
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "request_key": "alert-1",
        "snapshot": {
            "observed_at": 1,
            "expires_at_ms": 10,
            "symbol": "BTCUSDT",
            "signal": "TREND_UP",
            "features": {"price": "100"},
        },
    }
    signal_id = service.signals.admit(**args)
    assert service.signals.admit(**args) == signal_id
    proof = DecisionEvidence(
        "strategy-v1",
        evidence().config_json,
        canonical(
            dict(
                json.loads(evidence().snapshot_json),
                signal_id=signal_id,
                expires_at_ms=10,
            )
        ),
    )
    request = replace(
        intent(),
        request_key="signal:" + signal_id,
        evidence_ref=proof.evidence_ref,
        config_digest=proof.config_digest,
    )
    return service, request, proof, signal_id
