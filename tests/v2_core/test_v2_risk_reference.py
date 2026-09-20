from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from market_fakes import ArchiveClient, candle_batch
from test_v2_account_risk import POLICY, SCOPE, now, prepare, runner
from test_v2_intent_admission import database as database_fixture

from services.v2_risk_reference import (
    SOURCE,
    ArchivedRiskReference,
    ReferenceUnavailable,
    create_guarded_runtime,
)
from services.v2_s3_candles import S3CandleRunner
from services.v2_s3_runtime import S3Runtime
from services.v2_s3_source import DurableCandleSource
from v2_core.account_risk import AccountRisk, RiskProvenance, RiskReference
from v2_core.chunked_archive import ClickHouseChunkedArchive
from v2_core.producer import ProducerPublisher
from v2_core.runner import ExchangeObservation
from v2_core.scoping import ScopeMismatch

database = database_fixture


def request(**changes):
    return {**asdict(SCOPE), "symbol": "BTCUSDT", "leg": "OPEN", **changes}


def published(database, *, confirm=True, precise=False):
    client = ArchiveClient()
    archive = ClickHouseChunkedArchive(client)
    publisher = ProducerPublisher(
        database,
        source="s3",
        environment="SANDBOX",
        market=SimpleNamespace(put=lambda _: True),
        clock_ms=now,
        max_age_ms=90000,
        lifetime_ms=120000,
    )
    source = DurableCandleSource(publisher, archive=archive)
    batch = candle_batch(now() // 60000 * 60000 - 86400000)
    if precise:
        batch["candles"]["BTCUSDT"][0]["c"] = "101.123456789123456789"
    source.enqueue(batch)
    if confirm:
        assert (
            S3CandleRunner(S3Runtime(publisher), source=source).run_once()["status"]
            == "ACKNOWLEDGED"
        )
    return source, client, batch


def provider(database, archive, **changes):
    config = {
        "scope": SCOPE,
        "archive": archive,
        "clock_ms": now,
        "max_age_ms": 90000,
        **changes,
    }
    return ArchivedRiskReference(database, **config)


def test_reference_preserves_exact_close_and_receipt_provenance(database):
    source, _, batch = published(database, precise=True)
    reference = provider(database, source.archive)(request())
    assert reference.price == "101.123456789123456789"
    assert reference.observed_at_ms == batch["closed_at"]
    assert reference.source == SOURCE
    assert reference.provenance.frame_id == f"s3-closed-1m-v1:{batch['closed_at']}"
    assert len(reference.provenance.archive_digest) == 64
    assert reference.valid_until_ms == batch["closed_at"] + 90000


def test_missing_receipt_does_not_read_archive(database):
    with pytest.raises(ReferenceUnavailable, match="MISSING"):
        provider(
            database,
            SimpleNamespace(get=lambda _: pytest.fail("unconfirmed archive read")),
        )(request())


def test_pending_source_is_not_a_risk_reference(database):
    source, _, _ = published(database, confirm=False)
    with pytest.raises(ReferenceUnavailable, match="NOT_CONFIRMED"):
        provider(database, source.archive)(request())


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "other"},
        {"environment": "LIVE"},
        {"exchange": "OTHER"},
        {"product": "SPOT"},
        {"leg": "CLOSE"},
    ],
)
def test_request_binding_before_io(change):
    def forbidden():
        pytest.fail("wrong scope accessed storage")

    with pytest.raises(ReferenceUnavailable, match="REQUEST_SCOPE"):
        provider(forbidden, SimpleNamespace(get=lambda _: None))(request(**change))


def test_missing_symbol_never_uses_another_symbols_price(database):
    source, _, _ = published(database)
    with pytest.raises(ReferenceUnavailable, match="INPUT_MISMATCH"):
        provider(database, source.archive)(request(symbol="ETHUSDT"))


def test_missing_archive_cannot_fall_back_to_request_price(database):
    source, client, _ = published(database)
    client.tables["v2_candle_archive"].clear()
    with pytest.raises(ReferenceUnavailable, match="ARCHIVE_UNAVAILABLE"):
        provider(database, source.archive)(request(price="1", risk_reference="fake"))


def test_wrong_archive_digest_is_rejected(database):
    published(database)
    with pytest.raises(ReferenceUnavailable, match="ARCHIVE_UNAVAILABLE"):
        provider(database, SimpleNamespace(get=lambda _: "{}"))(request())


