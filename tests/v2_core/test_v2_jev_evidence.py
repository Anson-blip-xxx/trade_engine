"""JEV references survive admission evidence without changing trading decisions."""

import json
import threading
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario, worker
from test_v2_testnet_daemon_entry import settings

from services.v2_directional_admission import admission_decision
from services.v2_directional_context import DirectionalContext
from services.v2_jev_reference import (
    JevPrefetchMarketContext,
    JevReference,
    JevReferenceContext,
    public_features,
)
from services.v2_testnet_daemon_entry import (
    TestnetProcessConfig,
    create_testnet_process,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.jev_advisory import (
    AdvisoryPolicy,
    _result,
    parse_response,
    prepare_request,
)
from v2_core.producer import market_envelope
from v2_core.telegram import TelegramOperationalSink

database = database_fixture


def no_io(*args, **kwargs):
    pytest.fail("JEV evidence QA must not perform external I/O")


def public_context(context):
    """Use the same public market observations as the directional evaluator."""
    frames = deepcopy(context["directional"]["market"])
    frames["15m"]["close"] = context["directional"]["price"]
    context["sources"]["s3"] = market_envelope(
        "s3", "SANDBOX", context["symbol"], 1, frames
    )
    return context


def admission_case(strategy):
    facts = inputs(strategy)
    facts["event"]["strength"] = 70
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": 1,
        "expires_at_ms": 10,
        "signal": facts["event"]["type"],
        "features": facts["event"],
    }
    context = public_context(
        {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "assembled_at": 3,
            "valid_until_ms": 9,
            "sources": {},
            "account_scope": asdict(SCOPE),
            "directional": {
                "market": facts["market"],
                "price": facts["price"],
                "regime": facts["regime"],
                "short_ratio": facts["short_ratio"],
                "history": history(),
                "sizing": limits,
                "expected_move_pct": "8",
                "funding_rate": "0",
            },
        }
    )
    config = {
        "strategy": strategy,
        "source": "s3",
        "analysis_mode": "hard",
        "mode": "TESTNET_ADMISSION_ONLY",
        "account_scope": asdict(SCOPE),
    }
    return signal, context, config


def publish_reference(reference, context, strategy, opinion):
    """Seed the disposable cache; the real lookup still validates every field."""
    policy = AdvisoryPolicy(enabled=True)
    reference._remember_control(policy)
    reference.cache.clear()
    if opinion == "fallback":
        return
    features = public_features(
        context["sources"]["s3"],
        target=context["symbol"],
        direction="LONG" if strategy == "S6" else "SHORT",
    )
    prepared = prepare_request(features, now_ms=3, max_age_ms=policy.max_age_ms)
    strong = opinion == "strong"
    response = parse_response(
        {
            "model": "jev-1.13.0",
            "answers": {
                "trend_supported": {"type": "noul", "noul": "1" if strong else "0"},
                "chase_risk": {"type": "noul", "noul": "0" if strong else "1"},
                "liquidity_fragile": {"type": "noul", "noul": "0" if strong else "1"},
            },
            "usage": {"input_tokens": 900, "output_tokens": 60},
        }
    )
    result = _result(
        "ADVISORY_READY",
        "MODEL_OBSERVED",
        prepared,
        now_ms=3,
        expires_at_ms=30003,
        response=response,
    )
    reference.cache[prepared["request_key"]] = (policy, result)


