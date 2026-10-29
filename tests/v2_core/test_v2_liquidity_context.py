import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from market_fakes import Budget, MarketHTTP
from test_v2_directional_replay import SCOPE

import services.v2_liquidity_context as module
from services.v2_liquidity_context import LiquidityContext, LiveDepth
from v2_core.public_market import BinancePublicMarket, PublicMarketError


def raw_book():
    return {
        "T": 1000,
        "E": 1001,
        "lastUpdateId": 100,
        "bids": [["99.99", "10"]],
        "asks": [["100.01", "10"]],
    }


def live_transport(call):
    call.environment = "LIVE"
    return call


def test_injected_testnet_transport_cannot_be_mislabeled_as_live():
    with pytest.raises(ValueError, match="LIVE_TRANSPORT_REQUIRED"):
        LiveDepth(None, clock_ms=lambda: 1002, transport=lambda *_: raw_book())


def test_default_live_depth_constructs_without_io_with_separate_market_budget():
    depth = LiveDepth(None, clock_ms=lambda: 1002)
    assert depth.transport.environment == "LIVE"
    assert depth.transport.depth is True
    assert depth.transport.timeout == 1
    assert depth.transport.budget.environment == "LIVE"
    assert depth.transport.budget.lane == "market"


def test_live_depth_explicit_host_budget_and_no_credentials():
    http, budget = MarketHTTP(raw=json.dumps(raw_book()).encode()), Budget()
    transport = BinancePublicMarket(
        environment="LIVE",
        budget=budget,
        enabled=True,
        timeout=1,
        depth=True,
        connection_factory=http,
    )
    result = LiveDepth(None, clock_ms=lambda: 1002, transport=transport)("BTCUSDT")
    assert result["source_environment"] == "LIVE"
    assert result["observed_at_ms"] == 1000
    assert result["received_at_ms"] == 1002
    assert result["last_update_id"] == 100
    assert result["requested_limit"] == 100
    assert len(result["raw_digest"]) == 64
    assert budget.weights == [5]
    assert len(http.calls) == http.closed == 1
    call = http.calls[0]
    assert call[0] == "fapi.binance.com"
    assert call[2] == "GET" and call[4] == {"Accept": "application/json"}


@pytest.mark.parametrize(
    "environment,enabled_depth,params",
    [
        ("SANDBOX", True, {"symbol": "BTCUSDT", "limit": 100}),
        ("LIVE", False, {"symbol": "BTCUSDT", "limit": 100}),
        ("LIVE", True, {"symbol": "BTCUSDT", "limit": 1000}),
        ("LIVE", True, {"symbol": "BTCUSDT", "limit": 100.0}),
        ("LIVE", True, {"symbol": "BTCUSDT", "limit": 100, "apiKey": "forbidden"}),
    ],
)
def test_depth_transport_is_opt_in_bounded_live_only(
    environment, enabled_depth, params
):
    http, budget = MarketHTTP(), Budget()
    transport = BinancePublicMarket(
        environment=environment,
        budget=budget,
        enabled=True,
        depth=enabled_depth,
        connection_factory=http,
    )
    with pytest.raises(PublicMarketError, match="ENDPOINT_DISABLED"):
        transport("/fapi/v1/depth", params)
    assert not http.calls and not budget.weights


@pytest.mark.parametrize(
    "failure",
    [
        {"T": 2000},
        {"T": True},
        {"E": 2000},
        {"lastUpdateId": 0},
        {"bids": []},
        {"asks": [["1", "1"]] * 101},
        {"T": None},
    ],
)
def test_bad_depth_envelope_is_unknown_not_synthesized(failure):
    result = LiveDepth(
        None,
        clock_ms=lambda: 1002,
        transport=live_transport(lambda *_: {**raw_book(), **failure}),
    )("BTCUSDT")
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == "DEPTH_UNAVAILABLE"
    assert "bids" not in result and "observed_at_ms" not in result


