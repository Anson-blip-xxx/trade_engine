"""Offline contracts only: never inspect real JEV config or contact its API."""

import io
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from v2_core import jev_advisory as jev


@pytest.fixture
def features():
    return {
        "symbol": "BTCUSDT",
        "observed_at_ms": 100000,
        "timeframe": "15m",
        "direction": "LONG",
        "price": "100.00",
        "atr_pct": "1.20",
        "rsi_15m": "65",
        "return_1h_pct": "2.5",
        "ema_gap_15m_pct": "1.1",
    }


def response():
    return {
        "model": jev.MODEL,
        "answers": {
            "trend_supported": {"type": "noul", "noul": 0.8},
            "chase_risk": {"type": "noul", "noul": 0.25},
            "liquidity_fragile": {"type": "noul", "noul": 0.5},
        },
        "usage": {"input_tokens": 900, "output_tokens": 60},
    }


class Coordinator:
    """Only a test double; production must use the shared durable coordinator."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls, self.completions, self.results, self.claims = [], [], {}, {}
        self.denial = None
        self.publish = True

    def lookup(self, *, request_key, now_ms):
        return deepcopy(self.results.get(request_key))

    def claim(self, **request):
        with self.lock:
            self.calls.append(deepcopy(request))
            key = request["request_key"]
            if self.denial:
                return {"status": self.denial}
            if key in self.results:
                return {"status": "CACHED", "result": deepcopy(self.results[key])}
            if key in self.claims:
                return {"status": "BUSY"}
            token = str(uuid4())
            self.claims[key] = token
            return {"status": "GRANTED", "claim_id": token}

    def complete(self, **publication):
        self.completions.append(deepcopy(publication))
        if self.publish is not True:
            return self.publish
        if self.claims.get(publication["request_key"]) != publication["claim_id"]:
            return False
        self.results[publication["request_key"]] = deepcopy(publication["result"])
        return True


@pytest.fixture
def case():
    clock = SimpleNamespace(now=100100)
    coordinator, calls = Coordinator(), []

    def query(request, *, timeout_ms):
        calls.append((deepcopy(request), timeout_ms))
        return response()

    engine = jev.JevAdvisory(
        coordinator,
        query,
        policy=jev.AdvisoryPolicy(enabled=True),
        clock_ms=lambda: clock.now,
    )
    return engine, coordinator, calls, clock


def assert_advisory_only(result):
    assert result["execution_authorized"] is False
    assert result["risk_override"] is False
    assert result["decision_effect"] == "NONE"
    assert (
        result["probability_semantics"]
        == "UNCALIBRATED_MARKET_HYPOTHESES_NOT_TRADE_WIN_RATE"
    )


def test_default_off_does_not_read_inputs_cache_clock_or_api(case):
    engine, coordinator, calls, _ = case
    engine.policy = jev.AdvisoryPolicy()
    engine.clock = lambda: pytest.fail("disabled advisory accessed clock")
    for operation in (engine.lookup, engine.evaluate):
        result = operation({"api_key": "must-not-leave"})
        assert result["status"] == "OFF"
        assert_advisory_only(result)
    assert not coordinator.calls and not calls


def test_public_request_is_normalized_unknowns_explicit_and_input_unchanged(features):
    before = deepcopy(features)
    prepared = jev.prepare_request(features, now_ms=100100, max_age_ms=60000)
    assert features == before
    assert prepared["request"]["state"]["price"] == "100"
    assert prepared["request"]["state"]["atr_pct"] == "1.2"
    assert prepared["request"]["state"]["depth_imbalance"] is None
    assert prepared["valid_until_ms"] == 160000
    assert len(prepared["request_key"]) == 64
    assert len(jev._json(prepared["request"]).encode()) <= jev.MAX_REQUEST_BYTES
    assert set(prepared["request"]) == {"state", "questions", "model"}
    assert prepared["request"]["model"] == "jev-1.13.0"
    assert all("noul" == q["type"] for q in prepared["request"]["questions"].values())


def test_digest_binds_features_prompt_model_and_canonical_numbers(
    features, monkeypatch
):
    def key(snapshot):
        return jev.prepare_request(snapshot, now_ms=100100, max_age_ms=60000)[
            "request_key"
        ]

    first = key(features)
    assert key({**features, "price": 100}) == first
    assert key({**features, "price": "101"}) != first
    assert key({**features, "observed_at_ms": 100001}) != first
    assert key({**features, "direction": "SHORT"}) != first
    assert key({**features, "symbol": "ETHUSDT"}) != first
    monkeypatch.setattr(jev, "PROMPT_VERSION", "next-version")
    assert key(features) != first


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "account_id",
        "balance",
        "sizing",
        "position",
        "order_id",
        "signal_id",
        "account_scope",
        "instructions",
        "webhook_secret",
        "email",
        "raw",
        "url",
    ],
)
def test_unknown_or_sensitive_feature_keys_reject_whole_request(features, case, key):
    engine, coordinator, calls, _ = case
    secret = "synthetic-sensitive-value"
    result = engine.evaluate({**features, key: secret})
    assert result["reason"] == "INVALID_OR_STALE_MARKET"
    assert secret not in json.dumps(result)
    assert not calls and not coordinator.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", {"secret": "hidden"}),
        ("price", True),
        ("price", float("nan")),
        ("price", float("inf")),
        ("price", 100.5),
        ("price", "1e3"),
        ("price", " 100"),
        ("price", "0"),
        ("price", "9999999999999"),
        ("price", "0.0000000000001"),
        ("rsi_15m", "101"),
        ("taker_buy_ratio", "1.01"),
        ("depth_imbalance", "-1.1"),
        ("spread_bps", "-1"),
        ("funding_rate", "2"),
        ("return_1h_pct", "-101"),
        ("symbol", "BTCUSDT;curl"),
        ("symbol", "https://example.com"),
        ("symbol", "BTCUSDT\n"),
        ("timeframe", "caller prompt"),
        ("direction", "BUY NOW"),
        ("observed_at_ms", True),
        ("observed_at_ms", -1),
        ("observed_at_ms", "100000"),
        ("observed_at_ms", 100101),
        ("observed_at_ms", 40100),
    ],
)
def test_malformed_or_stale_market_never_claims_or_calls(features, case, field, value):
    engine, coordinator, calls, _ = case
    assert engine.evaluate({**features, field: value})["status"] == "FALLBACK"
    assert not calls and not coordinator.calls


def test_insufficient_public_features_are_not_replaced_with_zero(features, case):
    engine, coordinator, calls, _ = case
    sparse = {key: features[key] for key in jev._BASE_FIELDS}
    assert engine.evaluate(sparse)["reason"] == "INVALID_OR_STALE_MARKET"
    assert not coordinator.calls and not calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("enabled", 1),
        ("timeout_ms", 99),
        ("timeout_ms", 2001),
        ("timeout_ms", True),
        ("max_age_ms", 999),
        ("max_age_ms", 300001),
        ("cache_ttl_ms", 999),
        ("cache_ttl_ms", 60001),
        ("requests_per_minute", 0),
        ("requests_per_minute", 61),
        ("tokens_per_day", 8191),
        ("tokens_per_day", 1000001),
    ],
)
def test_policy_rejects_unbounded_or_implicit_configuration(field, value):
    with pytest.raises(ValueError):
        jev.AdvisoryPolicy(**{field: value})


def test_success_budget_metadata_cache_and_no_execution_authority(features, case):
    engine, coordinator, calls, clock = case
    result = engine.evaluate(features)
    assert result["status"] == "ADVISORY_READY"
    assert result["hypotheses"] == {
        "trend_supported": "0.8",
        "chase_risk": "0.25",
        "liquidity_fragile": "0.5",
    }
    assert result["expires_at_ms"] == clock.now + 30000
    assert_advisory_only(result)
    assert coordinator.calls[0]["reserve_tokens"] == 8192
    assert coordinator.calls[0]["deadline_ms"] == clock.now + 1500
    assert coordinator.completions[0]["charged_input_tokens"] == 900
    assert calls[0][1] == 1500
    assert engine.lookup(features) == result
    assert engine.evaluate(features) == result
    assert len(calls) == 1 and len(coordinator.completions) == 1


def test_admission_lookup_does_not_claim_or_call_model(features, case):
    engine, coordinator, calls, _ = case
    assert engine.lookup(features)["reason"] == "CACHE_MISS"
    assert not calls and not coordinator.calls


@pytest.mark.parametrize(
    "denial", ["BUSY", "BUDGET_EXHAUSTED", "DISABLED", "raw-secret-error"]
)
def test_coordinator_denial_never_calls_model_or_echoes_unknown_reason(
    features, case, denial
):
    engine, coordinator, calls, _ = case
    coordinator.denial = denial
    result = engine.evaluate(features)
    assert result["status"] == "FALLBACK" and not calls
    assert result["reason"] == (
        denial if denial in {"BUSY", "BUDGET_EXHAUSTED"} else "COORDINATOR_UNAVAILABLE"
    )


def test_claim_failure_is_redacted_and_does_not_call_model(features, case):
    engine, coordinator, calls, _ = case

    def fail(**kwargs):
        raise RuntimeError("synthetic-secret")

    coordinator.claim = fail
    assert engine.evaluate(features)["reason"] == "COORDINATOR_UNAVAILABLE"
    assert not calls


@pytest.mark.parametrize(
    "failure", [TimeoutError("private"), RuntimeError("private"), ValueError("private")]
)
def test_failure_is_neutral_cached_and_unknown_usage_is_not_refunded(
    features, case, failure
):
    engine, coordinator, calls, _ = case

    def fail(request, *, timeout_ms):
        calls.append("attempt")
        raise failure

    engine.query = fail
    first = engine.evaluate(features)
    assert first["status"] == "FALLBACK" and "private" not in json.dumps(first)
    assert first["hypotheses"] is None
    assert coordinator.completions[0]["charged_input_tokens"] is None
    second = engine.evaluate(features)
    assert second["status"] == "FALLBACK" and len(calls) == 1
    assert_advisory_only(first)


def test_model_reply_after_deadline_is_never_used(features, case):
    engine, coordinator, _, clock = case

    def slow(request, *, timeout_ms):
        clock.now += timeout_ms
        return response()

    engine.query = slow
    result = engine.evaluate(features)
    assert result["reason"] == "MODEL_LATE_RESPONSE" and result["hypotheses"] is None
    assert coordinator.completions[0]["charged_input_tokens"] == 900


def test_claim_latency_consumes_same_request_deadline(features, case):
    engine, coordinator, calls, clock = case
    claim = coordinator.claim

    def delayed(**kwargs):
        claimed = claim(**kwargs)
        clock.now += 1600
        return claimed

    coordinator.claim = delayed
    assert engine.evaluate(features)["status"] == "FALLBACK"
    assert not calls
    assert coordinator.completions[0]["charged_input_tokens"] is None


@pytest.mark.parametrize("published", [False, None, 1, "true"])
def test_unconfirmed_publication_returns_no_usable_advice(features, case, published):
    engine, coordinator, _, _ = case
    coordinator.publish = published
    result = engine.evaluate(features)
    assert result["reason"] == "PUBLICATION_UNAVAILABLE"
    assert result["hypotheses"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(model="jev-latest"),
        lambda r: r.update(secret="must-not-escape"),
        lambda r: r["answers"].pop("chase_risk"),
        lambda r: r["answers"]["chase_risk"].update(type="score"),
        lambda r: r["answers"]["chase_risk"].update(noul=True),
        lambda r: r["answers"]["chase_risk"].update(noul=float("nan")),
        lambda r: r["answers"]["chase_risk"].update(noul=1.01),
        lambda r: r["answers"]["chase_risk"].update(noul=1.0000000000000002),
        lambda r: r["answers"]["chase_risk"].update(noul=-0.000000000000001),
        lambda r: r["answers"]["chase_risk"].update(noul=-0.01),
        lambda r: r["answers"]["chase_risk"].update(explanation="secret"),
        lambda r: r["usage"].update(input_tokens=True),
        lambda r: r["usage"].update(input_tokens=-1),
        lambda r: r["usage"].update(input_tokens=0),
        lambda r: r["usage"].update(output_tokens=4097),
    ],
)
def test_model_response_must_match_exact_fixed_contract(features, case, mutation):
    engine, coordinator, _, _ = case
    raw = response()
    mutation(raw)
    engine.query = lambda request, timeout_ms: raw
    result = engine.evaluate(features)
    assert result["reason"] == "MODEL_RESPONSE_INVALID"
    assert result["hypotheses"] is None and "secret" not in json.dumps(result)
    assert coordinator.completions[0]["charged_input_tokens"] is None


def test_unexpected_token_cost_is_charged_but_not_used(features, case):
    engine, coordinator, _, _ = case
    raw = response()
    raw["usage"]["input_tokens"] = 9000
    engine.query = lambda request, timeout_ms: raw
    result = engine.evaluate(features)
    assert result["reason"] == "TOKEN_RESERVATION_EXCEEDED"
    assert coordinator.completions[0]["charged_input_tokens"] == 9000


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(request_key="0" * 64),
        lambda r: r.update(model="jev-latest"),
        lambda r: r.update(prompt_version="different"),
        lambda r: r.update(execution_authorized=True),
        lambda r: r.update(execution_authorized=0),
        lambda r: r.update(risk_override=True),
        lambda r: r.update(decision_effect="OPEN"),
        lambda r: r.update(probability_semantics="WIN_PROBABILITY"),
        lambda r: r.update(observed_at_ms=1),
        lambda r: r.update(produced_at_ms=200000),
        lambda r: r.update(expires_at_ms=100100),
        lambda r: r.update(expires_at_ms=159999),
        lambda r: r.update(extra="secret"),
        lambda r: r["hypotheses"].update(chase_risk="-0.1"),
    ],
)
def test_poisoned_cache_cannot_supply_advice(features, case, mutation):
    engine, coordinator, calls, _ = case
    result = engine.evaluate(features)
    mutation(coordinator.results[result["request_key"]])
    assert engine.lookup(features)["reason"] == "CACHE_UNAVAILABLE"
    assert len(calls) == 1


def test_concurrent_same_snapshot_requires_one_atomic_claim(features, case):
    engine, coordinator, calls, _ = case
    entered, release = threading.Event(), threading.Event()

    def query(request, *, timeout_ms):
        calls.append("called")
        entered.set()
        assert release.wait(2)
        return response()

    engine.query = query
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(engine.evaluate, features)
        assert entered.wait(1)
        others = [pool.submit(engine.evaluate, features) for _ in range(3)]
        assert all(f.result()["reason"] == "BUSY" for f in others)
        release.set()
        assert first.result()["status"] == "ADVISORY_READY"
    assert len(calls) == 1 and len(coordinator.completions) == 1


def test_shared_sdk_query_uses_only_fixed_command_and_sanitized_stdin(
    features, monkeypatch
):
    request = jev.prepare_request(features, now_ms=100100, max_age_ms=60000)["request"]
    calls = []

    def run(argv, payload, timeout_ms):
        calls.append((argv, json.loads(payload), timeout_ms))
        return json.dumps(response()).encode()

    monkeypatch.setattr(jev, "_bounded_process", run)
    result = jev.SharedJevQuery()(request, timeout_ms=500)
    assert result["model"] == jev.MODEL
    command, payload, timeout = calls[0]
    assert command[0] == jev.SHARED_PYTHON and command[1] == "-I"
    assert command[-1] == "--sdk-once"
    assert payload == {"request": request, "timeout_ms": 500} and timeout == 500


@pytest.mark.parametrize("alter", ["model", "questions", "secret"])
def test_shared_query_cannot_be_a_generic_prompt_or_gateway(
    features, monkeypatch, alter
):
    request = jev.prepare_request(features, now_ms=100100, max_age_ms=60000)["request"]
    request[alter] = "unapproved"
    monkeypatch.setattr(
        jev, "_bounded_process", lambda *a: pytest.fail("child launched")
    )
    with pytest.raises(ValueError):
        jev.SharedJevQuery()(request, timeout_ms=500)


def test_sdk_worker_pins_model_disables_retries_and_outputs_only_contract(
    features, monkeypatch
):
    request = jev.prepare_request(features, now_ms=100100, max_age_ms=60000)["request"]
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def system_one(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model_dump=lambda **kw: response())

    def create_client(**kwargs):
        assert kwargs == {"model": "jev-1.13.0"}
        return Client()

    monkeypatch.setitem(
        sys.modules, "jev_shared", SimpleNamespace(create_client=create_client)
    )
    monkeypatch.setitem(
        sys.modules, "typesafe_sdk", SimpleNamespace(RetryPolicy=lambda **kw: kw)
    )
    monkeypatch.setattr(jev, "version", lambda name: "0.7.5")
    monkeypatch.setattr(jev.logging, "disable", lambda level: None)
    stdout = io.BytesIO()
    monkeypatch.setattr(
        jev.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                json.dumps({"request": request, "timeout_ms": 800}).encode()
            )
        ),
    )
    monkeypatch.setattr(jev.sys, "stdout", SimpleNamespace(buffer=stdout))
    assert jev._sdk_once() == 0
    assert calls[0]["retry"] == {"max_retries": 0, "timeout": 0.8}
    assert calls[0]["timeout"] == 0.8
    assert json.loads(stdout.getvalue()) == jev.parse_response(response())


def test_sdk_mismatch_does_not_import_shared_secret_client(features, monkeypatch):
    request = jev.prepare_request(features, now_ms=100100, max_age_ms=60000)["request"]
    monkeypatch.setattr(jev, "version", lambda name: "0.7.6")
    monkeypatch.setitem(
        sys.modules,
        "jev_shared",
        SimpleNamespace(create_client=lambda **kw: pytest.fail("read config")),
    )
    monkeypatch.setattr(
        jev.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                json.dumps({"request": request, "timeout_ms": 800}).encode()
            )
        ),
    )
    assert jev._sdk_once() == 1


def test_child_timeout_is_bounded_and_only_own_process_is_killed(monkeypatch):
    children = []
    popen = subprocess.Popen

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(jev.subprocess, "Popen", record)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        jev._bounded_process(
            [sys.executable, "-I", "-c", "import time; time.sleep(10)"], b"{}", 100
        )
    assert time.monotonic() - started < 1
    assert len(children) == 1
    assert children[0].wait(timeout=1) < 0


def test_child_stdout_is_bounded_and_stderr_is_not_exposed():
    with pytest.raises(ValueError, match="JEV_RESPONSE_TOO_LARGE"):
        jev._bounded_process(
            [sys.executable, "-I", "-c", "print('x' * 10000)"], b"{}", 1000
        )
    with pytest.raises(ValueError, match="JEV_SDK_UNAVAILABLE"):
        jev._bounded_process(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; print('secret',file=sys.stderr); sys.exit(1)",
            ],
            b"{}",
            1000,
        )


def test_child_does_not_inherit_trading_or_loader_environment(monkeypatch):
    forbidden = {
        "BINANCE_API_KEY",
        "BINANCE_TESTNET_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "CREDENTIALS_DIRECTORY",
        "LD_PRELOAD",
        "PYTHONPATH",
        "HTTPS_PROXY",
        "TYPESAFE_MODEL",
        "TYPESAFE_BASE_URL",
        "TYPESAFE_LOG_LEVEL",
    }
    for name in forbidden:
        monkeypatch.setenv(name, "synthetic-not-real")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-approved-test-override")
    popen = subprocess.Popen
    captures = []

    def capture(*args, **kwargs):
        captures.append(kwargs["env"])
        return popen(*args, **kwargs)

    monkeypatch.setattr(jev.subprocess, "Popen", capture)
    assert (
        jev._bounded_process([sys.executable, "-I", "-c", "print('{}')"], b"{}", 1000)
        == b"{}\n"
    )
    assert set(captures[0]) == {"PATH", "LANG", "TYPESAFE_API_KEY"}
    assert not forbidden & captures[0].keys()


def test_spawn_time_consumes_hard_deadline(monkeypatch):
    popen = subprocess.Popen
    children = []

    def delayed(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        time.sleep(0.15)
        return process

    monkeypatch.setattr(jev.subprocess, "Popen", delayed)
    with pytest.raises(TimeoutError):
        jev._bounded_process(
            [sys.executable, "-I", "-c", "import time; time.sleep(10)"], b"{}", 100
        )
    assert children[0].wait(timeout=1) < 0
