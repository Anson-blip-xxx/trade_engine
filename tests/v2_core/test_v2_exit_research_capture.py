"""Prospective capture uses only existing calls; all stores/venues are fakes."""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import services.v2_directional_exit as stage_module
import services.v2_exit_research_snapshot as adapter
from services.v2_directional_exit import DirectionalExitStage
from v2_core.account_risk import AccountScope
from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.evidence import canonical, digest
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.runtime_policy import PolicySnapshot, PolicyStore, resolve
from v2_core.state import StateSnapshot, StateWrite

HOUR = 3_600_000
NOW = 72 * HOUR + 123_000
EPISODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SCOPE = AccountScope("BINANCE", "capture-qa", "SANDBOX", "FUTURES")


def forbidden(*args, **kwargs):
    raise AssertionError("research capture must not open a database or real venue")


class MemoryStore:
    def __init__(self):
        self.rows, self.requests, self.writes = {}, {}, []
        self.reads = 0

    def read(self, key):
        self.reads += 1
        return self.rows.get(key.identity)

    def change(self, key, *, expected_version, request_key, payload, reason):
        encoded = canonical(payload)
        identity = (key.identity, request_key)
        content = (expected_version, encoded, reason)
        if identity in self.requests:
            assert self.requests[identity] == content, "state request content conflict"
        previous = self.rows.get(key.identity)
        assert expected_version == (previous.version if previous else 0)
        version = expected_version + 1
        self.rows[key.identity] = StateSnapshot(version, encoded, False)
        self.requests[identity] = content
        self.writes.append((key.namespace, request_key, deepcopy(payload)))
        return StateWrite("APPLIED", version)


class QueryVenue:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self):
        self.calls, self.raw = [], None

    def __call__(self, method, path, params):
        assert (method, path) == ("GET", "/fapi/v1/algoOrder")
        self.calls.append((method, path, deepcopy(params)))
        return deepcopy(self.raw)


def hourly_capture():
    return {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "interval": "1h",
        "rows": [
            [t, "100", "101", "99", "100", "20", t + HOUR - 1, "0", 1, "0", "0", "0"]
            for t in range(52 * HOUR, 72 * HOUR, HOUR)
        ],
    }


