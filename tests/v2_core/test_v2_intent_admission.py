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


def test_proven_not_submitted_is_terminal_without_manual_recovery(database):
    from v2_core.errors import SubmissionNotSent
    from v2_core.runner import ExecutionRunner
    from v2_core.service import TradingData

    original, _orders, opening, _client = opened(database)
    calls = []

    def submit(_):
        calls.append("preflight")
        raise SubmissionNotSent("VENUE_PREFLIGHT_UNAVAILABLE")

    runner = ExecutionRunner(
        database,
        submit=submit,
        query=lambda _: pytest.fail("terminal must not query"),
        risk_check=lambda _: True,
    )
    assert runner.dispatch(opening) == "REJECTED"
    assert runner.dispatch(opening) == "REJECTED"
    assert calls == ["preflight"]
    trace = TradingData(database).trace(original.intent_id)
    assert trace["episode"]["status"] == "ABORTED"


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


def test_income_scope_dedup_and_unallocated_facts_survive_replay(database):
    from v2_core.income import IncomeJournal

    journal = IncomeJournal(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(
            pool.map(
                lambda _: journal.ingest(income_scope(), **income_fact()), range(4)
            )
        )
    assert len(set(identities)) == 1
    assert (
        journal.ingest(income_scope(), **income_fact(amount_text="-0.0030"))
        == identities[0]
    )
    with pytest.raises(ValueError, match="conflict"):
        journal.ingest(income_scope(), **income_fact(amount_text="-0.004"))
    other = journal.ingest(income_scope("other"), **income_fact())
    different_type = journal.ingest(
        income_scope(), **income_fact(income_type="COMMISSION")
    )
    assert len({other, different_type, identities[0]}) == 3
    assert len(IncomeJournal(database).pending(income_scope())) == 2
    assert len(journal.pending(income_scope(), income_type="FUNDING_FEE")) == 1
    assert len(journal.pending(income_scope("other"))) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_cash_adjustments").fetchone() == (
            0,
        )


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


def test_income_allocation_commits_cash_audit_and_outbox_once(database):
    from decimal import Decimal

    from v2_core.service import TradingData

    original = closed_for_income(database)
    data = TradingData(database)
    identity = data.income.ingest(income_scope(), **income_fact())
    args = {
        "expected_revision": 2,
        "evidence": {"source": "query", "fills_complete": True},
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: data.income.assign_funding(
                    identity, original.intent_id, **args
                ),
                range(4),
            )
        )
    assert sum(results) == 1
    assert data.income.pending(income_scope()) == []
    trace = data.trace(original.intent_id)
    assert len(trace["cash"]) == len(trace["income_allocations"]) == 1
    report = data.ledger.report(original.intent_id, settlement_currency="USDT")
    assert Decimal(report["net_pnl"]) == Decimal("0.097")
    assert report["accounting_revision"] == 3
    assert (
        len([e for e in trace["domain_events"] if e["kind"].startswith("CASH:")]) == 1
    )
    with pytest.raises(ValueError, match="conflict"):
        data.income.assign_funding(
            identity, original.intent_id, expected_revision=3, evidence=args["evidence"]
        )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"income_type": "COMMISSION"}, "only funding"),
        ({"income_type": "REALIZED_PNL"}, "only funding"),
        ({"symbol": "ETHUSDT"}, "account and symbol"),
        ({"occurred_at_ms": 100}, "unambiguous"),
        ({"occurred_at_ms": 200}, "unambiguous"),
        ({"occurred_at_ms": 201}, "unambiguous"),
    ],
)
def test_income_not_blindly_counted_or_attributed(database, changes, reason):
    from v2_core.service import TradingData

    original = closed_for_income(database)
    data = TradingData(database)
    identity = data.income.ingest(income_scope(), **income_fact(**changes))
    with pytest.raises(ValueError, match=reason):
        data.income.assign_funding(
            identity,
            original.intent_id,
            expected_revision=2,
            evidence={"source": "query", "fills_complete": True},
        )
    assert len(data.income.pending(income_scope())) == 1
    assert data.trace(original.intent_id)["cash"] == []


def test_income_assignment_failure_rolls_back_accounting_and_event(database):
    from v2_core.service import TradingData

    original = closed_for_income(database)
    data = TradingData(database)
    identity = data.income.ingest(income_scope(), **income_fact())
    before = data.trace(original.intent_id)
    with database() as conn:
        conn.execute("""CREATE FUNCTION fail_income_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected failure'; END; $$;
            CREATE TRIGGER fail_income_assignment BEFORE INSERT ON v2_income_allocations
            FOR EACH ROW EXECUTE FUNCTION fail_income_assignment();""")
    with pytest.raises(Exception, match="injected failure"):
        data.income.assign_funding(
            identity,
            original.intent_id,
            expected_revision=2,
            evidence={"source": "query", "fills_complete": True},
        )
    assert data.trace(original.intent_id) == before
    assert len(data.income.pending(income_scope())) == 1


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


def test_income_import_replays_windows_and_retains_failed_page_progress(database):
    from v2_core.binance_income import BinanceIncomeImporter
    from v2_core.income import IncomeJournal

    calls = []

    def request(method, path, params):
        assert method == "GET" and path == "/fapi/v1/income"
        calls.append(params)
        if params["page"] == 1:
            return [income_row(i) for i in range(1000)]
        raise TimeoutError("private details")

    importer = BinanceIncomeImporter(
        database,
        request,
        account_id="test-account",
        environment="SANDBOX",
        clock_ms=lambda: 1000,
    )
    result = importer.import_window(start_ms=100, end_ms=200)
    assert (
        result["status"],
        result["pages"],
        result["rows"],
        result["error_code"],
    ) == ("FAILED", 1, 1000, "TimeoutError")
    assert len(IncomeJournal(database).pending(income_scope(), limit=1000)) == 1000
    importer.request = lambda method, path, params: (
        [income_row(i) for i in range(1000)] if params["page"] == 1 else []
    )
    assert importer.import_window(start_ms=100, end_ms=200)["status"] == "FETCHED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_exchange_income").fetchone() == (
            1000,
        )
        assert conn.execute(
            "SELECT status,error_code FROM v2_income_imports WHERE run_id=%s",
            (result["run_id"],),
        ).fetchone() == ("FAILED", "TimeoutError")


@pytest.mark.parametrize("mode", ["limit", "duplicate", "bad-row"])
def test_income_import_never_labels_truncated_or_invalid_pages_fetched(database, mode):
    from v2_core.binance_income import BinanceIncomeImporter

    page = (
        [income_row(i) for i in range(1000)]
        if mode == "limit"
        else [income_row(), income_row()]
    )
    if mode == "bad-row":
        page[1] = income_row(124, time=201)
    importer = BinanceIncomeImporter(
        database,
        lambda *_: page,
        account_id="test-account",
        environment="SANDBOX",
        clock_ms=lambda: 1000,
        max_pages=1,
    )
    result = importer.import_window(start_ms=100, end_ms=200)
    assert result["status"] == ("FAILED" if mode == "bad-row" else "PARTIAL")
    if mode == "bad-row":
        with database() as conn:
            assert conn.execute(
                "SELECT count(*) FROM v2_exchange_income"
            ).fetchone() == (0,)


def test_income_page_commit_ack_loss_reports_durable_progress(database):
    from v2_core.binance_income import BinanceIncomeImporter

    calls = 0

    @contextmanager
    def lost_ack():
        nonlocal calls
        calls += 1
        with database() as conn:
            yield conn
        if calls == 2:  # start committed, page committed, then acknowledgement lost
            raise ConnectionError("simulated commit reply lost")

    importer = BinanceIncomeImporter(
        lost_ack,
        lambda *_: [income_row()],
        account_id="test-account",
        environment="SANDBOX",
        clock_ms=lambda: 1000,
    )
    result = importer.import_window(start_ms=100, end_ms=200)
    assert (result["status"], result["pages"], result["rows"]) == ("FAILED", 1, 1)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_exchange_income").fetchone() == (
            1,
        )
    importer._connect = database
    assert importer.import_window(start_ms=100, end_ms=200)["status"] == "FETCHED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_exchange_income").fetchone() == (
            1,
        )
    with pytest.raises(Exception, match="terminal result"), database() as conn:
        conn.execute(
            "UPDATE v2_income_imports SET status='RUNNING' WHERE run_id=%s",
            (result["run_id"],),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"start_ms": True, "end_ms": 200},
        {"start_ms": 0, "end_ms": 1001},
        {"start_ms": 201, "end_ms": 200},
    ],
)
def test_income_window_validation_precedes_network_and_database(changes):
    from v2_core.binance_income import BinanceIncomeImporter

    def forbidden(*args, **kwargs):
        pytest.fail("invalid window must not perform I/O")

    importer = BinanceIncomeImporter(
        forbidden,
        forbidden,
        account_id="qa",
        environment="SANDBOX",
        clock_ms=lambda: 1000,
    )
    with pytest.raises(ValueError):
        importer.import_window(**changes)


def test_income_journal_financial_facts_are_immutable(database):
    from v2_core.income import IncomeJournal

    journal = IncomeJournal(database)
    identity = journal.ingest(income_scope(), **income_fact())
    with pytest.raises(Exception, match="immutable"), database() as conn:
        conn.execute(
            "UPDATE v2_exchange_income SET amount=0 WHERE income_id=%s", (identity,)
        )
    assert journal.pending(income_scope())[0]["income_id"] == identity


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


