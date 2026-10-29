"""Public model facts retain source clocks and never include private context."""

import io
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_v2_jev_entry_context import binding, publish, refresh
from test_v2_jev_entry_context import case as context_case_fixture
from test_v2_jev_reference import envelope

from services.v2_jev_coordinator import JevCoordinator
from services.v2_jev_reference import PUBLIC_SCOPE, public_features
from v2_core import jev_advisory as subject
from v2_core.signal_fusion import FusionPolicy, build_fusion

context_case = context_case_fixture


def context(now=1200):
    return {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": now,
        "valid_until_ms": now + 15000,
        "account_scope": {"account_id": "ACCOUNT_SECRET"},
        "account_evidence": {
            "sizing_mark_observed_at_ms": now - 20,
            "private": "KEY_SECRET",
        },
        "directional": {
            "funding_rate": "0.0001",
            "balance": "BALANCE_SECRET",
            "leverage_venue": {
                "symbol": "BTCUSDT",
                "book_at_ms": now - 10,
                "spread_fraction": "0.0002",
                "private": "NOT_FOR_MODEL",
            },
        },
    }


def features(value=None, *, now=1200):
    return public_features(
        envelope(),
        target="BTCUSDT",
        direction="LONG",
        context=context(now) if value is None else value,
        now_ms=now,
    )


def test_only_observed_public_scalars_and_original_timestamps_leave_context():
    original = context()
    before = deepcopy(original)
    result = features(original)
    prepared = subject.prepare_request(result, now_ms=1200, max_age_ms=60000)
    state = prepared["request"]["state"]
    assert state["funding_rate"] == "0.0001" and state["funding_observed_at_ms"] == 1180
    assert state["spread_bps"] == "2" and state["spread_observed_at_ms"] == 1190
    assert (
        state["depth_imbalance"] is None and state["open_interest_change_pct"] is None
    )
    assert original == before
    assert "SECRET" not in json.dumps(prepared) and "NOT_FOR_MODEL" not in json.dumps(
        prepared
    )
    assert subject.PROMPT_VERSION == "market-hypotheses-v2"


@pytest.mark.parametrize("stamp", [None, True, "1180", -13800, 1201])
def test_missing_stale_future_or_invalid_funding_clock_does_not_invent_fact(stamp):
    value = context()
    value["account_evidence"]["sizing_mark_observed_at_ms"] = stamp
    result = subject.prepare_request(features(value), now_ms=1200, max_age_ms=60000)
    assert result["request"]["state"]["funding_rate"] is None
    assert result["request"]["state"]["funding_observed_at_ms"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"book_at_ms": -13800},
        {"book_at_ms": 1201},
        {"book_at_ms": None},
        {"symbol": "ETHUSDT"},
    ],
)
def test_unusable_book_does_not_become_fake_spread(change):
    value = context()
    value["directional"]["leverage_venue"].update(change)
    result = subject.prepare_request(features(value), now_ms=1200, max_age_ms=60000)
    assert result["request"]["state"]["spread_bps"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"environment": "LIVE"},
        {"valid_until_ms": 1200},
        {"assembled_at": 1201},
    ],
)
def test_context_scope_and_freshness_mandatory(change):
    value = context()
    value.update(change)
    with pytest.raises(ValueError, match="CONTEXT_INVALID"):
        features(value)


def test_supplemental_clock_bounds_model_expiry_without_retimestamp():
    result = features()
    result["funding_observed_at_ms"] = 900
    prepared = subject.prepare_request(result, now_ms=1200, max_age_ms=60000)
    assert prepared["valid_until_ms"] == 60900
    result["funding_observed_at_ms"] = 1201
    with pytest.raises(ValueError, match="SUPPLEMENTAL_SNAPSHOT_EXPIRED"):
        subject.prepare_request(result, now_ms=1200, max_age_ms=60000)


def test_coordinator_cache_uses_oldest_public_fact_clock(context_case):
    case = context_case
    now = case.db.now
    value = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "timeframe": "15m",
        "observed_at_ms": now,
        "price": "100",
        "atr_pct": "1",
        "rsi_15m": "60",
        "tv_confirmation": "1",
        "tv_observed_at_ms": now - 59500,
    }
    coordinator = JevCoordinator(case.db.connect, scope=PUBLIC_SCOPE, enabled=True)
    from test_v2_jev_entry_context import model_response

    def query(request, timeout_ms):
        case.db.now += 50
        return model_response()

    result = subject.JevAdvisory(
        coordinator,
        query,
        policy=subject.AdvisoryPolicy(enabled=True),
        clock_ms=lambda: case.db.now,
    ).evaluate(value)
    assert result["status"] == "ADVISORY_READY" and result["expires_at_ms"] == now + 500
    assert (
        coordinator.lookup(request_key=result["request_key"], now_ms=case.db.now)
        == result
    )


