"""Real typed producer/account composition against isolated PG and fake HTTPS.

Only the transport's socket factory and the closed-candle archive port are
synthetic. Provider, quota, account policy, capital calculations/publication,
route fencing, immutable candidates, admission and preparation are production
code. Nothing calls a venue or starts a runtime/service.
"""

import json
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

import services.v2_strategy_runtime_job as runtime
from services.v2_strategy_entry_job import EntryJobSpec, run_entry_work
from tests.v2_core import test_v2_strategy_candidate_producer as synthetic
from tests.v2_core.test_v2_settlement_execution_fence import (
    database,
    fenced,
    route_update,
)
from tests.v2_core.test_v2_strategy_candidate_builder import bars
from v2_core import entry_controls
from v2_core.liquidity import evaluate_execution_quote
from v2_core.public_market import BinancePublicMarket
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_execution_context import read_execution_context
from v2_core.transport import BinanceSignedTransport

__all__ = ["database", "fenced"]


class HTTPS:
    def __init__(self, owner, host, timeout):
        assert host == "demo-fapi.binance.com" and timeout == 3
        self.owner, self.path = owner, None

    def request(self, method, path, body=None, headers=None):
        # Inspect only the path: signatures and credentials never enter logs.
        assert method == "GET" and body is None
        self.path = urlsplit(path).path
        assert self.path in self.owner.responses
        self.owner.calls.append((method, self.path))
        if self.owner.on_request is not None:
            self.owner.on_request(self.path)

    def getresponse(self):
        value = deepcopy(self.owner.responses[self.path])
        if self.path in {"/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker"}:
            value["time"] = self.owner.clock()
        return SimpleNamespace(
            status=200,
            read=lambda _: json.dumps(value).encode(),
            getheader=lambda *args: None,
        )

    def close(self):
        pass


def capital(connect, scope):
    saved = BusinessState(connect).read(
        StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    )
    return None if saved is None else (saved.version, json.loads(saved.payload_json))


def composition(fenced, monkeypatch, *, side="BUY", source="tv_bridge"):
    selected = synthetic.scene(fenced, side=side, source=source)
    entry = EntryJobSpec(
        selected.binding,
        "real-causal-producer",
        (runtime.SPEC,),
        (runtime.SPEC.entry_handler,),
        (runtime.SPEC.exit_handler,),
        (runtime.SPEC.plan_schema,),
    )
    config = runtime.StrategyJobConfig(entry, True, 2)
    deployment = entry.registry().bind(
        scope=entry.binding.scope,
        instance_id=entry.producer,
        strategy_id=runtime.SPEC.strategy_id,
        code_version=runtime.SPEC.code_version,
        config=selected.deployment.config,
        generation=1,
        mode="ACTIVE",
    )
    StrategyDeployments(fenced.admin, entry.registry()).publish(
        deployment, expected_generation=0, request_id=str(uuid4())
    )
    policy = PolicyStore(fenced.admin, entry.binding.scope)
    policy.patch(
        {
            "capital.enabled": True,
            "capital.model_enabled": True,
            "capital.reference_wallet": "2000",
            "capital.initial_equity": "1000",
            "portfolio_risk.enabled": True,
            "capital.margin_buffer": "1.10",
            "capital.pool_fraction": ".70",
            "sizing.pool_fraction": ".70",
            "entry.force_isolated": True,
            "entry.reentry_enabled": False,
            "leverage.adaptive_enabled": True,
            "leverage.low": 2,
            "leverage.medium": 3,
            "leverage.adaptive_high": 5,
        },
        expected_version=policy.read().version,
        reason="isolated real composition fixture",
    )
    tick = synthetic.now(fenced.admin)
    clock = lambda: synthetic.now(fenced.admin)
    price = Decimal(bars(side=side)[-1]["c"])
    account = {
        "totalWalletBalance": "2000",
        "totalMarginBalance": "2000",
        "availableBalance": "2000",
        "totalInitialMargin": "0",
        "totalPositionInitialMargin": "0",
        "totalOpenOrderInitialMargin": "0",
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "2000",
                "marginBalance": "2000",
                "availableBalance": "2000",
                "initialMargin": "0",
                "positionInitialMargin": "0",
                "openOrderInitialMargin": "0",
            }
        ],
    }
    state = SimpleNamespace(
        selected=selected,
        config=config,
        deployment=deployment,
        scope=entry.binding.scope,
        clock=clock,
        stop=Event(),
        calls=[],
        transports=[],
        on_request=None,
        responses={
            "/fapi/v3/positionRisk": [],
            "/fapi/v3/account": account,
            "/fapi/v1/accountConfig": {
                "canTrade": True,
                "dualSidePosition": False,
                "multiAssetsMargin": False,
            },
            "/fapi/v1/symbolConfig": [
                {"symbol": "BTCUSDT", "maxNotionalValue": "1000000000000"}
            ],
            "/fapi/v1/leverageBracket": [
                {
                    "symbol": "BTCUSDT",
                    "brackets": [
                        {
                            "notionalFloor": 0,
                            "notionalCap": 1000000000000,
                            "initialLeverage": 20,
                            "maintMarginRatio": 0.005,
                        }
                    ],
                }
            ],
            "/fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT",
                "markPrice": str(price),
                "time": tick,
            },
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT",
                "bidPrice": str(price - Decimal(".01")),
                "askPrice": str(price + Decimal(".01")),
                "bidQty": "100000",
                "askQty": "100000",
                "time": tick,
            },
            "/fapi/v1/exchangeInfo": {"symbols": [selected.catalog]},
        },
    )
    # Preserve exact transport types and all request/signature/quota behavior.
    # Production binds HTTPSConnection as a default argument, so patch just
    # construction's injected socket factory rather than replace either port.
    for cls in (BinanceSignedTransport, BinancePublicMarket):
        original = cls.__init__

        def construct(self, *args, _original=original, **kwargs):
            kwargs["connection_factory"] = lambda host, timeout: HTTPS(
                state, host, timeout
            )
            _original(self, *args, **kwargs)
            state.transports.append(self)

        monkeypatch.setattr(cls, "__init__", construct)
    monkeypatch.setattr(runtime, "_clock", lambda: state.clock())
    state.producer, state.public = runtime._real_producer(
        fenced.admin,
        config=config,
        source=source,
        stop=state.stop,
        guard=fenced.guard,
        reader=selected.producer.market,
        private={"api_key": "qa-dummy", "api_secret": "qa-dummy-secret"},
    )
    return state


