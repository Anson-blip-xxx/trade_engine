"""Shadow admission integration on isolated PG and fake venue transports only.

An unresolved or unavailable research observation must leave the original
strategy decision, dispatch risk controls, native stop and settlement intact.
These tests do not measure provider latency or claim model coverage.
"""

import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import pytest
from market_fakes import ArchiveClient, MarketHTTP
from test_v2_account_inventory import TelegramConnection, sink
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario, worker
from test_v2_jev_fullflow import SimulatedVenue
from test_v2_testnet_daemon_entry import settings
from test_v2_trading_pipeline import case as pipeline_fixture

from services.v2_directional_admission import admission_decision
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_testnet_daemon_entry import (
    TestnetProcessConfig,
    create_testnet_process,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy, RiskReference
from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical, digest
from v2_core.runner import RiskVerdict
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness

database = database_fixture
pipeline_case = pipeline_fixture


def queued_receipt(request):
    return {
        "schema": "jev-shadow-receipt-v1",
        "signal_id": request["signal_id"],
        "consumer": request["consumer"],
        "candidate_digest": digest(canonical(asdict(request["decision"]))),
        "status": "SUBMITTED_NOT_DURABLE",
        "coverage": "MISSING_UNTIL_DURABLE_RESULT",
        "decision_effect": "NONE",
        "admission_authorized": False,
        "execution_authorized": False,
        "risk_override": False,
    }


class Observer:
    def __init__(self, respond=queued_receipt):
        self.requests, self.respond = [], respond

    def submit(self, **request):
        self.requests.append(deepcopy(request))
        return self.respond(request)

    def assess(self, *_args, **_kwargs):
        pytest.fail("shadow observations cannot become an admission gate")


@pytest.mark.parametrize("strategy,side", [("S6", "BUY"), ("S8", "SELL")])
def test_unresolved_shadow_preserves_original_order_and_deadline(
    database, strategy, side
):
    _, runtime, signal_id, context = scenario(database, strategy)
    observer = Observer()
    admission = formal(runtime, strategy, entry_observer=observer)
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    original_context = deepcopy(context)
    baseline = admission_decision(signal, context, json.loads(admission.config_json))
    assert baseline.action == "OPEN"

    result = admission.consume(signal_id, context=context)
    assert result["status"] == "PREPARED"
    saved = admission.decision(signal_id)
    evaluation = saved["snapshot"]["features"]["evaluation"]
    receipt = evaluation.pop("jev_shadow")
    assert evaluation == json.loads(baseline.features_json)
    assert saved["snapshot"]["result"] == {
        "action": "OPEN",
        "side": side,
        "quantity": baseline.quantity,
    }
    assert saved["snapshot"]["expires_at_ms"] == 8
    assert saved["snapshot"]["features"]["context"] == context == original_context
    assert saved["config"]["jev_mode"] == "SHADOW"
    assert receipt == queued_receipt(observer.requests[0])
    assert receipt["coverage"] == "MISSING_UNTIL_DURABLE_RESULT"
    assert "jev_entry_gate" not in evaluation
    assert observer.requests[0]["decision"] == baseline
    assert len(observer.requests) == 1
    with database() as conn:
        assert conn.execute("SELECT status,quantity FROM v2_orders").fetchone() == (
            "PREPARED",
            Decimal(baseline.quantity),
        )


@pytest.mark.parametrize(
    "failure",
    [
        "throws",
        "none",
        "signal",
        "consumer",
        "digest",
        "authorize",
        "admission_authorize",
        "boolean_type",
        "risk_override",
        "decision_effect",
        "coverage",
        "oversize",
        "credentials",
    ],
)
def test_bad_shadow_receipt_records_missing_coverage_without_veto(database, failure):
    _, runtime, signal_id, context = scenario(database)

    def respond(request):
        if failure == "throws":
            raise RuntimeError("provider secret must not enter decision evidence")
        if failure == "none":
            return None
        receipt = queued_receipt(request)
        changes = {
            "signal": {"signal_id": "wrong-signal"},
            "consumer": {"consumer": "other-account-consumer"},
            "digest": {"candidate_digest": "0" * 64},
            "authorize": {"execution_authorized": True},
            "admission_authorize": {"admission_authorized": True},
            "boolean_type": {"execution_authorized": 0},
            "risk_override": {"risk_override": True},
            "decision_effect": {"decision_effect": "VETO"},
            "coverage": {"coverage": "COMPLETE"},
            "oversize": {"detail": "x" * 16384},
            "credentials": {"authorization": "must-not-be-recorded"},
        }
        return {**receipt, **changes[failure]}

    observer = Observer(respond)
    admission = formal(runtime, entry_observer=observer)
    assert admission.consume(signal_id, context=context)["status"] == "PREPARED"
    saved = admission.decision(signal_id)["snapshot"]
    receipt = saved["features"]["evaluation"]["jev_shadow"]
    assert receipt == {
        **queued_receipt(observer.requests[0]),
        "status": "OBSERVER_UNAVAILABLE",
        "coverage": "MISSING",
    }
    assert saved["result"] == {"action": "OPEN", "side": "BUY", "quantity": "1.25"}
    assert saved["expires_at_ms"] == 8
    assert "secret" not in canonical(saved)
    assert "must-not-be-recorded" not in canonical(saved)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("history", "LOW_QUALITY"),
        ("margin", "INSUFFICIENT_BUDGET"),
        ("funding", "ADVERSE_FUNDING"),
        ("rr", "REWARD_RISK_TOO_LOW"),
    ],
)
def test_original_risk_rejection_never_queues_shadow(database, failure, reason):
    _, runtime, signal_id, context = scenario(database)
    facts = context["directional"]
    if failure == "history":
        facts["history"].update(win_rate="20", avg_quality_score="20")
    elif failure == "margin":
        facts["sizing"]["available_margin"] = "0"
    elif failure == "funding":
        facts["funding_rate"] = "0.002"
    else:
        facts["expected_move_pct"] = "1"
    observer = Observer()
    admission = formal(runtime, entry_observer=observer)
    result = admission.consume(signal_id, context=context)
    assert result["status"] == "IGNORED" and result["reason"] == reason
    assert observer.requests == []
    assert (
        "jev_shadow"
        not in admission.decision(signal_id)["snapshot"]["features"]["evaluation"]
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_restart_reuses_original_shadow_receipt_without_resubmission(database):
    _, runtime, signal_id, context = scenario(database)
    observer = Observer()
    admission = formal(runtime, entry_observer=observer)
    first = admission.consume(signal_id, context=context)
    frozen = admission.decision(signal_id)
    _, restarted_runtime = worker(database, now=4)
    restarted_observer = Observer(lambda _: pytest.fail("committed shadow resubmitted"))
    restarted = formal(restarted_runtime, entry_observer=restarted_observer)
    restarted.decide = lambda *_: pytest.fail("committed decision reevaluated")
    assert restarted.consume(signal_id, context={})["order_id"] == first["order_id"]
    assert restarted.decision(signal_id) == frozen
    assert len(observer.requests) == 1 and restarted_observer.requests == []


def test_observer_receives_disposable_copies_of_original_evidence(database):
    _, runtime, signal_id, context = scenario(database)
    original = deepcopy(context)

    def change(request):
        receipt = queued_receipt(request)
        request["context"]["directional"]["sizing"]["available_margin"] = "0"
        request["context"]["valid_until_ms"] = 1
        request["signal"]["symbol"] = "OTHERUSDT"
        request["config"]["account_scope"]["account_id"] = "other-account"
        return receipt

    admission = formal(runtime, entry_observer=Observer(change))
    assert admission.consume(signal_id, context=context)["status"] == "PREPARED"
    saved = admission.decision(signal_id)
    assert saved["config"]["account_scope"] == asdict(SCOPE)
    assert saved["snapshot"]["symbol"] == "BTCUSDT"
    assert saved["snapshot"]["expires_at_ms"] == 8
    assert saved["snapshot"]["features"]["context"] == context == original
    assert saved["snapshot"]["result"]["quantity"] == "1.25"


def test_shadow_cannot_replace_dispatch_account_risk(database):
    _, runtime, signal_id, context = scenario(database)
    admission = formal(runtime, entry_observer=Observer())
    result = admission.consume(signal_id, context=context)
    runtime.risk_check = lambda _: RiskVerdict(False, "ACCOUNT_COVERAGE_BLOCKED")
    assert runtime.execution.dispatch(result["order_id"]) == "DENIED"


def test_shadow_and_admission_gate_cannot_be_combined(database):
    _, runtime, _, _ = scenario(database)
    with pytest.raises(TypeError, match="independent observer"):
        formal(runtime, entry_observer=Observer(), entry_filter=Observer())


@pytest.mark.parametrize(
    "target,mode",
    [
        (None, "GATE"),
        ("testnet-primary", "SHADOW"),
        ("other-account", "GATE"),
        ("TESTNET-PRIMARY", "GATE"),
    ],
)
def test_daemon_shadow_selector_requires_exact_account(target, mode):
    changes = {} if target is None else {"V2_JEV_SHADOW_ACCOUNT_ID": target}
    config = TestnetProcessConfig.from_mapping(settings(**changes))
    assert config.jev_shadow_account_id == target
    assert config.public_summary()["jev_entry_mode"] == mode
    assert config.daemon.write_enabled is False


@pytest.mark.parametrize(
    "target", ["", " testnet-primary", "testnet-primary\n", "x" * 129, True]
)
def test_invalid_shadow_selector_is_rejected(target):
    with pytest.raises(ValueError):
        TestnetProcessConfig.from_mapping(settings(V2_JEV_SHADOW_ACCOUNT_ID=target))


@pytest.mark.parametrize("target", [None, "testnet-primary", "other-account"])
def test_process_composition_selects_shadow_only_for_bound_account(database, target):
    from services.v2_jev_entry_context import JevEntryContext
    from services.v2_jev_shadow import JevShadow

    redis = pytest.importorskip("redis")
    cache = redis.Redis(
        unix_socket_path=os.environ["V2_REDIS_TEST_SOCKET"], decode_responses=True
    )
    cache.flushdb()
    public, telegram = MarketHTTP(), TelegramConnection()
    changes = {} if target is None else {"V2_JEV_SHADOW_ACCOUNT_ID": target}
    stop = threading.Event()
    try:
        process = create_testnet_process(
            TestnetProcessConfig.from_mapping(
                settings(
                    V2_ENABLE_TV_SIGNALS="true",
                    V2_TV_ADMISSION_START_MS="1790245000000",
                    **changes,
                )
            ),
            {
                "BINANCE_TESTNET_API_KEY": "test-key",
                "BINANCE_TESTNET_API_SECRET": "test-secret",
                "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
                "TG_NOTIFY_CHAT_ID": "-123",
            },
            connect=database,
            redis_client=cache,
            clickhouse=ArchiveClient(),
            stop=stop,
            telegram_sink=sink(telegram),
            clock_ms=lambda: 1790245000001,
            monotonic_ms=lambda: 0,
            signed_connection_factory=lambda *_: pytest.fail("signed I/O during build"),
            public_connection_factory=public,
        )
        workers = [scheduler.worker for scheduler in process.daemon.pipeline.schedulers]
        assert len(workers) == 4
        if target == "testnet-primary":
            assert all(isinstance(item.entry_observer, JevShadow) for item in workers)
            assert all(item.entry_filter is None for item in workers)
            assert all(
                item.entry_observer is workers[0].entry_observer for item in workers
            )
            assert all(
                json.loads(item.config_json)["jev_mode"] == "SHADOW" for item in workers
            )
        else:
            assert all(
                isinstance(item.entry_filter, JevEntryContext) for item in workers
            )
            assert all(item.entry_observer is None for item in workers)
        assert public.calls == [] and telegram.calls == []
    finally:
        stop.set()
        cache.close()


def test_unresolved_shadow_allows_fake_fill_native_stop_and_natural_settlement(
    database,
    pipeline_case,
):
    pipeline, _, account, _, _ = pipeline_case
    pipeline.market.run_once()
    with database() as conn:
        signal_id, signal = conn.execute(
            "SELECT signal_id::text,snapshot FROM v2_inbound_signals "
            "WHERE source='s3' AND snapshot->>'signal'='TREND_UP'"
        ).fetchone()
    scheduler, runtime = pipeline.schedulers[0], pipeline.runtime
    clock = SimpleNamespace(offset=0)
    clock.now = lambda: time.time_ns() // 1000000 + clock.offset
    runtime.clock_ms = clock.now
    runtime.account_risk.configure(
        SCOPE,
        AccountPolicy("USDT", "10000", 10, 0, "test-mark", 60000),
        expected_version=0,
    )
    scheduler.context_provider.clock = clock.now
    scheduler.context_provider.market.clock_ms = clock.now
    account.update(observed_at_ms=clock.now(), valid_until_ms=clock.now() + 60000)
    observer = Observer()
    scheduler.worker.entry_observer = observer
    prepared = scheduler.worker.consume(
        signal_id, context=scheduler.context_provider(signal)
    )
    assert prepared["status"] == "PREPARED"
    saved = scheduler.worker.decision(signal_id)["snapshot"]
    evaluation = saved["features"]["evaluation"]
    assert "jev_entry_gate" not in evaluation
    assert evaluation["jev_shadow"]["coverage"] == "MISSING_UNTIL_DURABLE_RESULT"
    frozen = deepcopy(saved)
    venue = SimulatedVenue(clock, evaluation["market_plan"])
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    runtime.risk_check = lambda _: True
    runtime.execution.risk_reference = lambda order: RiskReference(
        order["environment"],
        order["symbol"],
        "test-mark",
        "100",
        clock.now(),
    )
    reference = lambda _: {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "mark_price": "100",
        "observed_at_ms": clock.now(),
        "tick_size": "0.01",
    }
    runtime.execution.submit = GuardedOpeningSubmit(
        database,
        readiness=TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=clock.now),
        reference=reference,
        plan=lambda _: {
            "leverage": evaluation["market_plan"]["leverage"],
            "margin_type": evaluation["market_plan"]["margin_mode"],
        },
        submit=adapter.submit,
        enabled=True,
    )
    runtime.execution.query = adapter.query
    assert runtime.execution.dispatch(prepared["order_id"]) == "ACKNOWLEDGED"
    assert runtime.execution.dispatch(prepared["order_id"]) == "FILLED"
    protection = DirectionalProtectionStage(
        database,
        venue,
        scope=SCOPE,
        reference=reference,
        clock_ms=clock.now,
        allow_writes=True,
    )
    assert protection.run_once()["status"] == "CLEAR"
    expected_net = venue.trigger_stop()
    clock.offset += 10
    assert protection.run_once()["status"] == "CLEAR"
    clock.offset += 60001
    settlement = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=clock.now
    )
    settled = settlement.run_once()
    assert settled["status"] == "CLEAR"
    assert (
        Decimal(settled["settlements"][prepared["intent_id"]]["net_pnl"])
        == expected_net
    )
    assert runtime.data.trace(prepared["intent_id"])["episode"]["status"] == "SETTLED"
    assert scheduler.worker.decision(signal_id)["snapshot"] == frozen
    assert len(observer.requests) == 1
    assert [path for path, _ in venue.posts] == ["/fapi/v1/order", "/fapi/v1/algoOrder"]
