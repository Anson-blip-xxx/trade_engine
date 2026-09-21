import json
import os
import time
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture
from test_v2_venue_readiness import Venue

from services.v2_directional_admission import create_directional_admission_scheduler
from services.v2_directional_context import DirectionalContext
from services.v2_directional_execution import bind_directional_venue_gate
from services.v2_s3_runtime import S3Runtime
from services.v2_trading_pipeline import TradingPipeline
from v2_core.ingress import ContextProvider
from v2_core.producer import ProducerPublisher, market_envelope
from v2_core.runtime import DataRuntime
from v2_core.state import BusinessState, StateKey

database = database_fixture


@pytest.fixture
def case(database):
    now = time.time_ns() // 1000000
    calls = []
    envelopes = {
        ("s0", "SANDBOX", "*"): market_envelope(
            "s0", "SANDBOX", "*", now, {"regime": "neutral"}
        )
    }

    class Cache:
        def put(self, envelope):
            envelopes[
                (envelope["source"], envelope["environment"], envelope["symbol"])
            ] = deepcopy(envelope)
            return True

    publisher = ProducerPublisher(
        database,
        source="s3",
        environment="SANDBOX",
        market=Cache(),
        clock_ms=lambda: now,
        max_age_ms=60000,
        lifetime_ms=60000,
    )
    s3 = S3Runtime(publisher)
    frames = inputs()["market"]
    frames["15m"].update(close="100", chg="0.5", vol_ratio="1")
    frames["1h"].update(chg="4")
    frames["4h"].update(chg="3")
    raw = {
        "BTCUSDT": {
            "4h": [{"h": "110", "l": "90", "c": "100"}] * 3,
            "15m": [{"h": "101", "l": "99", "c": "100"}] * 3,
        }
    }

    class Market:
        source = SimpleNamespace(publisher=publisher)
        fail = False

        def run_once(self):
            calls.append("market")
            if self.fail:
                raise TimeoutError("secret-url")
            result = s3.process(
                frame_id="qa-frame",
                observed_at=now,
                windows={"BTCUSDT": frames},
                raw_windows=raw,
            )
            assert result["market_status"] == "PROJECTED"
            return {"status": "ACKNOWLEDGED", "signal_ids": result["signal_ids"]}

    market = Market()
    base = ContextProvider(
        environment="SANDBOX",
        policy={
            "s0": {"scope": "GLOBAL", "max_age_ms": 60000},
            "s3": {"scope": "SYMBOL", "max_age_ms": 90000},
        },
        read=lambda *key: envelopes.get(key),
        clock_ms=lambda: now,
    )
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    account = {
        "account_scope": asdict(SCOPE),
        "symbol": "BTCUSDT",
        "observed_at_ms": now,
        "valid_until_ms": now + 60000,
        "evidence": {"source": "explicit-QA-account"},
        "short_ratio": "0.5",
        "history": history(),
        "sizing": limits,
        "expected_move_pct": "8",
        "funding_rate": "0",
    }
    context = DirectionalContext(
        base, lambda _: account, scope=SCOPE, clock_ms=lambda: now
    )
    runtime = DataRuntime(
        database,
        scope=SCOPE,
        clock_ms=lambda: now,
        submit=lambda _: pytest.fail("entry dispatch disabled in this integration"),
        query=lambda _: None,
        risk_check=lambda _: False,
        risk_reference=lambda _: pytest.fail("no order submission"),
    )
    bind_directional_venue_gate(runtime, Venue(), mark_reference=lambda _: None)
    schedulers = [
        create_directional_admission_scheduler(
            runtime,
            context_provider=context,
            strategy=strategy,
            analysis_mode="hard",
            max_delay_ms=30000,
            enable_admission=True,
        )
        for strategy in ("S6", "S8")
    ]

    class Stage:
        scope = SCOPE
        status = "CLEAR"

        def __init__(self, name):
            self.name = name

        def run_once(self):
            calls.append(self.name)
            return {"status": self.status}

    pipeline = TradingPipeline(
        runtime=runtime,
        market=market,
        schedulers=schedulers,
        protection=Stage("protection"),
        exits=Stage("exits"),
        settlement=Stage("settlement"),
    )
    return pipeline, calls, account, envelopes, now