def stage(
    monkeypatch, *, private=True, mark="100.2", held_ms=90 * 60_000, allow_writes=False
):
    service = object.__new__(DirectionalExitStage)
    service.connect, service.scope = forbidden, SCOPE
    service.clock, service.allow_writes = lambda: NOW, allow_writes
    service.quant_market = None
    service.store = MemoryStore()
    spec = ProtectionSpec(EPISODE, "BTCUSDT", "SELL", "STOP_MARKET", "95")
    venue = QueryVenue()
    service.protection = TestnetProtection(service.store, venue, scope=SCOPE)
    venue.raw = {
        **service.protection.params(spec),
        "orderType": "STOP_MARKET",
        "closePosition": True,
        "priceProtect": False,
        "algoStatus": "NEW",
        "algoId": 42,
        # Unverified metadata must never become fixed quantity/time evidence.
        "actualQty": "999",
        "quantity": "999",
        "updateTime": NOW - HOUR,
    }
    service.store.rows[service.protection.key(spec).identity] = StateSnapshot(
        1,
        canonical(
            {"spec": asdict(spec), "status": "NEW", "observation": deepcopy(venue.raw)}
        ),
        False,
    )
    service.stop = SimpleNamespace(plan=lambda _: spec)
    trace = {
        "intent_id": EPISODE,
        "data_revision": 1,
        "scope": {**asdict(SCOPE), "producer": "s6"},
        "request": {"symbol": "BTCUSDT", "side": "BUY"},
        "configuration": {
            "mode": "TESTNET_ADMISSION_ONLY",
            "account_scope": asdict(SCOPE),
        },
        "decision": {
            "source": "s3",
            "features": {
                "evaluation": {
                    "admission_authorized": True,
                    "execution_market_gate": "MARKET_GATES_PASSED",
                    "market_plan": {"system_tag": "S6A"},
                    "sizing": {
                        "reason": "SIZED",
                        "planned_loss": "5",
                        "stop_price": "95",
                    },
                },
                "context": {"directional": {"sizing": {"quantity_step": "0.1"}}},
            },
        },
        "orders": [{"order_id": "open", "leg": "OPEN", "status": "FILLED"}],
        "fills": [
            {
                "order_id": "open",
                "quantity": "1",
                "price": "100",
                "occurred_at_ms": NOW - held_ms,
            }
        ],
    }
    order_calls, dispatch_calls, effects, market_calls = [], [], [], []

    def prepare(episode, **kwargs):
        assert episode == EPISODE
        order_calls.append(deepcopy(kwargs))
        return "close-order", "close-client"

    def dispatch(order_id, **kwargs):
        dispatch_calls.append((order_id, kwargs))
        return "ACKNOWLEDGED"

    service.data = SimpleNamespace(
        trace=lambda _: deepcopy(trace), orders=SimpleNamespace(prepare=prepare)
    )
    service.runtime = SimpleNamespace(execution=SimpleNamespace(dispatch=dispatch))
    observation = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "observed_at_ms": NOW - 100,
        "mark_price": mark,
        "funding_rate": "0",
        "ema9_1h": "100.2",
        "ema20_1h": "100",
        "momentum_closes_15m": ["100", "100", "100", "100.2"],
        "exit_fee_rate": ".0004",
        "source": "binance-public-exit-v1",
        "exchange_server_time_ms": NOW - 110,
        "closed_15m_at_ms": NOW // 900_000 * 900_000,
        "closed_1h_at_ms": NOW // HOUR * HOUR,
    }
    if private:
        observation["_research_hourly"] = hourly_capture()

    def observe(symbol):
        market_calls.append(symbol)
        return deepcopy(observation)

    service.observe = observe
    policy = PolicySnapshot(0, canonical(resolve()))
    monkeypatch.setattr(PolicyStore, "read", lambda _: policy)
    monkeypatch.setattr(
        stage_module, "session_advisory_lock", lambda *_: nullcontext(True)
    )
    monkeypatch.setattr(stage_module, "coverage_from_facts", lambda *_, **__: [])
    monkeypatch.setattr(
        stage_module,
        "persist_inventory",
        lambda *_, **__: effects.append("persist_inventory"),
    )
    monkeypatch.setattr(
        stage_module, "halt_opening", lambda *_, **__: effects.append("halt_opening")
    )
    service.audit = SimpleNamespace(
        facts=dict,
        excluded_position_symbols=(),
        inventory=SimpleNamespace(
            collect=lambda _: {
                "observation_id": "capture-qa-inventory",
                "started_at_ms": NOW,
                "finished_at_ms": NOW,
            }
        ),
    )
    return SimpleNamespace(
        service=service,
        venue=venue,
        observation=observation,
        orders=order_calls,
        dispatches=dispatch_calls,
        effects=effects,
        market_calls=market_calls,
        spec=spec,
    )


def payload(case):
    return json.loads(
        case.service.store.rows[case.service._key(EPISODE).identity].payload_json
    )


