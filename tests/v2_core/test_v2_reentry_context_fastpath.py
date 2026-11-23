"""Pure boundary tests: definitive upstream refusals perform no storage I/O."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services import v2_reentry_gate as module
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import resolve

SCOPE = AccountScope("BINANCE", "synthetic-reentry-fastpath", "SANDBOX", "FUTURES")
NOW = 120_001
SIGNAL = {
    "symbol": "BTCUSDT",
    "observed_at": NOW - 1,
    "expires_at_ms": NOW + 30_000,
}


def forbidden(*_args, **_kwargs):
    pytest.fail("definitive rejection must not access policy/history/archive")


def provider(context, *, connect=forbidden, strategy="S6"):
    return module.StructuralReentryContext(
        connect,
        lambda _signal: context,
        scope=SCOPE,
        strategy=strategy,
        read_bars=forbidden,
        clock_ms=lambda: NOW,
    )


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize(
    "context",
    [
        {"market_only": True},
        {"universe_admission": {"eligible": False}},
        {"market_only": False, "universe_admission": {"eligible": False}},
        {"market_only": True, "universe_admission": {"eligible": True}},
    ],
)
def test_definitive_rejection_never_constructs_policy_or_reads_storage(
    monkeypatch, strategy, context
):
    monkeypatch.setattr(module, "PolicyStore", forbidden)
    monkeypatch.setattr(module, "prior_close", forbidden)
    monkeypatch.setattr(module, "assess", forbidden)
    context = {**context, "nested": {"unchanged": [1, 2]}}
    original = deepcopy(context)
    result = provider(context, strategy=strategy)(SIGNAL)
    assert result == original
    assert result is not context
    result["nested"]["unchanged"].append(3)
    assert context == original
    assert "reentry" not in result


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_eligible_candidate_reads_current_policy_every_time(monkeypatch, strategy):
    versions = iter((False, True, False))
    policy_reads, history_reads, statements = [], [], []

    class Policy:
        def __init__(self, connect, scope):
            assert connect is connection and scope == SCOPE

        def read(self):
            enabled = next(versions)
            policy_reads.append(enabled)
            return SimpleNamespace(values=resolve({"entry.reentry_enabled": enabled}))

    class Cursor:
        def execute(self, query):
            statements.append(query)

    @contextmanager
    def connection():
        yield Cursor()

    def previous(_conn, scope, target, now):
        history_reads.append((scope, target, now))
        return None, "NO_PREVIOUS_FILLED_EPISODE"

    monkeypatch.setattr(module, "PolicyStore", Policy)
    monkeypatch.setattr(module, "prior_close", previous)
    context = {
        "assembled_at": NOW - 100,
        "valid_until_ms": NOW + 30_000,
        "sources": {"s3": {"observed_at": NOW}},
        "universe_admission": {"eligible": True},
    }
    original = deepcopy(context)
    wrapper = provider(context, connect=connection, strategy=strategy)
    assert wrapper(SIGNAL) == original
    enabled = wrapper(SIGNAL)
    assert enabled["reentry"]["assessment"] == {
        "status": "NOT_APPLICABLE",
        "reason": "NO_PREVIOUS_FILLED_EPISODE",
    }
    assert enabled["reentry"]["side"] == ("BUY" if strategy == "S6" else "SELL")
    assert wrapper(SIGNAL) == original
    assert policy_reads == [False, True, False]
    assert history_reads == [(SCOPE, "BTCUSDT", NOW)]
    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout='2s'",
    ]
    assert context == original


@pytest.mark.parametrize(
    "context",
    [{}, {"market_only": False}, {"universe_admission": {"eligible": True}}],
)
def test_non_rejected_candidate_does_not_bypass_policy_failure(monkeypatch, context):
    class Policy:
        def __init__(self, *_args):
            pass

        def read(self):
            raise ValueError("SYNTHETIC_POLICY_UNAVAILABLE")

    monkeypatch.setattr(module, "PolicyStore", Policy)
    with pytest.raises(ValueError, match="SYNTHETIC_POLICY_UNAVAILABLE"):
        provider(context)(SIGNAL)
