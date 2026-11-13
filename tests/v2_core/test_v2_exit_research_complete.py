"""Production adapter -> captured exit -> research, with no real network/PG."""

import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from market_fakes import Budget

from services.v2_directional_exit_market import BinanceDirectionalExitMarket
from tests.v2_core.test_v2_directional_exit_market import Market, provider
from tests.v2_core.test_v2_exit_research_capture import (
    EPISODE,
    HOUR,
    NOW,
    SCOPE,
    payload,
    stage,
)
from v2_core.public_market import BinancePublicMarket


def execution_part(value):
    return {k: v for k, v in value.items() if k != "_research_hourly"}


@pytest.mark.parametrize("missing", range(5))
def test_extra_history_missing_does_not_change_twenty_bar_execution(missing):
    baseline = provider(research_hourly_bars=20)("BTCUSDT")
    market = Market()
    market.mutate = lambda path, rows: (
        rows[missing:] if path.endswith("klines") and len(rows) == 24 else rows
    )
    result = provider(market)("BTCUSDT")
    assert execution_part(result) == execution_part(baseline)
    assert len(result["_research_hourly"]["rows"]) == 24 - missing
    assert len(market.calls) == 4
    assert market.calls[-1][1]["limit"] == 24


@pytest.mark.parametrize("damage", [None, "bad", [], [0] * 12])
def test_invalid_older_research_rows_do_not_become_execution_dependency(damage):
    market = Market()

    def mutate(path, rows):
        if path.endswith("klines") and len(rows) == 24:
            rows[:4] = [deepcopy(damage) for _ in range(4)]
        return rows

    market.mutate = mutate
    assert execution_part(provider(market)("BTCUSDT")) == execution_part(
        provider(research_hourly_bars=20)("BTCUSDT")
    )


@pytest.mark.parametrize("damage", ["missing_latest", "invalid_required"])
def test_required_execution_window_still_fails_closed(damage):
    market = Market()

    def mutate(path, rows):
        if path.endswith("klines") and len(rows) == 24:
            if damage == "missing_latest":
                rows.pop()
            else:
                rows[-20][4] = "NaN"
        return rows

    market.mutate = mutate
    with pytest.raises(ValueError):
        provider(market)("BTCUSDT")


def test_early_history_keeps_execution_without_negative_request_start():
    market = Market(server=20 * HOUR)
    result = provider(market, clock=20 * HOUR)("BTCUSDT")
    assert len(result["_research_hourly"]["rows"]) == 20
    assert market.calls[-1][1]["startTime"] == 0


@pytest.mark.parametrize("bad", [True, 19, 21, 25, 73, "24", None])
def test_research_bounds_rejected_before_network(bad):
    market = Market()
    with pytest.raises(ValueError):
        provider(market, research_hourly_bars=bad)
    assert not market.calls