def test_strategy_decision_retries_reuse_original_config_context_and_deadline(database):
    _data, _request, _proof, signal_id = signal_request(database)
    calls = []
    from v2_core.strategy import StrategyDecision

    def decide(signal, context, config):
        calls.append(signal)
        signal["symbol"] = "ETHUSDT"
        context["price"] = "999"
        config["size"] = "2"
        return StrategyDecision(
            "OPEN", "original rationale", "BUY", "0.01", '{"score":80}'
        )

    worker, runtime = strategy_worker(database, decide=decide)
    result = worker.consume(signal_id, context={"price": "100"})
    assert result["status"] == "PREPARED"
    assert len(calls) == 1
    record = worker.decision(signal_id)
    assert record["config"] == {"size": "0.01"}
    assert record["snapshot"]["features"]["context"] == {"price": "100"}
    assert record["snapshot"]["symbol"] == "BTCUSDT"
    assert record["snapshot"]["expires_at_ms"] == 8
    replacement, _ = strategy_worker(
        database,
        config={"size": "20"},
        decide=lambda *_: pytest.fail("must reuse committed decision"),
    )
    retried = replacement.consume(signal_id, context={"price": "10000"})
    assert retried["intent_id"] == result["intent_id"]
    assert retried["order_id"] == result["order_id"]
    assert replacement.decision(signal_id) == record
    assert runtime.data.trace(result["intent_id"])["decision"] == record["snapshot"]


def test_strategy_crash_after_decision_cannot_refresh_expired_scene(database):
    _data, _request, _proof, signal_id = signal_request(database)
    worker, runtime = strategy_worker(database)

    def fail(*_):
        raise ConnectionError("simulated admission outage")

    runtime.accept_open = fail
    with pytest.raises(ConnectionError):
        worker.consume(signal_id, context={"price": "100"})
    assert worker.decision(signal_id)["action"] == "OPEN"
    resumed, runtime2 = strategy_worker(
        database, now=lambda: 20, decide=lambda *_: pytest.fail("do not reevaluate")
    )
    result = resumed.consume(signal_id, context={"price": "500"})
    assert result["status"] == "EXPIRED"
    assert runtime2.data.trace(result["intent_id"])["orders"] == []


def test_strategy_concurrent_evaluation_has_one_committed_result_and_order(database):
    from threading import Barrier

    from v2_core.strategy import StrategyDecision

    _data, _request, _proof, signal_id = signal_request(database)
    barrier = Barrier(4)

    def decide(signal, context, config):
        barrier.wait(timeout=10)
        return StrategyDecision("OPEN", context["reason"], "BUY", "0.01")

    worker, _runtime = strategy_worker(database, decide=decide)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda n: worker.consume(signal_id, context={"reason": str(n)}),
                range(4),
            )
        )
    assert (
        len({r["intent_id"] for r in results})
        == len({r["order_id"] for r in results})
        == 1
    )
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize("expired", [False, True])
def test_strategy_ignored_or_expired_signal_never_creates_order(database, expired):
    from v2_core.strategy import StrategyDecision

    _data, _request, _proof, signal_id = signal_request(database)

    def decide(*_):
        assert not expired
        return StrategyDecision(
            "IGNORED", "trend gate denied", features_json='{"trend":"DOWN"}'
        )

    worker, _runtime = strategy_worker(
        database, decide=decide, now=lambda: 20 if expired else 3
    )
    first = worker.consume(signal_id, context={})
    assert first["status"] == ("EXPIRED" if expired else "IGNORED")
    assert worker.consume(signal_id, context={})["status"] == first["status"]
    assert worker.consume(signal_id, context={})["decision_id"] == first["decision_id"]
    assert worker.decision(signal_id)["action"] == first["status"]
    assert counts(database) == (0, 0)


def test_strategy_wrong_source_or_future_signal_cannot_be_evaluated(database):
    _data, _request, _proof, signal_id = signal_request(database)
    worker, _runtime = strategy_worker(
        database, now=lambda: 0, decide=lambda *_: pytest.fail("no evaluation")
    )
    assert worker.consume(signal_id, context={})["status"] == "DEFERRED"
    assert worker.decision(signal_id) is None
    worker.source = "s3"
    with pytest.raises(ValueError, match="bound source"):
        worker.consume(signal_id, context={})


def test_strategy_decision_commit_ack_loss_is_replayed_without_reevaluation(database):
    _data, _request, _proof, signal_id = signal_request(database)
    calls = 0

    @contextmanager
    def lost_ack():
        nonlocal calls
        calls += 1
        with database() as conn:
            yield conn
        if calls == 2:
            raise ConnectionError(
                "simulated durable decision commit acknowledgement lost"
            )

    worker, _runtime = strategy_worker(database)
    worker._connect = lost_ack
    with pytest.raises(ConnectionError):
        worker.consume(signal_id, context={})
    assert counts(database) == (0, 0)
    resumed, _runtime2 = strategy_worker(
        database, decide=lambda *_: pytest.fail("must reuse")
    )
    assert resumed.consume(signal_id, context={})["status"] == "PREPARED"


def test_strategy_evidence_and_decision_roll_back_together(database):
    _data, _request, _proof, signal_id = signal_request(database)
    worker, _runtime = strategy_worker(database)
    with database() as conn:
        before = conn.execute("SELECT count(*) FROM v2_decision_evidence").fetchone()
        conn.execute("""CREATE FUNCTION fail_strategy_decision() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'decision injection'; END; $$;
            CREATE TRIGGER fail_strategy_decision BEFORE INSERT ON v2_strategy_decisions
            FOR EACH ROW EXECUTE FUNCTION fail_strategy_decision();""")
    with pytest.raises(Exception, match="decision injection"):
        worker.consume(signal_id, context={})
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_decision_evidence").fetchone()
            == before
        )
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)
    assert counts(database) == (0, 0)


def test_strategy_ignored_decision_readonly_cli_and_immutable_audit(
    database, monkeypatch, capsys
):
    import json

    from v2_core import __main__ as cli
    from v2_core.strategy import StrategyDecision

    _data, _request, _proof, signal_id = signal_request(database)
    worker, runtime = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "risk context stale")
    )
    result = worker.consume(signal_id, context={"observed_at_ms": 1})
    trace = runtime.data.decision_trace(result["decision_id"])
    assert trace["receipt"]["outcome"] == "IGNORED"
    assert trace["snapshot"]["rationale"] == "risk context stale"
    assert trace["signal"]["signal"] == "TREND_UP"

    def factory(dsn, *, schema, read_only):
        assert read_only is True
        return database

    monkeypatch.setattr(cli, "connection_factory", factory)
    monkeypatch.setenv("V2_POSTGRES_DSN", "dummy-not-used")
    monkeypatch.setattr("sys.argv", ["v2_core", result["decision_id"], "--decision"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == trace
    with pytest.raises(Exception, match="immutable"), database() as conn:
        conn.execute(
            "UPDATE v2_strategy_decisions SET action='EXPIRED' WHERE decision_id=%s",
            (result["decision_id"],),
        )


def test_strategy_existing_receipt_cannot_be_revived_and_consumers_are_isolated(
    database,
):
    from v2_core.strategy import StrategyDecision

    data, _request, _proof, signal_id = signal_request(database)
    first, _runtime = strategy_worker(
        database, decide=lambda *_: pytest.fail("consumed signal cannot be reevaluated")
    )
    data.signals.complete(
        consumer=first.scope.consumer,
        signal_id=signal_id,
        outcome="IGNORED",
        reason="already consumed",
    )
    assert first.consume(signal_id, context={})["status"] == "IGNORED"
    second, _runtime2 = strategy_worker(
        database,
        producer="s8",
        decide=lambda *_: StrategyDecision("IGNORED", "independent strategy"),
    )
    assert second.consume(signal_id, context={})["status"] == "IGNORED"
    assert second.decision(signal_id)["snapshot"]["rationale"] == "independent strategy"
    assert first.decision(signal_id) is None


def test_strategy_waiting_for_capacity_keeps_original_deadline(database):
    occupied, _orders, _opening, _client = opened(database)
    _data, _request, _proof, signal_id = signal_request(database)
    worker, runtime = strategy_worker(database)
    waiting = worker.consume(signal_id, context={"price": "100"})
    assert waiting["status"] == "WAITING_CAPACITY"
    assert runtime.data.trace(waiting["intent_id"])["orders"] == []
    resumed, runtime2 = strategy_worker(
        database, now=lambda: 20, decide=lambda *_: pytest.fail("no reevaluation")
    )
    expired = resumed.consume(signal_id, context={"price": "200"})
    assert expired["status"] == "EXPIRED"
    assert expired["intent_id"] == waiting["intent_id"]
    assert runtime2.data.trace(waiting["intent_id"])["orders"] == []
    assert runtime2.data.trace(occupied.intent_id)["episode"]["status"] == "ACTIVE"


def test_runtime_does_not_hide_unrelated_preparation_failure(database):
    _data, _request, _proof, signal_id = signal_request(database)
    worker, runtime = strategy_worker(database)

    def fail(*_, **__):
        raise ConnectionError("database unavailable")

    runtime.data.orders.prepare = fail
    with pytest.raises(ConnectionError):
        worker.consume(signal_id, context={})


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


def test_strategy_scheduler_failed_signal_does_not_starve_later_signal(database):
    from v2_core.scheduling import StrategyScheduler

    bad = scheduler_signal(database, "bad", "BTCUSDT")
    good = scheduler_signal(database, "good")
    worker, _ = strategy_worker(database)

    def context(snapshot):
        if snapshot["symbol"] == "BTCUSDT":
            raise ConnectionError("sensitive upstream error must not be persisted")
        return {"price": "100"}

    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(1) == {bad: "UNAVAILABLE"}
    assert scheduler.run_once(1) == {good: "PREPARED"}
    assert scheduler.run_once(1) == {}
    with database() as conn:
        rows = conn.execute(
            "SELECT signal_id::text,error_code,completed_at IS NOT NULL FROM v2_strategy_tasks"
        ).fetchall()
    assert set(rows) == {(bad, "ConnectionError", False), (good, None, True)}


def test_strategy_scheduler_persists_only_fixed_diagnostic_detail(database):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "fixed-code")
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(
        worker,
        context_provider=lambda _: (_ for _ in ()).throw(
            ValueError("ACCOUNT_MODE_UNVERIFIED")
        ),
    )
    assert scheduler.run_once() == {signal_id: "UNAVAILABLE"}
    with database() as conn:
        assert conn.execute(
            "SELECT error_code FROM v2_strategy_tasks WHERE signal_id=%s", (signal_id,)
        ).fetchone() == ("ACCOUNT_MODE_UNVERIFIED",)


def test_strategy_scheduler_progress_distinguishes_undiscovered_and_incomplete(
    database,
):
    from v2_core.scheduling import StrategyScheduler
    from v2_core.strategy import StrategyDecision

    scheduler_signal(database, "first")
    scheduler_signal(database, "second")
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "filtered")
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.progress() == {
        "source_signals": 2,
        "scheduled": 0,
        "completed": 0,
        "incomplete": 0,
        "undiscovered": 2,
        "caught_up": False,
    }
    scheduler.run_once(1)
    assert scheduler.progress() == {
        "source_signals": 2,
        "scheduled": 1,
        "completed": 1,
        "incomplete": 0,
        "undiscovered": 1,
        "caught_up": False,
    }
    scheduler.run_once(1)
    assert scheduler.progress()["caught_up"] is True


