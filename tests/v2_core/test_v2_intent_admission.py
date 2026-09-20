import os
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.parametrize(
    "quantity", [0.1, "NaN", "Infinity", "-1", "0", "1e20", "1e-19", "bad"]
)
def test_reject_invalid_quantity(quantity):
    with pytest.raises(ValueError):
        replace(intent(), quantity=quantity)


def test_quantity_is_canonical_and_precision_preserved():
    assert intent().quantity == "0.01"
    assert replace(intent(), quantity="1e-18").quantity == "0.000000000000000001"
    assert replace(intent(), quantity="10").quantity == "10"


@pytest.fixture
def database():
    dsn = os.environ.get("V2_CORE_TEST_DSN")
    if not dsn or os.environ.get("V2_CORE_TEST_ISOLATED") != "YES":
        pytest.skip("requires explicit isolated QA database")
    import psycopg
    from psycopg import sql

    name = "v2_core_test_" + uuid4().hex
    ddl = (
        Path(__file__).resolve().parents[2] / "db/postgres_v2_core_schema.sql"
    ).read_text()
    with psycopg.connect(dsn) as conn:
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


def test_different_uuid_same_request_returns_original(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    retry = store.admit(replace(original, intent_id=str(uuid4()), quantity="1e-2"))
    assert retry.code is Code.ALREADY_ACCEPTED
    assert retry.intent_id == original.intent_id
    assert counts(database) == (1, 1)


def test_concurrent_request_has_single_intent_and_event(database):
    store = IntentStore(database)
    original = intent()
    requests = [replace(original, intent_id=str(uuid4())) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(store.admit, requests))
    assert sum(r.code is Code.ACCEPTED for r in results) == 1
    assert sum(r.code is Code.ALREADY_ACCEPTED for r in results) == 7
    assert len({r.intent_id for r in results}) == 1
    assert counts(database) == (1, 1)


def test_payload_conflict_and_account_isolation(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    conflict = replace(original, intent_id=str(uuid4()), quantity="2")
    assert store.admit(conflict).code is Code.CONFLICT
    separate = replace(original, intent_id=str(uuid4()), account_id="other")
    assert store.admit(separate).code is Code.ACCEPTED
    assert counts(database) == (2, 2)


def test_uuid_collision_cannot_create_second_request(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    assert store.admit(replace(original, request_key="other")).code is Code.CONFLICT
    assert counts(database) == (1, 1)


def test_outbox_failure_rolls_back_intent(database):
    with database() as conn:
        conn.execute(
            "ALTER TABLE v2_domain_outbox ADD CONSTRAINT injected_failure "
            "CHECK (event_type <> 'INTENT_ACCEPTED')"
        )
    assert IntentStore(database).admit(intent()).code is Code.UNKNOWN
    assert counts(database) == (0, 0)


def test_commit_ack_loss_is_resolved_by_same_request(database):
    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        raise OSError("commit acknowledgement lost after commit")

    original = intent()
    assert IntentStore(lost_ack).admit(original).code is Code.UNKNOWN
    assert counts(database) == (1, 1)
    result = IntentStore(database).admit(replace(original, intent_id=str(uuid4())))
    assert result.code is Code.ALREADY_ACCEPTED
    assert result.intent_id == original.intent_id


def test_database_unavailable_is_unknown():
    def unavailable():
        raise OSError("database unavailable")

    assert IntentStore(unavailable).admit(intent()).code is Code.UNKNOWN


def test_missing_or_mismatched_evidence_rejected(database):
    store = IntentStore(database)
    assert store.admit(replace(intent(), evidence_ref="missing")).code is Code.CONFLICT
    assert store.admit(replace(intent(), config_digest="b" * 64)).code is Code.CONFLICT
    assert store.admit(replace(intent(), symbol="ETHUSDT")).code is Code.CONFLICT
    assert counts(database) == (0, 0)


def test_evidence_is_immutable(database):
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException), database() as conn:
        conn.execute("UPDATE v2_decision_evidence SET config='{}'")


def test_evidence_digest_changes_with_configuration():
    changed = replace(evidence(), config_json='{"size":"0.02"}')
    assert changed.config_digest != evidence().config_digest
    assert changed.evidence_ref != evidence().evidence_ref


def opened(database):
    from v2_core.orders import Orders

    original = intent()
    assert IntentStore(database).admit(original).code is Code.ACCEPTED
    orders = Orders(database)
    order_id, client_id = orders.prepare(original.intent_id)
    return original, orders, order_id, client_id


def test_prepare_and_submit_have_one_concurrent_winner(database):
    original, orders, order_id, client_id = opened(database)
    assert orders.prepare(original.intent_id) == (order_id, client_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: orders.transition(
                    order_id, expected_version=1, status="SUBMITTING", evidence={}
                ),
                range(4),
            )
        )
    assert results.count(True) == 1
    assert results.count(False) == 3
    assert orders.recovery_candidates()[0][1] == client_id
    assert orders.transition(
        order_id, expected_version=2, status="UNKNOWN", evidence={}
    )
    with pytest.raises(ValueError, match="illegal"):
        orders.transition(
            order_id, expected_version=3, status="SUBMITTING", evidence={}
        )


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


def test_fill_dedup_pnl_and_cash_are_exact(database):
    from decimal import Decimal

    from v2_core.ledger import Ledger

    original, orders, opening, _ = opened(database)
    ledger = Ledger(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    assert record(ledger, opening, "fill-open", "100")
    assert not record(ledger, opening, "fill-open", "100")
    with pytest.raises(ValueError, match="conflict"):
        record(ledger, opening, "fill-open", "101")
    assert orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    closing, _ = orders.prepare(original.intent_id, leg="CLOSE")
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    record(ledger, closing, "fill-close", "110")
    assert orders.transition(
        closing, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    adjustment = {
        "episode_id": original.intent_id,
        "source_id": "income-1",
        "amount_text": "-0.003",
        "currency": "USDT",
        "kind": "FUNDING",
        "occurred_at_ms": 100,
        "evidence": {},
    }
    assert ledger.adjustment(**adjustment)
    assert not ledger.adjustment(**adjustment)
    report = ledger.report(original.intent_id, settlement_currency="USDT")
    assert Decimal(report["gross_pnl"]) == Decimal("0.1")
    assert Decimal(report["net_pnl"]) == Decimal("0.095")
    assert report["decision"]["rationale"] == "momentum"
    proof = {
        "exchange_flat": True,
        "orders_terminal": True,
        "fills_complete": True,
        "cash_complete": True,
        "observed_at_ms": 200,
        "source": "test-exchange-query",
        "ledger_revision": report["accounting_revision"],
    }
    assert ledger.settle(original.intent_id, currency="USDT", evidence=proof)
    assert not ledger.settle(original.intent_id, currency="USDT", evidence=proof)
    assert (
        ledger.report(original.intent_id, settlement_currency="USDT")[
            "accounting_status"
        ]
        == "SETTLED"
    )
    assert not record(ledger, opening, "fill-open", "100")
    assert ledger.adjustment(**{**adjustment, "source_id": "late-income"})
    revised = ledger.report(original.intent_id, settlement_currency="USDT")
    assert revised["accounting_status"] == "CALCULATED"
    with pytest.raises(ValueError, match="stale ledger revision"):
        ledger.settle(original.intent_id, currency="USDT", evidence=proof)
    assert ledger.settle(
        original.intent_id,
        currency="USDT",
        evidence={**proof, "ledger_revision": revised["accounting_revision"]},
    )
    # Settled slot can accept a new episode without reusing historical identity.
    new = replace(original, intent_id=str(uuid4()), request_key="new-request")
    assert IntentStore(database).admit(new).code is Code.ACCEPTED
    orders.prepare(new.intent_id)


def test_incomplete_reconciliation_cannot_settle(database):
    from v2_core.ledger import Ledger

    original, _orders, _opening, _client = opened(database)
    with pytest.raises(ValueError, match="complete reconciliation"):
        Ledger(database).settle(original.intent_id, currency="USDT", evidence={})


def test_unconverted_fee_does_not_claim_complete_pnl(database):
    from v2_core.ledger import Ledger

    original, orders, opening, _ = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger = Ledger(database)
    record(ledger, opening, "open", "100", currency="BNB")
    report = ledger.report(original.intent_id, settlement_currency="USDT")
    assert report["net_pnl"] is None
    assert report["accounting_status"] == "PENDING"


def test_outbox_replays_when_sink_fails_before_receipt(database):
    from v2_core.delivery import Projector

    original = intent()
    IntentStore(database).admit(original)
    events = {}

    def sink(event_id, intent_id, kind, payload):
        events[event_id] = (intent_id, kind, payload)
        return True

    def failed(*args):
        sink(*args)
        raise OSError("delivery reply lost")

    with pytest.raises(OSError):
        Projector(database, "test-sink", failed).run_batch()
    assert len(events) == 1
    assert Projector(database, "test-sink", sink).run_batch() == 1
    assert Projector(database, "test-sink", sink).run_batch() == 0
    assert len(events) == 1


def test_service_trace_preserves_decision_and_execution_history(database):
    from v2_core.service import TradingData

    service = TradingData(database)
    original = intent()
    assert service.accept(original, evidence()).code is Code.ACCEPTED
    opening, _ = service.orders.prepare(original.intent_id)
    service.orders.transition(
        opening, expected_version=1, status="SUBMITTING", evidence={}
    )
    service.orders.transition(
        opening,
        expected_version=2,
        status="UNKNOWN",
        evidence={"reason": "network timeout"},
    )
    trace = service.trace(original.intent_id)
    assert trace["status"] == "UNKNOWN"
    assert trace["configuration"] == {"size": "0.01"}
    assert [e["status"] for e in trace["timeline"]] == [
        "PREPARED",
        "SUBMITTING",
        "UNKNOWN",
    ]


def test_multiple_partial_close_orders_preserve_remaining_quantity(database):
    from v2_core.ledger import Ledger

    original, orders, opening, _ = opened(database)
    ledger = Ledger(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    record(ledger, opening, "open", "100")
    orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    first, _ = orders.prepare(
        original.intent_id, leg="CLOSE", quantity="0.004", request_key="tp1"
    )
    with pytest.raises(ValueError, match="exceeds"):
        orders.prepare(
            original.intent_id, leg="CLOSE", quantity="0.01", request_key="too-large"
        )
    orders.transition(first, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=first,
        exchange_fill_id="tp1",
        quantity="0.004",
        price="110",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=100,
        evidence={},
    )
    orders.transition(
        first, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    second, _ = orders.prepare(
        original.intent_id, leg="CLOSE", quantity="0.006", request_key="tp2"
    )
    assert second != first
    orders.transition(second, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=second,
        exchange_fill_id="tp2",
        quantity="0.006",
        price="90",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=100,
        evidence={},
    )
    from decimal import Decimal

    assert Decimal(
        ledger.report(original.intent_id, settlement_currency="USDT")["gross_pnl"]
    ) == Decimal("-0.02")


def test_concurrent_fill_replay_counts_once(database):
    from v2_core.ledger import Ledger

    _original, orders, opening, _ = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger = Ledger(database)
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(
            pool.map(lambda _: record(ledger, opening, "duplicate", "100"), range(6))
        )
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 5


def test_conflicting_active_episode_is_not_silently_shared(database):
    import psycopg

    original, orders, _opening, _ = opened(database)
    other = replace(original, intent_id=str(uuid4()), request_key="other")
    assert IntentStore(database).admit(other).code is Code.ACCEPTED
    with pytest.raises(psycopg.errors.UniqueViolation):
        orders.prepare(other.intent_id)


def test_cancelled_open_without_fills_releases_slot_and_records_result(database):
    original, orders, opening, _ = opened(database)
    orders.transition(
        opening, expected_version=1, status="CANCELLED", evidence={"reason": "expired"}
    )
    from v2_core.service import TradingData

    assert TradingData(database).trace(original.intent_id)["status"] == "CANCELLED"
    other = replace(original, intent_id=str(uuid4()), request_key="next")
    IntentStore(database).admit(other)
    orders.prepare(other.intent_id)


def test_unstarted_rejection_remains_traceable(database):
    from v2_core.service import TradingData

    service = TradingData(database)
    original = intent()
    service.accept(original, evidence())
    assert service.intents.terminate_unstarted(
        original.intent_id, status="REJECTED", reason="risk limit"
    )
    trace = service.trace(original.intent_id)
    assert trace["status"] == "REJECTED"
    assert trace["domain_events"][-1]["payload"]["reason"] == "risk limit"
    assert trace["orders"] == []
    with pytest.raises(ValueError, match="terminal"):
        service.orders.prepare(original.intent_id)


def test_dispatch_timeout_queries_same_identity_and_never_resubmits(database):
    from v2_core.runner import ExchangeObservation, ExecutionRunner

    _original, _orders, opening, client_id = opened(database)
    submitted = []

    def submit(request):
        submitted.append(request["client_order_id"])
        raise TimeoutError("exchange accepted but response lost")

    fill = {
        "exchange_fill_id": "recovered",
        "quantity": "0.01",
        "price": "100",
        "fee": "0",
        "fee_currency": "USDT",
        "occurred_at_ms": 100,
        "evidence": {"source": "query"},
    }

    def query(request):
        assert request["client_order_id"] == client_id
        return ExchangeObservation(
            client_id, "FILLED", "exchange-1", (fill,), {"source": "query"}
        )

    runner = ExecutionRunner(
        database, submit=submit, query=query, risk_check=lambda _: True
    )
    assert runner.dispatch(opening) == "UNKNOWN"
    assert runner.dispatch(opening) == "FILLED"
    assert runner.dispatch(opening) == "FILLED"
    assert submitted == [client_id]


def test_commit_ack_loss_does_not_send_an_order(database):
    from v2_core.runner import ExecutionRunner

    _original, _orders, opening, _client = opened(database)
    count = 0

    @contextmanager
    def lose_second_commit():
        nonlocal count
        count += 1
        current = count
        with database() as conn:
            yield conn
        if current == 2:
            raise OSError("dispatch state commit acknowledgement lost")

    calls = []
    runner = ExecutionRunner(
        lose_second_commit,
        submit=lambda request: calls.append(request),
        query=lambda _: None,
        risk_check=lambda _: True,
    )
    with pytest.raises(OSError):
        runner.dispatch(opening)
    assert calls == []
    assert runner.dispatch(opening) == "UNKNOWN"
    assert calls == []


def test_risk_rejection_is_durable_without_exchange_calls(database):
    from v2_core.runner import ExecutionRunner

    original, _orders, opening, _client = opened(database)
    calls = []
    runner = ExecutionRunner(
        database,
        submit=lambda r: calls.append(r),
        query=lambda _: None,
        risk_check=lambda _: False,
    )
    assert runner.dispatch(opening) == "DENIED"
    assert calls == []
    from v2_core.service import TradingData

    assert TradingData(database).trace(original.intent_id)["status"] == "CANCELLED"


def test_risk_port_cannot_mutate_submitted_identity(database):
    from v2_core.runner import ExchangeObservation, ExecutionRunner

    _original, _orders, opening, client_id = opened(database)
    sent = []

    def risk(request):
        request["client_order_id"] = "changed"
        request["intent"]["quantity"] = "999"
        return True

    def submit(request):
        sent.append(request)
        return ExchangeObservation(
            client_id, "ACKNOWLEDGED", "ex-1", evidence={"source": "ack"}
        )

    runner = ExecutionRunner(
        database, submit=submit, query=lambda _: None, risk_check=risk
    )
    assert runner.dispatch(opening) == "ACKNOWLEDGED"
    assert sent[0]["client_order_id"] == client_id
    assert sent[0]["intent"]["quantity"] == "0.01"


def test_observation_is_atomic_and_rejects_wrong_exchange_identity(database):
    from v2_core.runner import ExchangeObservation, ExecutionRunner
    from v2_core.service import TradingData

    original, orders, opening, client_id = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(
        opening,
        expected_version=2,
        status="ACKNOWLEDGED",
        evidence={"source": "ack"},
        exchange_order_id="ex-1",
    )
    fill = {
        "exchange_fill_id": "f-1",
        "quantity": "0.005",
        "price": "100",
        "fee": "0",
        "fee_currency": "USDT",
        "occurred_at_ms": 100,
        "evidence": {"source": "query"},
    }
    runner = ExecutionRunner(
        database, submit=lambda _: None, query=lambda _: None, risk_check=lambda _: True
    )
    with pytest.raises(ValueError, match="identity"):
        runner._apply(
            opening,
            ExchangeObservation(
                client_id, "ACKNOWLEDGED", "ex-2", (fill,), {"source": "query"}
            ),
        )
    assert TradingData(database).trace(original.intent_id)["fills"] == []
    with pytest.raises(ValueError, match="exceeds"):
        runner._apply(
            opening,
            ExchangeObservation(
                client_id,
                "FILLED",
                "ex-1",
                (fill, dict(fill, exchange_fill_id="f-2", quantity="0.01")),
                {"source": "query"},
            ),
        )
    assert TradingData(database).trace(original.intent_id)["fills"] == []
    assert runner.snapshot(opening)["status"] == "ACKNOWLEDGED"


def test_outbox_contains_replayable_accounting_facts(database):
    from v2_core.ledger import Ledger
    from v2_core.service import TradingData

    original, orders, opening, _client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    record(Ledger(database), opening, "replay-fill", "123.456")
    events = TradingData(database).trace(original.intent_id)["domain_events"]
    admitted = next(e["payload"] for e in events if e["kind"] == "INTENT_ACCEPTED")
    filled = next(e["payload"] for e in events if e["kind"].startswith("FILL:"))
    assert admitted["decision"]["rationale"]
    assert admitted["config"]
    assert filled["quantity"] == "0.01"
    assert filled["price"] == "123.456"
    assert filled["leg"] == "OPEN"
    assert filled["evidence"]["source"] == "exchange-query"


def test_cache_rebuild_uses_aggregate_revision_without_receipts(database):
    from v2_core.projections import RedisTraces
    from v2_core.service import TradingData

    original, orders, opening, _client = opened(database)
    snapshots = []

    class Projection:
        def put(self, identity, revision, payload):
            snapshots.append((identity, revision, payload))
            return True

    cache = RedisTraces(database, Projection())
    assert cache.rebuild_batch()["count"] == 1
    before = snapshots[-1][1]
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    cache(None, original.intent_id, None, None)
    assert snapshots[-1][1] > before
    assert snapshots[-1][2] == TradingData(database).trace(original.intent_id)
    assert cache.rebuild_batch(after=original.intent_id)["count"] == 0
    assert cache.rebuild_batch()["count"] == 1


def test_scheduled_projection_does_not_starve_healthy_events(database):
    from v2_core.delivery import Projector

    first, second = intent(), replace(intent(), request_key="second")
    IntentStore(database).admit(first)
    IntentStore(database).admit(second)
    delivered = []

    def sink(event_id, intent_id, _kind, _payload):
        if intent_id == first.intent_id:
            raise TimeoutError("sensitive transport detail")
        delivered.append(event_id)
        return True

    worker = Projector(database, "scheduled", sink)
    result = worker.run_scheduled_batch()
    assert result == {"claimed": 2, "delivered": 1, "failed": 1, "superseded": 0}
    assert worker.run_scheduled_batch()["claimed"] == 0

    with database() as conn:
        assert conn.execute(
            "SELECT error_code FROM v2_delivery_attempts WHERE event_id=%s",
            (first.intent_id,),
        ).fetchone() == ("TimeoutError",)
        conn.execute(
            "UPDATE v2_delivery_attempts SET next_attempt_at=clock_timestamp()"
        )
    assert (
        Projector(database, "scheduled", lambda *args: True).run_scheduled_batch()[
            "delivered"
        ]
        == 1
    )
    assert worker.run_scheduled_batch()["claimed"] == 0


def test_database_guards_request_and_order_identity(database):
    import psycopg

    original, _orders, opening, _client = opened(database)
    with pytest.raises(psycopg.Error, match="immutable intent"), database() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET account_id='other' WHERE intent_id=%s",
            (original.intent_id,),
        )
    with pytest.raises(psycopg.Error, match="immutable order"), database() as conn:
        conn.execute(
            "UPDATE v2_orders SET quantity=1,version=version+1 WHERE order_id=%s",
            (opening,),
        )


def test_delivery_lease_takeover_fences_old_receipt(database):
    from threading import Event

    from v2_core.delivery import Projector

    IntentStore(database).admit(intent())
    entered, resume = Event(), Event()

    def slow_sink(*_):
        entered.set()
        assert resume.wait(5)
        return True

    old = Projector(database, "lease-test", slow_sink)
    new = Projector(database, "lease-test", lambda *_: True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(old.run_scheduled_batch)
        try:
            assert entered.wait(5)
            assert new.run_scheduled_batch()["claimed"] == 0
            with database() as conn:
                conn.execute(
                    "UPDATE v2_delivery_attempts SET lease_until=clock_timestamp()-interval '1 second' WHERE consumer='lease-test'"
                )
            assert new.run_scheduled_batch()["delivered"] == 1
        finally:
            resume.set()
        assert pending.result(timeout=5)["superseded"] == 1
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_consumer_receipts WHERE consumer='lease-test'"
        ).fetchone() == (1,)


def test_late_commit_is_not_lost_by_consumer_high_water(database):
    from v2_core.delivery import Projector

    # Keep one admission uncommitted while a newer event is delivered.
    first, second = intent(), intent()
    delivered = set()

    def sink(event_id, *_):
        delivered.add(event_id)
        return True

    with database() as conn:

        @contextmanager
        def borrowed():
            yield conn

        assert IntentStore(borrowed).admit(first).code is Code.ACCEPTED
        assert IntentStore(database).admit(second).code is Code.ACCEPTED
        assert (
            Projector(database, "late-commit", sink).run_scheduled_batch()["delivered"]
            == 1
        )
        assert delivered == {second.intent_id}
    assert (
        Projector(database, "late-commit", sink).run_scheduled_batch()["delivered"] == 1
    )
    assert delivered == {first.intent_id, second.intent_id}
