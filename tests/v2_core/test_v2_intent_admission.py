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
    from psycopg.conninfo import conninfo_to_dict

    host = conninfo_to_dict(dsn).get("host", "")
    if not host.startswith(("/tmp/v2-data-qa.", "/tmp/codex-v2-data-qa.")):
        pytest.fail("V2 database QA requires a dedicated temporary Unix socket")

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


def test_fill_provenance_is_append_only_without_double_accounting(database):
    from v2_core.ledger import Ledger
    from v2_core.service import TradingData

    original, orders, opening, _ = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger = Ledger(database)
    args = {
        "order_id": opening,
        "exchange_fill_id": "same-fill",
        "quantity": "0.01",
        "price": "100",
        "fee": "-0.001",
        "fee_currency": "USDT",
        "occurred_at_ms": 100,
    }
    assert ledger.record_fill(**args, evidence={"source": "stream"})
    before = TradingData(database).trace(original.intent_id)
    assert not ledger.record_fill(**args, evidence={"source": "REST"})
    after = TradingData(database).trace(original.intent_id)
    assert len(after["fills"]) == 1
    assert len(after["fill_observations"]) == 2
    assert after["data_revision"] == before["data_revision"] + 1
    assert not ledger.record_fill(**args, evidence={"source": "REST"})
    assert TradingData(database).trace(original.intent_id) == after
    with pytest.raises(ValueError, match="conflict"):
        ledger.record_fill(**{**args, "fee": "0.001"}, evidence={"source": "REST"})
    orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "test"}
    )
    closing, _ = orders.prepare(original.intent_id, leg="CLOSE")
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    record(ledger, closing, "closing", "100", fee="0")
    from decimal import Decimal

    report = ledger.report(original.intent_id, settlement_currency="USDT")
    assert Decimal(report["net_pnl"]) == Decimal("0.001")
    assert report["accounting_revision"] == 2


def test_unknown_query_binds_exchange_identity_without_status_change(database):
    from v2_core.runner import ExchangeObservation, ExecutionRunner

    _original, orders, opening, client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(opening, expected_version=2, status="UNKNOWN", evidence={})
    runner = ExecutionRunner(
        database,
        submit=lambda _: pytest.fail("must not submit"),
        query=lambda _: ExchangeObservation(
            client, "UNKNOWN", "123", evidence={"source": "query"}
        ),
        risk_check=lambda _: True,
    )
    assert runner.recover(opening) == "UNKNOWN"
    assert runner.snapshot(opening)["exchange_order_id"] == "123"
    version = runner.snapshot(opening)["version"]
    assert runner.recover(opening) == "UNKNOWN"
    assert runner.snapshot(opening)["version"] == version
    runner.query = lambda _: ExchangeObservation(
        client, "UNKNOWN", "456", evidence={"source": "query"}
    )
    with pytest.raises(ValueError, match="identity"):
        runner.recover(opening)


def test_binance_protocol_through_pg_runner_records_fill_once(database):
    from v2_core.binance import BinanceFutures
    from v2_core.runner import ExecutionRunner
    from v2_core.service import TradingData

    original, _orders, opening, client = opened(database)
    calls = []

    def transport(method, path, params):
        calls.append((method, path))
        if path.endswith("/dual"):
            return {"dualSidePosition": False}
        if path.endswith("/userTrades"):
            return [
                {
                    "id": 9,
                    "orderId": 123,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "qty": "0.01",
                    "price": "100",
                    "commission": "0.001",
                    "commissionAsset": "USDT",
                    "time": 100,
                }
            ]
        return {
            "clientOrderId": client,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "reduceOnly": False,
            "origQty": "0.01",
            "executedQty": "0.01",
            "orderId": 123,
            "status": "FILLED",
        }

    venue = BinanceFutures(transport, account_id="test-account", environment="SANDBOX")
    runner = ExecutionRunner(
        database, submit=venue.submit, query=venue.query, risk_check=lambda _: True
    )
    assert runner.dispatch(opening) == "ACKNOWLEDGED"
    assert runner.recover(opening) == "FILLED"
    assert runner.recover(opening) == "FILLED"
    trace = TradingData(database).trace(original.intent_id)
    assert len(trace["fills"]) == 1
    assert sum(method == "POST" for method, _ in calls) == 1


