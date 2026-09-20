from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_replay import create_directional_replay_worker
from v2_core.account_risk import AccountScope
from v2_core.runtime import DataRuntime

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


def worker(database, *, strategy="S6", now=3, analysis_mode="hard"):
    runtime = DataRuntime(
        database,
        scope=SCOPE,
        clock_ms=lambda: now,
        submit=lambda _: pytest.fail("replay must never submit"),
        query=lambda _: pytest.fail("replay must never query"),
        risk_check=lambda _: pytest.fail("replay cannot request opening permission"),
    )
    replay = create_directional_replay_worker(
        runtime,
        strategy=strategy,
        analysis_mode=analysis_mode,
        max_delay_ms=5,
    )
    return replay, runtime


def scenario(database, strategy="S6"):
    replay, runtime = worker(database, strategy=strategy)
    facts = inputs(strategy)
    facts["event"]["strength"] = 70  # actual S3 lifecycle contract
    facts["market"]["1h"]["atr_pct"] = 1
    signal_id = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key="replay-" + strategy,
        snapshot={
            "symbol": "BTCUSDT",
            "observed_at": 1,
            "expires_at_ms": 10,
            "signal": facts["event"]["type"],
            "features": facts["event"],
        },
    )
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    context = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": 3,
        "valid_until_ms": 9,
        "sources": {"s3": {"snapshot_id": "explicit-qa"}},
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
    return replay, runtime, signal_id, context


