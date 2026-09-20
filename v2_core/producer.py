"""Transactional producer confirmations plus advisory market projections.

Market history belongs in analytics. PG keeps signal facts and a batch digest;
Redis holds current context, which becomes immutable evidence when consumed.
"""

import json
from contextlib import nullcontext

from v2_core.evidence import canonical, digest
from v2_core.ingress import SignalIngress, identity, milliseconds, symbol
from v2_core.projections import RedisProjection
from v2_core.signals import SignalConflict, Signals


def scope(source, environment):
    if source not in {"s0", "s2", "s3"} or environment not in {"SANDBOX", "LIVE"}:
        raise ValueError("explicit producer source/environment required")


def market_envelope(source, environment, target, observed_at, features):
    scope(source, environment)
    if target != "*":
        symbol(target)
    if source == "s0" and target != "*":
        raise ValueError("S0 requires explicit global context")
    if source != "s0" and target == "*":
        raise ValueError("S2/S3 require symbol context")
    milliseconds(observed_at)
    if not isinstance(features, dict):
        raise TypeError("context features must be an object")
    record = {
        "source": source,
        "environment": environment,
        "symbol": target,
        "observed_at": observed_at,
        "features": features,
    }
    encoded = canonical(record)
    if len(encoded.encode()) > 1_000_000:
        raise ValueError("context exceeds size limit")
    return {"snapshot_id": digest(encoded), **json.loads(encoded)}


class RedisMarketContext:
    def __init__(self, client, *, environment):
        scope("s0", environment)
        self.client, self.environment = client, environment

    def _namespace(self, source, environment):
        scope(source, environment)
        if environment != self.environment:
            raise ValueError("market environment binding mismatch")
        return "v2:market:" + environment + ":" + source

    def put(self, envelope):
        expected = market_envelope(
            envelope["source"],
            envelope["environment"],
            envelope["symbol"],
            envelope["observed_at"],
            envelope["features"],
        )
        if envelope != expected:
            raise ValueError("market envelope identity conflict")
        namespace = self._namespace(envelope["source"], envelope["environment"])
        # Integer ms, with +1 to support epoch zero. Older observations cannot
        # overwrite newer ones; same-time different content is an explicit fault.
        return RedisProjection(self.client, namespace).put(
            envelope["symbol"], envelope["observed_at"] + 1, envelope
        )

    def read(self, source, environment, target):
        namespace = self._namespace(source, environment)
        if target != "*":
            symbol(target)
        raw = self.client.hget(namespace + ":" + target, "payload")
        if raw is None:
            return None
        if not isinstance(raw, (str, bytes)) or len(raw) > 1_001_000:
            raise ValueError("invalid cached context")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {
            "snapshot_id",
            "source",
            "environment",
            "symbol",
            "observed_at",
            "features",
        }:
            raise ValueError("invalid cached context")
        if (envelope["source"], envelope["environment"], envelope["symbol"]) != (
            source,
            environment,
            target,
        ):
            raise ValueError("cached context scope mismatch")
        if envelope != market_envelope(
            source, environment, target, envelope["observed_at"], envelope["features"]
        ):
            raise ValueError("cached context digest mismatch")
        return envelope