def test_foreign_fee_valuation_is_versioned_and_invalidates_old_settlement(database):
    from decimal import Decimal

    from v2_core.service import TradingData

    original, orders, opening, _ = opened(database)
    data = TradingData(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    record(data.ledger, opening, "fee-bnb", "100", fee="0.001", currency="BNB")
    orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "test"}
    )
    closing, _ = orders.prepare(original.intent_id, leg="CLOSE")
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    record(data.ledger, closing, "closing", "110", fee="0")
    orders.transition(
        closing, expected_version=2, status="FILLED", evidence={"source": "test"}
    )
    report = data.ledger.report(original.intent_id, settlement_currency="USDT")
    assert report["net_pnl"] is None and len(report["missing_valuations"]) == 1
    key = report["missing_valuations"][0]["fact_key"]
    args = {
        "kind": "FEE",
        "fact_key": key,
        "target_currency": "USDT",
        "rate": "300",
        "quote_at_ms": 100,
        "max_distance_ms": 0,
        "expected_version": 0,
        "request_key": "quote-1",
        "evidence": {"source": "historical-test-quote", "reason": "fee valuation"},
    }
    assert data.valuations.record(original.intent_id, **args)
    assert not data.valuations.record(original.intent_id, **args)
    report = data.ledger.report(original.intent_id, settlement_currency="USDT")
    assert Decimal(report["net_pnl"]) == Decimal("-0.2")
    assert report["valuation_basis"] == "HISTORICAL_MARK"
    proof = {
        "exchange_flat": True,
        "orders_terminal": True,
        "fills_complete": True,
        "cash_complete": True,
        "observed_at_ms": 200,
        "source": "test",
        "ledger_revision": report["accounting_revision"],
    }
    assert data.ledger.settle(original.intent_id, currency="USDT", evidence=proof)
    with pytest.raises(ValueError, match="conflict"):
        data.valuations.record(original.intent_id, **{**args, "rate": "301"})
    with pytest.raises(ValueError, match="stale"):
        data.valuations.record(original.intent_id, **{**args, "request_key": "stale"})
    assert data.valuations.record(
        original.intent_id,
        **{**args, "expected_version": 1, "request_key": "quote-2", "rate": "200"},
    )
    assert not data.valuations.record(original.intent_id, **args)
    revised = data.ledger.report(original.intent_id, settlement_currency="USDT")
    assert revised["accounting_status"] == "CALCULATED"
    assert Decimal(revised["net_pnl"]) == Decimal("-0.1")
    assert len(data.trace(original.intent_id)["valuations"]) == 2
    with pytest.raises(ValueError, match="stale"):
        data.ledger.settle(original.intent_id, currency="USDT", evidence=proof)


def test_valuation_cash_scope_freshness_and_concurrent_cas(database):
    from v2_core.service import TradingData

    original, _orders, _opening, _client = opened(database)
    data = TradingData(database)
    data.ledger.adjustment(
        episode_id=original.intent_id,
        source_id="foreign",
        amount_text="-0.2",
        currency="BNB",
        kind="CORRECTION",
        occurred_at_ms=100,
        evidence={"source": "test"},
    )
    key = data.trace(original.intent_id)["cash"][0]["adjustment_key"]
    args = {
        "kind": "CASH",
        "fact_key": key,
        "target_currency": "USDT",
        "rate": "300",
        "quote_at_ms": 100,
        "max_distance_ms": 1,
        "expected_version": 0,
        "request_key": "one",
        "evidence": {"source": "quote", "reason": "foreign adjustment"},
    }
    with pytest.raises(ValueError, match="distant"):
        data.valuations.record(original.intent_id, **{**args, "quote_at_ms": 102})
    other = intent()
    assert data.intents.admit(other).code is Code.ACCEPTED
    with pytest.raises(ValueError, match="this episode"):
        data.valuations.record(other.intent_id, **args)

    def attempt(n):
        try:
            return data.valuations.record(
                original.intent_id, **{**args, "request_key": str(n)}
            )
        except ValueError as exc:
            assert "stale" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    report = data.ledger.report(original.intent_id, settlement_currency="USDT")
    from decimal import Decimal

    assert Decimal(report["cash_adjustments"]) == Decimal(-60)
    assert report["accounting_status"] == "PENDING"  # no opening/closing fills


