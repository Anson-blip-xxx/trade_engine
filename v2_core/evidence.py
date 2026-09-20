"""Immutable, content-addressed strategy configuration and decision evidence."""

import hashlib
import json
from dataclasses import dataclass


def canonical(value):
    def validate(item):
        if item is None or type(item) in (str, int, bool):
            return
        if isinstance(item, list):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("JSON must use decimal strings instead of floats")

    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(encoded):
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class DecisionEvidence:
    strategy_version: str
    config_json: str
    snapshot_json: str

    def __post_init__(self):
        if (
            not self.strategy_version
            or self.strategy_version != self.strategy_version.strip()
        ):
            raise ValueError("strategy_version is required")
        config = canonical(json.loads(self.config_json))
        snapshot = json.loads(self.snapshot_json)
        required = {
            "observed_at",
            "decided_at",
            "source",
            "symbol",
            "rationale",
            "features",
        }
        if not isinstance(snapshot, dict) or not required <= snapshot.keys():
            raise ValueError(
                "snapshot requires timestamps, source, symbol, rationale, features"
            )
        for key in ("observed_at", "decided_at"):
            if type(snapshot[key]) is not int or snapshot[key] < 0:
                raise ValueError("timestamps must be nonnegative epoch milliseconds")
        if snapshot["observed_at"] > snapshot["decided_at"]:
            raise ValueError("observation cannot follow decision")
        for key in ("source", "symbol", "rationale"):
            if not isinstance(snapshot[key], str) or not snapshot[key].strip():
                raise ValueError(f"{key} is required")
        if not isinstance(snapshot["features"], dict):
            raise TypeError("features must be an object")
        object.__setattr__(self, "config_json", config)
        object.__setattr__(self, "snapshot_json", canonical(snapshot))

    @property
    def config_digest(self):
        return digest(self.config_json)

    @property
    def evidence_ref(self):
        return digest(
            canonical(
                {
                    "strategy_version": self.strategy_version,
                    "config_digest": self.config_digest,
                    "snapshot": json.loads(self.snapshot_json),
                }
            )
        )


class EvidenceStore:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def put(self, evidence):
        if not isinstance(evidence, DecisionEvidence):
            raise TypeError("expected DecisionEvidence")
        # Exceptions propagate: callers must not admit a trade after failed evidence persistence.
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO v2_decision_evidence
                (evidence_ref, config_digest, strategy_version, config, snapshot)
                VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)
                ON CONFLICT DO NOTHING""",
                (
                    evidence.evidence_ref,
                    evidence.config_digest,
                    evidence.strategy_version,
                    evidence.config_json,
                    evidence.snapshot_json,
                ),
            )
            row = conn.execute(
                """SELECT config, snapshot, strategy_version
                FROM v2_decision_evidence WHERE evidence_ref=%s""",
                (evidence.evidence_ref,),
            ).fetchone()
            if row != (
                json.loads(evidence.config_json),
                json.loads(evidence.snapshot_json),
                evidence.strategy_version,
            ):
                raise ValueError("evidence content conflict")
        return evidence.evidence_ref
