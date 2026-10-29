"""Bounded public-market JEV observations, never trading authorization.

An external coordinator owns durable atomic claims, shared budgets and caching.
No local business-state files or per-process budget pretending to be global.
Use evaluate only in an independent prefetch worker; admission uses lookup only.
Neither method may replace an existing decision or an execution/risk gate.
Typed transport failures and confirmed HTTP 4xx/5xx provider rejections are
distinct from invalid responses and unknown failures. Rejections do not imply a
model opinion or a specific billing cause; error bodies are never interpreted.
"""

import json
import logging
import math
import os
import re
import selectors
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

MODEL = "jev-1.13.0"
SDK_VERSION = "0.7.5"
PROMPT_VERSION = "market-hypotheses-v2"
SCHEMA = "jev-market-advisory-v1"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 4096
RESERVE_TOKENS = 8192
SHARED_PYTHON = "/home/ubuntu/shared/jev/.venv/bin/python"
_BASE_FIELDS = {"symbol", "observed_at_ms", "timeframe", "direction"}
_OBSERVATION_TIMES = {
    "spread_observed_at_ms": "spread_bps",
    "funding_observed_at_ms": "funding_rate",
    "tv_observed_at_ms": "tv_confirmation",
}
_NUMBERS = {
    "price": ("0.000000000001", "1000000000"),
    "return_15m_pct": ("-100", "10000"),
    "return_1h_pct": ("-100", "10000"),
    "return_4h_pct": ("-100", "10000"),
    "atr_pct": ("0", "1000"),
    "rsi_15m": ("0", "100"),
    "ema_gap_15m_pct": ("-100", "10000"),
    "ema_gap_1h_pct": ("-100", "10000"),
    "relative_volume_15m": ("0", "1000"),
    "taker_buy_ratio": ("0", "1"),
    "spread_bps": ("0", "10000"),
    "depth_imbalance": ("-1", "1"),
    "depth_change_pct": ("-100", "10000"),
    "funding_rate": ("-1", "1"),
    "open_interest_change_pct": ("-100", "10000"),
    "tv_confirmation": ("-1", "1"),
}
_QUESTIONS = {
    "trend_supported": {
        "type": "noul",
        "instructions": "Do returns, EMA gaps, momentum, volume and independently confirmed TradingView direction support the stated direction? tv_confirmation is 1 aligned, -1 opposed, null unknown; it is not a win probability. Use only supplied public facts and original observation times. Null is unknown, not zero. Missing or conflicting evidence means uncertainty. This is not trading permission.",
    },
    "chase_risk": {
        "type": "noul",
        "instructions": "Do returns, ATR, RSI, EMA distance, funding and volume indicate overextension or fragile late pursuit in the stated direction? Funding is context, not future return. Null is unknown. Do not invent missing order flow, predict profit, or recommend orders.",
    },
    "liquidity_fragile": {
        "type": "noul",
        "instructions": "Do the supplied spread, depth imbalance, depth change and taker flow show fragile liquidity? Without spread/depth evidence this is unknown. Never infer an order book or cancellations from candles. Funding and open interest are context, not proof of depth. This is not a calibrated shock probability.",
    },
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("JEV_INVALID_INTEGER")
    return value


def _decimal(value, low, high):
    if type(value) not in {str, int} or not re.fullmatch(
        r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?", str(value)
    ):
        raise ValueError("JEV_INVALID_MARKET_NUMBER")
    number = Decimal(value)
    if not Decimal(low) <= number <= Decimal(high):
        raise ValueError("JEV_MARKET_NUMBER_OUT_OF_RANGE")
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _features(features):
    if (
        type(features) is not dict
        or not _BASE_FIELDS <= features.keys()
        or not features.keys()
        <= _BASE_FIELDS | _NUMBERS.keys() | _OBSERVATION_TIMES.keys()
        or not isinstance(features["symbol"], str)
        or not re.fullmatch(r"[A-Z0-9]{1,20}USDT", features["symbol"])
        or features["timeframe"] not in {"15m", "1h", "4h"}
        or features["direction"] not in {"LONG", "SHORT"}
    ):
        raise ValueError("JEV_PUBLIC_FEATURES_REQUIRED")
    _integer(features["observed_at_ms"], 0, 2**53 - 1)
    cleaned = {key: features[key] for key in sorted(_BASE_FIELDS)}
    for key, bounds in _NUMBERS.items():
        value = features.get(key)
        cleaned[key] = None if value is None else _decimal(value, *bounds)
    for stamp, field in _OBSERVATION_TIMES.items():
        value = features.get(stamp)
        if value is not None:
            _integer(value, 0, 2**53 - 1)
            if cleaned[field] is None:
                raise ValueError("JEV_OBSERVATION_VALUE_REQUIRED")
        cleaned[stamp] = value
    if sum(cleaned[key] is not None for key in _NUMBERS) < 3:
        raise ValueError("JEV_INSUFFICIENT_PUBLIC_FEATURES")
    return cleaned


def _request(features):
    result = {
        "model": MODEL,
        "state": _features(features),
        "questions": deepcopy(_QUESTIONS),
    }
    if len(_json(result).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("JEV_REQUEST_TOO_LARGE")
    return result


def prepare_request(features, *, now_ms, max_age_ms):
    """Normalize an explicitly public snapshot; never accept a whole context."""
    _integer(now_ms, 0, 2**53 - 1)
    _integer(max_age_ms, 1000, 300000)
    request = _request(features)
    observed = request["state"]["observed_at_ms"]
    if not 0 <= now_ms - observed < max_age_ms:
        raise ValueError("JEV_MARKET_SNAPSHOT_EXPIRED")
    for field in _OBSERVATION_TIMES:
        stamp = request["state"][field]
        if stamp is not None and not 0 <= now_ms - stamp < max_age_ms:
            raise ValueError("JEV_SUPPLEMENTAL_SNAPSHOT_EXPIRED")
    return {
        "request_key": sha256(
            _json({"prompt_version": PROMPT_VERSION, "request": request}).encode()
        ).hexdigest(),
        "request": request,
        "observed_at_ms": observed,
        "valid_until_ms": min(
            [
                observed + max_age_ms,
                *(
                    request["state"][field] + max_age_ms
                    for field in _OBSERVATION_TIMES
                    if request["state"][field] is not None
                ),
            ]
        ),
    }


def parse_response(raw):
    """Discard all unapproved response fields; never export free-form output."""
    if (
        type(raw) is not dict
        or set(raw) != {"model", "answers", "usage"}
        or raw["model"] != MODEL
        or type(raw["answers"]) is not dict
        or set(raw["answers"]) != set(_QUESTIONS)
        or type(raw["usage"]) is not dict
        or set(raw["usage"]) != {"input_tokens", "output_tokens"}
    ):
        raise ValueError("JEV_RESPONSE_INVALID")
    answers = {}
    for key, answer in raw["answers"].items():
        if (
            type(answer) is not dict
            or set(answer) != {"type", "noul"}
            or answer["type"] != "noul"
            or type(answer["noul"]) not in {str, int, float}
        ):
            raise ValueError("JEV_ANSWER_INVALID")
        value = answer["noul"]
        if type(value) is float:
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("JEV_ANSWER_INVALID")
            value = format(value, ".12f")
        answers[key] = {"type": "noul", "noul": _decimal(value, "0", "1")}
    usage = {
        "input_tokens": _integer(raw["usage"]["input_tokens"], 1, 1000000),
        "output_tokens": _integer(raw["usage"]["output_tokens"], 0, 4096),
    }
    return {"model": MODEL, "answers": answers, "usage": usage}


@dataclass(frozen=True)
class AdvisoryPolicy:
    enabled: bool = False
    timeout_ms: int = 1500
    max_age_ms: int = 60000
    cache_ttl_ms: int = 30000
    requests_per_minute: int = 5
    tokens_per_day: int = 500000
    token_budget_pacing: bool = False
    token_pacing_burst_tokens: int = 16384

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("JEV_EXPLICIT_SWITCH_REQUIRED")
        _integer(self.timeout_ms, 100, 2000)
        _integer(self.max_age_ms, 1000, 300000)
        _integer(self.cache_ttl_ms, 1000, self.max_age_ms)
        _integer(self.requests_per_minute, 1, 60)
        _integer(self.tokens_per_day, RESERVE_TOKENS, 1000000)
        if type(self.token_budget_pacing) is not bool:
            raise ValueError("JEV_EXPLICIT_PACING_SWITCH_REQUIRED")
        _integer(self.token_pacing_burst_tokens, RESERVE_TOKENS, 131072)


class JevProviderUnavailable(ConnectionError):
    """Typed, body-free provider transport/429/5xx failure, not invalid data."""


class JevProviderRequestRejected(ConnectionError):
    """Confirmed SDK HTTP rejection; no model opinion or error body attached."""


class JevConfigurationUnavailable(ValueError):
    """Typed shared-client creation failure; not provider balance or an opinion."""


class JevResponseInvalid(ValueError):
    """The provider returned unusable data; baseline fallback is forbidden."""


def _result(status, reason, prepared=None, *, now_ms=0, expires_at_ms=0, response=None):
    return {
        "schema": SCHEMA,
        "status": status,
        "reason": reason,
        "request_key": prepared["request_key"] if prepared else None,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "observed_at_ms": prepared["observed_at_ms"] if prepared else None,
        "produced_at_ms": now_ms,
        "expires_at_ms": expires_at_ms,
        "hypotheses": (
            {key: value["noul"] for key, value in response["answers"].items()}
            if response
            else None
        ),
        "usage": response["usage"] if response else None,
        "probability_semantics": "UNCALIBRATED_MARKET_HYPOTHESES_NOT_TRADE_WIN_RATE",
        "decision_effect": "NONE",
        "execution_authorized": False,
        "risk_override": False,
    }


def _cached(value, prepared, now, max_cache_ttl_ms):
    """Never trust stale, poisoned, different-model or differently scoped cache."""
    if type(value) is not dict or set(value) != set(_result("", "")):
        raise ValueError("JEV_CACHE_INVALID")
    _integer(now, 0, 2**53 - 1)
    if (
        value["status"] != "ADVISORY_READY"
        or value["reason"] != "MODEL_OBSERVED"
        or value["request_key"] != prepared["request_key"]
        or value["observed_at_ms"] != prepared["observed_at_ms"]
        or type(value["produced_at_ms"]) is not int
        or type(value["expires_at_ms"]) is not int
        or not prepared["observed_at_ms"] <= value["produced_at_ms"] <= now
        or not now < value["expires_at_ms"] <= prepared["valid_until_ms"]
        or value["expires_at_ms"] > value["produced_at_ms"] + max_cache_ttl_ms
        or type(value["hypotheses"]) is not dict
        or value["execution_authorized"] is not False
        or value["risk_override"] is not False
    ):
        raise ValueError("JEV_CACHE_INVALID")
    response = parse_response(
        {
            "model": value["model"],
            "usage": value["usage"],
            "answers": {
                key: {"type": "noul", "noul": score}
                for key, score in value["hypotheses"].items()
            },
        }
    )
    if response["usage"]["input_tokens"] > RESERVE_TOKENS:
        raise ValueError("JEV_CACHE_INVALID")
    expected = _result(
        "ADVISORY_READY",
        "MODEL_OBSERVED",
        prepared,
        now_ms=value["produced_at_ms"],
        expires_at_ms=value["expires_at_ms"],
        response=response,
    )
    if value != expected:
        raise ValueError("JEV_CACHE_INVALID")
    return expected


class JevAdvisory:
    """Coordinator is mandatory, atomic and shared across all local workers.

    lookup(*, request_key, now_ms) -> cached result | None.
    claim(*, request_key, request, now_ms, deadline_ms, reserve_tokens,
          requests_per_minute, tokens_per_day) -> GRANTED + UUID claim_id,
          CACHED + result, BUSY, or BUDGET_EXHAUSTED.
    complete(*, request_key, claim_id, result, now_ms, expires_at_ms,
             charged_input_tokens) -> True only on owned CAS publication.
    Unknown usage (None) MUST retain the reservation, never refund a timeout.
    A crashed claim MUST NOT permit automatic duplicate requests for the same
    snapshot; retain its charged/tombstone state until that snapshot expires.
    No advisory result changes an admission or grants permission by itself.
    """

    def __init__(self, coordinator, query, *, policy=None, clock_ms=None):
        if not all(
            callable(getattr(coordinator, n, None))
            for n in ("lookup", "claim", "complete")
        ) or not callable(query):
            raise TypeError("JEV_EXPLICIT_PORTS_REQUIRED")
        self.coordinator, self.query = coordinator, query
        self.policy = policy if policy is not None else AdvisoryPolicy()
        if not isinstance(self.policy, AdvisoryPolicy):
            raise TypeError("JEV_POLICY_REQUIRED")
        self.clock = (
            clock_ms if clock_ms is not None else (lambda: time.time_ns() // 1000000)
        )
        if not callable(self.clock):
            raise TypeError("JEV_CLOCK_REQUIRED")

    def _prepare(self, features):
        now = self.clock()
        return now, prepare_request(
            features, now_ms=now, max_age_ms=self.policy.max_age_ms
        )

    def lookup(self, features):
        if not self.policy.enabled:
            return _result("OFF", "DISABLED")
        try:
            now, prepared = self._prepare(features)
        except Exception:  # noqa: BLE001 - advisory failure cannot change admission
            return _result("FALLBACK", "INVALID_OR_STALE_MARKET")
        try:
            cached = self.coordinator.lookup(
                request_key=prepared["request_key"], now_ms=now
            )
            if cached is None:
                return _result("FALLBACK", "CACHE_MISS", prepared)
            return _cached(cached, prepared, self.clock(), self.policy.cache_ttl_ms)
        except Exception:  # noqa: BLE001 - never expose cache/backend error details
            return _result("FALLBACK", "CACHE_UNAVAILABLE", prepared)

    def evaluate(self, features):
        if not self.policy.enabled:
            return _result("OFF", "DISABLED")
        try:
            started, prepared = self._prepare(features)
        except Exception:  # noqa: BLE001 - advisory failure cannot change admission
            return _result("FALLBACK", "INVALID_OR_STALE_MARKET")
        deadline = min(started + self.policy.timeout_ms, prepared["valid_until_ms"])
        try:
            claimed = self.coordinator.claim(
                request_key=prepared["request_key"],
                request=deepcopy(prepared["request"]),
                now_ms=started,
                deadline_ms=deadline,
                reserve_tokens=RESERVE_TOKENS,
                requests_per_minute=self.policy.requests_per_minute,
                tokens_per_day=self.policy.tokens_per_day,
                token_budget_pacing=self.policy.token_budget_pacing,
                token_pacing_burst_tokens=self.policy.token_pacing_burst_tokens,
            )
            if claimed["status"] == "CACHED":
                return _cached(
                    claimed["result"], prepared, self.clock(), self.policy.cache_ttl_ms
                )
            if claimed["status"] in {"BUSY", "BUDGET_EXHAUSTED", "TOKEN_BUDGET_PACED"}:
                return _result("FALLBACK", claimed["status"], prepared)
            if (
                claimed["status"] != "GRANTED"
                or str(UUID(claimed["claim_id"])) != claimed["claim_id"]
            ):
                raise ValueError("JEV_CLAIM_INVALID")
            claim_id = claimed["claim_id"]
        except Exception:  # noqa: BLE001 - no backend diagnostics in shared evidence
            return _result("FALLBACK", "COORDINATOR_UNAVAILABLE", prepared)
        response, charged, reason = None, None, "MODEL_UNKNOWN_FAILURE"
        try:
            now = self.clock()
            if type(now) is not int or not started <= now < deadline:
                raise TimeoutError()
            raw = self.query(deepcopy(prepared["request"]), timeout_ms=deadline - now)
            try:
                response = parse_response(raw)
            except (ValueError, TypeError, KeyError):
                raise JevResponseInvalid() from None
            charged = response["usage"]["input_tokens"]
            if charged > RESERVE_TOKENS:
                response, reason = None, "TOKEN_RESERVATION_EXCEEDED"
            else:
                reason = "MODEL_OBSERVED"
        except TimeoutError:
            reason = "MODEL_TIMEOUT"
        except JevProviderUnavailable:
            reason = "MODEL_UNAVAILABLE"
        except JevProviderRequestRejected:
            reason = "PROVIDER_REQUEST_REJECTED"
        except JevConfigurationUnavailable:
            reason = "MODEL_CONFIGURATION_UNAVAILABLE"
        except JevResponseInvalid:
            reason = "MODEL_RESPONSE_INVALID"
        except Exception:  # noqa: BLE001 - SDK errors may contain secrets and bodies
            reason = "MODEL_UNKNOWN_FAILURE"
        try:
            finished = self.clock()
        except Exception:  # noqa: BLE001 - retain unknown budget charge on clock failure
            return _result("FALLBACK", "CLOCK_INVALID", prepared)
        if type(finished) is not int or not started <= finished <= 2**53 - 1:
            return _result("FALLBACK", "CLOCK_INVALID", prepared)
        if finished >= deadline and reason not in {
            "MODEL_RESPONSE_INVALID",
            "MODEL_UNKNOWN_FAILURE",
            "TOKEN_RESERVATION_EXCEEDED",
            "PROVIDER_REQUEST_REJECTED",
            "MODEL_CONFIGURATION_UNAVAILABLE",
        }:
            # A response that already exists is not provider unavailability:
            # even a late negative model opinion must never enable baseline.
            reason = "MODEL_LATE_RESPONSE" if response else "DEADLINE_EXPIRED"
            response = None
        expires = max(
            finished,
            min(prepared["valid_until_ms"], finished + self.policy.cache_ttl_ms),
        )
        result = _result(
            "ADVISORY_READY" if response else "FALLBACK",
            reason,
            prepared,
            now_ms=finished,
            expires_at_ms=expires,
            response=response,
        )
        try:
            committed = self.coordinator.complete(
                request_key=prepared["request_key"],
                claim_id=claim_id,
                result=deepcopy(result),
                now_ms=finished,
                expires_at_ms=expires,
                charged_input_tokens=charged,
            )
            if committed is not True:
                raise ValueError("JEV_PUBLICATION_NOT_CONFIRMED")
        except Exception:  # noqa: BLE001 - publication failure is never an execution veto
            return _result("FALLBACK", "PUBLICATION_UNAVAILABLE", prepared)
        return result


def _bounded_process(argv, payload, timeout_ms):
    """Bound output and wall time; kill only the child created by this call."""
    _integer(timeout_ms, 1, 2000)
    if type(payload) is not bytes or len(payload) > MAX_REQUEST_BYTES + 512:
        raise ValueError("JEV_REQUEST_TOO_LARGE")
    deadline = time.monotonic() + timeout_ms / 1000
    # Reserve a small part of the total budget for SIGKILL/reaping; do not add
    # a fresh timeout after an exhausted model call.
    work_deadline = deadline - min(0.1, timeout_ms / 2000)
    # No exchange/TG/credential directory, loader injection, proxy, Python path
    # or service environment follows this child. Shared config has a fixed
    # absolute path, so HOME is unnecessary. Only the shared API-key override
    # is supported; the model is always passed explicitly and cannot drift.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if os.environ.get("TYPESAFE_API_KEY"):
        environment["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        output = bytearray()
        sent, finished = 0, False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while not finished:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError()
                for ready, _ in events:
                    if ready.fileobj is process.stdin:
                        try:
                            sent += os.write(process.stdin.fileno(), payload[sent:])
                        except BrokenPipeError:
                            raise ValueError("JEV_SDK_UNAVAILABLE") from None
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(
                            process.stdout.fileno(),
                            MAX_RESPONSE_BYTES + 1 - len(output),
                        )
                        if not chunk:
                            finished = True
                            break
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        process.wait(timeout=max(0.001, work_deadline - time.monotonic()))
        if process.returncode == 2:
            raise JevProviderUnavailable()
        if process.returncode == 3:
            raise TimeoutError()
        if process.returncode == 4:
            raise JevResponseInvalid()
        if process.returncode == 5:
            raise JevProviderRequestRejected()
        if process.returncode == 6:
            raise JevConfigurationUnavailable()
        if process.returncode != 0:
            raise ValueError("JEV_SDK_UNAVAILABLE")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise TimeoutError() from None
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            # Never block a trading host indefinitely even during OS trouble.
            # Popen retains/reaps its own child later; no process-group kill.
            pass
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


class SharedJevQuery:
    """Official pinned shared SDK in a disposable process, not a public gateway."""

    def __call__(self, request, *, timeout_ms):
        _integer(timeout_ms, 1, 2000)
        if request != _request(request.get("state")):
            raise ValueError("JEV_REQUEST_CONTRACT_CHANGED")
        payload = _json({"request": request, "timeout_ms": timeout_ms}).encode()
        raw = _bounded_process(
            [SHARED_PYTHON, "-I", str(Path(__file__).resolve()), "--sdk-once"],
            payload,
            timeout_ms,
        )
        try:
            return parse_response(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            raise JevResponseInvalid() from None


def _sdk_once():
    """Child-only entry: configuration is consumed solely by the shared client."""
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 513)
        if len(raw) > MAX_REQUEST_BYTES + 512:
            raise ValueError("JEV_REQUEST_TOO_LARGE")
        envelope = json.loads(raw)
        if type(envelope) is not dict or set(envelope) != {"request", "timeout_ms"}:
            raise ValueError("JEV_REQUEST_INVALID")
        timeout_ms = _integer(envelope["timeout_ms"], 1, 2000)
        request = envelope["request"]
        if (
            request != _request(request.get("state"))
            or version("typesafe-sdk") != SDK_VERSION
        ):
            raise ValueError("JEV_PINNED_SDK_REQUIRED")
        from jev_shared import create_client
        from typesafe_sdk import RetryPolicy

        logging.disable(logging.CRITICAL)
        try:
            client = create_client(model=MODEL)
        except Exception as exc:
            from jev_shared import ConfigurationError

            if isinstance(exc, ConfigurationError):
                # Missing/invalid local credentials or client configuration,
                # never a response or provider balance inference. No text read.
                return 6
            raise
        with client:
            response = client.system_one(
                **request,
                timeout=timeout_ms / 1000,
                retry=RetryPolicy(max_retries=0, timeout=timeout_ms / 1000),
            )
        try:
            encoded = _json(parse_response(response.model_dump(mode="json"))).encode()
        except (ValueError, TypeError, KeyError):
            return 4
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as exc:  # noqa: BLE001 - child boundary exports no raw failures
        # No SDK messages, bodies, config, headers or traceback may escape.
        try:
            from typesafe_sdk import (
                TypeSafeAPIConnectionError,
                TypeSafeAPIError,
                TypeSafeAPIResponseValidationError,
                TypeSafeAPITimeoutError,
            )

            if isinstance(exc, TypeSafeAPITimeoutError):
                return 3
            if isinstance(exc, TypeSafeAPIResponseValidationError):
                return 4
            if isinstance(exc, TypeSafeAPIError):
                # SDK's structured HTTP status only: never infer token balance
                # from a body/message or treat an invalid 200 as availability.
                status = getattr(exc, "status", None)
                return 5 if type(status) is int and 400 <= status <= 599 else 1
            if isinstance(exc, TypeSafeAPIConnectionError):
                return 2
        except (ImportError, AttributeError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_sdk_once() if sys.argv[1:] == ["--sdk-once"] else 2)