def test_recovery_schedule_does_not_starve_later_orders(database):
    from v2_core.orders import Orders
    from v2_core.runner import ExecutionRunner

    orders = Orders(database)
    identities = []
    for n in range(3):
        request = replace(intent(), account_id=f"account-{n}")
        assert IntentStore(database).admit(request).code is Code.ACCEPTED
        identity, _client = orders.prepare(request.intent_id)
        orders.transition(
            identity, expected_version=1, status="SUBMITTING", evidence={}
        )
        identities.append(identity)
    calls = []

    def query(order):
        calls.append(order["order_id"])
        if order["order_id"] == identities[0]:
            raise TimeoutError("sensitive transport detail")

    runner = ExecutionRunner(
        database,
        submit=lambda _: pytest.fail("no submit"),
        query=query,
        risk_check=lambda _: True,
    )
    results = {}
    for _ in range(3):
        results.update(runner.recover_batch(1))
    assert set(results) == set(identities) and len(calls) == 3
    assert results[identities[0]] == "UNAVAILABLE"
    assert runner.recover_batch(3) == {}
    with database() as conn:
        assert conn.execute(
            "SELECT error_code FROM v2_order_recovery WHERE order_id=%s",
            (identities[0],),
        ).fetchone() == ("TimeoutError",)
        conn.execute(
            "UPDATE v2_order_recovery SET next_attempt_at=clock_timestamp()-interval '1 second'"
        )
    assert len(runner.recover_batch(3)) == 3


def test_recovery_workers_claim_one_query_and_recover_abandoned_lease(database):
    from v2_core.runner import ExecutionRunner

    _original, orders, opening, _client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    calls = []
    runner = ExecutionRunner(
        database,
        submit=lambda _: pytest.fail("no submit"),
        query=lambda order: calls.append(order["order_id"]),
        risk_check=lambda _: True,
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runner.recover_batch(1), range(4)))
    assert sum(len(result) for result in results) == 1
    assert calls == [opening]
    with database() as conn:
        conn.execute(
            """UPDATE v2_order_recovery SET next_attempt_at=clock_timestamp()-interval '1 second',
            lease_token=%s,lease_until=clock_timestamp()-interval '1 second'""",
            (str(uuid4()),),
        )
    assert runner.recover_batch(1) == {opening: "UNKNOWN"}


def test_approved_risk_evidence_is_committed_before_submission(database):
    from v2_core.runner import ExchangeObservation, ExecutionRunner, RiskVerdict

    _original, _orders, opening, client = opened(database)

    def submit(_):
        with database() as conn:
            proof = conn.execute(
                "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='SUBMITTING'",
                (opening,),
            ).fetchone()[0]
        assert proof["risk"] == {
            "allowed": True,
            "reason": "within account limit",
            "evidence": {"limit": "100"},
        }
        return ExchangeObservation(
            client, "ACKNOWLEDGED", "123", evidence={"source": "test"}
        )

    runner = ExecutionRunner(
        database,
        submit=submit,
        query=lambda _: None,
        risk_check=lambda _: RiskVerdict(
            True, "within account limit", {"limit": "100"}
        ),
    )
    assert runner.dispatch(opening) == "ACKNOWLEDGED"


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


