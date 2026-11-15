"""Causal expiry classification; pure memory, no account or exchange I/O."""

from copy import deepcopy
from dataclasses import asdict

import pytest

from services.v2_directional_context import DirectionalContext
from v2_core.account_risk import AccountScope
from v2_core.scheduling import candidate_wait_reason, diagnostic_code

SCOPE = AccountScope("BINANCE", "context-expiry-qa", "SANDBOX", "FUTURES")


def case(*, first=1000, second=1000, screen=False):
    signal = {"symbol": "HANAUSDT", "observed_at": 900, "expires_at_ms": 2000}
    market = {
        "environment": "SANDBOX",
        "symbol": signal["symbol"],
        "assembled_at": 900,
        "valid_until_ms": 1500,
        "sources": {
            "s3": {"features": {"15m": {"close": "100"}}},
            "s0": {"features": {"regime": "neutral"}},
        },
    }
    account = {
        "account_scope": asdict(SCOPE),
        "symbol": signal["symbol"],
        "evidence": {"source": "qa"},
        "observed_at_ms": 900,
        "valid_until_ms": 1800,
        "short_ratio": "0.5",
        "history": {},
        "sizing": {},
        "expected_move_pct": "1",
        "funding_rate": "0",
    }
    calls = []
    clocks = iter((first, second))
    provider = DirectionalContext(
        lambda _: deepcopy(market),
        lambda _: calls.append("account") or deepcopy(account),
        scope=SCOPE,
        clock_ms=lambda: next(clocks),
        market_screen=lambda *_: calls.append("screen") or screen,
    )
    return provider, signal, market, account, calls


def failure(provider, signal, code, *, wait=False):
    with pytest.raises(ValueError, match=f"^{code}$") as caught:
        provider(signal)
    assert diagnostic_code(caught.value) == code
    assert candidate_wait_reason(caught.value, stage="CONTEXT") == (
        "CANDIDATE_CONTEXT_EXPIRED" if wait else None
    )
    assert candidate_wait_reason(caught.value, stage="CONSUME") is None


@pytest.mark.parametrize("late", [0, 1, 100])
@pytest.mark.parametrize("source", ["market", "signal"])
def test_proven_expiry_before_screen_never_reads_account(source, late):
    provider, signal, market, _, calls = case(first=1500 + late, screen=True)
    if source == "signal":
        signal["expires_at_ms"] = 1500
        market["valid_until_ms"] = 2000
    code = "SIGNAL_EXPIRED_DURING_CONTEXT" if source == "signal" else "STALE_CONTEXT"
    failure(provider, signal, code, wait=True)
    assert calls == []


@pytest.mark.parametrize("source", ["market", "account", "signal"])
@pytest.mark.parametrize("late", [0, 1])
def test_proven_expiry_during_account_collection_is_a_bounded_wait(source, late):
    provider, signal, market, account, calls = case(second=1500 + late)
    market["valid_until_ms"] = 2000
    if source == "signal":
        signal["expires_at_ms"] = 1500
    else:
        {"market": market, "account": account}[source]["valid_until_ms"] = 1500
    code = "SIGNAL_EXPIRED_DURING_CONTEXT" if source == "signal" else "STALE_CONTEXT"
    failure(provider, signal, code, wait=True)
    assert calls == ["screen", "account"]


@pytest.mark.parametrize(
    "source,key,value",
    [
        ("market", "environment", "LIVE"),
        ("market", "symbol", "BTCUSDT"),
        ("account", "symbol", "BTCUSDT"),
        ("account", "account_scope", {**asdict(SCOPE), "account_id": "other"}),
    ],
)
def test_scope_mismatch_is_hard_even_when_other_evidence_expired(source, key, value):
    provider, signal, market, account, _ = case(second=2000)
    {"market": market, "account": account}[source][key] = value
    failure(provider, signal, "CONTEXT_SCOPE_MISMATCH")


@pytest.mark.parametrize("value", [{}, [], None, "not evidence"])
def test_bad_account_evidence_cannot_be_hidden_by_expiry(value):
    provider, signal, _, account, _ = case(second=2000)
    account["evidence"] = value
    failure(provider, signal, "ACCOUNT_CONTEXT_EVIDENCE_INVALID")


@pytest.mark.parametrize("source", ["signal", "market", "account"])
@pytest.mark.parametrize("value", [True, -1, "1000", 1000.0, None])
def test_malformed_timestamp_is_hard_not_a_candidate_wait(source, value):
    provider, signal, market, account, _ = case(second=2000)
    data = {"signal": signal, "market": market, "account": account}[source]
    key = {"signal": "observed_at", "market": "assembled_at", "account": "observed_at_ms"}[source]
    data[key] = value
    failure(provider, signal, "INVALID_CONTEXT_TIMESTAMPS")


@pytest.mark.parametrize("source", ["signal", "market", "account"])
@pytest.mark.parametrize("invalid_end", [899, 900, True, "1500"])
def test_empty_inverted_or_malformed_interval_is_not_staleness(source, invalid_end):
    provider, signal, market, account, _ = case()
    data = {"signal": signal, "market": market, "account": account}[source]
    data["expires_at_ms" if source == "signal" else "valid_until_ms"] = invalid_end
    failure(provider, signal, "INVALID_CONTEXT_TIMESTAMPS")


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("value", [True, -1, 1000.0, None])
def test_invalid_clock_is_hard(phase, value):
    provider, signal, *_ = case(**{"first" if phase == "before" else "second": value})
    failure(provider, signal, "INVALID_CONTEXT_TIMESTAMPS")


@pytest.mark.parametrize("source", ["signal", "market", "account"])
def test_future_source_is_hard_even_when_signal_expires_during_collection(source):
    provider, signal, market, account, _ = case(second=2000)
    data = {"signal": signal, "market": market, "account": account}[source]
    key = {"signal": "observed_at", "market": "assembled_at", "account": "observed_at_ms"}[source]
    data[key] = 2100
    data["expires_at_ms" if source == "signal" else "valid_until_ms"] = 3000
    failure(provider, signal, "FUTURE_CONTEXT")


@pytest.mark.parametrize("screen", [False, True])
def test_fresh_context_and_rejection_only_fast_path_are_unchanged(screen):
    provider, signal, market, _, calls = case(screen=screen)
    before = deepcopy((signal, market))
    output = provider(signal)
    assert (signal, market) == before
    assert calls == (["screen"] if screen else ["screen", "account"])
    assert output.get("market_only", False) is screen
    assert ("account_evidence" in output) is not screen
    assert output["valid_until_ms"] == 1500