class ProducerPublisher:
    def __init__(
        self,
        connection_factory,
        *,
        source,
        environment,
        market,
        clock_ms,
        max_age_ms,
        lifetime_ms,
    ):
        scope(source, environment)
        # S0 publishes context only; validate policy through the common contract.
        SignalIngress(
            None,
            source="s3" if source == "s0" else source,
            environment=environment,
            clock_ms=clock_ms,
            max_age_ms=max_age_ms,
            max_lifetime_ms=lifetime_ms,
        )
        self._connect, self.source, self.environment = (
            connection_factory,
            source,
            environment,
        )
        self.market, self.clock_ms, self.max_age_ms, self.lifetime_ms = (
            market,
            clock_ms,
            max_age_ms,
            lifetime_ms,
        )

    def publish(self, *, frame_id, observed_at, contexts, events):
        identity(frame_id)
        milliseconds(observed_at)
        if not isinstance(contexts, dict) or not contexts or len(contexts) > 1000:
            raise ValueError("bounded nonempty context frame required")
        if (
            not isinstance(events, list)
            or len(events) > 1000
            or (self.source == "s0" and events)
        ):
            raise ValueError("bounded event list required; S0 is context only")
        envelopes = [
            market_envelope(
                self.source, self.environment, target, observed_at, features
            )
            for target, features in sorted(contexts.items())
        ]
        normalized = []
        keys = set()
        by_symbol = {envelope["symbol"]: envelope for envelope in envelopes}
        for event in events:
            if not isinstance(event, dict) or set(event) != {
                "event_id",
                "symbol",
                "signal",
                "features",
            }:
                raise ValueError(
                    "explicit stable event identity and normalized fields required"
                )
            key = identity(event["event_id"])
            if key in keys or event["symbol"] not in contexts:
                raise ValueError("duplicate event ID or missing symbol context")
            keys.add(key)
            if (
                not isinstance(event["features"], dict)
                or "producer_context" in event["features"]
            ):
                raise ValueError("producer context reference is reserved")
            reference = {
                key: value
                for key, value in by_symbol[event["symbol"]].items()
                if key != "features"
            }
            normalized.append(
                {
                    **event,
                    "features": {**event["features"], "producer_context": reference},
                    "observed_at": observed_at,
                    "expires_at_ms": observed_at + self.lifetime_ms,
                }
            )
        # Freeze the entire frame before any I/O; callers cannot mutate retries.
        encoded = canonical(
            {"contexts": envelopes, "events": normalized, "observed_at": observed_at}
        )
        if len(encoded.encode()) > 8_000_000:
            raise ValueError("producer batch exceeds size limit")
        frozen, fingerprint = json.loads(encoded), digest(encoded)
        with self._connect() as conn:
            # Producer-level ordering prevents opposite event ordering deadlocks
            # between batches. A hash collision only serializes unrelated writers.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (canonical({"source": self.source, "environment": self.environment}),),
            )
            prior = conn.execute(
                """SELECT content_digest,signal_ids FROM v2_producer_batches
                WHERE source=%s AND environment=%s AND frame_id=%s""",
                (self.source, self.environment, frame_id),
            ).fetchone()
            if prior is not None:
                if prior[0] != fingerprint:
                    raise SignalConflict("producer frame identity content conflict")
                signal_ids = prior[1]
            else:
                now = milliseconds(self.clock_ms())
                if (
                    not observed_at <= now < observed_at + self.lifetime_ms
                    or now - observed_at > self.max_age_ms
                ):
                    raise ValueError("producer frame is stale or future")
                ingress = SignalIngress(
                    Signals(lambda: nullcontext(conn)),
                    source="s3" if self.source == "s0" else self.source,
                    environment=self.environment,
                    clock_ms=lambda: now,
                    max_age_ms=self.max_age_ms,
                    max_lifetime_ms=self.lifetime_ms,
                )
                signal_ids = [ingress.accept(event) for event in frozen["events"]]
                conn.execute(
                    """INSERT INTO v2_producer_batches(source,environment,frame_id,content_digest,observed_at_ms,signal_ids)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                    (
                        self.source,
                        self.environment,
                        frame_id,
                        fingerprint,
                        observed_at,
                        json.dumps(signal_ids),
                    ),
                )
        # Cache is non-authoritative. No market write if PG commit/ack failed.
        # After a successful commit, callers retry this same frame to repair cache.
        failures = {}
        for envelope in frozen["contexts"]:
            try:
                if self.market.put(envelope) is not True:
                    raise ValueError("market projection conflict")
            except Exception as exc:  # noqa: BLE001 - signal facts already committed; report bounded diagnostics
                failures[envelope["symbol"]] = type(exc).__name__
        return {
            "status": "RECORDED",
            "signal_ids": signal_ids,
            "market_status": "UNAVAILABLE" if failures else "PROJECTED",
            "market_errors": failures,
        }
