from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_account_coverage import Account
from test_v2_directional_replay import scenario, worker

from services.v2_directional_admission import (
    admission_decision,
    create_directional_admission_scheduler,
    create_directional_admission_worker,
)
from services.v2_directional_protection import DirectionalStopRecovery
from tests.support.v2_admission import database as database_fixture
from v2_core.runner import RiskVerdict

database = database_fixture


@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
@pytest.mark.parametrize(
    "strategy,event",
    [
        ("S6", "HIGH_VOL"),
        ("S8", "HIGH_VOL"),
        ("S6", "TREND_DOWN"),
        ("S8", "TREND_UP"),
    ],
)
def test_dynamic_nonmatching_events_complete_without_context(
    database, source, strategy, event
):
    _, runtime, _, _ = scenario(database)
    runtime.execution.risk_reference = lambda _: pytest.fail("no dispatch")
    signal = runtime.data.signals.admit(
        source=source,
        environment="SANDBOX",
        request_key="nonmatching-dynamic",
        snapshot={
            "symbol": "ETHUSDT",
            "observed_at": 2,
            "expires_at_ms": 10,
            "signal": event,
            "features": {},
        },
    )
    scheduler = create_directional_admission_scheduler(
        runtime,
        context_provider=lambda _: pytest.fail(
            "nonmatching events need no market/account reads"
        ),
        strategy=strategy,
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
        source=source,
        dynamic_universe=True,
        tv_start_ms=1,
    )
    # Existing scenario signal may be supported: schedule this case only.
    with database() as c:
        other = c.execute(
            "SELECT signal_id FROM v2_inbound_signals WHERE signal_id<>%s", (signal,)
        ).fetchall()
        for (identifier,) in other:
            c.execute(
                "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) VALUES(%s,%s,'IGNORED','QA unrelated fixture') ON CONFLICT DO NOTHING",
                (scheduler.worker.scope.consumer, identifier),
            )
    result = scheduler.run_once()
    assert result[signal] == "IGNORED"
    with database() as c:
        assert c.execute(
            "SELECT outcome,reason FROM v2_signal_receipts WHERE signal_id=%s AND consumer=%s",
            (signal, scheduler.worker.scope.consumer),
        ).fetchone() == ("IGNORED", "UNSUPPORTED_DIRECTIONAL_EVENT")
        assert c.execute(
            "SELECT error_code,completed_at IS NOT NULL FROM v2_strategy_tasks WHERE signal_id=%s AND consumer=%s",
            (signal, scheduler.worker.scope.consumer),
        ).fetchone() == (None, True)
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
    assert scheduler.run_once() == {}


def test_dynamic_actionable_event_still_requires_contract_proof():
    config = {
        "mode": "TESTNET_ADMISSION_ONLY",
        "account_scope": {"environment": "SANDBOX"},
        "strategy": "S6",
        "dynamic_universe": True,
    }
    with pytest.raises(ValueError, match="CONTRACT_ADMISSION_EVIDENCE_REQUIRED"):
        admission_decision(
            {"signal": "TREND_UP", "symbol": "BTCUSDT", "features": {}}, {}, config
        )
    with pytest.raises(ValueError, match="conflicting event type"):
        admission_decision(
            {
                "signal": "HIGH_VOL",
                "symbol": "BTCUSDT",
                "features": {"type": "TREND_UP"},
            },
            {},
            config,
        )