def test_runtime_expires_stale_decisions_without_submission(database):
    import json

    from v2_core.evidence import canonical
    from v2_core.runtime import DataRuntime

    clock = [5]
    calls = []
    runtime = DataRuntime(
        database,
        submit=lambda request: calls.append(request),
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=lambda: clock[0],
    )
    old = intent()
    assert runtime.accept_open(old, evidence())["status"] == "EXPIRED"
    assert (
        runtime.accept_open(replace(old, intent_id=str(uuid4())), evidence())[
            "intent_id"
        ]
        == old.intent_id
    )
    fresh_evidence = DecisionEvidence(
        "strategy-v1",
        evidence().config_json,
        canonical(dict(json.loads(evidence().snapshot_json), expires_at_ms=10)),
    )
    fresh = replace(
        intent(),
        evidence_ref=fresh_evidence.evidence_ref,
        config_digest=fresh_evidence.config_digest,
    )
    accepted = runtime.accept_open(fresh, fresh_evidence)
    assert accepted["status"] == "PREPARED"
    assert (
        runtime.accept_open(replace(fresh, intent_id=str(uuid4())), fresh_evidence)[
            "status"
        ]
        == "PREPARED"
    )
    clock[0] = 10
    assert runtime.execution.dispatch(accepted["order_id"]) == "DENIED"
    assert calls == []
    trace = runtime.data.trace(fresh.intent_id)
    assert (
        trace["timeline"][-1]["evidence"]["reason"]
        == "decision expired before dispatch"
    )
    assert trace["status"] == "CANCELLED"


def test_runtime_acceptance_does_not_submit_and_preserves_risk_reason(database):
    import json

    from v2_core.evidence import canonical
    from v2_core.runner import RiskVerdict
    from v2_core.runtime import DataRuntime

    calls = []
    runtime = DataRuntime(
        database,
        submit=lambda r: calls.append(r),
        query=lambda _: None,
        risk_check=lambda _: RiskVerdict(
            False, "account exposure limit", {"rule": "max-notional"}
        ),
        clock_ms=lambda: 5,
    )
    proof = DecisionEvidence(
        "strategy-v1",
        evidence().config_json,
        canonical(dict(json.loads(evidence().snapshot_json), expires_at_ms=10)),
    )
    request = replace(
        intent(), evidence_ref=proof.evidence_ref, config_digest=proof.config_digest
    )
    accepted = runtime.accept_open(request, proof)
    assert accepted["status"] == "PREPARED"
    assert calls == []
    assert runtime.execution.dispatch(accepted["order_id"]) == "DENIED"
    assert calls == []
    assert (
        runtime.data.trace(request.intent_id)["timeline"][-1]["evidence"]["reason"]
        == "account exposure limit"
    )


def test_business_state_concurrent_writers_and_tombstone(database):
    from v2_core.state import BusinessState, StateKey

    store = BusinessState(database)
    key = StateKey("BINANCE", "qa", "SANDBOX", "FUTURES", "s6:cooldown", "BTCUSDT")
    assert store.read(key) is None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda n: store.change(
                    key,
                    expected_version=0,
                    request_key=str(n),
                    payload={"until_ms": 100},
                    reason="loss cooldown",
                ),
                range(6),
            )
        )
    assert [r.code for r in results].count("APPLIED") == 1
    assert [r.code for r in results].count("STALE") == 5
    assert (
        store.change(
            key,
            expected_version=1,
            request_key="delete",
            payload={},
            deleted=True,
            reason="cooldown elapsed",
        ).version
        == 2
    )
    assert store.read(key).deleted is True
    assert (
        store.change(
            key,
            expected_version=0,
            request_key="stale-process",
            payload={"until_ms": 200},
            reason="old snapshot",
        ).code
        == "STALE"
    )
    assert store.read(key).version == 2
    assert (
        store.change(
            key,
            expected_version=2,
            request_key="new-cycle",
            payload={"until_ms": 300},
            reason="new loss cooldown",
        ).version
        == 3
    )
    assert store.read(replace(key, account_id="other")) is None