@pytest.mark.parametrize(
    "exception,reason",
    [
        (PublicMarketError("QUOTA_DENIED"), "QUOTA_DENIED"),
        (PublicMarketError("RATE_LIMITED"), "RATE_LIMITED"),
        (TimeoutError("sensitive"), "DEPTH_UNAVAILABLE"),
        (PublicMarketError("sensitive"), "DEPTH_UNAVAILABLE"),
    ],
)
def test_read_failures_have_one_attempt_no_secret_no_fallback(exception, reason):
    calls = []

    def transport(*args):
        calls.append(args)
        raise exception

    result = LiveDepth(
        None, clock_ms=lambda: 1002, transport=live_transport(transport)
    )("BTCUSDT")
    assert len(calls) == 1
    assert result["status"] == "UNAVAILABLE" and result["reason"] == reason
    assert "sensitive" not in json.dumps(result)


@pytest.fixture
def context_case(monkeypatch):
    context = {
        "assembled_at": 1000,
        "valid_until_ms": 20000,
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
    }
    control = SimpleNamespace(
        values={"mode": "ENFORCE", "max_age_ms": 5000},
        snapshot=lambda: {"frozen": "configured"},
    )
    monkeypatch.setattr(module, "read", lambda *_: control)
    monkeypatch.setattr(
        module,
        "PolicyStore",
        lambda *_: SimpleNamespace(read=lambda: SimpleNamespace(values={})),
    )
    monkeypatch.setattr(
        module,
        "replay_decision",
        lambda *_: SimpleNamespace(rationale="DIRECTIONAL_REPLAY_ONLY"),
    )
    calls = []
    book = {"observed_at_ms": 1001, "received_at_ms": 1002}

    def depth(symbol):
        calls.append(symbol)
        return deepcopy(book)

    provider = LiquidityContext(
        None,
        lambda _: context,
        scope=SCOPE,
        strategy="S6",
        source="s3",
        clock_ms=lambda: 1002,
        depth=depth,
    )
    signal = {"symbol": "BTCUSDT", "expires_at_ms": 20000}
    return provider, signal, context, control, calls, book


def test_enforce_clamps_deadline_without_mutating_delegate(context_case):
    provider, signal, context, _, calls, _ = context_case
    original = deepcopy(context)
    result = provider(signal)
    assert calls == ["BTCUSDT"] and context == original
    assert result["environment"] == "SANDBOX"
    assert result["assembled_at"] == 1000 and result["valid_until_ms"] == 6001
    assert result["liquidity_as_of_ms"] == 1002
    assert result["liquidity_controls"] == {"frozen": "configured"}


def test_shadow_does_not_clamp_execution_deadline(context_case):
    provider, signal, _, control, calls, _ = context_case
    control.values["mode"] = "SHADOW"
    result = provider(signal)
    assert calls == ["BTCUSDT"] and result["valid_until_ms"] == 20000


@pytest.mark.parametrize("skip", ["OFF", "market_only", "universe", "prior_gate"])
def test_no_http_for_disabled_or_already_rejected_candidate(
    context_case, monkeypatch, skip
):
    provider, signal, context, control, calls, _ = context_case
    if skip == "OFF":
        control.values["mode"] = "OFF"
    elif skip == "market_only":
        context["market_only"] = True
    elif skip == "universe":
        context["universe_admission"] = {"eligible": False}
    else:
        monkeypatch.setattr(
            module,
            "replay_decision",
            lambda *_: SimpleNamespace(rationale="LOW_QUALITY"),
        )
    result = provider(signal)
    assert not calls and "liquidity_evidence" not in result
    assert result["assembled_at"] == context["assembled_at"]


def test_control_change_during_fetch_requires_new_context(context_case, monkeypatch):
    provider, signal, _, control, _, _ = context_case
    other = SimpleNamespace(snapshot=lambda: {"frozen": "changed"})
    replies = iter([control, other])
    monkeypatch.setattr(module, "read", lambda *_: next(replies))
    with pytest.raises(ValueError, match="CHANGED_DURING_CONTEXT"):
        provider(signal)


def test_fetch_cannot_extend_original_account_deadline(context_case):
    provider, signal, context, _, _, _ = context_case
    context["valid_until_ms"] = 1002
    with pytest.raises(ValueError, match="LIQUIDITY_CONTEXT_EXPIRED"):
        provider(signal)


def test_stale_depth_is_preserved_for_explicit_gate_rejection(context_case):
    provider, signal, _, control, _, book = context_case
    control.values["max_age_ms"] = 100
    book["observed_at_ms"] = 1
    result = provider(signal)
    assert result["valid_until_ms"] == 20000
    assert result["liquidity_evidence"]["observed_at_ms"] == 1