def test_tv_scheduler_uses_real_gates_and_never_backfills_or_expands_universe(database):
    _, runtime, _, context = scenario(database)
    runtime.execution.risk_reference = lambda _: pytest.fail("no venue dispatch")
    signals = runtime.data.signals

    def record(key, observed, target):
        return signals.admit(
            source="tv_bridge",
            environment="SANDBOX",
            request_key=key,
            snapshot={
                "symbol": target,
                "observed_at": observed,
                "expires_at_ms": 10,
                "signal": "TREND_UP",
                "features": {
                    "tv_signal": "TREND_UP_LONG",
                    "strength": 70,
                    "price": "100",
                },
            },
        )

    fresh = record("tv-fresh", 2, "BTCUSDT")
    old = record("tv-old", 1, "BTCUSDT")
    outside = record("tv-outside", 2, "ETHUSDT")
    context_calls = []

    def market(snapshot):
        context_calls.append(snapshot["symbol"])
        return deepcopy(context)

    scheduler = create_directional_admission_scheduler(
        runtime,
        context_provider=market,
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
        source="tv_bridge",
        allowed_symbols=("BTCUSDT",),
        tv_start_ms=2,
    )
    result = scheduler.run_once()
    assert result[fresh] == "PREPARED"
    assert result[old] == result[outside] == "IGNORED"
    assert context_calls == ["BTCUSDT"]
    assert scheduler.progress()["caught_up"] is True
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_signal_receipts WHERE outcome='INTENT'"
            ).fetchone()[0]
            == 1
        )
    assert scheduler.run_once() == {}


def test_tv_scheduler_rejects_implicit_start_or_empty_universe(database):
    _, runtime, _, context = scenario(database)
    runtime.execution.risk_reference = lambda _: None
    for settings in (
        {"allowed_symbols": (), "tv_start_ms": 2},
        {"allowed_symbols": ("BTCUSDT",), "tv_start_ms": None},
    ):
        with pytest.raises(ValueError, match="TV admission requires"):
            create_directional_admission_scheduler(
                runtime,
                context_provider=lambda _: context,
                strategy="S6",
                analysis_mode="hard",
                max_delay_ms=5,
                enable_admission=True,
                source="tv_bridge",
                **settings,
            )


def formal(runtime, strategy="S6", **overrides):
    # This injected reference must not be called during admission.
    runtime.execution.risk_reference = lambda _: pytest.fail(
        "admission must not dispatch"
    )
    return create_directional_admission_worker(
        runtime,
        **{
            "strategy": strategy,
            "analysis_mode": "hard",
            "max_delay_ms": 5,
            "enable_admission": True,
            **overrides,
        },
    )


@pytest.mark.parametrize("strategy,side", [("S6", "BUY"), ("S8", "SELL")])
def test_actual_rules_to_durable_order_no_venue_calls(database, strategy, side):
    replay, runtime, signal, context = scenario(database, strategy)
    original = deepcopy(context)
    assert (
        replay.consume(signal, context=context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    )
    admission = formal(runtime, strategy)
    result = admission.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    saved = admission.decision(signal)
    assert saved["action"] == "OPEN" and saved["side"] == side
    assert saved["snapshot"]["expires_at_ms"] == 8
    evaluation = saved["snapshot"]["features"]["evaluation"]
    assert evaluation["admission_authorized"] is True
    assert evaluation["execution_authorized"] is False
    assert evaluation["sizing"]["quantity"] == "1.25"
    assert saved["snapshot"]["features"]["context"] == original == context
    assert admission.scope.producer == strategy.lower()
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
            == 2
        )


def test_restart_never_re_evaluates_or_duplicates_order(database):
    _, runtime, signal, context = scenario(database)
    admission = formal(runtime)
    first = admission.consume(signal, context=context)
    saved = admission.decision(signal)
    _, restarted = worker(database, now=4)
    recovered = formal(restarted, analysis_mode="soft")
    recovered.decide = lambda *_: pytest.fail("frozen decision cannot change")
    assert recovered.consume(signal, context={})["order_id"] == first["order_id"]
    assert recovered.decision(signal) == saved


