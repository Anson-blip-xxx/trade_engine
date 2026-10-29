"""Shared, credential-free quant research with PG-first immutable observations.

Uses the existing audited business-state/outbox rather than a local cache as
truth. Publication can retry after a crash without renewing the original signal.
Neither research mode nor candidate publication authorizes account admissions.
"""

import json
from dataclasses import asdict, dataclass, field

from v2_core.evidence import canonical, digest
from v2_core.ingress import SignalIngress, milliseconds, symbol
from v2_core.quant_indicators import VERSION, aggregate_minutes, decimal, encode
from v2_core.quant_registry import (
    PLUGINS,
    REGISTRY_VERSION,
    evaluate,
    feature_frame,
    parameters,
    warmup,
)
from v2_core.signals import Signals
from v2_core.state import BusinessState, StateKey


@dataclass(frozen=True)
class QuantPolicy:
    mode: str = "OFF"
    timeframes: tuple = (5, 15)
    plugins: tuple = ("donchian", "squeeze")
    settings: dict = field(default_factory=dict)
    lifetime_ms: int = 90000
    max_history_minutes: int = 14400
    cycle_budget_ms: int = 2000

    def __post_init__(self):
        if self.mode not in {"OFF", "SHADOW", "CANDIDATE"}:
            raise ValueError("QUANT_MODE_REQUIRED")
        for values, allowed in (
            (self.timeframes, {1, 5, 15, 30, 60, 240}),
            (self.plugins, set(PLUGINS)),
        ):
            if (
                not isinstance(values, (list, tuple))
                or not values
                or len(values) > len(allowed)
                or len(set(values)) != len(values)
                or not set(values) <= allowed
            ):
                raise ValueError("QUANT_POLICY_SELECTION")
        if any(type(x) is not int for x in self.timeframes):
            raise ValueError("QUANT_TIMEFRAME_TYPE")
        if type(self.lifetime_ms) is not int or not 1000 <= self.lifetime_ms <= 180000:
            raise ValueError("QUANT_LIFETIME_BOUND")
        if (
            type(self.cycle_budget_ms) is not int
            or not 100 <= self.cycle_budget_ms <= 5000
        ):
            raise ValueError("QUANT_COMPUTE_BUDGET")
        if (
            type(self.max_history_minutes) is not int
            or not 1440 <= self.max_history_minutes <= 14400
        ):
            raise ValueError("QUANT_HISTORY_BOUND")
        object.__setattr__(self, "settings", parameters(self.settings))
        if any(
            warmup(self.settings) * tf > self.max_history_minutes
            for tf in self.timeframes
        ):
            raise ValueError("QUANT_WARMUP_EXCEEDS_BUDGET")

    def values(self):
        return {
            **asdict(self),
            "timeframes": list(self.timeframes),
            "plugins": list(self.plugins),
        }

    @staticmethod
    def key(environment):
        if environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("QUANT_ENVIRONMENT")
        return StateKey(
            "BINANCE",
            "market-data",
            environment,
            "FUTURES",
            "quant-policy-v1",
            "current",
        )

    @classmethod
    def read(cls, connect, environment):
        row = BusinessState(connect).read(cls.key(environment))
        if row is None:
            return cls()
        if row.deleted:
            raise ValueError("QUANT_POLICY_DELETED")
        return cls(**json.loads(row.payload_json))

    def save(self, connect, environment, *, expected_version, request_key):
        if environment != "SANDBOX" and self.mode == "CANDIDATE":
            raise ValueError("QUANT_LIVE_ADMISSION_NOT_IMPLEMENTED")
        return BusinessState(connect).change(
            self.key(environment),
            expected_version=expected_version,
            request_key=request_key,
            payload=self.values(),
            reason="QUANT_POLICY_UPDATE",
        )