def quantity_proof():
    return {
        "account_scope": asdict(SCOPE),
        "response": {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"},
        "observed_at_ms": NOW - 200,
        "available_at_ms": NOW - 150,
    }


def complete_case(monkeypatch):
    case = stage(monkeypatch, allow_writes=True)
    hours = provider(Market(server=NOW), clock=NOW)("BTCUSDT")["_research_hourly"]
    case.observation["_research_hourly"] = hours
    proof = quantity_proof()
    case.service.audit.research_position = lambda target: deepcopy(proof)
    return case, proof


def test_existing_position_and_hourly_response_complete_research_without_new_io(
    monkeypatch,
):
    case, _ = complete_case(monkeypatch)
    result = case.service.evaluate(EPISODE)
    saved = payload(case)
    research = saved["research_snapshot"]
    assert result["status"] == "DISPATCHED"
    assert research["status"] == "AVAILABLE"
    assert research["qualification_gaps"] == []
    assert len(research["snapshot"]["hourly_history"]) == 24
    native = research["snapshot"]["native_protection"]
    assert native["protected_quantity"] == "1"
    assert native["observed_at_ms"] == NOW - 200
    assert native["available_at_ms"] == NOW
    assert len(case.venue.calls) == 2 and len(case.market_calls) == 1
    assert len(case.service.store.writes) == 2
    assert case.orders[0]["quantity"] == "1"
    assert case.orders[0]["request_key"] == "exit:full:low-yield-stagnation"


@pytest.mark.parametrize("bad", ["stale", "future", "account", "side", "quantity"])
def test_bad_optional_position_evidence_never_blocks_original_exit(monkeypatch, bad):
    case, proof = complete_case(monkeypatch)
    if bad == "stale":
        proof["observed_at_ms"] = NOW - 10000
    elif bad == "future":
        proof["available_at_ms"] = NOW + 1
    elif bad == "account":
        proof["account_scope"]["account_id"] = "another-qa-account"
    elif bad == "side":
        proof["response"]["positionAmt"] = "-1"
    else:
        proof["response"]["positionAmt"] = "0.5"
    assert case.service.evaluate(EPISODE)["status"] == "DISPATCHED"
    research = payload(case)["research_snapshot"]
    assert research["components"]["native_quantity"]["status"] == "UNKNOWN"
    assert research["snapshot"]["native_protection"] is None
    assert case.orders[0]["quantity"] == "1"
    assert len(case.venue.calls) == 2


def test_optional_position_projection_exception_cannot_stop_exit(monkeypatch):
    case, _ = complete_case(monkeypatch)

    def broken(_):
        raise RuntimeError("private-data-must-not-be-exported")

    case.service.audit.research_position = broken
    assert case.service.evaluate(EPISODE)["status"] == "DISPATCHED"
    assert payload(case)["research_snapshot"]["components"]["native_quantity"][
        "reason"
    ] == "NATIVE_POSITION_QUANTITY_NOT_SUPPLIED"


def test_one_actual_market_bundle_drives_execution_and_research(monkeypatch):
    case = stage(monkeypatch, allow_writes=True)
    market = Market(server=NOW)
    case.service.observe = provider(market, clock=NOW)
    case.service.audit.research_position = lambda target: quantity_proof()
    result = case.service.evaluate(EPISODE)
    saved = payload(case)
    assert result["status"] == "DISPATCHED"
    assert saved["decision"]["reason"] == "LOW_YIELD_STAGNATION"
    assert saved["facts"]["mark_price"] == "100.25"
    assert saved["facts"]["ema20_1h"] == "90.5"
    assert saved["research_snapshot"]["status"] == "AVAILABLE"
    assert saved["research_snapshot"]["snapshot"]["facts"] == saved["facts"]
    assert len(market.calls) == 4
    assert case.orders[0]["quantity"] == "1"
    assert case.orders[0]["request_key"] == "exit:full:low-yield-stagnation"


@pytest.mark.parametrize("version", ["binance-public-exit-v1", "binance-public-exit-v2"])
def test_actual_public_transport_contract_accepts_complete_exit_observation(version):
    """Exercise real request validation/budget, not just a callable market stub."""
    market = Market(server=NOW)
    budget = Budget()

    def factory(host, timeout):
        assert host == "demo-fapi.binance.com" and timeout == 5

        class Connection:
            def request(self, method, target, headers):
                assert method == "GET"
                url = urlsplit(target)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                for k in ("startTime", "endTime", "limit"):
                    if k in params:
                        params[k] = int(params[k])
                self.body = json.dumps(market(url.path, params)).encode()

            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda limit: self.body[:limit])

            def close(self):
                pass

        return Connection()

    transport = BinancePublicMarket(
        environment="SANDBOX", budget=budget, enabled=True, connection_factory=factory
    )
    result = BinanceDirectionalExitMarket(
        transport,
        environment="SANDBOX",
        clock_ms=lambda: NOW,
        exit_fee_rate=".0004",
        indicator_version=version,
    )("BTCUSDT")
    assert len(result["_research_hourly"]["rows"]) == (24 if version.endswith("v1") else 60)
    assert budget.weights == [1, 1, 1, 1]
    assert len(market.calls) == 4