def test_strategy_scheduler_recovers_capacity_wait_without_feed_and_expires(database):
    from v2_core.scheduling import StrategyScheduler

    opened(database)
    signal_id = scheduler_signal(database, "waiting", "BTCUSDT")
    worker, _ = strategy_worker(database)
    assert StrategyScheduler(worker, context_provider=lambda _: {}).run_once() == {
        signal_id: "WAITING_CAPACITY"
    }
    with database() as conn:
        assert conn.execute(
            "SELECT completed_at FROM v2_strategy_tasks"
        ).fetchone() == (None,)
    scheduler_due(database)
    restarted, runtime = strategy_worker(database, now=lambda: 20)
    assert StrategyScheduler(
        restarted,
        context_provider=lambda _: pytest.fail("recovery cannot require fresh context"),
    ).run_once() == {signal_id: "EXPIRED"}
    assert (
        runtime.data.trace(restarted.decision(signal_id)["decision_id"])["orders"] == []
    )
    assert StrategyScheduler(restarted, context_provider=lambda _: {}).run_once() == {}


def test_strategy_scheduler_expiry_bypasses_failed_context_and_future_defers(database):
    from v2_core.scheduling import StrategyScheduler

    expired = scheduler_signal(database, "expired", expires=2)
    future = scheduler_signal(database, "future", observed=100, expires=200)
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(
        worker,
        context_provider=lambda _: pytest.fail("no context for expired/future signals"),
    )
    assert scheduler.run_once() == {expired: "EXPIRED", future: "DEFERRED"}
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE completed_at IS NULL"
        ).fetchone() == (1,)


def test_strategy_scheduler_concurrent_workers_claim_once(database):
    from v2_core.scheduling import StrategyScheduler
    from v2_core.strategy import StrategyDecision

    ids = {scheduler_signal(database, str(n)) for n in range(8)}
    calls = []

    def run(_):
        worker, _ = strategy_worker(
            database, decide=lambda *_: StrategyDecision("IGNORED", "filtered")
        )
        return StrategyScheduler(
            worker, context_provider=lambda s: calls.append(s) or {}
        ).run_once()

    with ThreadPoolExecutor(4) as pool:
        batches = list(pool.map(run, range(4)))
    assert sum(len(batch) for batch in batches) == 8
    assert set().union(*(set(batch) for batch in batches)) == ids
    assert len(calls) == 8
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE completed_at IS NOT NULL"
        ).fetchone() == (8,)


def test_strategy_scheduler_crashed_lease_is_reclaimed(database):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "crashed")
    worker, _ = strategy_worker(database)
    with database() as conn:
        conn.execute(
            """INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until)
                     VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour')""",
            (worker.scope.consumer, signal_id, str(uuid4())),
        )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once() == {}
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second'"
        )
    assert scheduler.run_once() == {signal_id: "PREPARED"}


def test_strategy_scheduler_stale_worker_cannot_ack_new_lease(database):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "stale")
    worker, _ = strategy_worker(database)
    new_token = str(uuid4())

    def context(_):
        with database() as conn:
            conn.execute(
                "UPDATE v2_strategy_tasks SET lease_token=%s,lease_until=clock_timestamp()+interval '1 hour'",
                (new_token,),
            )
        return {}

    assert StrategyScheduler(worker, context_provider=context).run_once() == {
        signal_id: "PREPARED"
    }
    with database() as conn:
        assert conn.execute(
            "SELECT lease_token::text,completed_at FROM v2_strategy_tasks"
        ).fetchone() == (new_token, None)


def test_strategy_scheduler_admission_crash_recovers_frozen_decision(database):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "decision-crash")
    worker, runtime = strategy_worker(database)

    def fail(*_, **__):
        raise ConnectionError("admission unavailable")

    runtime.accept_open = fail
    assert StrategyScheduler(
        worker, context_provider=lambda _: {"price": "100"}
    ).run_once() == {signal_id: "UNAVAILABLE"}
    scheduler_due(database)
    restarted, _ = strategy_worker(
        database, decide=lambda *_: pytest.fail("decision already frozen")
    )
    assert StrategyScheduler(
        restarted, context_provider=lambda _: pytest.fail("context already frozen")
    ).run_once() == {signal_id: "PREPARED"}


@pytest.mark.parametrize(
    "options",
    [{"limit": True}, {"limit": 0}, {"interval_seconds": 0}, {"lease_seconds": 3601}],
)
def test_strategy_scheduler_rejects_unbounded_options(database, options):
    from v2_core.scheduling import StrategyScheduler

    worker, _ = strategy_worker(database)
    with pytest.raises(ValueError):
        StrategyScheduler(worker, context_provider=lambda _: {}).run_once(**options)


def test_strategy_scheduler_capacity_release_resumes_without_new_decision(database):
    from v2_core.scheduling import StrategyScheduler

    _occupied, orders, opening, _ = opened(database)
    signal_id = scheduler_signal(database, "waiting", "BTCUSDT")
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {"price": "100"})
    assert scheduler.run_once() == {signal_id: "WAITING_CAPACITY"}
    before = worker.decision(signal_id)
    orders.transition(
        opening,
        expected_version=1,
        status="CANCELLED",
        evidence={"reason": "never submitted"},
    )
    scheduler_due(database)
    scheduler.context_provider = lambda _: pytest.fail("must reuse original context")
    assert scheduler.run_once() == {signal_id: "PREPARED"}
    assert worker.decision(signal_id) == before


@pytest.mark.parametrize("commit_succeeded", [True, False])
def test_strategy_scheduler_ack_failure_replays_only_unfinished_task(
    database, commit_succeeded
):
    from v2_core.scheduling import StrategyScheduler

    signal_id = scheduler_signal(database, "ack-failure")
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})

    @contextmanager
    def interrupted_ack():
        acknowledged = False
        with database() as conn:

            class Proxy:
                def execute(self, sql, params=None):
                    nonlocal acknowledged
                    if "completed_at=CASE" in sql:
                        acknowledged = True
                        if not commit_succeeded:
                            raise ConnectionError("ack failed before commit")
                    return conn.execute(sql, params)

            yield Proxy()
        if acknowledged:
            raise ConnectionError("commit response lost")

    scheduler._connect = interrupted_ack
    assert scheduler.run_once(1) == {signal_id: "UNAVAILABLE"}
    before = worker.decision(signal_id)
    scheduler._connect = database
    scheduler.context_provider = lambda _: pytest.fail(
        "no re-evaluation on ack recovery"
    )
    if not commit_succeeded:
        assert scheduler.run_once(1) == {}
        with database() as conn:
            conn.execute(
                "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second'"
            )
        assert scheduler.run_once(1) == {signal_id: "PREPARED"}
    assert scheduler.run_once(1) == {}
    assert worker.decision(signal_id) == before
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


def test_strategy_scheduler_scope_isolation(database):
    from v2_core.scheduling import StrategyScheduler
    from v2_core.signals import Signals
    from v2_core.strategy import StrategyDecision

    valid = scheduler_signal(database, "scoped")
    Signals(database).admit(
        source="s3",
        environment="SANDBOX",
        request_key="wrong-source",
        snapshot={
            "observed_at": 1,
            "expires_at_ms": 10,
            "symbol": "BTCUSDT",
            "signal": "TREND_UP",
            "features": {},
        },
    )
    Signals(database).admit(
        source="tv_bridge",
        environment="LIVE",
        request_key="wrong-environment",
        snapshot={
            "observed_at": 1,
            "expires_at_ms": 10,
            "symbol": "BTCUSDT",
            "signal": "TREND_UP",
            "features": {},
        },
    )
    for producer in ("s6", "s8"):
        worker, _ = strategy_worker(
            database,
            producer=producer,
            decide=lambda *_: StrategyDecision("IGNORED", "filtered"),
        )
        assert StrategyScheduler(worker, context_provider=lambda _: {}).run_once() == {
            valid: "IGNORED"
        }
    with database() as conn:
        assert conn.execute(
            "SELECT count(*),count(DISTINCT consumer) FROM v2_strategy_tasks"
        ).fetchone() == (2, 2)


