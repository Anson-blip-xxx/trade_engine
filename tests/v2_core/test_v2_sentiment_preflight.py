"""Public-only cache and early rejection; no venue or model network is used."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Lock

import pytest
from test_v2_directional_account_context import build, signal
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_account_context import _PublicSentimentCache, _short_ratio

database = database_fixture
NOW = 1_800_000_000_000
MAX_AGE = 7_200_000


class PublicRatio:
    environment = "LIVE"

    def __init__(self):
        self.calls = []
        self.timestamp = NOW
        self.failure = None
        self.lock = Lock()

    def __call__(self, path, params):
        assert path == "/futures/data/globalLongShortAccountRatio"
        assert params["period"] == "1h" and params["limit"] == 3
        with self.lock:
            self.calls.append((path, deepcopy(params)))
        if self.failure is not None:
            raise self.failure
        return [
            {
                "symbol": params["symbol"],
                "shortAccount": "0.47",
                "timestamp": self.timestamp,
            }
        ]


def cache(**kwargs):
    port, clock = PublicRatio(), [NOW]
    return _PublicSentimentCache(port, lambda: clock[0], **kwargs), port, clock


def test_only_public_route_is_cached_and_original_source_time_is_unchanged():
    cached, port, clock = cache()
    first = cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    first[0]["shortAccount"] = "0.99"
    first[0]["timestamp"] = NOW + 500
    clock[0] += 29_999
    second = cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert second == [{"symbol": "BTCUSDT", "shortAccount": "0.47", "timestamp": NOW}]
    assert len(port.calls) == 1
    assert len(cached._entries) == 1


def test_exact_ttl_boundary_refreshes_without_renewing_old_source_time():
    cached, port, clock = cache()
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    clock[0] += 30_000
    refreshed = cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert len(port.calls) == 2 and refreshed[0]["timestamp"] == NOW


def test_cached_event_expires_at_original_source_boundary_not_cache_ttl():
    cached, port, clock = cache()
    port.timestamp = NOW - MAX_AGE
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    clock[0] += 1
    with pytest.raises(ValueError, match="^SHORT_RATIO_STALE$"):
        cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert len(port.calls) == 2 and not cached._entries
    port.timestamp = clock[0]
    assert cached.read("BTCUSDT", max_age_ms=MAX_AGE)[0]["timestamp"] == clock[0]
    assert len(port.calls) == 3


def test_source_environment_symbol_and_tightened_age_do_not_reuse_other_evidence():
    cached, port, _ = cache()
    port.timestamp = NOW - 200
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    cached.read("ETHUSDT", max_age_ms=MAX_AGE)
    port.environment = "SANDBOX"
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert len(port.calls) == 3
    with pytest.raises(ValueError, match="^SHORT_RATIO_STALE$"):
        cached.read("BTCUSDT", max_age_ms=199)
    assert len(port.calls) == 4


def test_clock_rollback_never_accepts_cached_future_event():
    cached, port, clock = cache()
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    clock[0] -= 1
    with pytest.raises(ValueError, match="^SHORT_RATIO_STALE$"):
        cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert len(port.calls) == 2 and not cached._entries


def test_lru_is_bounded_and_recent_hit_keeps_only_that_public_entry():
    cached, port, _ = cache(max_entries=2)
    for target in ("BTCUSDT", "ETHUSDT", "BTCUSDT", "SOLUSDT", "ETHUSDT"):
        cached.read(target, max_age_ms=MAX_AGE)
        assert len(cached._entries) <= 2
    assert [params["symbol"] for _, params in port.calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "ETHUSDT",
    ]


def test_concurrent_same_source_symbol_read_is_single_flight():
    cached, port, _ = cache()
    barrier = Barrier(8)

    def read(_):
        barrier.wait()
        return cached.read("BTCUSDT", max_age_ms=MAX_AGE)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(8)))
    assert len(port.calls) == 1 and all(row == results[0] for row in results)
    assert len({id(row) for row in results}) == 8


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("secret-not-cached"),
        ConnectionError("opaque"),
        ValueError("ADMISSION_BUDGET_UNAVAILABLE"),
    ],
)
def test_failures_are_not_cached_or_defaulted(error):
    cached, port, _ = cache()
    port.failure = error
    for _ in range(2):
        with pytest.raises(type(error)) as caught:
            cached.read("BTCUSDT", max_age_ms=MAX_AGE)
        assert caught.value is error
    assert len(port.calls) == 2 and not cached._entries
    port.failure = None
    assert cached.read("BTCUSDT", max_age_ms=MAX_AGE)[0]["shortAccount"] == "0.47"


@pytest.mark.parametrize(
    "response,reason",
    [
        ([], "SHORT_RATIO_MISSING"),
        ([{}], "SHORT_RATIO_SCOPE"),
        (
            [{"symbol": "BTCUSDT", "shortAccount": "0.4", "timestamp": True}],
            "SHORT_RATIO_TIMESTAMP",
        ),
        (
            [{"symbol": "BTCUSDT", "shortAccount": "0.4", "timestamp": NOW + 1}],
            "SHORT_RATIO_STALE",
        ),
    ],
)
def test_invalid_or_missing_public_evidence_is_not_cached(response, reason):
    class Invalid:
        environment = "LIVE"
        calls = 0

        def __call__(self, *_):
            self.calls += 1
            return deepcopy(response)

    port = Invalid()
    cached = _PublicSentimentCache(port, lambda: NOW)
    for _ in range(2):
        with pytest.raises(ValueError, match=reason):
            cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert port.calls == 2 and not cached._entries


def test_cache_can_be_disabled_and_oversized_public_response_is_not_retained():
    cached, port, _ = cache(ttl_ms=0)
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    cached.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert len(port.calls) == 2 and not cached._entries

    class Oversized(PublicRatio):
        def __call__(self, *args):
            result = super().__call__(*args)
            result[0]["extra"] = "x" * 20_000
            return result

    large = _PublicSentimentCache(Oversized(), lambda: NOW)
    large.read("BTCUSDT", max_age_ms=MAX_AGE)
    assert not large._entries


@pytest.mark.parametrize(
    "settings",
    [
        {"ttl_ms": -1},
        {"ttl_ms": True},
        {"ttl_ms": 60001},
        {"max_entries": 0},
        {"max_entries": 1025},
    ],
)
def test_cache_parameters_are_bounded(settings):
    with pytest.raises(ValueError, match="SENTIMENT_CACHE_BOUNDS"):
        cache(**settings)


def test_stale_sentiment_costs_one_public_read_and_zero_private_reads(database):
    provider, request, public, clock, _ = build(database)
    public.ratio_time = clock.now - MAX_AGE - 1
    for _ in range(2):
        with pytest.raises(ValueError, match="^SHORT_RATIO_STALE$"):
            provider(signal())
    assert request.calls == []
    assert [path for path, _ in public.calls] == [
        "/futures/data/globalLongShortAccountRatio"
    ] * 2
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='directional-account-context-v1'"
        ).fetchone() == (0,)


def test_public_preflight_failure_cannot_spend_private_quota(database):
    provider, request, public, _, _ = build(database)

    def denied(*_):
        raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")

    denied.environment = "LIVE"
    provider.sentiment = denied
    with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
        provider(signal())
    assert request.calls == [] and public.calls == []


def test_slow_sentiment_preflight_aborts_before_private_reads(database):
    provider, request, public, clock, _ = build(database)

    def slow(*args):
        result = public(*args)
        clock.now += 15_001
        return result

    slow.environment = "SANDBOX"
    provider.sentiment = slow
    with pytest.raises(ValueError, match="^ACCOUNT_CONTEXT_DEADLINE$"):
        provider(signal())
    assert request.calls == []


def test_ratio_that_expires_during_private_collection_is_not_published(database):
    provider, request, public, clock, _ = build(database)
    public.ratio_time = clock.now - MAX_AGE + 100
    original = provider.history

    def advance(value):
        clock.now += 100
        return original(value)

    provider.history = advance
    with pytest.raises(ValueError, match="^SHORT_RATIO_STALE$"):
        provider(signal())
    assert len(request.calls) == 5
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='directional-account-context-v1'"
        ).fetchone() == (0,)


def test_valid_context_deadline_does_not_outlive_original_ratio_event(database):
    provider, _, public, clock, _ = build(database)
    public.ratio_time = clock.now - MAX_AGE + 1000
    result = provider(signal())
    assert result["valid_until_ms"] == public.ratio_time + MAX_AGE + 1
    assert result["evidence"]["sentiment_observed_at_ms"] == public.ratio_time
    assert result["observed_at_ms"] < result["valid_until_ms"]


def test_repeated_valid_public_sample_still_reads_private_account_fresh(database):
    provider, request, public, _, _ = build(database)
    before = deepcopy(request.positions)
    first = provider(signal())
    request.positions = deepcopy(before)
    request.account["availableBalance"] = "500"
    second = provider(signal())
    assert first["sizing"]["available_margin"] == "700"
    assert second["sizing"]["available_margin"] == "500"
    assert len(request.calls) == 10
    assert (
        sum(
            path == "/futures/data/globalLongShortAccountRatio"
            for path, _ in public.calls
        )
        == 1
    )
    assert (
        second["evidence"]["sentiment_observed_at_ms"]
        == first["evidence"]["sentiment_observed_at_ms"]
    )


def test_ratio_number_is_not_replaced_with_safe_default():
    with pytest.raises((ValueError, TypeError)):
        _short_ratio(
            [{"symbol": "BTCUSDT", "shortAccount": "NaN", "timestamp": NOW}],
            "BTCUSDT",
            now=NOW,
            max_age_ms=MAX_AGE,
        )
