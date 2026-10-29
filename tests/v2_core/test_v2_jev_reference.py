"""JEV latency/failure is outside account decisions and signed order gates."""

import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.v2_jev_reference import (
    JevPrefetchMarketContext,
    JevReference,
    JevReferenceContext,
    public_features,
)
from v2_core.jev_advisory import (
    AdvisoryPolicy,
    _result,
    parse_response,
    prepare_request,
)
from v2_core.producer import market_envelope


def wait_idle(reference, timeout=2):
    deadline = time.monotonic() + timeout
    while reference.queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.001)
    assert reference.queue.unfinished_tasks == 0


def ready(features, now):
    prepared = prepare_request(features, now_ms=now, max_age_ms=60000)
    response = parse_response(
        {
            "model": "jev-1.13.0",
            "answers": {
                "trend_supported": {"type": "noul", "noul": 0.8},
                "chase_risk": {"type": "noul", "noul": 0.3},
                "liquidity_fragile": {"type": "noul", "noul": 0.5},
            },
            "usage": {"input_tokens": 900, "output_tokens": 60},
        }
    )
    return _result(
        "ADVISORY_READY",
        "MODEL_OBSERVED",
        prepared,
        now_ms=now,
        expires_at_ms=min(prepared["valid_until_ms"], now + 30000),
        response=response,
    )


@pytest.fixture
def reference():
    stop, clock, calls = threading.Event(), SimpleNamespace(now=1000), []
    ref = JevReference(None, clock_ms=lambda: clock.now, stop=stop)

    def policy():
        assert threading.current_thread() is not threading.main_thread()
        return AdvisoryPolicy(enabled=True)

    def evaluate(features):
        assert threading.current_thread() is not threading.main_thread()
        calls.append(deepcopy(features))
        return ready(features, clock.now)

    ref.policy = policy
    ref.advisor = lambda _: SimpleNamespace(evaluate=evaluate)
    yield ref, clock, calls
    stop.set()
    if ref.thread:
        ref.thread.join(2)
        assert not ref.thread.is_alive()


def envelope(target="BTCUSDT", observed=1000):
    return market_envelope(
        "s3",
        "SANDBOX",
        target,
        observed,
        {
            "15m": {
                "close": "101",
                "ema20": "100",
                "rsi": "60",
                "atr_pct": "2",
                "chg": "1",
                "taker_buy_ratio": "0.55",
            },
            "1h": {"close": "101", "ema20": "100", "chg": "2"},
            "4h": {"chg": "3"},
            "private_injected": "ACCOUNT-SECRET",
        },
    )


def test_public_mapping_never_forwards_extra_or_fabricates_depth():
    value = public_features(envelope(), target="BTCUSDT", direction="LONG")
    assert value["price"] == "101"
    assert value["ema_gap_15m_pct"] == "1"
    assert value["return_1h_pct"] == "2"
    assert "ACCOUNT-SECRET" not in str(value)
    assert "depth_imbalance" not in value and "funding_rate" not in value


def numeric_envelope(frame, field, value):
    features = deepcopy(envelope()["features"])
    features[frame][field] = value
    return market_envelope("s3", "SANDBOX", "BTCUSDT", 1000, features)


@pytest.mark.parametrize(
    ("frame", "field", "value", "mapped", "expected"),
    [
        ("15m", "atr_pct", "0.03301390845684003", "atr_pct", "0.033013908457"),
        (
            "15m",
            "atr_pct",
            "0.0330139084568400300000000001",
            "atr_pct",
            "0.033013908457",
        ),
        ("15m", "atr_pct", "3.301390845684003e-2", "atr_pct", "0.033013908457"),
        ("15m", "atr_pct", "1e-12", "atr_pct", "0.000000000001"),
        ("15m", "rsi", "65.12345678901234", "rsi_15m", "65.123456789012"),
        ("15m", "taker_buy_ratio", "0.5500000000000001", "taker_buy_ratio", "0.55"),
        ("15m", "chg", "-1.1234567890125", "return_15m_pct", "-1.123456789012"),
        ("1h", "chg", "1.1234567890135", "return_1h_pct", "1.123456789014"),
        ("4h", "chg", "1e2", "return_4h_pct", "100"),
        (
            "15m",
            "close",
            "101.1234567890123456789012345678",
            "price",
            "101.123456789012",
        ),
    ],
)
def test_public_numbers_are_bounded_quantized_before_strict_core(
    frame, field, value, mapped, expected
):
    result = public_features(
        numeric_envelope(frame, field, value), target="BTCUSDT", direction="LONG"
    )
    assert result[mapped] == expected
    prepared = prepare_request(result, now_ms=1000, max_age_ms=60000)
    assert prepared["request"]["state"][mapped] == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("atr_pct", "NaN"),
        ("atr_pct", "sNaN"),
        ("atr_pct", "Infinity"),
        ("atr_pct", "1e-999999"),
        ("atr_pct", "1e999999"),
        ("atr_pct", "0" * 81),
        ("atr_pct", "not-number"),
        ("atr_pct", "-0.0000000000000000001"),
        ("atr_pct", "1000.0000000000000000001"),
        ("rsi", "100.0000000000000000001"),
        ("taker_buy_ratio", "1.0000000000000000001"),
        ("close", "0.0000000000009999999999"),
        ("close", "1000000000.000000000000000001"),
        ("ema20", "1e-64"),
    ],
)
def test_public_invalid_original_numbers_are_not_rounded_into_valid_range(field, value):
    with pytest.raises(ValueError, match="JEV_PUBLIC_NUMBER_INVALID"):
        public_features(
            numeric_envelope("15m", field, value), target="BTCUSDT", direction="LONG"
        )