def frozen(case):
    return case.producer._state(case.selected.identity).candidate


def consume(fenced, case):
    return run_entry_work(
        fenced.admin,
        binding=case.config.entry.binding,
        registry=case.config.entry.registry(),
        producer=case.config.entry.producer,
        source=case.producer.source,
        stop=case.stop,
        clock_ms=case.clock,
        guard=fenced.guard,
    )


def assert_no_entry(fenced, case):
    assert frozen(case) is None
    with fenced.admin() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("source", ["quant", "tv_bridge"])
def test_real_provider_capital_quote_freeze_and_prepared_are_one_compatible_composition(
    fenced, monkeypatch, side, source
):
    case = composition(fenced, monkeypatch, side=side, source=source)
    before = capital(fenced.admin, case.scope)
    result = case.producer.prepare(case.selected.identity)
    assert result["status"] == "FROZEN" and result["execution_authorized"] is False
    saved = frozen(case)
    assert saved.plan.side == side and saved.plan.leverage == 2
    after = capital(fenced.admin, case.scope)
    assert after[0] == before[0] + 1
    body = after[1]
    assert body["account_scope"] == asdict(case.scope)
    assert body["account"]["totalWalletBalance"] == "2000"
    assert Decimal(body["capital_health"]["base"]) == 1000
    assert body["observation_id"].startswith("typed-account:")
    assert body["started_at_ms"] <= saved.plan.decided_at_ms < body["valid_until_ms"]
    assert saved.plan.expires_at_ms <= body["valid_until_ms"]
    assert Decimal(saved.features["sizing"]["calculation"]["margin_buffer"]) == Decimal(
        "1.10"
    )
    quote = saved.features["execution"]["execution_quote"]
    assert quote["symbol"] == "BTCUSDT" and quote["environment"] == "SANDBOX"
    assert quote["source_host"] == "demo-fapi.binance.com"
    report = evaluate_execution_quote(
        evidence=quote,
        controls={
            "mode": "ENFORCE",
            "max_execution_quote_age_ms": 30000,
            "max_spread_bps": "20",
        },
        symbol="BTCUSDT",
        now_ms=saved.plan.decided_at_ms,
    )
    assert report["passed"] is True
    assert all(method == "GET" for method, _ in case.calls)
    assert len(case.calls) == 8
    signed = next(
        item for item in case.transports if type(item) is BinanceSignedTransport
    )
    assert signed._trading is signed._settings is signed._protection is False
    assert consume(fenced, case)["signals"] == {case.selected.identity: "PREPARED"}
    with fenced.admin() as conn:
        (order_id,) = conn.execute(
            "SELECT order_id::text FROM v2_orders WHERE status='PREPARED'"
        ).fetchone()
        plan, execution = read_execution_context(conn, case.scope, order_id)
        assert plan == saved.plan and execution == saved.features["execution"]
        assert conn.execute("SELECT count(*) FROM v2_fills").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_egress_budgets WHERE used_weight>0"
        ).fetchone() == (1,)
    calls = list(case.calls)
    assert (
        case.producer.prepare(case.selected.identity)["status"] == "RECOVERY_REQUIRED"
    )
    assert case.calls == calls
    assert capital(fenced.admin, case.scope) == after