def test_concurrent_strategy_consumers_share_one_order(database):
    _, runtime, signal, context = scenario(database)
    admission = formal(runtime)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: admission.consume(signal, context=context), range(4))
        )
    assert len({r["order_id"] for r in results}) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 1


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("history", "LOW_QUALITY"),
        ("margin", "INSUFFICIENT_BUDGET"),
        ("funding", "ADVERSE_FUNDING"),
        ("rr", "REWARD_RISK_TOO_LOW"),
    ],
)
def test_rejected_candidates_remain_no_order(database, failure, reason):
    _, runtime, signal, context = scenario(database)
    snapshot = context["directional"]
    if failure == "history":
        snapshot["history"].update(win_rate="20", avg_quality_score="20")
    elif failure == "margin":
        snapshot["sizing"]["available_margin"] = "0"
    elif failure == "funding":
        snapshot["funding_rate"] = "0.002"
    else:
        snapshot["expected_move_pct"] = "1"
    assert formal(runtime).consume(signal, context=context)["reason"] == reason
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_crash_after_decision_expiry_never_revives_opening(database, monkeypatch):
    _, runtime, signal, context = scenario(database)
    admission = formal(runtime)

    def crash(*_):
        raise ConnectionError("before admission")

    monkeypatch.setattr(runtime, "accept_open", crash)
    with pytest.raises(ConnectionError):
        admission.consume(signal, context=context)
    _, restarted = worker(database, now=20)
    recovered = formal(restarted)
    recovered.decide = lambda *_: pytest.fail("cannot reevaluate expired decision")
    assert recovered.consume(signal, context={})["status"] == "EXPIRED"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_dispatch_still_requires_account_risk_approval(database):
    _, runtime, signal, context = scenario(database)
    result = formal(runtime).consume(signal, context=context)
    runtime.risk_check = lambda _: RiskVerdict(False, "ACCOUNT_COVERAGE_BLOCKED")
    assert runtime.execution.dispatch(result["order_id"]) == "DENIED"


def test_scheduler_persists_formal_task_and_order(database):
    _, runtime, signal, context = scenario(database)
    formal(runtime)  # bind QA-only reference; never dispatch
    scheduler = create_directional_admission_scheduler(
        runtime,
        context_provider=lambda _: deepcopy(context),
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
    )
    assert scheduler.run_once() == {signal: "PREPARED"}
    assert scheduler.run_once() == {}


def test_scheduler_ignores_unrelated_s3_event_without_account_context(database):
    _, runtime, original, context = scenario(database)
    formal(runtime).consume(original, context=context)
    signal = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key="unrelated-high-vol",
        snapshot={
            "symbol": "BTCUSDT",
            "observed_at": 1,
            "expires_at_ms": 8,
            "signal": "HIGH_VOL",
            "features": {"type": "HIGH_VOL", "strength": 31},
        },
    )
    scheduler = create_directional_admission_scheduler(
        runtime,
        context_provider=lambda _: pytest.fail(
            "unrelated event needs no account context"
        ),
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
    )
    assert scheduler.run_once() == {original: "PREPARED", signal: "IGNORED"}
    assert scheduler.run_once() == {}
    assert scheduler.progress()["caught_up"] is True
    with database() as conn:
        assert conn.execute(
            "SELECT reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (scheduler.worker.scope.consumer, signal),
        ).fetchone() == ("UNSUPPORTED_DIRECTIONAL_EVENT",)