def test_jev_uses_validated_tv_fact_without_peer_text(context_case):
    case = context_case
    now = case.db.now
    case.args["signal"]["signal"] = "TREND_UP"
    case.args["context"]["signal_fusion"] = build_fusion(
        case.args["signal"],
        source="s3",
        peers=[
            {
                "signal_id": "not-exported",
                "source": "tv_bridge",
                "environment": "SANDBOX",
                "symbol": "BTCUSDT",
                "signal": "TREND_UP",
                "observed_at": now - 10,
                "expires_at_ms": now + 60000,
            }
        ],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=now,
    )
    assert case.service.assess(**case.args).status == "PENDING"
    state = binding(case)[2]["public_features"]
    assert state["tv_confirmation"] == "1" and state["tv_observed_at_ms"] == now - 10
    assert "not-exported" not in json.dumps(state)


def test_forged_tv_bonus_cannot_enter_model_or_enable_fallback(context_case):
    case = context_case
    case.args["context"]["signal_fusion"] = {
        "version": "directional-fusion-v1",
        "status": "CONFIRMS",
        "score_bonus": 100,
    }
    assert case.service.assess(**case.args).status == "SKIP"
    assert not case.queued


def test_peer_expiry_bounds_fixed_model_review_even_after_tv_disappears(context_case):
    case = context_case
    now = case.db.now
    case.args["signal"]["signal"] = "TREND_UP"
    proof = build_fusion(
        case.args["signal"],
        source="s3",
        peers=[
            {
                "signal_id": "expiring-peer",
                "source": "tv_bridge",
                "environment": "SANDBOX",
                "symbol": "BTCUSDT",
                "signal": "TREND_UP",
                "observed_at": now - 10,
                "expires_at_ms": now + 1100,
            }
        ],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=now,
    )
    case.args["context"]["signal_fusion"] = proof
    assert case.service.assess(**case.args).status == "PENDING"
    assert binding(case)[2]["deadline_ms"] == now + 1100
    publish(case)
    case.db.now = now + 2000
    refresh(case.args, case.db.now, new_market=True)
    case.args["context"]["signal_fusion"] = build_fusion(
        case.args["signal"],
        source="s3",
        peers=[],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=case.db.now,
    )
    result = case.service.assess(**case.args)
    assert result.status == "SKIP" and result.reason == "JEV_REVIEW_DEADLINE_EXPIRED"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("timeout", 3),
        ("connection", 2),
        ("rate", 5),
        ("server", 5),
        ("invalid", 4),
        ("unknown", 1),
        ("http400", 5),
        ("http401", 5),
        ("http402", 5),
        ("http403", 5),
        ("http422", 5),
        ("http529", 5),
        ("http599", 5),
        ("http200", 1),
        ("http399", 1),
        ("http600", 1),
        ("http_missing", 1),
        ("http_text", 1),
        ("http_boolean", 1),
        ("forged_status", 1),
    ],
)
def test_child_classifies_typed_sdk_failure_without_exporting_bodies(
    monkeypatch, kind, expected
):
    class APIError(Exception):
        def __init__(self, text, status=None):
            super().__init__(text)
            self.status = status

    types = {
        name: type(
            name,
            (APIError if name in {"rate", "server", "invalid"} else Exception,),
            {},
        )
        for name in ("timeout", "connection", "rate", "server", "invalid")
    }
    fake_sdk = SimpleNamespace(
        RetryPolicy=lambda **kw: kw,
        TypeSafeAPIError=APIError,
        TypeSafeAPITimeoutError=types["timeout"],
        TypeSafeAPIConnectionError=types["connection"],
        TypeSafeRateLimitError=types["rate"],
        TypeSafeInternalServerError=types["server"],
        TypeSafeAPIResponseValidationError=types["invalid"],
    )

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def system_one(self, **kwargs):
            if kind.startswith("http"):
                status = {
                    "http_missing": None,
                    "http_text": "402",
                    "http_boolean": True,
                }.get(kind)
                if kind[4:].isdigit():
                    status = int(kind[4:])
                raise APIError("insufficient quota: SECRET_BODY", status)
            if kind == "forged_status":
                error = RuntimeError("insufficient quota: SECRET_BODY")
                error.status = 402
                raise error
            if kind in {"rate", "server", "invalid"}:
                raise types[kind](
                    "SECRET_BODY", {"rate": 429, "server": 500, "invalid": 200}[kind]
                )
            raise types.get(kind, RuntimeError)("SECRET_BODY")

    monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
    monkeypatch.setitem(
        sys.modules, "jev_shared", SimpleNamespace(create_client=lambda **kw: Client())
    )
    monkeypatch.setattr(subject, "version", lambda _: subject.SDK_VERSION)
    monkeypatch.setattr(subject.logging, "disable", lambda _: None)
    prepared = subject.prepare_request(features(), now_ms=1200, max_age_ms=60000)
    payload = json.dumps({"request": prepared["request"], "timeout_ms": 1000}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(
        subject.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))
    )
    monkeypatch.setattr(subject.sys, "stdout", SimpleNamespace(buffer=output))
    assert subject._sdk_once() == expected
    assert output.getvalue() == b""


@pytest.mark.parametrize(
    "code,error",
    [
        (2, subject.JevProviderUnavailable),
        (3, TimeoutError),
        (4, subject.JevResponseInvalid),
        (5, subject.JevProviderRequestRejected),
    ],
)
def test_child_exit_protocol_preserves_failure_type(code, error):
    with pytest.raises(error):
        subject._bounded_process(
            [sys.executable, "-I", "-c", f"import sys;sys.exit({code})"], b"{}", 1000
        )