def test_business_state_commit_ack_loss_and_request_conflict(database):
    from v2_core.state import BusinessState, StateKey

    key = StateKey("BINANCE", "qa", "SANDBOX", "FUTURES", "strategy:s8", "cursor")

    @contextmanager
    def ambiguous():
        with database() as conn:
            yield conn
        raise OSError("commit acknowledgement lost")

    args = {
        "expected_version": 0,
        "request_key": "signal-1",
        "payload": {"cursor": 12},
        "reason": "processed signal",
    }
    with pytest.raises(OSError):
        BusinessState(ambiguous).change(key, **args)
    assert BusinessState(database).change(key, **args).code == "ALREADY_APPLIED"
    with pytest.raises(ValueError, match="content conflict"):
        BusinessState(database).change(key, **dict(args, payload={"cursor": 13}))
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_state_history WHERE state_id=%s", (key.identity,)
        ).fetchone() == (1,)


def test_business_state_cannot_commit_without_audit_history(database):
    import psycopg

    from v2_core.state import BusinessState, StateKey

    key = StateKey("BINANCE", "qa", "SANDBOX", "FUTURES", "s7:grid", "generation")
    BusinessState(database).change(
        key, expected_version=0, request_key="init", payload={}, reason="initialize"
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database() as conn:
        conn.execute(
            "UPDATE v2_business_state SET version=version+1 WHERE state_id=%s",
            (key.identity,),
        )
    assert BusinessState(database).read(key).version == 1


def test_runtime_sweeper_never_expires_submitted_unknown(database):
    from v2_core.runtime import DataRuntime

    runtime = DataRuntime(
        database,
        submit=lambda _: None,
        query=lambda _: None,
        risk_check=lambda _: True,
        clock_ms=lambda: 100,
    )
    unstarted = intent()
    IntentStore(database).admit(unstarted)
    prepared, orders, opening, _client = opened(database)
    assert runtime.expire_once() == {
        unstarted.intent_id: "EXPIRED",
        prepared.intent_id: "EXPIRED",
    }
    original, orders, opening, _client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(opening, expected_version=2, status="UNKNOWN", evidence={})
    assert runtime.expire_once() == {}
    assert runtime.data.trace(original.intent_id)["status"] == "UNKNOWN"


def test_recovery_attention_is_durable_deduplicated_and_superseded(database):
    import time

    from v2_core.attention import AttentionNotifications, RecoveryAttention
    from v2_core.service import TradingData

    original, orders, opening, _client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(opening, expected_version=2, status="UNKNOWN", evidence={})
    watcher = RecoveryAttention(database)
    now = int(time.time() * 1000) + 10000
    assert watcher.scan(now_ms=now, overdue_ms=1000) == 1
    assert watcher.scan(now_ms=now, overdue_ms=1000) == 0
    trace = TradingData(database).trace(original.intent_id)
    event = next(
        e for e in trace["domain_events"] if e["kind"].startswith("ATTENTION_REQUIRED:")
    )
    assert event["payload"]["fallback"] == "QUERY_ONLY"
    sent = []

    def send(text):
        sent.append(text)
        return True

    notifications = AttentionNotifications(database, send)
    assert notifications(
        event["event_id"], original.intent_id, event["kind"], event["payload"]
    )
    assert event["event_id"] in sent[0]
    orders.transition(
        opening,
        expected_version=3,
        status="REJECTED",
        evidence={"source": "verified exchange response"},
    )
    assert notifications(
        event["event_id"], original.intent_id, event["kind"], event["payload"]
    )
    assert len(sent) == 1
    assert watcher.scan(now_ms=now, overdue_ms=1000) == 0


def test_explicit_connection_factory_enforces_readonly_and_durability(database):
    import psycopg

    from v2_core.database import connection_factory
    from v2_core.service import TradingData

    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    dsn = os.environ["V2_CORE_TEST_DSN"]
    writable = connection_factory(dsn, schema=schema)
    original = intent()
    assert IntentStore(writable).admit(original).code is Code.ACCEPTED
    readonly = connection_factory(dsn, schema=schema, read_only=True)
    assert (
        TradingData(readonly).trace(original.intent_id)["scope"]["account_id"]
        == original.account_id
    )
    with readonly() as conn:
        assert conn.execute("SHOW synchronous_commit").fetchone() == ("on",)
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), readonly() as conn:
        conn.execute("UPDATE v2_trade_intents SET version=version+1")