def test_capture_freezes_real_availability_without_claiming_quantity(monkeypatch):
    case = stage(monkeypatch)
    answer = case.service.evaluate(EPISODE)
    saved = payload(case)
    assert answer["reason"] == "LOW_YIELD_STAGNATION"
    assert answer["status"] == "ACTION_DISABLED"
    research = saved["research_snapshot"]
    assert saved["research_snapshot_digest"] == digest(canonical(research))
    assert research["status"] == "PARTIAL"
    assert research["snapshot"]["observation"]["available_at_ms"] == NOW
    assert research["snapshot"]["observation"]["observed_at_ms"] == NOW - 100
    hours = research["snapshot"]["hourly_history"]
    assert len(hours) == 20
    assert {row["available_at_ms"] for row in hours} == {NOW}
    assert research["components"]["hourly_history"]["status"] == "PARTIAL"
    assert research["components"]["native_terms"]["status"] == "AVAILABLE"
    assert research["components"]["native_quantity"]["status"] == "UNKNOWN"
    assert research["snapshot"]["native_protection"] is None
    assert not research["execution_authorized"] and not research["promotion_authorized"]
    expected = {k: v for k, v in case.observation.items() if k != "_research_hourly"}
    assert saved["observation"] == expected
    assert len(case.venue.calls) == len(case.market_calls) == 1
    assert len(case.service.store.writes) == 1
    assert "actualQty" not in canonical(research) and "updateTime" not in canonical(
        research
    )


@pytest.mark.parametrize(
    "mark,held,action",
    [
        ("100.2", 60_000, "WAIT"),
        ("95", 90 * 60_000, "WAIT_NATIVE_STOP"),
        ("105.1", 60_000, "PARTIAL"),
        ("100.2", 90 * 60_000, "CLOSE"),
    ],
)
def test_capture_does_not_change_real_action_order_quantity_or_request_key(
    monkeypatch,
    mark,
    held,
    action,
):
    cases = []
    for private in (False, True):
        case = stage(
            monkeypatch, private=private, mark=mark, held_ms=held, allow_writes=True
        )
        result = case.service.evaluate(EPISODE)
        saved = payload(case)
        facts = ExitFacts(**saved["facts"])
        assert evaluate_directional_exit(facts, policy=saved["policy"]).action == action
        assert saved["decision"]["action"] == action
        if action in {"CLOSE", "PARTIAL"}:
            assert result["status"] == "DISPATCHED"
            assert case.orders[0]["quantity"] == ("1" if action == "CLOSE" else "0.5")
            assert case.orders[0]["request_key"] == (
                "exit:full:low-yield-stagnation"
                if action == "CLOSE"
                else "exit:partial-take-profit"
            )
            assert len(case.venue.calls) == 2  # unchanged original preflight query
            assert case.effects == ["persist_inventory", "halt_opening"]
            assert len(case.service.store.writes) == 2
        else:
            assert result["status"] == action and not case.orders
            assert len(case.venue.calls) == len(case.service.store.writes) == 1
        if action != "CLOSE":
            assert "research_snapshot" not in saved
            assert "research_snapshot_digest" not in saved
        assert "_research_hourly" not in canonical(saved)
        cases.append(
            (
                saved["decision"],
                case.orders,
                case.venue.calls,
                case.service.store.reads,
                case.market_calls,
                case.effects,
            )
        )
    assert cases[0] == cases[1]


@pytest.mark.parametrize(
    "damage",
    ["bad_ohlc", "oversized_hours", "wrong_source", "unknown_provider", "bad_terms"],
)
def test_partial_component_failure_does_not_block_stagnation_exit(monkeypatch, damage):
    case = stage(monkeypatch, allow_writes=True)
    if damage == "bad_ohlc":
        case.observation["_research_hourly"]["rows"][0][2] = "invalid"
    elif damage == "oversized_hours":
        case.observation["_research_hourly"]["rows"] *= 4
    elif damage == "wrong_source":
        case.observation["_research_hourly"]["market_source"] = "BINANCE_USDM_LIVE"
    elif damage == "unknown_provider":
        case.observation["source"] = "UNKNOWN_PROVIDER"
    else:
        original = case.service.protection.query

        def bad_terms(spec, **kwargs):
            result = original(spec, **kwargs)
            if "research_terms" in result:
                result["research_terms"]["symbol"] = "ETHUSDT"
            return result

        case.service.protection.query = bad_terms
    assert case.service.evaluate(EPISODE)["status"] == "DISPATCHED"
    saved = payload(case)
    component = "native_terms" if damage == "bad_terms" else "hourly_history"
    assert saved["research_snapshot"]["components"][component]["status"] == "UNKNOWN"
    assert saved["research_snapshot"]["components"]["observation"]["status"] == (
        "UNKNOWN" if damage == "unknown_provider" else "AVAILABLE"
    )
    assert case.orders[0]["quantity"] == "1"
    assert case.orders[0]["request_key"] == "exit:full:low-yield-stagnation"
    assert len(case.venue.calls) == 2 and len(case.market_calls) == 1