@pytest.mark.parametrize("trigger", ["stop", "route"])
def test_revocation_during_final_get_cannot_publish_capital_or_freeze(
    fenced, monkeypatch, trigger
):
    case = composition(fenced, monkeypatch)
    before = capital(fenced.admin, case.scope)

    def revoke(path):
        if (
            path == "/fapi/v3/positionRisk"
            and sum(p == path for _, p in case.calls) == 2
        ):
            if trigger == "stop":
                case.stop.set()
            else:
                route_update(fenced, "SET phase='DRAINING'")

    case.on_request = revoke
    with pytest.raises(ValueError, match="STOPPED|EXECUTION_FENCED"):
        case.producer.prepare(case.selected.identity)
    assert capital(fenced.admin, case.scope) == before
    assert_no_entry(fenced, case)


def test_account_collection_crossing_original_expiry_cannot_publish_or_freeze(
    fenced, monkeypatch
):
    case = composition(fenced, monkeypatch)
    before = capital(fenced.admin, case.scope)
    deadline = case.selected.snapshot["expires_at_ms"]
    case.on_request = lambda _: setattr(case, "clock", lambda: deadline)
    with pytest.raises(ValueError, match="STRATEGY_FACTS_DEADLINE"):
        case.producer.prepare(case.selected.identity)
    assert capital(fenced.admin, case.scope) == before
    assert_no_entry(fenced, case)


@pytest.mark.parametrize("field", ["capital.model_enabled", "portfolio_risk.enabled"])
def test_disabled_required_capital_guard_never_reads_account_or_publishes(
    fenced, monkeypatch, field
):
    case = composition(fenced, monkeypatch)
    policy = PolicyStore(fenced.admin, case.scope)
    policy.patch(
        {field: False, "leverage.adaptive_enabled": False},
        expected_version=policy.read().version,
        reason="disabled guard negative control",
    )
    before = capital(fenced.admin, case.scope)
    with pytest.raises(ValueError, match="CAPITAL_GUARDS_REQUIRED"):
        case.producer.prepare(case.selected.identity)
    assert not case.calls
    assert capital(fenced.admin, case.scope) == before
    assert_no_entry(fenced, case)


@pytest.mark.parametrize(
    "changes",
    [
        {"history.mode": "SOFT"},
        {"sizing.remaining_budget_enabled": True},
        {"reentry.mode": "ENFORCE"},
    ],
)
def test_unsupported_old_directional_controls_are_not_silently_ignored(
    fenced, monkeypatch, changes
):
    case = composition(fenced, monkeypatch)
    entry_controls.configure(
        fenced.admin,
        case.scope,
        changes,
        expected_version=0,
        reason="unsupported contract negative control",
    )
    before = capital(fenced.admin, case.scope)
    with pytest.raises(ValueError, match="STRATEGY_ENTRY_CONTROLS_UNSUPPORTED"):
        case.producer.prepare(case.selected.identity)
    assert not case.calls
    assert capital(fenced.admin, case.scope) == before
    assert_no_entry(fenced, case)