@pytest.mark.parametrize(
    "schema", ["public", "pg_catalog", "pg_temp", "bad;DROP SCHEMA public", ""]
)
def test_connection_factory_rejects_shared_or_unsafe_schemas(schema):
    from v2_core.database import connection_factory

    with pytest.raises(ValueError, match="dedicated"):
        connection_factory("unused-dsn", schema=schema)


def test_grid_orders_reserve_budget_preserve_identity_and_partial_close(database):
    from decimal import Decimal

    from v2_core.ledger import Ledger
    from v2_core.orders import Orders
    from v2_core.runner import ExecutionRunner
    from v2_core.service import TradingData

    original = replace(intent(), producer="s7")
    IntentStore(database).admit(original)
    orders, ledger = Orders(database), Ledger(database)
    first_args = {
        "quantity": "0.004",
        "request_key": "grid-1",
        "order_type": "LIMIT",
        "limit_price": "100",
        "time_in_force": "GTC",
        "evidence": {"reason": "grid lower level"},
    }
    first, client = orders.prepare(original.intent_id, **first_args)
    second, _ = orders.prepare(
        original.intent_id,
        quantity="0.006",
        request_key="grid-2",
        evidence={"reason": "grid second level"},
    )
    assert orders.prepare(original.intent_id, **first_args) == (first, client)
    with pytest.raises(ValueError, match="content conflict"):
        orders.prepare(original.intent_id, **dict(first_args, limit_price="99"))
    with pytest.raises(ValueError, match="exceeds"):
        orders.prepare(
            original.intent_id,
            quantity="0.001",
            request_key="over-budget",
            evidence={"reason": "grid excess"},
        )
    orders.transition(first, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=first,
        exchange_fill_id="grid-fill",
        quantity="0.004",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=100,
        evidence={"source": "query"},
    )
    orders.transition(
        first, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    assert (
        TradingData(database).trace(original.intent_id)["status"] == "PARTIALLY_FILLED"
    )
    # Cancelling the unfilled sibling cannot release the slot holding real fills.
    orders.transition(
        second,
        expected_version=1,
        status="CANCELLED",
        evidence={"reason": "grid refresh"},
    )
    assert (
        TradingData(database).trace(original.intent_id)["episode"]["status"] == "ACTIVE"
    )
    replacement, _ = orders.prepare(
        original.intent_id,
        quantity="0.006",
        request_key="grid-replace",
        evidence={"reason": "refreshed grid"},
    )
    # Reduce only the confirmed fill; the sibling may still be waiting to open.
    closing, _ = orders.prepare(
        original.intent_id,
        leg="CLOSE",
        quantity="0.004",
        request_key="grid-tp",
        evidence={"reason": "grid take profit"},
    )
    runner = ExecutionRunner(
        database, submit=lambda _: None, query=lambda _: None, risk_check=lambda _: True
    )
    assert runner.snapshot(closing)["reduce_only"] is True
    assert runner.snapshot(closing)["side"] == "SELL"
    assert runner.snapshot(first)["order_type"] == "LIMIT"
    assert Decimal(runner.snapshot(first)["limit_price"]) == Decimal(100)
    orders.transition(
        replacement,
        expected_version=1,
        status="CANCELLED",
        evidence={"reason": "end grid cycle"},
    )
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id="grid-exit",
        quantity="0.004",
        price="110",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=200,
        evidence={"source": "query"},
    )
    orders.transition(
        closing, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    report = ledger.report(original.intent_id, settlement_currency="USDT")
    assert Decimal(report["net_pnl"]) == Decimal("0.04")
    ledger.settle(
        original.intent_id,
        currency="USDT",
        evidence={
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
            "observed_at_ms": 200,
            "source": "query",
            "ledger_revision": report["accounting_revision"],
        },
    )
    assert orders.prepare(original.intent_id, **first_args) == (first, client)


def test_concurrent_grid_allocations_cannot_exceed_intent_budget(database):
    from v2_core.orders import Orders

    original = intent()
    IntentStore(database).admit(original)

    def allocate(n):
        try:
            Orders(database).prepare(
                original.intent_id,
                quantity="0.006",
                request_key=f"level-{n}",
                evidence={"reason": "grid level"},
            )
            return "PREPARED"
        except ValueError as exc:
            assert "exceeds" in str(exc)
            return "DENIED"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(allocate, range(4)))
    assert outcomes.count("PREPARED") == 1
    assert outcomes.count("DENIED") == 3


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