@pytest.mark.parametrize(
    "side,close_price,expected_pnl",
    [("BUY", "110", "0.095"), ("SELL", "110", "-0.105")],
)
def test_signal_to_binance_protocol_ledger_and_cache_rebuild(
    database, side, close_price, expected_pnl
):
    """Protocol-level simulation, NOT live venue or automated settlement QA."""
    import json
    from decimal import Decimal

    import redis

    from v2_core.binance import BinanceFutures
    from v2_core.delivery import Projector
    from v2_core.projections import RedisProjection, RedisTraces
    from v2_core.runtime import DataRuntime
    from v2_core.scheduling import StrategyScheduler
    from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker

    socket = os.environ.get("V2_REDIS_TEST_SOCKET")
    assert socket and socket.startswith("/tmp/v2-data-qa.")
    submitted, venue_orders, venue_fills = [], {}, {}

    def fake_request(method, path, params):
        if path.endswith("/dual"):
            return {"dualSidePosition": False}
        if method == "POST":
            client_id = params["newClientOrderId"]
            assert client_id not in venue_orders, "duplicate external submission"
            closing = params["reduceOnly"] == "true"
            if closing:
                assert params["side"] != side
            identity = len(submitted) + 1
            raw = {
                "clientOrderId": client_id,
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": closing,
                "origQty": params["quantity"],
                "executedQty": params["quantity"],
                "orderId": identity,
                "status": "FILLED",
            }
            venue_orders[client_id] = raw
            venue_fills[identity] = {
                "id": identity,
                "orderId": identity,
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "qty": params["quantity"],
                "price": close_price if closing else "100",
                "commission": "0.001",
                "commissionAsset": "USDT",
                "time": 6 if closing else 4,
            }
            submitted.append(client_id)
            if not closing:
                raise TimeoutError("opening accepted, response lost")
            return raw
        if path.endswith("/userTrades"):
            return [venue_fills[params["orderId"]]]
        return venue_orders[params["origClientOrderId"]]

    venue = BinanceFutures(
        fake_request, account_id="test-account", environment="SANDBOX"
    )
    runtime = DataRuntime(
        database,
        submit=venue.submit,
        query=venue.query,
        risk_check=lambda _: True,
        clock_ms=lambda: 3,
    )
    worker = StrategyWorker(
        runtime,
        StrategyScope("BINANCE", "test-account", "SANDBOX", "FUTURES", "s6"),
        source="tv_bridge",
        strategy_version="qa-rule",
        config={"quantity": "0.01"},
        decide=lambda *_: StrategyDecision("OPEN", "QA momentum", side, "0.01"),
        max_delay_ms=5,
    )
    signal_id = scheduler_signal(database, "e2e", "BTCUSDT")
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {"price": "100"})
    assert scheduler.run_once() == {signal_id: "PREPARED"}
    identity = worker.decision(signal_id)["decision_id"]
    opening = runtime.data.trace(identity)["orders"][0]["order_id"]
    assert runtime.execution.dispatch(opening) == "UNKNOWN"
    assert runtime.recover_once() == {opening: "FILLED"}
    assert runtime.execution.dispatch(opening) == "FILLED"
    closing, _ = runtime.data.orders.prepare(identity, leg="CLOSE")
    assert runtime.execution.dispatch(closing) == "ACKNOWLEDGED"
    assert runtime.recover_once() == {closing: "FILLED"}
    assert len(submitted) == 2
    assert scheduler.run_once() == {}
    income_id = runtime.data.income.ingest(
        income_scope(), **income_fact(occurred_at_ms=5)
    )
    assert runtime.data.income.assign_funding(
        income_id,
        identity,
        expected_revision=2,
        evidence={"source": "QA complete simulated fills", "fills_complete": True},
    )
    report = runtime.data.ledger.report(identity, settlement_currency="USDT")
    assert Decimal(report["net_pnl"]) == Decimal(expected_pnl)
    # Explicit synthetic proof: production authenticated reconciliation remains
    # a release gate. This test checks the composition/financial invariants only.
    runtime.data.ledger.settle(
        identity,
        currency="USDT",
        evidence={
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
            "observed_at_ms": 10,
            "source": "QA simulation",
            "ledger_revision": report["accounting_revision"],
        },
    )
    trace = runtime.data.trace(identity)
    assert trace["signal"]["signal_id"] == signal_id
    assert len(trace["orders"]) == len(trace["fills"]) == 2
    assert len(trace["income_allocations"]) == 1
    assert trace["episode"]["status"] == "SETTLED"
    namespace = "v2:qa:e2e:" + uuid4().hex
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    cache = RedisTraces(database, RedisProjection(client, namespace))
    try:
        projection = Projector(database, "qa-e2e-cache", cache)
        assert projection.run_scheduled_batch()["failed"] == 0
        assert projection.run_scheduled_batch()["claimed"] == 0
        key = namespace + ":" + identity
        assert json.loads(client.hget(key, "payload")) == trace
        client.delete(key)
        assert cache.rebuild_batch()["count"] == 1
        assert json.loads(client.hget(key, "payload")) == trace
    finally:
        client.delete(namespace + ":" + identity)
        client.close()


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


def test_service_factory_records_to_explicit_isolated_schema(database):
    from services.v2_signal_ingress import create_application

    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    app = create_application(
        environ={
            "V2_SIGNAL_INGRESS_ENABLED": "YES",
            "V2_POSTGRES_DSN": os.environ["V2_CORE_TEST_DSN"],
            "V2_POSTGRES_SCHEMA": schema,
            "V2_ENVIRONMENT": "SANDBOX",
            "V2_TV_WEBHOOK_SECRET": "qa-webhook-secret-never-production",
            "V2_SIGNAL_MAX_AGE_MS": "100",
            "V2_SIGNAL_MAX_LIFETIME_MS": "100",
        },
        clock_ms=lambda: 120,
    )
    assert pg_webhook_call(app)[0] == 200
    with database() as conn:
        assert conn.execute(
            "SELECT source,environment FROM v2_inbound_signals"
        ).fetchone() == ("tv_bridge", "SANDBOX")


def test_pg_webhook_concurrent_retries_commit_one_signal_without_orders(database):
    app = pg_webhook(database)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: pg_webhook_call(app), range(6)))
    assert all(status == 200 for status, _ in results)
    assert len({result["signal_id"] for _, result in results}) == 1
    with database() as conn:
        snapshots = conn.execute("SELECT snapshot FROM v2_inbound_signals").fetchall()
        assert len(snapshots) == 1
        assert "secret" not in repr(snapshots)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_pg_webhook_concurrent_conflicting_content_is_not_acknowledged(database):
    app = pg_webhook(database)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda price: pg_webhook_call(app, price=price), ["100", "101"])
        )
    assert sorted(status for status, _ in results) == [200, 409]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            1,
        )


@pytest.mark.parametrize("commit_succeeded", [True, False])
def test_pg_webhook_commit_fault_never_acks_and_retry_uses_original_identity(
    database, commit_succeeded
):
    @contextmanager
    def interrupted():
        written = False
        with database() as conn:

            class Proxy:
                def execute(self, sql, params=None):
                    nonlocal written
                    if "INSERT INTO v2_inbound_signals" in sql:
                        written = True
                        if not commit_succeeded:
                            raise ConnectionError("sensitive DSN must not escape")
                    return conn.execute(sql, params)

            yield Proxy()
        if written:
            raise ConnectionError("commit response lost")

    assert pg_webhook_call(pg_webhook(interrupted)) == (
        503,
        {"error": "INGRESS_UNAVAILABLE"},
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            int(commit_succeeded),
        )
    # A committed, expired replay is acknowledged without renewing its deadline.
    retry = pg_webhook(database, now=lambda: 999 if commit_succeeded else 120)
    status, result = pg_webhook_call(retry)
    assert status == 200
    original = result["signal_id"]
    assert pg_webhook_call(retry)[1]["signal_id"] == original
    with database() as conn:
        assert conn.execute(
            "SELECT snapshot->>'expires_at_ms' FROM v2_inbound_signals"
        ).fetchone() == ("200",)


def test_pg_webhook_fresh_context_is_persisted_with_decision(database):
    from v2_core.ingress import ContextProvider
    from v2_core.scheduling import StrategyScheduler

    status, result = pg_webhook_call(pg_webhook(database))
    assert status == 200
    worker, runtime = strategy_worker(database, now=lambda: 120)
    context = ContextProvider(
        environment="SANDBOX",
        policy={
            "s0": {"scope": "GLOBAL", "max_age_ms": 50},
            "s3": {"scope": "SYMBOL", "max_age_ms": 30},
        },
        read=lambda source, env, symbol: {
            "snapshot_id": source + ":100",
            "source": source,
            "environment": env,
            "symbol": symbol,
            "observed_at": 100,
            "features": {"price": "100"},
        },
        clock_ms=lambda: 120,
    )
    assert StrategyScheduler(worker, context_provider=context).run_once() == {
        result["signal_id"]: "PREPARED"
    }
    decision = worker.decision(result["signal_id"])
    trace = runtime.data.trace(decision["decision_id"])
    assert trace["signal"]["snapshot"]["observed_at"] == 100
    assert trace["decision"]["features"]["context"]["sources"]["s0"]["symbol"] == "*"
    assert (
        trace["decision"]["features"]["context"]["sources"]["s3"]["snapshot_id"]
        == "s3:100"
    )