def test_real_s3_detection_through_context_strategy_and_pg_order(case, database):
    pipeline, calls, _, _, _ = case
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE"
    assert calls == ["protection", "exits", "settlement", "market"]
    assert result["phases"]["s6"] and "PREPARED" in result["phases"]["s6"].values()
    assert result["entries"] == {}
    with database() as conn:
        rows = conn.execute("""SELECT s.snapshot->>'signal',i.producer,o.status,e.snapshot->'features'->'context'
            FROM v2_inbound_signals s JOIN v2_trade_intents i USING(signal_id)
            JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_decision_evidence e USING(evidence_ref)""").fetchall()
    assert any(row[:3] == ("TREND_UP", "s6", "PREPARED") for row in rows)
    assert all(row[3]["sources"]["s3"]["features"]["1h"]["chg"] == "4" for row in rows)
    key = StateKey(
        **asdict(SCOPE), namespace="trading-pipeline-cycle-v1", key=result["cycle_id"]
    )
    assert json.loads(BusinessState(database).read(key).payload_json) == result
    pipeline.run_once()
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == len(rows)


@pytest.mark.parametrize("stage", ["protection", "exits", "settlement"])
def test_incomplete_lifecycle_stage_prevents_new_decisions(case, database, stage):
    pipeline, calls, _, _, _ = case
    getattr(pipeline, stage).status = "PENDING"
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert calls == ["protection", "exits", "settlement", "market"]
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
            == 0
        )


def test_market_outage_never_skips_position_management(case):
    pipeline, calls, _, _, _ = case
    pipeline.market.fail = True
    result = pipeline.run_once()
    assert calls == ["protection", "exits", "settlement", "market"]
    assert result["status"] == "ENTRY_BLOCKED"
    assert "secret-url" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure", ["missing_s0", "stale_account", "wrong_account", "missing_history"]
)
def test_missing_context_is_not_replaced_with_neutral_defaults(case, database, failure):
    pipeline, _, account, envelopes, now = case
    if failure == "missing_s0":
        del envelopes[("s0", "SANDBOX", "*")]
    elif failure == "stale_account":
        account["valid_until_ms"] = now
    elif failure == "wrong_account":
        account["account_scope"]["account_id"] = "other"
    else:
        del account["history"]
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_pg_cycle_registration_failure_launches_no_stages(case, monkeypatch):
    pipeline, calls, _, _, _ = case

    def fail(*_, **__):
        raise ConnectionError("database down")

    monkeypatch.setattr(BusinessState, "change", fail)
    with pytest.raises(ConnectionError):
        pipeline.run_once()
    assert not calls


def test_pipeline_replica_lock_skips_duplicate_cycle(case, database):
    pipeline, calls, _, _, _ = case
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("v2-pipeline:" + SCOPE.key,),
        )
        conn.commit()
        assert pipeline.run_once() == {"status": "BUSY"}
    assert not calls


def test_public_collection_to_durable_source_to_actual_strategy_decisions(
    case, database
):
    import redis
    from market_fakes import ArchiveClient, MarketHTTP

    from services.v2_market_pipeline import create_market_pipeline
    from v2_core.producer import RedisMarketContext

    pipeline, _, _, envelopes, now = case
    socket = os.environ["V2_REDIS_TEST_SOCKET"]
    assert socket.startswith("/tmp/v2-data-qa.")
    client = redis.Redis(unix_socket_path=socket, decode_responses=True)
    key = "v2:market:SANDBOX:s3:BTCUSDT"
    assert not client.exists(key)
    archive, http = ArchiveClient(), MarketHTTP(server_time=now)
    try:
        pipeline.market = create_market_pipeline(
            database,
            clickhouse=archive,
            redis_client=client,
            config={
                "environment": "SANDBOX",
                "symbols": ["BTCUSDT"],
                "egress_scope": "pipeline-qa",
                "weight_limit": 100,
                "max_pending": 2,
                "max_age_ms": 90000,
                "lifetime_ms": 120000,
                "enabled": True,
            },
            notify=lambda _: True,
            clock_ms=lambda: now,
            monotonic_ms=lambda: 0,
            http_connection_factory=http,
        )
        cache = RedisMarketContext(client, environment="SANDBOX")
        for scheduler in pipeline.schedulers:
            scheduler.context_provider.market.read = lambda *args: (
                envelopes.get(args) if args[0] == "s0" else cache.read(*args)
            )
        result = pipeline.run_once()
        assert result["phases"]["market"]["status"] == "ACKNOWLEDGED"
        assert len(http.calls) == 2
        assert archive.tables["v2_candle_archive"]
        with database() as conn:
            assert conn.execute(
                "SELECT status FROM v2_candle_deliveries"
            ).fetchone() == ("ACKNOWLEDGED",)
            signals = conn.execute(
                "SELECT count(*) FROM v2_inbound_signals"
            ).fetchone()[0]
            decisions = conn.execute(
                "SELECT count(*) FROM v2_strategy_decisions"
            ).fetchone()[0]
        assert signals > 0 and decisions == 2 * signals
        assert (
            result["entries"] == {}
        )  # Real strategies may reject; never force a trade.
    finally:
        client.delete(key)
        client.close()


