"""Bound-source admission. Never invent event IDs or refresh source deadlines."""

import json
import re
from copy import deepcopy

from v2_core.evidence import canonical
from v2_core.signals import SignalConflict


class IntakeRejected(ValueError):
    """Fixed, non-sensitive reason safe to return to a producer."""


def milliseconds(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise IntakeRejected("INVALID_TIMESTAMP")
    return value


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:/-]{1,128}", value
    ):
        raise IntakeRejected("INVALID_EVENT_ID")
    return value


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Z0-9]{1,32}(USDT|USDC)", value
    ):
        raise IntakeRejected("INVALID_SYMBOL")
    return value


class SignalIngress:
    def __init__(
        self, signals, *, source, environment, clock_ms, max_age_ms, max_lifetime_ms
    ):
        if source not in {"s2", "s3", "tv_bridge"} or environment not in {
            "LIVE",
            "SANDBOX",
        }:
            raise ValueError("explicit supported source/environment required")
        if not callable(clock_ms) or any(
            type(n) is not int or not 1 <= n <= 86400000
            for n in (max_age_ms, max_lifetime_ms)
        ):
            raise ValueError("explicit clock and bounded freshness policy required")
        self.signals, self.source, self.environment = signals, source, environment
        self.clock_ms, self.max_age_ms, self.max_lifetime_ms = (
            clock_ms,
            max_age_ms,
            max_lifetime_ms,
        )

    def accept(self, event):
        if not isinstance(event, dict) or set(event) != {
            "event_id",
            "observed_at",
            "expires_at_ms",
            "symbol",
            "signal",
            "features",
        }:
            raise IntakeRejected("INVALID_EVENT_FIELDS")
        try:
            frozen = json.loads(canonical(event))
        except (TypeError, ValueError, RecursionError):
            raise IntakeRejected("INVALID_EVENT_CONTENT") from None
        if len(canonical(frozen).encode()) > 16384:
            raise IntakeRejected("EVENT_TOO_LARGE")
        key = identity(frozen.pop("event_id"))
        symbol(frozen["symbol"])
        observed, expires = (
            milliseconds(frozen["observed_at"]),
            milliseconds(frozen["expires_at_ms"]),
        )
        if not observed < expires <= observed + self.max_lifetime_ms:
            raise IntakeRejected("INVALID_EVENT_LIFETIME")
        if (
            not isinstance(frozen["features"], dict)
            or not isinstance(frozen["signal"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", frozen["signal"])
        ):
            raise IntakeRejected("INVALID_SIGNAL")
        existing = self.signals.lookup(
            source=self.source, environment=self.environment, request_key=key
        )
        if existing is not None:
            if existing["snapshot"] != frozen:
                raise SignalConflict("signal identity content conflict")
            # An expired exact replay acknowledges the old record. It neither
            # creates a new event nor renews the worker's deadline.
            return existing["signal_id"]
        now = milliseconds(self.clock_ms())
        if observed > now:
            raise IntakeRejected("FUTURE_EVENT")
        if now >= expires or now - observed > self.max_age_ms:
            raise IntakeRejected("STALE_EVENT")
        return self.signals.admit(
            source=self.source,
            environment=self.environment,
            request_key=key,
            snapshot=frozen,
        )


class ContextProvider:
    """Read explicit source envelopes; freeze only fresh, correctly scoped facts.

    `read(source, environment, symbol)` must be read-only and finitely timed.
    Policy maps source to {scope: GLOBAL|SYMBOL, max_age_ms: positive int}.
    S0 is context, not an executable opening signal.
    """

    def __init__(self, *, environment, policy, read, clock_ms):
        if (
            environment not in {"LIVE", "SANDBOX"}
            or not callable(read)
            or not callable(clock_ms)
        ):
            raise ValueError("explicit context bindings required")
        if (
            not isinstance(policy, dict)
            or not policy
            or not set(policy) <= {"s0", "s2", "s3"}
        ):
            raise ValueError("explicit context source policy required")
        self.policy = json.loads(canonical(policy))
        for rule in self.policy.values():
            if (
                not isinstance(rule, dict)
                or set(rule) != {"scope", "max_age_ms"}
                or rule["scope"] not in {"GLOBAL", "SYMBOL"}
                or type(rule["max_age_ms"]) is not int
                or not 1 <= rule["max_age_ms"] <= 86400000
            ):
                raise ValueError("invalid context freshness policy")
        self.environment, self.read, self.clock_ms = environment, read, clock_ms

    def __call__(self, signal):
        target = symbol(signal["symbol"])
        facts = {}
        for source, rule in self.policy.items():
            scoped_symbol = "*" if rule["scope"] == "GLOBAL" else target
            envelope = deepcopy(self.read(source, self.environment, scoped_symbol))
            if not isinstance(envelope, dict) or set(envelope) != {
                "snapshot_id",
                "source",
                "environment",
                "symbol",
                "observed_at",
                "features",
            }:
                raise IntakeRejected("MISSING_CONTEXT")
            if (envelope["source"], envelope["environment"], envelope["symbol"]) != (
                source,
                self.environment,
                scoped_symbol,
            ):
                raise IntakeRejected("CONTEXT_SCOPE_MISMATCH")
            identity(envelope["snapshot_id"])
            milliseconds(envelope["observed_at"])
            if not isinstance(envelope["features"], dict):
                raise IntakeRejected("INVALID_CONTEXT")
            facts[source] = envelope
        now = milliseconds(self.clock_ms())
        for source, fact in facts.items():
            if not 0 <= now - fact["observed_at"] <= self.policy[source]["max_age_ms"]:
                raise IntakeRejected("STALE_CONTEXT")
        context = {
            "assembled_at": now,
            "valid_until_ms": min(
                fact["observed_at"] + self.policy[source]["max_age_ms"] + 1
                for source, fact in facts.items()
            ),
            "environment": self.environment,
            "symbol": target,
            "sources": facts,
            "freshness_policy": self.policy,
        }
        try:
            encoded = canonical(context)
        except (TypeError, ValueError, RecursionError):
            raise IntakeRejected("INVALID_CONTEXT") from None
        if len(encoded.encode()) > 1_000_000:
            raise IntakeRejected("CONTEXT_TOO_LARGE")
        return json.loads(encoded)