@pytest.mark.parametrize("context_failure", ["missing", "stale", "wrong-environment"])
def test_pg_webhook_bad_context_prevents_order_then_signal_expires(
    database, context_failure
):
    from v2_core.ingress import ContextProvider
    from v2_core.scheduling import StrategyScheduler

    status, result = pg_webhook_call(pg_webhook(database))
    assert status == 200
    now = [120]
    worker, _ = strategy_worker(
        database,
        now=lambda: now[0],
        decide=lambda *_: pytest.fail("bad context cannot authorize decision"),
    )

    def read(source, env, symbol):
        if context_failure == "missing":
            return None
        return {
            "snapshot_id": "s3:1",
            "source": source,
            "environment": "LIVE" if context_failure == "wrong-environment" else env,
            "symbol": symbol,
            "observed_at": 0 if context_failure == "stale" else 100,
            "features": {"price": "100"},
        }

    provider = ContextProvider(
        environment="SANDBOX",
        policy={"s3": {"scope": "SYMBOL", "max_age_ms": 30}},
        read=read,
        clock_ms=lambda: now[0],
    )
    scheduler = StrategyScheduler(worker, context_provider=provider)
    assert scheduler.run_once() == {result["signal_id"]: "UNAVAILABLE"}
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)
    scheduler_due(database)
    now[0] = 201
    assert scheduler.run_once() == {result["signal_id"]: "EXPIRED"}


def test_context_freshness_deadline_survives_waiting_and_expired_dispatch(database):
    from v2_core.ingress import ContextProvider

    _status, result = pg_webhook_call(pg_webhook(database))
    now = [120]
    worker, runtime = strategy_worker(database, now=lambda: now[0])
    provider = ContextProvider(
        environment="SANDBOX",
        policy={"s3": {"scope": "SYMBOL", "max_age_ms": 20}},
        read=lambda source, env, symbol: {
            "snapshot_id": "s3:100",
            "source": source,
            "environment": env,
            "symbol": symbol,
            "observed_at": 100,
            "features": {"price": "100"},
        },
        clock_ms=lambda: now[0],
    )
    accepted = worker.consume(
        result["signal_id"], context=provider({"symbol": "BTCUSDT"})
    )
    assert accepted["status"] == "PREPARED"
    assert worker.decision(result["signal_id"])["snapshot"]["expires_at_ms"] == 121
    now[0] = 122
    assert runtime.execution.dispatch(accepted["order_id"]) == "DENIED"
    assert (
        runtime.data.trace(accepted["intent_id"])["orders"][0]["status"] == "CANCELLED"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "LIVE"},
        {"symbol": "ETHUSDT"},
        {"valid_until_ms": 120},
        {"assembled_at": 121},
    ],
)
def test_strategy_rechecks_context_scope_and_age_before_evaluation(database, changes):
    _status, result = pg_webhook_call(pg_webhook(database))
    worker, _ = strategy_worker(
        database,
        now=lambda: 120,
        decide=lambda *_: pytest.fail("invalid context cannot be evaluated"),
    )
    context = {
        "assembled_at": 100,
        "valid_until_ms": 150,
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "sources": {"s3": {}},
        **changes,
    }
    with pytest.raises(ValueError, match="context"):
        worker.consume(result["signal_id"], context=context)
    assert worker.decision(result["signal_id"]) is None


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