@pytest.mark.parametrize("field", ["atr_pct", "rsi", "taker_buy_ratio", "chg", "ema20"])
def test_public_unknown_numbers_stay_unknown(field):
    mapping = {
        "atr_pct": "atr_pct",
        "rsi": "rsi_15m",
        "taker_buy_ratio": "taker_buy_ratio",
        "chg": "return_15m_pct",
        "ema20": "ema_gap_15m_pct",
    }
    result = public_features(
        numeric_envelope("15m", field, None), target="BTCUSDT", direction="LONG"
    )
    assert result[mapping[field]] is None
    assert (
        prepare_request(result, now_ms=1000, max_age_ms=60000)["request"]["state"][
            mapping[field]
        ]
        is None
    )


@pytest.mark.parametrize("defect", ["source", "environment", "symbol", "digest"])
def test_invalid_envelope_never_reaches_model(defect):
    e = envelope()
    if defect == "digest":
        e["features"]["15m"]["close"] = "102"
    else:
        e[defect] = "unexpected"
    with pytest.raises(ValueError):
        public_features(e, target="BTCUSDT", direction="LONG")


def test_prefetch_uses_original_read_before_account_context_and_final_lookup():
    early, final = envelope(), envelope(observed=1010)
    context = {
        "sources": {"s3": final},
        "account_scope": {"account_id": "SECRET"},
        "directional": {"sizing": {"balance": "9999"}},
        "valid_until_ms": 2000,
    }
    original = deepcopy(context)
    events = []

    def market(_):
        events.append("market")
        return {"sources": {"s3": early}}

    def delegate(signal):
        prefetch(signal)
        events.append("account")
        return context

    def submit(features):
        events.append("submit")
        assert features["observed_at_ms"] == 1000
        assert "SECRET" not in str(features)

    def lookup(features):
        events.append("lookup")
        assert features["observed_at_ms"] == 1010
        return {
            "status": "FALLBACK",
            "reason": "CACHE_MISS",
            "execution_authorized": False,
        }

    reference = SimpleNamespace(submit=submit, lookup=lookup)
    prefetch = JevPrefetchMarketContext(market, reference=reference, strategy="S8")
    wrapper = JevReferenceContext(delegate, reference=reference, strategy="S8")
    result = wrapper({"symbol": "BTCUSDT"})
    assert events == ["market", "submit", "account", "lookup"]
    assert context == original
    assert {key: result[key] for key in original} == original
    assert result["jev_advisory"]["status"] == "FALLBACK"


def test_model_or_db_failure_does_not_mask_normal_context_or_original_error():
    def failed(*_):
        raise RuntimeError("SECRET")

    ref = SimpleNamespace(submit=failed, lookup=failed)
    wrapper = JevReferenceContext(
        lambda _: {"sources": {"s3": envelope()}, "passed_original_gate": False},
        reference=ref,
        strategy="S6",
    )
    result = wrapper({"symbol": "BTCUSDT"})
    assert result["passed_original_gate"] is False
    assert result["jev_advisory"]["decision_effect"] == "NONE"
    assert "SECRET" not in str(result["jev_advisory"])
    wrapper.delegate = failed
    with pytest.raises(RuntimeError, match="SECRET"):
        wrapper({"symbol": "BTCUSDT"})


def test_disabled_reference_reads_control_only_in_worker_and_never_calls_model():
    stop, observed = threading.Event(), threading.Event()
    ref = JevReference(None, clock_ms=lambda: 1000, stop=stop)

    def policy():
        assert threading.current_thread() is not threading.main_thread()
        observed.set()
        return AdvisoryPolicy()

    ref.policy = policy
    ref.advisor = lambda _: pytest.fail("disabled advisory built model client")
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    try:
        ref.submit(features)
        assert observed.wait(1)
        wait_idle(ref)
        assert ref.lookup(features)["status"] == "OFF"
        ref.submit(features)
        assert ref.queue.empty()
    finally:
        stop.set()
        if ref.thread:
            ref.thread.join(1)