def test_signal_intent_receipt_is_atomic_and_cannot_bypass_dedup(database):
    from v2_core.signals import signal_consumer

    service, request, proof, signal_id = signal_request(database)
    consumer = signal_consumer(request)
    assert (
        len(
            service.signals.pending(
                consumer=consumer, environment="SANDBOX", source="tv_bridge"
            )
        )
        == 1
    )
    result = service.accept_signal(request, proof)
    assert result.code is Code.ACCEPTED
    replay = service.accept_signal(replace(request, intent_id=str(uuid4())), proof)
    assert replay.code is Code.ALREADY_ACCEPTED and replay.intent_id == result.intent_id
    assert (
        service.signals.pending(
            consumer=consumer, environment="SANDBOX", source="tv_bridge"
        )
        == []
    )
    assert (
        service.intents.admit(
            replace(request, intent_id=str(uuid4()), request_key="different-key")
        ).code
        is Code.CONFLICT
    )
    assert (
        service.intents.admit(
            replace(request, intent_id=str(uuid4()), environment="LIVE")
        ).code
        is Code.CONFLICT
    )
    assert counts(database) == (1, 1)
    assert service.trace(request.intent_id)["signal"]["signal_id"] == signal_id
    assert (
        service.trace(request.intent_id)["signal"]["snapshot"]["features"]["price"]
        == "100"
    )