def test_s3_concurrent_frame_and_restart_replay_original_config(database):
    processor, _cache = lifecycle_processor(database)
    with ThreadPoolExecutor(4) as pool:
        results = list(
            pool.map(lambda _: processor.process(**lifecycle_frame()), range(4))
        )
    assert all(result == results[0] for result in results)
    restarted, _ = lifecycle_processor(
        database,
        now=lambda: 9999,
        evaluate=lambda *args: pytest.fail("replay must not evaluate"),
    )
    restarted.publisher.lifetime_ms = 500
    restarted.config["detector_version"] = "changed"
    assert restarted.process(**lifecycle_frame()) == results[0]
    with database() as conn:
        for table in (
            "v2_s3_frames",
            "v2_state_history",
            "v2_producer_batches",
            "v2_inbound_signals",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_s3_restart_continues_cooldown_and_updates_last_seen(database):
    first, _ = lifecycle_processor(database)
    original = first.process(**lifecycle_frame())
    restarted, _ = lifecycle_processor(database, now=lambda: 129)
    assert restarted.process(**lifecycle_frame(129))["emitted_events"] == []
    restarted.publisher.clock_ms = lambda: 130
    updated = restarted.process(**lifecycle_frame(130))["emitted_events"][0]
    assert updated["features"]["state"] == "UPDATE"
    assert (
        updated["features"]["episode_id"]
        == original["emitted_events"][0]["features"]["episode_id"]
    )
    assert updated["features"]["revision"] == 2


def test_s3_state_batch_signal_rollback_and_no_cache_on_failure(database, monkeypatch):
    from v2_core.signals import Signals
    from v2_core.state import BusinessState

    processor, cache = lifecycle_processor(database)
    original = Signals.admit

    def failed(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("after signal insert")

    monkeypatch.setattr(Signals, "admit", failed)
    with pytest.raises(RuntimeError):
        processor.process(**lifecycle_frame())
    assert cache.records == []
    assert BusinessState(database).read(processor.key) is None
    with database() as conn:
        for table in (
            "v2_s3_frames",
            "v2_state_history",
            "v2_producer_batches",
            "v2_inbound_signals",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(Signals, "admit", original)
    assert (
        processor.process(**lifecycle_frame())["emitted_events"][0]["features"]["state"]
        == "ACTIVE"
    )


def test_s3_commit_ack_loss_replays_without_state_duplicate(database):
    processor, cache = lifecycle_processor(database)

    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        raise RuntimeError("commit ack lost")

    processor.publisher._connect = lost_ack
    with pytest.raises(RuntimeError):
        processor.process(**lifecycle_frame())
    assert not cache.records
    restarted, _ = lifecycle_processor(
        database, evaluate=lambda *args: pytest.fail("already committed")
    )
    result = restarted.process(**lifecycle_frame())
    assert len(result["signal_ids"]) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0] == 1


def test_s3_rejects_reused_identity_and_regressing_observation(database):
    from v2_core.signals import SignalConflict

    processor, _ = lifecycle_processor(database)
    processor.process(**lifecycle_frame())
    with pytest.raises(SignalConflict):
        processor.process(**lifecycle_frame(contexts={"BTCUSDT": {"strength": 80}}))
    for time in (99, 100):
        with pytest.raises(ValueError, match="monotonically"):
            processor.process(**lifecycle_frame(time, frame_id=f"another:{time}"))


def test_s3_missing_feed_then_explicit_removal_needs_no_market_or_strategy(database):
    from v2_core.scheduling import StrategyScheduler

    processor, _ = lifecycle_processor(database)
    processor.process(**lifecycle_frame())
    processor.publisher.clock_ms = lambda: 999
    assert (
        processor.process(**lifecycle_frame(999, contexts={}))["emitted_events"] == []
    )
    processor.publisher.clock_ms = lambda: 1000
    ended = processor.process(
        **lifecycle_frame(1000, contexts={}, removed_symbols=["BTCUSDT"])
    )
    assert ended["emitted_events"][0]["signal"] == "EVENT_END"
    worker, _ = strategy_worker(
        database,
        now=lambda: 1001,
        decide=lambda *args: pytest.fail("END must never evaluate"),
    )
    worker.source = "s3"
    result = StrategyScheduler(
        worker, context_provider=lambda _: pytest.fail("END/expired needs no context")
    ).run_once()
    assert result[ended["signal_ids"][0]] == "IGNORED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_s3_actual_detector_breakout_restart_transaction(database):
    from services.v2_s3_runtime import S3Runtime

    publisher, _ = producer(database)
    windows = {
        "BTCUSDT": {
            "15m": {"chg": 6, "vol_ratio": 2},
            "1h": {"chg": 9},
            "4h": {"chg": 3},
            "24h": {"chg": 6},
        }
    }
    raw = {
        "BTCUSDT": {
            "4h": [{"h": "100", "l": "90", "c": "95"}] * 3,
            "15m": [{"h": "101", "l": "99", "c": "100"}] * 3,
        }
    }
    first = S3Runtime(publisher).process(
        frame_id="real:100", observed_at=100, windows=windows, raw_windows=raw
    )
    assert "PULSE_UP" in {e["signal"] for e in first["emitted_events"]}
    second = S3Runtime(publisher).process(
        frame_id="real:101", observed_at=101, windows=windows, raw_windows=raw
    )
    assert [e["signal"] for e in second["emitted_events"]] == ["FAILED_BREAKOUT"]


def test_s3_cache_failure_repaired_without_new_signal_or_state(database):
    processor, cache = lifecycle_processor(database)

    def unavailable(_):
        raise ConnectionError("offline")

    put = cache.put
    cache.put = unavailable
    first = processor.process(**lifecycle_frame())
    assert first["market_status"] == "UNAVAILABLE"
    cache.put = put
    processor.publisher.clock_ms = lambda: 9999
    repaired = processor.process(**lifecycle_frame())
    assert repaired["market_status"] == "PROJECTED"
    assert repaired["signal_ids"] == first["signal_ids"]
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0] == 1


def test_s3_evaluator_failure_preserves_previous_committed_state(database):
    from v2_core.state import BusinessState

    processor, cache = lifecycle_processor(database)
    processor.process(**lifecycle_frame())
    before = BusinessState(database).read(processor.key)

    def failed(*args):
        raise ValueError("invalid frame")

    processor.evaluate = failed
    with pytest.raises(ValueError, match="invalid frame"):
        processor.process(**lifecycle_frame(101))
    assert BusinessState(database).read(processor.key) == before
    assert len(cache.records) == 1


def test_s3_changed_breakout_without_coverage_rejected(database):
    from v2_core.state import BusinessState

    processor, _ = lifecycle_processor(
        database, evaluate=lambda *args: ([], {"BTCUSDT": {"state": "IDLE"}})
    )
    processor.process(**lifecycle_frame())
    before = BusinessState(database).read(processor.key)
    processor.evaluate = lambda *args: ([], {})
    with pytest.raises(ValueError, match="uncovered"):
        processor.process(**lifecycle_frame(101, contexts={}))
    assert BusinessState(database).read(processor.key) == before


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


def test_closed_candle_runner_pg_restart_after_lost_ack(database):
    from types import SimpleNamespace

    from services.v2_s3_candles import S3CandleRunner
    from services.v2_s3_runtime import S3Runtime

    publisher, _ = producer(database, now=lambda: 86400001)
    batch = closed_candle_batch()
    source = SimpleNamespace(read=lambda: batch, ack=lambda _: False)
    first = S3CandleRunner(S3Runtime(publisher), source=source).run_once()
    assert first["stage"] == "ACK" and first["status"] == "RETRY"
    publisher.clock_ms = lambda: 99999999
    source.ack = lambda _: True
    replay = S3CandleRunner(S3Runtime(publisher), source=source).run_once()
    assert replay["status"] == "ACKNOWLEDGED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone()[0] == 1
    # Even an old unused open price must conflict under the same source identity.
    batch["candles"]["BTCUSDT"][-1]["o"] = "100.5"
    conflict = S3CandleRunner(S3Runtime(publisher), source=source).run_once()
    assert conflict == {
        "status": "RETRY",
        "stage": "PUBLISH",
        "error_code": "SignalConflict",
    }


@pytest.mark.parametrize("clock", [86399999, 86400100])
def test_new_closed_candle_frame_cannot_refresh_stale_or_future_observation(
    database, clock
):
    from services.v2_s3_candles import build_frame
    from services.v2_s3_runtime import S3Runtime

    publisher, cache = producer(database, now=lambda: clock)
    with pytest.raises(ValueError, match="stale or future"):
        S3Runtime(publisher).process(
            **build_frame(closed_candle_batch(), environment="SANDBOX")
        )
    assert not cache.records
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 0


def test_candle_to_real_pg_redis_strategy_preserves_input_provenance(database):
    from types import SimpleNamespace

    import redis

    from services.v2_s3_candles import S3CandleRunner
    from services.v2_s3_runtime import S3Runtime
    from v2_core.ingress import ContextProvider
    from v2_core.producer import RedisMarketContext

    socket = os.environ["V2_REDIS_TEST_SOCKET"]
    assert socket.startswith("/tmp/v2-data-qa.")
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    key = "v2:market:SANDBOX:s3:BTCUSDT"
    assert not client.exists(key)
    try:
        cache = RedisMarketContext(client, environment="SANDBOX")
        publisher, _ = producer(database, market=cache, now=lambda: 86400001)
        batch = closed_candle_batch()
        batch["candles"]["BTCUSDT"][0].update(v="100", tbv="60")
        acknowledgements = []

        def acknowledge(frame_id):
            acknowledgements.append(frame_id)
            return True

        source = SimpleNamespace(read=lambda: batch, ack=acknowledge)
        runner = S3CandleRunner(S3Runtime(publisher), source=source)
        result = runner.run_once()
        assert result["status"] == "ACKNOWLEDGED" and result["signal_ids"]
        provider = ContextProvider(
            environment="SANDBOX",
            policy={"s3": {"scope": "SYMBOL", "max_age_ms": 100}},
            read=cache.read,
            clock_ms=lambda: 86400001,
        )
        worker, runtime = strategy_worker(database, now=lambda: 86400001)
        worker.source = "s3"
        signal_id = result["signal_ids"][0]
        with database() as conn:
            snapshot = conn.execute(
                "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s",
                (signal_id,),
            ).fetchone()[0]
        assert (
            worker.consume(signal_id, context=provider(snapshot))["status"]
            == "PREPARED"
        )
        trace = runtime.data.trace(worker.decision(signal_id)["decision_id"])
        provenance = trace["decision"]["features"]["context"]["sources"]["s3"][
            "features"
        ]["input_evidence"]
        assert provenance["bar_count"] == 1440 and provenance["closed_at"] == 86400000
        assert len(provenance["bars_digest"]) == 64
        # Cache loss and redelivery rebuild projection without a second decision.
        client.delete(key)
        assert runner.run_once()["signal_ids"] == result["signal_ids"]
        assert client.exists(key)
        with database() as conn:
            assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 1
            assert (
                conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
                == 1
            )
    finally:
        client.delete(key)
        client.close()


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


def test_durable_source_concurrent_enqueue_claim_and_restart(database):
    from services.v2_s3_candles import S3CandleRunner
    from services.v2_s3_runtime import S3Runtime
    from services.v2_s3_source import DurableCandleSource

    source, _ = durable_source(database)
    batch = closed_candle_batch()
    with ThreadPoolExecutor(4) as pool:
        identities = list(pool.map(lambda _: source.enqueue(batch), range(4)))
    assert len(set(identities)) == 1
    competitor = DurableCandleSource(source.publisher, archive=source.archive)
    assert source.read() == batch
    assert competitor.read() is None
    assert source.ack(identities[0]) is False  # Source ack cannot precede S3 commit.
    expire_source_lease(database)
    result = S3CandleRunner(S3Runtime(source.publisher), source=competitor).run_once()
    assert result["status"] == "ACKNOWLEDGED"
    assert source.ack(identities[0]) is False  # Stale token cannot confirm new owner.
    assert competitor.read() is None
    with database() as conn:
        assert conn.execute(
            "SELECT status,attempts FROM v2_candle_deliveries"
        ).fetchone() == ("ACKNOWLEDGED", 2)
        assert conn.execute(
            "SELECT outcome FROM v2_candle_delivery_events"
        ).fetchall() == [("ACKNOWLEDGED",)]


def test_durable_source_expired_unpublished_is_audited_not_retimed(database):
    from services.v2_s3_source import SourceQuarantined

    source, cache = durable_source(database, now=lambda: 86400100)
    source.enqueue(closed_candle_batch())
    with pytest.raises(SourceQuarantined):
        source.read()
    assert source.read() is None and not cache.records
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_candle_delivery_events"
        ).fetchone() == ("EXPIRED_UNPUBLISHED",)
        assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 0


def test_durable_source_committed_expired_frame_still_recovers(database):
    from services.v2_s3_candles import S3CandleRunner, build_frame
    from services.v2_s3_runtime import S3Runtime

    source, _ = durable_source(database)
    source.enqueue(closed_candle_batch())
    batch = source.read()
    S3Runtime(source.publisher).process(**build_frame(batch, environment="SANDBOX"))
    expire_source_lease(database)
    source.publisher.clock_ms = lambda: 99999999
    result = S3CandleRunner(S3Runtime(source.publisher), source=source).run_once()
    assert result["status"] == "ACKNOWLEDGED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 1


def test_durable_source_archive_corruption_quarantines_with_no_publish(database):
    from services.v2_s3_source import SourceQuarantined

    source, cache = durable_source(database)
    source.enqueue(closed_candle_batch())
    for key in source.archive.rows:
        source.archive.rows[key] = "corrupted"
    with pytest.raises(SourceQuarantined):
        source.read()
    assert not cache.records
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_candle_delivery_events"
        ).fetchone() == ("ARCHIVE_CORRUPT",)


def test_durable_source_missing_archive_remains_retryable(database):
    source, _ = durable_source(database)
    source.enqueue(closed_candle_batch())
    saved = dict(source.archive.rows)
    source.archive.rows.clear()
    with pytest.raises(RuntimeError, match="unavailable"):
        source.read()
    with database() as conn:
        assert conn.execute("SELECT status FROM v2_candle_deliveries").fetchone() == (
            "PENDING",
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_candle_delivery_events").fetchone()[0]
            == 0
        )
    source.archive.rows.update(saved)
    expire_source_lease(database)
    assert source.read() == closed_candle_batch()


def test_durable_source_archive_failure_cannot_create_queue_row(database):
    source, _ = durable_source(database)
    source.archive.put = lambda *args: False
    with pytest.raises(RuntimeError):
        source.enqueue(closed_candle_batch())
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_candle_deliveries").fetchone()[0] == 0
        )


def test_durable_source_identity_and_audit_are_immutable(database):
    from v2_core.signals import SignalConflict

    source, _ = durable_source(database)
    source.enqueue(closed_candle_batch())
    changed = closed_candle_batch()
    changed["candles"]["BTCUSDT"][0]["o"] = "100.5"
    with pytest.raises(SignalConflict):
        source.enqueue(changed)
    import psycopg

    with pytest.raises(psycopg.Error), database() as conn:
        conn.execute("UPDATE v2_candle_deliveries SET input_digest='changed'")
    source.publisher.clock_ms = lambda: 86400100
    from services.v2_s3_source import SourceQuarantined

    with pytest.raises(SourceQuarantined):
        source.read()
    for sql in (
        "UPDATE v2_candle_deliveries SET status='PENDING'",
        "DELETE FROM v2_candle_delivery_events",
    ):
        with pytest.raises(psycopg.Error), database() as conn:
            conn.execute(sql)