@pytest.mark.parametrize("offset", [-1, 90000])
def test_future_or_expired_reference_fails_before_archive_read(database, offset):
    _, _, batch = published(database)
    with pytest.raises(ReferenceUnavailable, match="STALE"):
        provider(
            database,
            SimpleNamespace(get=lambda _: pytest.fail("stale archive read")),
            clock_ms=lambda: batch["closed_at"] + offset,
        )(request())


def test_expiry_during_archive_read_is_not_refreshed(database):
    source, _, batch = published(database)
    clock = iter([batch["closed_at"] + 1, batch["closed_at"] + 90000])
    with pytest.raises(ReferenceUnavailable, match="STALE"):
        provider(database, source.archive, clock_ms=lambda: next(clock))(request())


def test_clock_rollback_during_read_fails_closed(database):
    source, _, batch = published(database)
    clock = iter([batch["closed_at"] + 2, batch["closed_at"] + 1])
    with pytest.raises(ReferenceUnavailable, match="STALE"):
        provider(database, source.archive, clock_ms=lambda: next(clock))(request())


def test_new_head_during_archive_read_requires_retry(database, monkeypatch):
    source, _, _ = published(database)
    reader = provider(database, source.archive)
    head = reader._head()
    heads = iter([head, None])
    monkeypatch.setattr(reader, "_head", lambda: next(heads))
    with pytest.raises(ReferenceUnavailable, match="SUPERSEDED"):
        reader(request())


def test_newer_unconfirmed_frame_cannot_fall_back_to_older_confirmed_one(database):
    source, _, batch = published(database)
    # Move only the injected publisher clock; do not change the system/PG clock.
    source.publisher.clock_ms = lambda: batch["closed_at"] + 60001
    source.enqueue(candle_batch(batch["closed_at"] + 60000 - 86400000))
    with pytest.raises(ReferenceUnavailable, match="NOT_CONFIRMED"):
        provider(database, source.archive)(request())


def factory(database, source, *, enabled=False, submit=None, query=None, scope=SCOPE):
    return create_guarded_runtime(
        database,
        scope=scope,
        archive=source.archive,
        clock_ms=now,
        max_reference_age_ms=90000,
        submit=submit or (lambda _: pytest.fail("write forbidden")),
        query=query or (lambda _: None),
        risk_check=lambda _: True,
        enabled=enabled,
    )


def configure(database):
    risk = AccountRisk(database)
    risk.configure(
        SCOPE,
        replace(POLICY, reference_source=SOURCE, max_reference_age_ms=120000),
        expected_version=0,
    )
    return risk


def test_guarded_factory_default_write_gate_releases_known_unsent_reservation(database):
    risk = configure(database)
    source, _, _ = published(database)
    original, data, order, _ = prepare(database)
    assert factory(database, source).execution.dispatch(order) == "REJECTED"
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["account_risk"]["reference"]["provenance"][
        "archive_digest"
    ]


def test_archive_to_account_budget_to_submission_to_trace(database):
    configure(database)
    source, _, _ = published(database, precise=True)
    original, data, order, _ = prepare(database)
    sent = []

    def submit(req):
        trace = data.trace(original.intent_id)
        assert trace["orders"][0]["status"] == "SUBMITTING"
        assert trace["account_risk"]["reference"]["price"] == "101.123456789123456789"
        sent.append(req["client_order_id"])
        return ExchangeObservation(
            req["client_order_id"], "ACKNOWLEDGED", evidence={"source": "qa"}
        )

    runtime = factory(database, source, enabled=True, submit=submit)
    assert runtime.execution.dispatch(order) == "ACKNOWLEDGED"
    assert len(sent) == 1
    assert (
        data.trace(original.intent_id)["account_risk"]["reference"]["source"] == SOURCE
    )


def test_corrupt_archive_prevents_reservation_and_send(database):
    risk = configure(database)
    source, client, _ = published(database)
    original, data, order, _ = prepare(database)
    client.tables["v2_candle_archive"].clear()
    with pytest.raises(ReferenceUnavailable):
        factory(database, source, enabled=True).execution.dispatch(order)
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["orders"][0]["status"] == "PREPARED"