def assert_no_orders(database):
    with database() as conn:
        for table in ("v2_trade_intents", "v2_orders", "v2_episodes"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_real_rules_persist_complete_evidence_without_order(database, strategy):
    replay, _, signal_id, context = scenario(database, strategy)
    original = deepcopy(context)
    result = replay.consume(signal_id, context=context)
    assert result["status"] == "IGNORED"
    assert result["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    assert context == original
    saved = replay.decision(signal_id)
    evaluation = saved["snapshot"]["features"]["evaluation"]
    assert evaluation["market_plan"]["system_tag"] == (
        "S6A" if strategy == "S6" else "S8"
    )
    assert evaluation["sizing"]["quantity"] == "1.25"
    assert evaluation["execution_authorized"] is False
    assert saved["snapshot"]["features"]["context"] == context
    assert saved["snapshot"]["expires_at_ms"] == 8
    assert replay.scope.producer == strategy.lower() + "-replay"
    assert_no_orders(database)


def test_restart_reuses_decision_even_with_new_context_and_policy(database):
    replay, _, signal_id, context = scenario(database)
    first = replay.consume(signal_id, context=context)
    record = replay.decision(signal_id)
    restarted, _ = worker(database, now=4, analysis_mode="soft")
    restarted.decide = lambda *_: pytest.fail("committed decision is frozen")
    assert restarted.consume(signal_id, context={}) == dict(first, intent_id=None)
    assert restarted.decision(signal_id) == record
    assert_no_orders(database)


def test_crash_between_decision_and_receipt_reuses_evidence(database, monkeypatch):
    replay, runtime, signal_id, context = scenario(database)

    def crash(**_):
        raise ConnectionError("explicit receipt outage")

    monkeypatch.setattr(runtime.data.signals, "complete", crash)
    with pytest.raises(ConnectionError):
        replay.consume(signal_id, context=context)
    record = replay.decision(signal_id)
    restarted, _ = worker(database, now=20)
    restarted.decide = lambda *_: pytest.fail("cannot reevaluate original scene")
    assert (
        restarted.consume(signal_id, context={})["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    assert restarted.decision(signal_id) == record
    assert_no_orders(database)


def test_concurrent_replay_has_one_decision_and_receipt(database):
    replay, _, signal_id, context = scenario(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: replay.consume(signal_id, context=context), range(4))
        )
    assert len({item["decision_id"] for item in results}) == 1
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone()[0] == 1
        )
    assert_no_orders(database)


@pytest.mark.parametrize(
    "change",
    ["account", "environment", "symbol", "expiry", "missing", "price_override"],
)
def test_invalid_context_does_not_commit_decision(database, change):
    replay, _, signal_id, context = scenario(database)
    if change == "account":
        context["account_scope"]["account_id"] = "other"
    elif change == "environment":
        context["environment"] = "LIVE"
    elif change == "symbol":
        context["symbol"] = "ETHUSDT"
    elif change == "expiry":
        context["valid_until_ms"] = 3
    elif change == "missing":
        del context["directional"]["history"]
    else:
        context["directional"]["sizing"]["price"] = "1"
    with pytest.raises((ValueError, KeyError, TypeError)):
        replay.consume(signal_id, context=context)
    assert replay.decision(signal_id) is None
    assert_no_orders(database)


@pytest.mark.parametrize(
    "stage,reason",
    [
        ("market", "entry_mode_unconfirmed"),
        ("history", "LOW_QUALITY"),
        ("size", "INSUFFICIENT_BUDGET"),
        ("funding", "ADVERSE_FUNDING"),
        ("rr", "REWARD_RISK_TOO_LOW"),
    ],
)
def test_rejection_reason_is_durable(database, stage, reason):
    replay, _, signal_id, context = scenario(database)
    snapshot = context["directional"]
    if stage == "market":
        snapshot["price"] = "98"
    elif stage == "history":
        snapshot["history"].update(win_rate="20", avg_quality_score="20")
    elif stage == "size":
        snapshot["sizing"]["available_margin"] = "0"
    elif stage == "funding":
        snapshot["funding_rate"] = "0.002"
    else:
        snapshot["expected_move_pct"] = "1"
    assert replay.consume(signal_id, context=context)["reason"] == reason
    assert replay.decision(signal_id)["snapshot"]["rationale"] == reason
    assert_no_orders(database)


def test_expired_signal_does_not_require_context_or_evaluate(database):
    _, _, signal_id, _ = scenario(database)
    replay, _ = worker(database, now=20)
    replay.decide = lambda *_: pytest.fail("expired signal")
    assert replay.consume(signal_id, context={})["status"] == "EXPIRED"
    assert_no_orders(database)


def test_replay_receipt_does_not_consume_formal_strategy_signal(database):
    from v2_core.strategy import StrategyDecision, StrategyScope, StrategyWorker

    replay, runtime, signal_id, context = scenario(database)
    replay.consume(signal_id, context=context)
    formal = StrategyWorker(
        runtime,
        StrategyScope(**asdict(SCOPE), producer="s6"),
        source="s3",
        strategy_version="formal-qa-only",
        config={},
        max_delay_ms=5,
        decide=lambda *_: StrategyDecision("IGNORED", "formal evaluation called"),
    )
    assert formal.consume(signal_id, context={})["reason"] == "formal evaluation called"
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone()[0] == 2
        )
    assert_no_orders(database)


def test_replay_configuration_cannot_enable_trading(database):
    import json

    from services.v2_directional_replay import replay_decision

    replay, _, signal_id, context = scenario(database)
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]
    config = json.loads(replay.config_json)
    config["mode"] = "LIVE"
    with pytest.raises(ValueError, match="cannot authorize"):
        replay_decision(signal, context, config)
    assert_no_orders(database)


@pytest.mark.parametrize("event_type", ["HIGH_VOL", "TREND_DOWN"])
def test_other_s3_events_are_ignored_without_retry_loop(database, event_type):
    replay, runtime, _, context = scenario(database)
    signal_id = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key=event_type,
        snapshot={
            "symbol": "BTCUSDT",
            "observed_at": 1,
            "expires_at_ms": 10,
            "signal": event_type,
            "features": {"type": event_type, "strength": 70},
        },
    )
    assert (
        replay.consume(signal_id, context=context)["reason"]
        == "UNSUPPORTED_DIRECTIONAL_EVENT"
    )
    assert_no_orders(database)


def test_global_runtime_cannot_create_replay_worker():
    with pytest.raises(ValueError, match="account-bound"):
        create_directional_replay_worker(
            SimpleNamespace(scope=None),
            strategy="S6",
            analysis_mode="hard",
            max_delay_ms=5,
        )