def reference_context(context, strategy, opinion):
    reference = JevReference(no_io, clock_ms=lambda: 3, stop=threading.Event())
    publish_reference(reference, context, strategy, opinion)
    provider = JevReferenceContext(
        lambda _: context, reference=reference, strategy=strategy
    )
    return provider, reference


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("opinion", ["strong", "weak", "fallback"])
@pytest.mark.parametrize(
    "gate,reason",
    [
        ("open", "DIRECTIONAL_MARKET_GATES_PASSED"),
        ("market", "entry_mode_unconfirmed"),
        ("history", "LOW_QUALITY"),
        ("margin", "INSUFFICIENT_BUDGET"),
        ("funding", "ADVERSE_FUNDING"),
        ("reward_risk", "REWARD_RISK_TOO_LOW"),
    ],
)
def test_reference_never_changes_real_admission_or_quantity(
    strategy, opinion, gate, reason
):
    signal, context, config = admission_case(strategy)
    directional = context["directional"]
    if gate == "market":
        directional["price"] = "98" if strategy == "S6" else "102"
        public_context(context)
    elif gate == "history":
        directional["history"].update(win_rate="20", avg_quality_score="20")
    elif gate == "margin":
        directional["sizing"]["available_margin"] = "0"
    elif gate == "funding":
        directional["funding_rate"] = "0.002" if strategy == "S6" else "-0.002"
    elif gate == "reward_risk":
        directional["expected_move_pct"] = "1"
    original = deepcopy(context)
    baseline = admission_decision(signal, context, config)
    provider, reference = reference_context(context, strategy, opinion)
    enriched = provider(signal)
    advisory = enriched["jev_advisory"]
    assert advisory["status"] == (
        "FALLBACK" if opinion == "fallback" else "ADVISORY_READY"
    )
    if opinion != "fallback":
        assert advisory["hypotheses"]["trend_supported"] == (
            "1" if opinion == "strong" else "0"
        )
    assert advisory["decision_effect"] == "NONE"
    assert advisory["execution_authorized"] is False
    assert advisory["risk_override"] is False
    actual = admission_decision(signal, enriched, config)
    assert actual == baseline
    assert actual.rationale == reason
    assert actual.action == ("OPEN" if gate == "open" else "IGNORED")
    assert actual.quantity == ("1.25" if gate == "open" else None)
    assert json.loads(actual.features_json)["execution_authorized"] is False
    assert context == original
    assert {key: enriched[key] for key in original} == original
    assert reference.thread is None


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("opinion", ["strong", "weak", "fallback"])
@pytest.mark.parametrize("accepted", [True, False])
def test_pg_freezes_reference_and_same_signal_retry_cannot_replace_it(
    database, strategy, opinion, accepted
):
    _, runtime, signal_id, context = scenario(database, strategy)
    public_context(context)
    if not accepted:
        context["directional"]["sizing"]["available_margin"] = "0"
    admission = formal(runtime, strategy)
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    provider, reference = reference_context(context, strategy, opinion)
    first_context = provider(signal)
    frozen_context = deepcopy(first_context)
    first = admission.consume(signal_id, context=first_context)
    assert first["status"] == ("PREPARED" if accepted else "IGNORED")
    saved = admission.decision(signal_id)
    assert saved["snapshot"]["features"]["context"] == frozen_context
    if accepted:
        assert Decimal(saved["quantity"]) == Decimal("1.25")
    else:
        assert saved["quantity"] is None

    def persisted():
        with database() as conn:
            return conn.execute(
                "SELECT d.evidence_ref,e.snapshot FROM v2_strategy_decisions d "
                "JOIN v2_decision_evidence e USING(evidence_ref) "
                "WHERE d.consumer=%s AND d.signal_id=%s",
                (admission.scope.consumer, signal_id),
            ).fetchone()

    original_evidence = persisted()
    assert original_evidence[1]["features"]["context"] == frozen_context
    first_context["jev_advisory"]["reason"] = "CALLER_MUTATED_AFTER_COMMIT"
    publish_reference(
        reference, context, strategy, "weak" if opinion == "strong" else "strong"
    )
    new_context = provider(signal)
    assert new_context["jev_advisory"] != frozen_context["jev_advisory"]
    _, restarted_runtime = worker(database, strategy=strategy, now=4)
    restarted = formal(restarted_runtime, strategy)
    restarted.decide = lambda *_: pytest.fail("same signal must reuse frozen evidence")
    retry = restarted.consume(signal_id, context=new_context)
    assert retry["decision_id"] == first["decision_id"]
    assert retry["status"] == first["status"]
    if accepted:
        assert retry["order_id"] == first["order_id"]
    else:
        assert retry["reason"] == "INSUFFICIENT_BUDGET"
    assert restarted.decision(signal_id) == saved
    assert persisted() == original_evidence
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == int(
            accepted
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("shared_market", [False, True])
def test_real_process_factory_asks_only_after_risk_for_every_consumer(
    shared_market,
):
    stop = threading.Event()
    config = TestnetProcessConfig.from_mapping(
        settings(
            V2_ENABLE_TV_SIGNALS="true",
            V2_TV_ADMISSION_START_MS="1",
            V2_SHARED_MARKET="true" if shared_market else "false",
        )
    )
    process = create_testnet_process(
        config,
        {
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
            "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
            "TG_NOTIFY_CHAT_ID": "-123",
        },
        connect=no_io,
        redis_client=SimpleNamespace(hget=no_io, eval=no_io),
        clickhouse=SimpleNamespace(query=no_io, insert=no_io),
        stop=stop,
        telegram_sink=TelegramOperationalSink(
            token="12345:abcdefghijklmnop",
            chat_id="-123",
            environment="SANDBOX",
            connection_factory=no_io,
        ),
        clock_ms=lambda: 86402001,
        monotonic_ms=lambda: 0,
        signed_connection_factory=no_io,
        public_connection_factory=no_io,
    )
    schedulers = process.daemon.pipeline.schedulers
    expected = {
        (strategy, source)
        for strategy in ("s6", "s8")
        for source in ("s3", "tv_bridge")
    }
    assert {(s.worker.scope.producer, s.worker.source) for s in schedulers} == expected
    references, market_sources = [], []
    from services.v2_jev_entry_context import JevEntryContext
    from services.v2_opening_quarantine import OpeningQuarantineContext

    filters = []
    for scheduler in schedulers:
        provider = scheduler.context_provider
        assert isinstance(provider, OpeningQuarantineContext)
        gate = scheduler.worker.entry_filter
        assert isinstance(gate, JevEntryContext)
        assert gate.policy is None  # live account-scoped PG policy, not a constant
        filters.append(gate)
        references.append(gate.reference)
        delegate = provider.delegate
        for _ in range(10):
            if isinstance(delegate, DirectionalContext):
                break
            delegate = delegate.delegate
        assert isinstance(delegate, DirectionalContext)
        assert not isinstance(delegate.market, JevPrefetchMarketContext)
        market_sources.append(delegate.market)
    assert all(gate is filters[0] for gate in filters)
    assert all(reference is references[0] for reference in references)
    assert all(source is market_sources[0] for source in market_sources)
    assert references[0].stop is stop
    assert references[0].thread is None
    assert references[0].queue.empty()