@pytest.mark.parametrize("failure", ["exception", "oversized", "non_json"])
def test_adapter_failure_has_bounded_failsoft_evidence(monkeypatch, failure):
    def broken(**kwargs):
        if failure == "exception":
            raise RuntimeError("untrusted-provider-error-must-not-be-saved")
        return {"value": "x" * 65537 if failure == "oversized" else object()}

    monkeypatch.setattr(adapter, "build_exit_research_snapshot", broken)
    case = stage(monkeypatch, allow_writes=True)
    assert case.service.evaluate(EPISODE)["status"] == "DISPATCHED"
    research = payload(case)["research_snapshot"]
    assert research["status"] == "UNKNOWN"
    assert research["reason"] == "RESEARCH_CAPTURE_UNAVAILABLE"
    assert len(canonical(research)) < 512
    assert case.orders[0]["request_key"] == "exit:full:low-yield-stagnation"


def test_query_clock_failure_is_unknown_and_does_not_replace_execution_clock(
    monkeypatch,
):
    case = stage(monkeypatch, allow_writes=True)
    readings = iter([RuntimeError("clock failed"), True])

    def clock():
        value = next(readings, NOW)
        if isinstance(value, Exception):
            raise value
        return value

    case.service.clock = clock
    assert case.service.evaluate(EPISODE)["status"] == "DISPATCHED"
    research = payload(case)["research_snapshot"]
    assert research["components"]["native_terms"]["status"] == "UNKNOWN"
    assert research["snapshot"]["observation"]["available_at_ms"] == NOW


def test_research_hash_participates_in_archive_identity_not_order_identity(monkeypatch):
    case = stage(monkeypatch)
    case.service.evaluate(EPISODE)
    first = case.service.store.writes[-1][1]
    case.observation["_research_hourly"]["rows"][0][2] = "102"
    case.service.evaluate(EPISODE)
    assert case.service.store.writes[-1][1] != first
    assert len(case.service.store.writes) == 2
    # Identical frozen content at identical local times remains idempotent.
    case.service.evaluate(EPISODE)
    assert len(case.service.store.writes) == 2
    assert len(case.venue.calls) == 3


def test_native_projection_reuses_query_without_persisting_capture_metadata(
    monkeypatch,
):
    case = stage(monkeypatch)
    key = case.service.protection.key(case.spec)
    before = case.service.store.rows[key.identity]
    for _ in range(2):
        result = case.service.protection.query(case.spec, research=True)
        terms = result["research_terms"]
        assert terms["closePosition"] is True and terms["algoStatus"] == "NEW"
        assert not {"quantity", "actualQty", "updateTime", "available_at_ms"} & set(
            terms
        )
        assert result["algo_id"] == 42
    assert case.service.store.rows[key.identity] == before
    assert not case.service.store.writes and len(case.venue.calls) == 2


def test_default_native_query_and_installer_results_do_not_expand_journals(monkeypatch):
    case = stage(monkeypatch)
    expected = {
        "status": "NEW",
        "algo_id": 42,
        "client_algo_id": case.venue.raw["clientAlgoId"],
    }
    assert case.service.protection.query(case.spec) == expected
    assert case.service.protection.submit_once(case.spec) == expected
    assert not case.service.store.writes and len(case.venue.calls) == 2