@pytest.mark.parametrize(
    "failure", ["disabled", "not_bool", "live", "unbound", "reference", "strategy"]
)
def test_unsafe_factory_configuration_fails_before_consumption(database, failure):
    _, runtime, _, _ = scenario(database)
    runtime.execution.risk_reference = lambda _: None
    settings = {
        "strategy": "S6",
        "analysis_mode": "hard",
        "max_delay_ms": 5,
        "enable_admission": True,
    }
    if failure == "disabled":
        del settings["enable_admission"]
    elif failure == "not_bool":
        settings["enable_admission"] = 1
    elif failure == "live":
        runtime.scope = replace(runtime.scope, environment="LIVE")
    elif failure == "unbound":
        runtime.scope = None
    elif failure == "reference":
        runtime.execution.risk_reference = None
    else:
        settings["strategy"] = "S7"
    with pytest.raises(ValueError):
        create_directional_admission_worker(runtime, **settings)
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "strategy,side,price", [("S6", "SELL", "92"), ("S8", "BUY", "108")]
)
def test_original_strategy_stop_is_recovered_without_new_context(
    database, strategy, side, price
):
    _, runtime, signal, context = scenario(database, strategy)
    result = formal(runtime, strategy).consume(signal, context=context)
    venue = Account()
    pm = DirectionalStopRecovery(
        database,
        venue,
        scope=runtime.scope,
        reference=lambda _: pytest.fail("planning never fetches market"),
        clock_ms=lambda: 1000,
    )
    context["directional"]["price"] = (
        "1"  # changed live context cannot rewrite old stop
    )
    runtime.clock_ms = lambda: 1000  # expired entry remains recoverable for protection
    spec = pm.plan(result["order_id"])
    assert spec.side == side and spec.trigger_price == price
    assert spec.episode == result["intent_id"]
    assert pm.ensure(result["order_id"]) == {
        "status": "OPENING_NOT_FINAL",
        "opening_status": "CANCELLATION_DISABLED",
    }
    assert not venue.calls


def test_protection_does_not_adopt_legacy_intent(database):
    from test_v2_directional_replay import SCOPE

    from tests.support.v2_admission import evidence, intent
    from v2_core.service import TradingData

    data, original, venue = TradingData(database), intent(), Account()
    data.accept(original, evidence())
    order, _ = data.orders.prepare(original.intent_id)
    pm = DirectionalStopRecovery(
        database,
        venue,
        scope=SCOPE,
        reference=lambda _: None,
        clock_ms=lambda: 1000,
        allow_writes=True,
    )
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        pm.ensure(order)
    assert not venue.calls


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_strategy_evidence_to_confirmed_fill_to_registered_stop(database, strategy):
    _, runtime, signal, context = scenario(database, strategy)
    result = formal(runtime, strategy).consume(signal, context=context)
    # Inject a confirmed venue fill; this test does not certify dispatch/margin.
    data, order = runtime.data, result["order_id"]
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="1",
        quantity="1.25",
        price="100",
        fee="0.05",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={"source": "isolated-confirmed-fill"},
    )
    data.orders.transition(
        order,
        expected_version=2,
        status="FILLED",
        exchange_order_id="10",
        evidence={"fills_complete": True},
    )

    class Venue(Account):
        def __init__(self):
            super().__init__()
            self.writes, self.parent = [], None
            self.rows["/fapi/v3/positionRisk"] = [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "1.25" if strategy == "S6" else "-1.25",
                }
            ]

        def __call__(self, method, path, params):
            if path == "/fapi/v1/algoOrder":
                if method == "POST":
                    self.writes.append(dict(params))
                    self.parent = {
                        **params,
                        "orderType": params["type"],
                        "algoId": 30,
                        "algoStatus": "NEW",
                        "closePosition": True,
                        "priceProtect": False,
                    }
                assert self.parent is not None
                return dict(self.parent)
            return super().__call__(method, path, params)

    venue = Venue()
    pm = DirectionalStopRecovery(
        database,
        venue,
        scope=runtime.scope,
        allow_writes=True,
        reference=lambda _: {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "mark_price": "100",
            "tick_size": "0.1",
            "observed_at_ms": 1000,
        },
        clock_ms=lambda: 1000,
    )
    assert pm.ensure(order)["status"] == "NEW"
    assert pm.ensure(order)["status"] == "NEW"
    assert len(venue.writes) == 1
    assert venue.writes[0]["triggerPrice"] == ("92" if strategy == "S6" else "108")
    assert venue.writes[0]["side"] == ("SELL" if strategy == "S6" else "BUY")
