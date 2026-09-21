from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_account_coverage import Account
from test_v2_directional_replay import scenario, worker
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_admission import (
    create_directional_admission_scheduler,
    create_directional_admission_worker,
)
from services.v2_directional_protection import DirectionalStopRecovery
from v2_core.runner import RiskVerdict

database = database_fixture


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
    from test_v2_intent_admission import evidence, intent

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