def test_durable_source_transport_decode_failure_is_not_integrity_proof(database):
    source, _ = durable_source(database)
    source.enqueue(closed_candle_batch())

    def unavailable(_):
        raise ValueError("transport response decode failure")

    source.archive.get = unavailable
    with pytest.raises(ValueError):
        source.read()
    with database() as conn:
        assert conn.execute("SELECT status FROM v2_candle_deliveries").fetchone() == (
            "PENDING",
        )


def test_durable_source_fifo_and_superseded_frame_audit(database):
    from services.v2_s3_candles import build_frame
    from services.v2_s3_runtime import S3Runtime
    from services.v2_s3_source import DurableCandleSource, SourceQuarantined

    source, _ = durable_source(database, now=lambda: 86460001)
    source.publisher.lifetime_ms = 200000
    source.publisher.max_age_ms = 200000
    old = closed_candle_batch()
    newer = closed_candle_batch()
    newer["closed_at"] += 60000
    for bar in newer["candles"]["BTCUSDT"]:
        bar["t"] += 60000
    source.enqueue(newer)
    source.enqueue(old)
    assert source.read() == old
    other = DurableCandleSource(source.publisher, archive=source.archive)
    assert other.read() is None  # Never skip a leased head and publish a newer row.
    # An independent producer may already have advanced the durable lifecycle.
    S3Runtime(source.publisher).process(**build_frame(newer, environment="SANDBOX"))
    expire_source_lease(database)
    with pytest.raises(SourceQuarantined):
        other.read()
    assert other.read() == newer
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_candle_delivery_events"
        ).fetchall() == [("SUPERSEDED_UNPUBLISHED",)]


def test_durable_source_ack_commit_loss_does_not_redeliver_completed_work(database):
    from services.v2_s3_candles import build_frame
    from services.v2_s3_runtime import S3Runtime

    source, _ = durable_source(database)
    identity = source.enqueue(closed_candle_batch())
    batch = source.read()
    S3Runtime(source.publisher).process(**build_frame(batch, environment="SANDBOX"))

    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        raise RuntimeError("ack response lost")

    source.publisher._connect = lost_ack
    with pytest.raises(RuntimeError):
        source.ack(identity)
    source.publisher._connect = database
    assert source.read() is None
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_candle_delivery_events").fetchone()[0]
            == 1
        )


def test_durable_source_queue_commit_loss_can_be_reenqueued(database):
    source, _ = durable_source(database)
    calls = []

    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("enqueue response lost")

    source.publisher._connect = lost_ack
    with pytest.raises(RuntimeError):
        source.enqueue(closed_candle_batch())
    source.publisher._connect = database
    source.enqueue(closed_candle_batch())
    assert source.read() == closed_candle_batch()
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_candle_deliveries").fetchone()[0] == 1
        )


@pytest.mark.parametrize("enqueue_first", [True, False])
def test_durable_source_existing_published_identity_conflict_is_terminal(
    database, enqueue_first
):
    from services.v2_s3_candles import build_frame
    from services.v2_s3_runtime import S3Runtime
    from services.v2_s3_source import SourceQuarantined
    from v2_core.signals import SignalConflict

    source, _ = durable_source(database)
    batch = closed_candle_batch()
    if enqueue_first:
        source.enqueue(batch)
    altered = closed_candle_batch()
    altered["candles"]["BTCUSDT"][-1]["o"] = "100.5"
    S3Runtime(source.publisher).process(**build_frame(altered, environment="SANDBOX"))
    if enqueue_first:
        with pytest.raises(SourceQuarantined):
            source.read()
        assert source.read() is None
        with database() as conn:
            assert conn.execute(
                "SELECT outcome FROM v2_candle_delivery_events"
            ).fetchone() == ("PUBLISHED_INPUT_CONFLICT",)
    else:
        with pytest.raises(SignalConflict):
            source.enqueue(batch)
        with database() as conn:
            assert (
                conn.execute("SELECT count(*) FROM v2_candle_deliveries").fetchone()[0]
                == 0
            )


def test_source_pending_capacity_is_atomic_and_replays_do_not_consume_capacity(
    database,
):
    from market_fakes import candle_batch

    from services.v2_s3_source import SourceBackpressure

    source, _ = durable_source(database)
    source.max_pending = 1

    def enqueue(offset):
        try:
            return source.enqueue(candle_batch(offset))
        except SourceBackpressure:
            return "FULL"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(enqueue, [0, 60000]))
    assert results.count("FULL") == 1
    accepted = 0 if results[0] != "FULL" else 60000
    assert source.enqueue(candle_batch(accepted)) == results[accepted // 60000]
    assert source.has_pending() and source.has_frame(results[accepted // 60000])
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_candle_deliveries").fetchone()[0] == 1
        )


def test_public_rate_budget_concurrent_reservations_and_restart_cooldown(database):
    from v2_core.public_market import PublicRateBudget

    budget = PublicRateBudget(database, scope="test-egress", limit=20)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: budget.permit(10), range(8)))
    assert results.count(True) == 2
    with database() as conn:
        conn.execute("UPDATE v2_public_market_budgets SET window_id=0")
    assert budget.permit(1)
    budget.penalize(60)
    assert not PublicRateBudget(database, scope="test-egress", limit=20).permit(1)
    with pytest.raises(ValueError, match="configuration"):
        PublicRateBudget(database, scope="test-egress", limit=21).permit(1)


def test_public_quota_pg_commit_failure_prevents_http(database):
    from market_fakes import MarketHTTP

    from v2_core.public_market import BinancePublicMarket, PublicRateBudget

    @contextmanager
    def lost_commit():
        with database() as conn:
            yield conn
            raise RuntimeError("PG failure")

    http = MarketHTTP()
    client = BinancePublicMarket(
        environment="SANDBOX",
        budget=PublicRateBudget(lost_commit, scope="qa-ip", limit=20),
        enabled=True,
        connection_factory=http,
    )
    with pytest.raises(RuntimeError):
        client("/fapi/v1/time", {})
    assert http.calls == []


def test_market_pipeline_factory_collects_archives_publishes_acknowledges_and_skips_same_frame(
    database,
):
    import redis
    from market_fakes import ArchiveClient, MarketHTTP

    from services.v2_market_pipeline import create_market_pipeline

    socket = os.environ["V2_REDIS_TEST_SOCKET"]
    assert socket.startswith("/tmp/v2-data-qa.")
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    key = "v2:market:SANDBOX:s3:BTCUSDT"
    assert not client.exists(key)
    try:
        http, archive, alerts = MarketHTTP(), ArchiveClient(), []
        config = {
            "environment": "SANDBOX",
            "symbols": ["BTCUSDT"],
            "egress_scope": "factory-egress",
            "weight_limit": 100,
            "max_pending": 2,
            "max_age_ms": 90000,
            "lifetime_ms": 120000,
            "enabled": True,
        }
        factory = lambda: create_market_pipeline(
            database,
            clickhouse=archive,
            redis_client=client,
            config=config,
            notify=lambda alert: alerts.append(alert) or True,
            clock_ms=lambda: 86402001,
            monotonic_ms=lambda: 0,
            http_connection_factory=http,
        )
        service = factory()
        result = service.run_once()
        assert result["status"] == "ACKNOWLEDGED" and result["signal_ids"]
        assert client.exists(key) and alerts == []
        assert len(archive.tables["v2_candle_archive"]) == 24
        assert factory().run_once() == {
            "status": "CURRENT",
            "alert_delivery": {
                "claimed": 0,
                "delivered": 0,
                "failed": 0,
                "superseded": 0,
            },
        }
        assert len(http.calls) == 3  # time + klines, then time only after restart.
        with database() as conn:
            assert conn.execute(
                "SELECT status FROM v2_candle_deliveries"
            ).fetchone() == ("ACKNOWLEDGED",)
            assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 1
            assert (
                conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
            )
    finally:
        client.delete(key)
        client.close()


def test_market_supervisor_recovers_published_frame_without_fetching_new_candles(
    database,
):
    from types import SimpleNamespace

    from services.v2_market_pipeline import MarketSupervisor
    from services.v2_s3_candles import build_frame
    from services.v2_s3_runtime import S3Runtime

    source, cache = durable_source(database)
    source.enqueue(closed_candle_batch())
    batch = source.read()
    runtime = S3Runtime(source.publisher)
    runtime.process(**build_frame(batch, environment="SANDBOX"))
    expire_source_lease(database)
    source.publisher.clock_ms = lambda: 99999999
    alerts = []
    collector = SimpleNamespace(environment="SANDBOX", collect=lambda **kwargs: None)
    service = MarketSupervisor(
        source=source,
        runtime=runtime,
        collector=collector,
        notify=lambda alert: alerts.append(alert) or True,
    )
    assert service.run_once() == {"status": "CURRENT"}
    assert len(cache.records) == 2 and alerts == []
    with database() as conn:
        assert conn.execute("SELECT status FROM v2_candle_deliveries").fetchone() == (
            "ACKNOWLEDGED",
        )


def test_rate_budget_clock_regression_cannot_refresh_capacity(database):
    from v2_core.public_market import PublicRateBudget

    budget = PublicRateBudget(database, scope="clock-guard", limit=10)
    assert budget.permit(10)
    with database() as conn:
        conn.execute("UPDATE v2_public_market_budgets SET window_id=window_id+10")
    assert not budget.permit(1)