def observation_key(environment, identity):
    if (
        environment not in {"SANDBOX", "LIVE"}
        or not isinstance(identity, str)
        or len(identity) != 64
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("QUANT_OBSERVATION_ID")
    return StateKey(
        "BINANCE",
        "market-data",
        environment,
        "FUTURES",
        "quant-observation-v1",
        identity,
    )


class QuantResearch:
    def __init__(self, connect, *, archive, environment, clock_ms):
        QuantPolicy.key(environment)
        if archive.environment != environment or not callable(clock_ms):
            raise ValueError("QUANT_RESEARCH_SCOPE")
        self.connect, self.archive, self.environment, self.clock = (
            connect,
            archive,
            environment,
            clock_ms,
        )
        self.store = BusinessState(connect)

    def observe(self, target, newest_first_minutes, observed_at_ms, *, policy=None):
        symbol(target)
        milliseconds(observed_at_ms)
        policy = policy or QuantPolicy.read(self.connect, self.environment)
        if policy.mode == "OFF":
            return {"status": "DISABLED", "observations": []}
        if self.environment != "SANDBOX" and policy.mode == "CANDIDATE":
            raise ValueError("QUANT_LIVE_ADMISSION_NOT_IMPLEMENTED")
        now = milliseconds(self.clock())
        if not 0 <= now - observed_at_ms < policy.lifetime_ms:
            raise ValueError("QUANT_MARKET_STALE")
        config = {
            "registry_version": REGISTRY_VERSION,
            "feature_version": VERSION,
            "plugins": list(policy.plugins),
            "settings": policy.settings,
            "lifetime_ms": policy.lifetime_ms,
        }
        fingerprint = digest(canonical(config))
        output = []
        for timeframe in policy.timeframes:
            closed = observed_at_ms // (timeframe * 60000) * timeframe * 60000
            identity = digest(
                canonical(
                    {
                        "environment": self.environment,
                        "symbol": target,
                        "timeframe": timeframe,
                        "closed_at_ms": closed,
                        "config_digest": fingerprint,
                    }
                )
            )
            key = observation_key(self.environment, identity)
            stored = self.store.read(key)
            if stored:
                if stored.deleted or stored.version != 1:
                    raise ValueError("QUANT_OBSERVATION_DELETED")
                payload = json.loads(stored.payload_json)
            else:
                count = warmup(policy.settings) * timeframe
                start = closed - count * 60000
                rows = [b for b in newest_first_minutes if start <= b["t"] < closed]
                if len(rows) < count:
                    rows = self.archive.read(target, max(0, start), closed)
                # Missing history remains retryable. Never freeze WARMUP under
                # the READY identity, which would prevent later archive recovery.
                if len(rows) < count:
                    output.append(
                        {
                            "observation_id": identity,
                            "status": "WARMUP",
                            "required_minutes": count,
                            "available_minutes": len(rows),
                        }
                    )
                    continue
                bars = aggregate_minutes(
                    list(reversed(rows)),
                    timeframe_minutes=timeframe,
                    closed_at_ms=closed,
                )
                frame = feature_frame(
                    bars, timeframe_minutes=timeframe, params=policy.settings
                )
                if frame["status"] != "READY":
                    raise ValueError("QUANT_HISTORY_WARMUP_MISMATCH")
                result = evaluate(frame, enabled=policy.plugins)
                payload = {
                    "environment": self.environment,
                    "symbol": target,
                    "observation_id": identity,
                    "configuration": config,
                    "config_digest": fingerprint,
                    "frame": frame,
                    "result": result,
                }
                self.store.change(
                    key,
                    expected_version=0,
                    request_key="observe:" + identity,
                    payload=payload,
                    reason="QUANT_RESEARCH_OBSERVATION",
                )
                actual = self.store.read(key)
                if (
                    actual is None
                    or actual.deleted
                    or actual.payload_json != canonical(payload)
                ):
                    raise ValueError("QUANT_OBSERVATION_CONFLICT")
            row = {
                "observation_id": identity,
                "status": payload["result"]["status"],
                "signal_id": None,
            }
            winner = payload["result"]["winner"]
            if policy.mode == "CANDIDATE" and winner:
                expires = closed + policy.lifetime_ms
                if self.clock() >= expires:
                    row["publication"] = "EXPIRED_NOT_PUBLISHED"
                else:
                    signal = (
                        "QUANT_"
                        + winner["strategy_id"].upper()
                        + ("_UP" if winner["side"] == "BUY" else "_DOWN")
                    )
                    target_move = encode(
                        decimal(winner["stop_fraction"])
                        * decimal(policy.settings["target_r_multiple"])
                        * 100
                    )
                    row["signal_id"] = SignalIngress(
                        Signals(self.connect),
                        source="quant",
                        environment=self.environment,
                        clock_ms=self.clock,
                        max_age_ms=policy.lifetime_ms,
                        max_lifetime_ms=policy.lifetime_ms,
                    ).accept(
                        {
                            "event_id": "quant:" + identity,
                            "observed_at": closed,
                            "expires_at_ms": expires,
                            "symbol": target,
                            "signal": signal,
                            "features": {
                                "type": signal,
                                "strength": winner["score"],
                                "quant_observation_id": identity,
                                "quant_config_digest": fingerprint,
                                "quant_timeframe_minutes": timeframe,
                                "quant_registry_version": REGISTRY_VERSION,
                                "quant_target_move_pct": target_move,
                                "expected_move_model": "ATR_TARGET_NOT_FORECAST",
                            },
                        }
                    )
                    row["publication"] = "PUBLISHED"
            else:
                row["publication"] = (
                    "SHADOW" if policy.mode == "SHADOW" else "NO_CANDIDATE"
                )
            output.append(row)
        return {"status": "RECORDED", "mode": policy.mode, "observations": output}


class QuantEvidenceContext:
    """Only frozen server-side observations may supply a quant market plan."""

    def __init__(self, connect, delegate, *, archive, environment="SANDBOX"):
        QuantPolicy.key(environment)
        if archive.environment != environment:
            raise ValueError("QUANT_ARCHIVE_SCOPE")
        self.archive = archive
        self.store, self.delegate, self.environment = (
            BusinessState(connect),
            delegate,
            environment,
        )

    def __call__(self, signal):
        identity = signal["features"].get("quant_observation_id")
        row = self.store.read(observation_key(self.environment, identity))
        if row is None or row.deleted or row.version != 1:
            raise ValueError("QUANT_EVIDENCE_MISSING")
        proof = json.loads(row.payload_json)
        if (
            proof["environment"] != self.environment
            or proof["symbol"] != signal["symbol"]
            or proof["config_digest"] != signal["features"].get("quant_config_digest")
            or proof["frame"]["closed_at_ms"] != signal["observed_at"]
            or digest(canonical(proof["configuration"])) != proof["config_digest"]
            or proof["configuration"]["registry_version"] != REGISTRY_VERSION
            or proof["configuration"]["feature_version"] != VERSION
            or signal["expires_at_ms"]
            != signal["observed_at"] + proof["configuration"]["lifetime_ms"]
            or signal["features"].get("expected_move_model")
            != "ATR_TARGET_NOT_FORECAST"
            or signal["features"].get("quant_timeframe_minutes")
            != proof["frame"]["timeframe_minutes"]
            or signal["features"].get("quant_registry_version") != REGISTRY_VERSION
        ):
            raise ValueError("QUANT_EVIDENCE_SCOPE")
        frame = proof["frame"]
        computed_id = digest(
            canonical(
                {
                    "environment": self.environment,
                    "symbol": signal["symbol"],
                    "timeframe": frame["timeframe_minutes"],
                    "closed_at_ms": frame["closed_at_ms"],
                    "config_digest": proof["config_digest"],
                }
            )
        )
        if identity != computed_id or proof["observation_id"] != computed_id:
            raise ValueError("QUANT_EVIDENCE_IDENTITY")
        # Never trust a Redis-derived frame just because it was persisted in PG.
        # Independent reconstruction from the immutable archive must match.
        settings = proof["configuration"]["settings"]
        start = (
            frame["closed_at_ms"]
            - warmup(settings) * frame["timeframe_minutes"] * 60000
        )
        rows = self.archive.read(signal["symbol"], start, frame["closed_at_ms"])
        bars = aggregate_minutes(
            list(reversed(rows)),
            timeframe_minutes=frame["timeframe_minutes"],
            closed_at_ms=frame["closed_at_ms"],
        )
        rebuilt = feature_frame(
            bars, timeframe_minutes=frame["timeframe_minutes"], params=settings
        )
        if frame != rebuilt:
            raise ValueError("QUANT_ARCHIVE_PROOF_MISMATCH")
        # Re-run the pure plugin calculation; an ID alone is never a candidate.
        if proof["result"] != evaluate(
            proof["frame"], enabled=proof["configuration"]["plugins"]
        ):
            raise ValueError("QUANT_EVIDENCE_RESULT")
        winner = proof["result"]["winner"]
        if winner is None or signal["signal"] != "QUANT_" + winner[
            "strategy_id"
        ].upper() + ("_UP" if winner["side"] == "BUY" else "_DOWN"):
            raise ValueError("QUANT_EVIDENCE_DIRECTION")
        expected = encode(
            decimal(winner["stop_fraction"])
            * decimal(proof["configuration"]["settings"]["target_r_multiple"])
            * 100
        )
        if (
            signal["features"].get("quant_target_move_pct") != expected
            or signal["features"].get("strength") != winner["score"]
        ):
            raise ValueError("QUANT_EVIDENCE_FEATURES")
        return {**self.delegate(signal), "quant": proof}