def test_slow_model_has_bounded_nonblocking_queue_and_duplicate_coalescing():
    stop, entered, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def evaluate(features):
        calls.append(features)
        entered.set()
        assert release.wait(2)

    ref = JevReference(None, clock_ms=lambda: 1000, stop=stop)
    ref.policy = lambda: AdvisoryPolicy(enabled=True)
    ref.advisor = lambda _: SimpleNamespace(evaluate=evaluate)
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    try:
        ref.submit(features)
        assert entered.wait(1)
        for _ in range(10):
            ref.submit(features)
        for i in range(20):
            ref.submit({**features, "symbol": f"TEST{i}USDT"})
        assert len(calls) == 1
        assert ref.queue.qsize() == 8 and len(ref.pending) == 9
    finally:
        stop.set()
        release.set()
        if ref.thread is not None:
            ref.thread.join(2)
    assert not ref.thread.is_alive()


def test_main_thread_never_waits_for_slow_control_database():
    from contextlib import contextmanager

    entered, release, stop = threading.Event(), threading.Event(), threading.Event()

    @contextmanager
    def connect():
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(2)
        raise RuntimeError("PRIVATE_DATABASE_ERROR")
        yield  # pragma: no cover - satisfy context manager protocol

    ref = JevReference(connect, clock_ms=lambda: 1000, stop=stop)
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    try:
        ref.submit(features)
        assert entered.wait(1)
        started = time.monotonic()
        for i in range(20):
            result = ref.lookup(features)
            assert result["status"] != "ADVISORY_READY"
            ref.submit({**features, "symbol": f"SLOW{i}USDT"})
        assert time.monotonic() - started < 0.2
        assert ref.queue.qsize() == 8
        assert "PRIVATE_DATABASE_ERROR" not in str(result)
    finally:
        stop.set()
        release.set()
        if ref.thread:
            ref.thread.join(2)
    assert not ref.thread.is_alive()


def test_successful_worker_publication_populates_only_disposable_memory(reference):
    ref, _, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    assert len(calls) == 1
    result = ref.lookup(features)
    assert result["status"] == "ADVISORY_READY"
    assert result["execution_authorized"] is False
    result["hypotheses"]["trend_supported"] = "0"
    assert ref.lookup(features)["hypotheses"]["trend_supported"] == "0.8"
    for _ in range(10):
        ref.submit(features)
    assert len(calls) == 1 and ref.queue.empty()


@pytest.mark.parametrize("age", [5000, 5001, -1])
def test_control_stale_or_future_is_neutral_without_any_io(reference, age):
    ref, clock, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    clock.now = 1000 + age
    ref.connect = lambda: pytest.fail("lookup accessed DB")
    ref.policy = lambda: pytest.fail("lookup read policy")
    ref.advisor = lambda _: pytest.fail("lookup constructed SDK")
    assert ref.lookup(features)["reason"] == "CONTROL_STALE"
    assert len(calls) == 1


def test_control_cannot_expire_during_lookup_and_still_return_ready(reference):
    ref, _, _ = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    moments = iter([1000, 6000])
    ref.clock = lambda: next(moments)
    assert ref.lookup(features)["reason"] == "CONTROL_STALE"


def test_policy_change_clears_local_results_in_worker(reference):
    ref, clock, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    ref.policy = AdvisoryPolicy
    clock.now = 6001
    ref.submit(features)
    wait_idle(ref)
    assert not ref.cache
    assert ref.lookup(features)["status"] == "OFF"
    assert len(calls) == 1


def test_worker_control_failure_clears_cache_and_never_leaks_error(reference):
    ref, clock, _ = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)

    def fail():
        raise RuntimeError("DATABASE_SECRET")

    ref.policy = fail
    clock.now = 6001
    ref.submit(features)
    wait_idle(ref)
    assert not ref.cache and ref.control is None
    assert "DATABASE_SECRET" not in str(ref.lookup(features))


@pytest.mark.parametrize("status", ["FALLBACK", "OFF", "READY", "COMPLETED"])
def test_only_confirmed_advisory_ready_can_enter_memory(reference, status):
    ref, _, _ = reference
    ref.advisor = lambda _: SimpleNamespace(
        evaluate=lambda features: {"status": status}
    )
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    assert not ref.cache and ref.lookup(features)["status"] != "ADVISORY_READY"


def test_malformed_ready_result_is_not_a_memory_shortcut(reference):
    ref, _, _ = reference

    def evaluate(features):
        result = ready(features, 1000)
        result["execution_authorized"] = True
        return result

    ref.advisor = lambda _: SimpleNamespace(evaluate=evaluate)
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    assert not ref.cache and ref.lookup(features)["status"] != "ADVISORY_READY"