@pytest.mark.parametrize("policy", ["lifetime_ms", "max_age_ms"])
def test_unpublished_source_policy_change_cannot_extend_old_frame_life(
    database, policy
):
    from services.v2_s3_source import SourceQuarantined

    source, _ = durable_source(database)
    source.enqueue(closed_candle_batch())
    setattr(source.publisher, policy, 500)
    with pytest.raises(SourceQuarantined):
        source.read()
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_candle_delivery_events"
        ).fetchone() == ("POLICY_CHANGED_UNPUBLISHED",)
        assert conn.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 0


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


def test_producer_concurrent_replay_commits_one_batch_and_no_duplicate_signals(
    database,
):
    publisher, cache = producer(database)
    with ThreadPoolExecutor(4) as pool:
        results = list(
            pool.map(lambda _: publisher.publish(**producer_frame()), range(4))
        )
    assert all(result == results[0] for result in results)
    assert results[0]["market_status"] == "PROJECTED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_producer_batches").fetchone() == (
            1,
        )
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            2,
        )
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
    assert len(cache.records) == 8


def test_producer_bad_second_event_rolls_back_entire_batch_before_market_write(
    database,
):
    publisher, cache = producer(database)
    frame = producer_frame()
    frame["events"][1]["signal"] = "invalid signal"
    with pytest.raises(ValueError):
        publisher.publish(**frame)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_producer_batches").fetchone() == (
            0,
        )
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            0,
        )
    assert cache.records == []


def test_producer_reused_event_id_rolls_back_new_events_and_frame(database):
    publisher, cache = producer(database)
    publisher.publish(**producer_frame())
    later = producer_frame(frame_id="s3:later", observed_at=110)
    later["events"][0]["event_id"] = "brand-new-id"
    with pytest.raises(ValueError, match="conflict"):
        publisher.publish(**later)
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_producer_batches").fetchone() == (
            1,
        )
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            2,
        )
        assert (
            conn.execute(
                "SELECT 1 FROM v2_inbound_signals WHERE request_key='brand-new-id'"
            ).fetchone()
            is None
        )
    assert len(cache.records) == 2


def test_producer_frame_conflict_does_not_overwrite_cache_or_receipt(database):
    publisher, cache = producer(database)
    publisher.publish(**producer_frame())
    with pytest.raises(ValueError, match="conflict"):
        publisher.publish(
            **producer_frame(
                contexts={"BTCUSDT": {"price": "999"}, "ETHUSDT": {"price": "200"}}
            )
        )
    assert len(cache.records) == 2
    with pytest.raises(Exception, match="immutable"), database() as conn:
        conn.execute("UPDATE v2_producer_batches SET observed_at_ms=200")


def test_producer_failed_cache_replay_keeps_signal_identity_and_original_expiry(
    database,
):
    class Failing:
        def put(self, _):
            raise ConnectionError("sensitive Redis detail")

    publisher, _ = producer(database, market=Failing())
    first = publisher.publish(**producer_frame())
    assert first["market_status"] == "UNAVAILABLE"
    assert first["market_errors"] == {
        "BTCUSDT": "ConnectionError",
        "ETHUSDT": "ConnectionError",
    }
    restarted, _ = producer(database, now=lambda: 999)
    repaired = restarted.publish(**producer_frame())
    assert repaired["signal_ids"] == first["signal_ids"]
    assert repaired["market_status"] == "PROJECTED"
    with database() as conn:
        assert conn.execute(
            "SELECT DISTINCT snapshot->>'expires_at_ms' FROM v2_inbound_signals"
        ).fetchall() == [("200",)]
    with pytest.raises(ValueError, match="stale"):
        restarted.publish(**producer_frame(frame_id="new-after-expiry"))


@pytest.mark.parametrize("commit_succeeded", [True, False])
def test_producer_commit_fault_has_no_cache_side_effect_and_retries_same_batch(
    database, commit_succeeded
):
    @contextmanager
    def interrupted():
        with database() as conn:
            yield conn
            if not commit_succeeded:
                raise ConnectionError("rollback transaction")
        raise ConnectionError("commit acknowledgement lost")

    publisher, cache = producer(interrupted)
    with pytest.raises(ConnectionError):
        publisher.publish(**producer_frame())
    assert cache.records == []
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_producer_batches").fetchone() == (
            int(commit_succeeded),
        )
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            2 * int(commit_succeeded),
        )
    restarted, _ = producer(database)
    assert restarted.publish(**producer_frame())["market_status"] == "PROJECTED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            2,
        )


def test_s0_publisher_is_context_only_and_records_empty_batch(database):
    from services.v2_market_publishers import S0Publisher

    publisher, cache = producer(database, source="s0")
    S0Publisher(publisher).publish_state(
        {"regime": "range", "breadth": 0.52}, frame_id="s0:1", observed_at=100
    )
    assert cache.records[0]["symbol"] == "*"
    assert cache.records[0]["features"]["breadth"] == "0.52"
    with database() as conn:
        assert conn.execute(
            "SELECT signal_ids FROM v2_producer_batches"
        ).fetchone() == ([],)
        assert conn.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (
            0,
        )


def test_s3_adapter_through_pg_redis_context_and_strategy(database):
    import redis

    from services.v2_market_publishers import S3Publisher
    from v2_core.ingress import ContextProvider, IntakeRejected
    from v2_core.producer import RedisMarketContext, market_envelope
    from v2_core.scheduling import StrategyScheduler

    socket = os.environ["V2_REDIS_TEST_SOCKET"]
    assert socket.startswith("/tmp/v2-data-qa.")
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    cache = RedisMarketContext(client, environment="SANDBOX")
    key = "v2:market:SANDBOX:s3:BTCUSDT"
    assert not client.exists(key)
    try:
        publisher, _ = producer(database, market=cache)
        result = S3Publisher(publisher).publish_frame(
            frame_id="s3:100",
            observed_at=100,
            windows={"BTCUSDT": {"1h": {"ema20": 99.0, "atr": 1.0}}},
            events=[
                {
                    "event_id": "s3:btc:100:active",
                    "symbol": "BTCUSDT",
                    "type": "TREND_UP",
                    "state": "ACTIVE",
                    "strength": 70,
                }
            ],
        )
        signal_id = result["signal_ids"][0]
        provider = ContextProvider(
            environment="SANDBOX",
            policy={"s3": {"scope": "SYMBOL", "max_age_ms": 100}},
            read=cache.read,
            clock_ms=lambda: 120,
        )
        worker, runtime = strategy_worker(database, now=lambda: 120)
        worker.source = "s3"  # New test worker binding before any use.
        assert StrategyScheduler(worker, context_provider=provider).run_once() == {
            signal_id: "PREPARED"
        }
        trace = runtime.data.trace(worker.decision(signal_id)["decision_id"])
        evidence = trace["decision"]["features"]["context"]["sources"]["s3"]
        assert evidence["features"]["1h"]["ema20"] == "99.0"
        assert (
            trace["signal"]["snapshot"]["features"]["producer_context"]["snapshot_id"]
            == evidence["snapshot_id"]
        )
        # Replayed old frames cannot overwrite newer observations.
        newer = market_envelope("s3", "SANDBOX", "BTCUSDT", 110, {"price": "102"})
        assert cache.put(newer)
        assert cache.put(evidence)
        assert cache.read("s3", "SANDBOX", "BTCUSDT") == newer
        assert not cache.put(
            market_envelope("s3", "SANDBOX", "BTCUSDT", 110, {"price": "999"})
        )
        # New signal's source context cannot be replaced by older but fresh cache.
        newer_reference = {k: v for k, v in newer.items() if k != "features"}
        client.delete(key)
        assert cache.put(evidence)
        with pytest.raises(IntakeRejected, match="PRODUCER_CONTEXT_UNAVAILABLE"):
            provider(
                {"symbol": "BTCUSDT", "features": {"producer_context": newer_reference}}
            )
        client.delete(key)
        with pytest.raises(IntakeRejected, match="MISSING_CONTEXT"):
            provider({"symbol": "BTCUSDT"})
        # Lost cache is repairable by original producer frame (not business file).
        assert (
            publisher.publish(
                frame_id="repair",
                observed_at=110,
                contexts={"BTCUSDT": {"price": "102"}},
                events=[],
            )["market_status"]
            == "PROJECTED"
        )
        with pytest.raises(ValueError, match="binding"):
            cache.read("s3", "LIVE", "BTCUSDT")
        client.hset(key, "payload", '{"bad":"shape"}')
        with pytest.raises(ValueError, match="cached context"):
            cache.read("s3", "SANDBOX", "BTCUSDT")
    finally:
        client.delete(key)
        client.close()


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
        assert conn.execute("SHOW TimeZone").fetchone() == ("Asia/Shanghai",)
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
    from v2_core.binance_income import BinanceIncomeImporter

    importer = BinanceIncomeImporter(
        database,
        lambda *_: [income_row()],
        account_id="test-account",
        environment="SANDBOX",
        clock_ms=lambda: 1000,
    )
    imported = importer.import_window(start_ms=100, end_ms=200)
    pending_income = service.income.pending(income_scope())
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
        assert TradingData(restored).income.pending(income_scope()) == pending_income
        with restored() as conn:
            assert conn.execute(
                "SELECT status,row_count FROM v2_income_imports WHERE run_id=%s",
                (imported["run_id"],),
            ).fetchone() == ("FETCHED", 1)
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