def test_signal_receipt_failure_rolls_back_intent_and_outbox(database):
    import psycopg

    service, request, proof, _signal_id = signal_request(database)
    with database() as conn:
        conn.execute(
            "ALTER TABLE v2_signal_receipts ADD CONSTRAINT qa_receipt_failure CHECK(outcome<>'INTENT')"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        service.accept_signal(request, proof)
    assert counts(database) == (0, 0)


def test_ignored_signal_cannot_be_revived_by_delayed_consumer(database):
    from v2_core.signals import signal_consumer

    service, request, proof, signal_id = signal_request(database)
    assert service.signals.complete(
        consumer=signal_consumer(request),
        signal_id=signal_id,
        outcome="EXPIRED",
        reason="expired signal",
    )
    assert service.accept_signal(request, proof).code is Code.CONFLICT
    assert counts(database) == (0, 0)


def test_signal_inbox_rejects_secret_and_identity_conflicts(database):
    service, _request, _proof, _signal_id = signal_request(database)
    args = {
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "request_key": "alert-1",
        "snapshot": {
            "observed_at": 1,
            "expires_at_ms": 10,
            "symbol": "BTCUSDT",
            "signal": "TREND_UP",
            "features": {"price": "101"},
        },
    }
    with pytest.raises(ValueError, match="content conflict"):
        service.signals.admit(**args)
    with pytest.raises(ValueError, match="secrets"):
        service.signals.admit(
            **dict(args, snapshot=dict(args["snapshot"], secret="do-not-store"))
        )


def test_backup_restore_preserves_trace_ledger_and_cache_rebuild(database, tmp_path):
    import subprocess

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from v2_core.database import connection_factory
    from v2_core.projections import RedisTraces
    from v2_core.service import TradingData

    service, request, proof, _signal_id = signal_request(database)
    service.accept_signal(request, proof)
    opening, _ = service.orders.prepare(request.intent_id)
    service.orders.transition(
        opening, expected_version=1, status="SUBMITTING", evidence={}
    )
    record(service.ledger, opening, "backup-open", "100")
    service.orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    closing, _ = service.orders.prepare(request.intent_id, leg="CLOSE")
    service.orders.transition(
        closing, expected_version=1, status="SUBMITTING", evidence={}
    )
    record(service.ledger, closing, "backup-close", "110")
    service.orders.transition(
        closing, expected_version=2, status="FILLED", evidence={"source": "query"}
    )
    report = service.ledger.report(request.intent_id, settlement_currency="USDT")
    service.ledger.settle(
        request.intent_id,
        currency="USDT",
        evidence={
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
            "observed_at_ms": 100,
            "source": "query",
            "ledger_revision": report["accounting_revision"],
        },
    )
    before = service.trace(request.intent_id)
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    archive = tmp_path / "qa-trade-backup.dump"
    dsn = os.environ["V2_CORE_TEST_DSN"]
    pg_bin = Path(os.environ.get("V2_QA_POSTGRES_BIN", "/usr/lib/postgresql/16/bin"))
    subprocess.run(
        [
            str(pg_bin / "pg_dump"),
            "--dbname",
            dsn,
            "--schema",
            schema,
            "--format=custom",
            "--file",
            str(archive),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    restored_db = "v2_restore_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_db)))
    try:
        restored_dsn = make_conninfo(dsn, dbname=restored_db)
        subprocess.run(
            [
                str(pg_bin / "pg_restore"),
                "--exit-on-error",
                "--dbname",
                restored_dsn,
                str(archive),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        restored = connection_factory(restored_dsn, schema=schema)
        assert TradingData(restored).trace(request.intent_id) == before
        after_report = TradingData(restored).ledger.report(
            request.intent_id, settlement_currency="USDT"
        )
        assert after_report["net_pnl"] == report["net_pnl"]
        assert after_report["accounting_status"] == "SETTLED"
        rebuilt = []

        class Cache:
            def put(self, identity, version, payload):
                rebuilt.append(payload)
                return True

        assert RedisTraces(restored, Cache()).rebuild_batch()["count"] == 1
        assert rebuilt == [before]
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(restored_db))
            )


def test_isolated_pg_crash_preserves_committed_intent_and_never_resubmits(database):
    import subprocess

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from v2_core.runner import ExecutionRunner
    from v2_core.service import TradingData

    if os.environ.get("V2_QA_CLUSTER_RESTART") != "YES" or os.environ.get(
        "PYTEST_XDIST_WORKER"
    ):
        pytest.skip("requires serial disposable-cluster QA runner")
    dsn = os.environ["V2_CORE_TEST_DSN"]
    host = Path(conninfo_to_dict(dsn)["host"])
    with database() as conn:
        data_dir = Path(conn.execute("SHOW data_directory").fetchone()[0])
        assert data_dir.name == "pg" and data_dir.parent == host.parent
        assert str(data_dir).startswith("/tmp/v2-data-qa.")
        assert conn.execute("SHOW cluster_name").fetchone() == ("v2_isolated_qa",)
        assert conn.execute("SHOW fsync").fetchone() == ("on",)
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    original, orders, opening, _client = opened(database)
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    committed = TradingData(database).trace(original.intent_id)
    uncommitted = intent()
    conn = psycopg.connect(dsn)
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

    @contextmanager
    def borrowed():
        yield conn

    assert IntentStore(borrowed).admit(uncommitted).code is Code.ACCEPTED
    pg_ctl = str(
        Path(os.environ.get("V2_QA_POSTGRES_BIN", "/usr/lib/postgresql/16/bin"))
        / "pg_ctl"
    )
    try:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        conn.close()
        subprocess.run(
            [
                pg_ctl,
                "-D",
                str(data_dir),
                "-l",
                str(data_dir.parent / "postgres.log"),
                "-o",
                f"-k {host} -p 55443 -c listen_addresses='' -c cluster_name=v2_isolated_qa",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    assert TradingData(database).trace(original.intent_id) == committed
    assert TradingData(database).trace(uncommitted.intent_id) is None
    submitted = []
    runner = ExecutionRunner(
        database,
        submit=lambda r: submitted.append(r),
        query=lambda _: None,
        risk_check=lambda _: True,
    )
    assert runner.dispatch(opening) == "UNKNOWN"
    assert submitted == []