def test_cache_is_bounded_at_128_and_not_persistent_business_storage(reference):
    ref, _, calls = reference
    initial = public_features(envelope(), target="BTCUSDT", direction="LONG")
    for i in range(140):
        ref.submit({**initial, "symbol": f"SAMPLE{i}USDT"})
        wait_idle(ref)
    assert len(ref.cache) == 128 and len(calls) == 140
    assert ref.lookup({**initial, "symbol": "SAMPLE0USDT"})["reason"] == "CACHE_MISS"
    assert (
        ref.lookup({**initial, "symbol": "SAMPLE139USDT"})["status"] == "ADVISORY_READY"
    )
    restarted = JevReference(None, clock_ms=lambda: 1000, stop=threading.Event())
    assert not restarted.cache and restarted.lookup(initial)["status"] == "OFF"


def test_memory_lock_contention_drops_optional_work_without_wait(reference):
    ref, _, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    with ref.lock:
        started = time.monotonic()
        ref.submit(features)
        assert ref.lookup(features)["reason"] == "LOCAL_CACHE_BUSY"
        assert time.monotonic() - started < 0.1
    assert not calls and ref.queue.empty()


def test_local_cache_does_not_cross_symbol_direction_or_snapshot(reference):
    ref, _, _ = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    for change in ({"symbol": "ETHUSDT"}, {"direction": "SHORT"}, {"price": "102"}):
        assert ref.lookup({**features, **change})["reason"] == "CACHE_MISS"


def test_prefetch_returns_exact_original_object_and_never_duplicates_read():
    context = {
        "sources": {"s3": envelope()},
        "secret": "PRIVATE",
        "valid_until_ms": 2000,
    }
    calls, submitted = [], []

    def delegate(signal):
        calls.append(signal)
        return context

    wrapper = JevPrefetchMarketContext(
        delegate, reference=SimpleNamespace(submit=submitted.append), strategy="S6"
    )
    assert wrapper({"symbol": "BTCUSDT"}) is context
    assert len(calls) == len(submitted) == 1
    assert "PRIVATE" not in str(submitted)


def test_reference_context_is_lookup_only_and_never_submits_again():
    context = {
        "sources": {"s3": envelope()},
        "account_scope": {"account_id": "PRIVATE"},
    }
    refs = SimpleNamespace(
        submit=lambda _: pytest.fail("outer context submitted again"),
        lookup=lambda features: {"status": "OFF", "execution_authorized": False},
    )
    result = JevReferenceContext(lambda _: context, reference=refs, strategy="S6")(
        {"symbol": "BTCUSDT"}
    )
    assert result["account_scope"] == context["account_scope"]
    assert "jev_advisory" not in context


def test_existing_market_error_is_never_replaced_by_model_fallback():
    def delegate(_):
        raise ValueError("ORIGINAL_MARKET_ERROR")

    refs = SimpleNamespace(
        submit=lambda _: pytest.fail("submitted after market failure")
    )
    wrapper = JevPrefetchMarketContext(delegate, reference=refs, strategy="S6")
    with pytest.raises(ValueError, match="ORIGINAL_MARKET_ERROR"):
        wrapper({"symbol": "BTCUSDT"})


@pytest.mark.parametrize("fill_queue", [False, True])
def test_failed_lazy_thread_start_can_recover_without_duplicate_work(
    reference, monkeypatch, fill_queue
):
    ref, _, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    original = threading.Thread.start

    def failed_start(_):
        raise RuntimeError("THREAD_START_FAILED")

    monkeypatch.setattr(threading.Thread, "start", failed_start)
    ref.submit(features)
    if fill_queue:
        for i in range(7):
            ref.submit({**features, "symbol": f"START{i}USDT"})
    assert ref.queue.qsize() == (8 if fill_queue else 1)
    monkeypatch.setattr(threading.Thread, "start", original)
    ref.submit({**features, "symbol": "EXTRAUSDT"} if fill_queue else features)
    wait_idle(ref)
    assert len(calls) == (8 if fill_queue else 1)
    assert len({call["symbol"] for call in calls}) == len(calls)
    assert ref.lookup(features)["status"] == "ADVISORY_READY"


def test_expired_advisory_is_unavailable_even_when_control_is_fresh(reference):
    ref, clock, calls = reference
    features = public_features(envelope(), target="BTCUSDT", direction="LONG")
    ref.submit(features)
    wait_idle(ref)
    clock.now = 31000
    with ref.lock:
        ref.control = (ref.control[0], clock.now)
    ref.connect = lambda: pytest.fail("expired lookup accessed DB")
    ref.policy = lambda: pytest.fail("expired lookup read policy")
    ref.advisor = lambda _: pytest.fail("expired lookup constructed SDK")
    assert ref.lookup(features)["status"] != "ADVISORY_READY"
    assert len(calls) == 1