def test_factory_prevents_cross_account_submit_and_recovery(database):
    source, _, _ = published(database)
    _, data, order, _ = prepare(database, account="other")
    with pytest.raises(ScopeMismatch):
        factory(database, source, enabled=True).execution.dispatch(order)
    # Separate compatibility writer creates a foreign recoverable order.
    _, _, foreign, _ = prepare(database, account="other", symbol="ETHUSDT")
    data.orders.transition(
        foreign, expected_version=1, status="SUBMITTING", evidence={}
    )
    with pytest.raises(ScopeMismatch):
        factory(
            database, source, query=lambda _: pytest.fail("foreign query")
        ).execution.recover(foreign)


def test_provider_deadline_is_rechecked_by_pg_even_with_looser_policy(database):
    configure(database)
    _, _, order, _ = prepare(database)
    old = now() - 1000
    quote = RiskReference(
        "SANDBOX", "BTCUSDT", SOURCE, "101", old, valid_until_ms=old + 1
    )
    assert runner(database, ref=lambda _: quote).dispatch(order) == "DENIED"


@pytest.mark.parametrize(
    "bad", [{"valid_until_ms": 1}, {"valid_until_ms": True}, {"provenance": {}}]
)
def test_reference_proof_and_deadline_validation(bad):
    with pytest.raises((ValueError, TypeError)):
        RiskReference("SANDBOX", "BTCUSDT", SOURCE, "100", 100, **bad)


def test_provenance_requires_complete_digest_identity():
    with pytest.raises(ValueError):
        RiskProvenance("frame", "bad", "a" * 64)


@pytest.mark.parametrize(
    "index,value", [(6, "QUARANTINED"), (7, None), (7, "0" * 64), (8, False)]
)
def test_receipt_publication_and_ack_must_all_agree(
    database, monkeypatch, index, value
):
    source, _, _ = published(database)
    reader = provider(database, source.archive)
    head = list(reader._head())
    head[index] = value
    monkeypatch.setattr(reader, "_head", lambda: tuple(head))
    with pytest.raises(ReferenceUnavailable, match="NOT_CONFIRMED"):
        reader(request())


def test_rebuilt_frame_must_match_pg_input_digest(database, monkeypatch):
    source, _, _ = published(database)
    reader = provider(database, source.archive)
    head = list(reader._head())
    head[5] = head[7] = "0" * 64
    monkeypatch.setattr(reader, "_head", lambda: tuple(head))
    with pytest.raises(ReferenceUnavailable, match="INPUT_MISMATCH"):
        reader(request())


def test_archive_timeout_does_not_create_a_reservation(database):
    risk = configure(database)
    source, _, _ = published(database)
    original, data, order, _ = prepare(database)

    def unavailable(_):
        raise TimeoutError("archive timeout")

    source.archive = SimpleNamespace(get=unavailable)
    with pytest.raises(TimeoutError):
        factory(database, source, enabled=True).execution.dispatch(order)
    assert risk.usage(SCOPE)["positions"] == 0
    assert data.trace(original.intent_id)["orders"][0]["status"] == "PREPARED"


def test_guarded_factory_close_does_not_require_available_archive(database):
    configure(database)
    source, _, _ = published(database)
    original, data, opening, _ = prepare(database)

    def submit(req):
        return ExchangeObservation(
            req["client_order_id"], "ACKNOWLEDGED", evidence={"source": "qa"}
        )

    runtime = factory(database, source, enabled=True, submit=submit)
    assert runtime.execution.dispatch(opening) == "ACKNOWLEDGED"
    data.ledger.record_fill(
        order_id=opening,
        exchange_fill_id="open-fill",
        quantity="0.01",
        price="101",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=now(),
        evidence={"source": "qa"},
    )
    data.orders.transition(
        opening, expected_version=3, status="FILLED", evidence={"source": "qa"}
    )
    closing, _ = data.orders.prepare(original.intent_id, leg="CLOSE")
    source.archive = SimpleNamespace(
        get=lambda _: pytest.fail("close must not read archive")
    )
    assert (
        factory(database, source, enabled=True, submit=submit).execution.dispatch(
            closing
        )
        == "ACKNOWLEDGED"
    )


@pytest.mark.parametrize("value", [0, True, 3600001])
def test_provider_policy_bounds_are_validated_before_io(value):
    with pytest.raises(ValueError):
        provider(
            lambda: pytest.fail("construction opened DB"),
            SimpleNamespace(get=lambda _: None),
            max_age_ms=value,
        )