@pytest.mark.parametrize("outcome", ["UNKNOWN", "FILLED", "timeout"])
def test_possible_submission_immediately_recovers_and_protects(
    case, monkeypatch, outcome
):
    pipeline, calls, _, _, _ = case
    pipeline.enable_entries = True
    execution = pipeline.runtime.execution

    def dispatch(order_id):
        calls.append("dispatch")
        if outcome == "timeout":
            raise TimeoutError("private transport")
        return outcome

    monkeypatch.setattr(execution, "dispatch", dispatch)
    monkeypatch.setattr(
        execution, "recover", lambda _: calls.append("recover") or "UNKNOWN"
    )
    result = pipeline.run_once()
    assert calls[-3:] == ["dispatch", "recover", "protection"]
    assert calls.count("dispatch") == 1
    assert result["status"] == "ENTRY_BLOCKED"
    assert "private transport" not in json.dumps(result)


def test_pipeline_uses_actual_protection_stage_and_preserves_prepared_orders(
    case, database
):
    from test_v2_directional_lifecycle import Venue, stage

    pipeline, _, _, _, _ = case
    venue = Venue()
    pipeline.protection = stage(database, venue, allow_writes=True)
    for _ in range(2):
        result = pipeline.run_once()
        assert result["status"] == "CYCLE_COMPLETE", result
        assert (
            result["phases"]["protection"]["coverage"]["status"]
            == "ACCOUNT_COVERAGE_CLEAR"
        )
    assert not venue.writes
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
            ).fetchone()[0]
            > 0
        )


def test_same_cycle_s3_strategy_injected_fill_and_actual_stop_install(
    case, database, monkeypatch
):
    from test_v2_directional_lifecycle import Venue, stage

    pipeline, _, _, _, _ = case
    venue = Venue()
    pipeline.protection = stage(database, venue, allow_writes=True)
    pipeline.enable_entries = True
    runtime = pipeline.runtime

    def confirmed_qa_fill(order_id):
        # Only the exchange opening is injected; this is not online acceptance.
        order = runtime.execution.snapshot(order_id)
        quantity = order["quantity"]
        runtime.data.orders.transition(
            order_id,
            expected_version=order["version"],
            status="SUBMITTING",
            evidence={},
        )
        runtime.data.ledger.record_fill(
            order_id=order_id,
            exchange_fill_id="1",
            quantity=quantity,
            price="100",
            fee="0.05",
            fee_currency="USDT",
            occurred_at_ms=runtime._now(),
            evidence={"source": "explicit-QA-fill"},
        )
        runtime.data.orders.transition(
            order_id,
            expected_version=order["version"] + 1,
            status="FILLED",
            exchange_order_id="10",
            evidence={"fills_complete": True},
        )
        venue.rows["/fapi/v3/positionRisk"] = [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": quantity}
        ]
        return "FILLED"

    monkeypatch.setattr(runtime.execution, "dispatch", confirmed_qa_fill)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert result["phases"]["after_dispatch_recovery"] == "FILLED"
    assert result["phases"]["after_dispatch_protection"]["status"] == "CLEAR"
    assert len(result["entries"]) == len(venue.writes) == 1
    assert venue.writes[0]["triggerPrice"] == "92"
