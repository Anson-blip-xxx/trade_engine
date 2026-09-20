"""Durable S3 frame/lifecycle transaction. Redis is projected after commit only."""

import json
from contextlib import nullcontext
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from v2_core.evidence import canonical, digest
from v2_core.ingress import identity, milliseconds, symbol
from v2_core.signals import SignalConflict
from v2_core.state import BusinessState, StateKey


def advance_events(
    previous,
    candidates,
    *,
    environment,
    frame_id,
    observed_at,
    covered,
    removed,
    cooldown_ms,
    absence_ms,
    strength_delta,
):
    active, emitted, seen = deepcopy(previous), [], set()

    def emit(key, entry, state, features):
        entry["revision"] += 1
        event_id = str(
            uuid5(NAMESPACE_URL, entry["episode"] + ":" + str(entry["revision"]))
        )
        emitted.append(
            {
                "event_id": event_id,
                "symbol": entry["symbol"],
                "signal": "EVENT_END" if state == "END" else entry["type"],
                "features": {
                    **features,
                    "type": entry["type"],
                    "state": state,
                    "episode_id": entry["episode"],
                    "revision": entry["revision"],
                    "since_ms": entry["started_at"],
                    "last_seen_ms": entry["last_seen"],
                },
            }
        )
        active[key] = entry

    if not isinstance(candidates, list) or len(candidates) > 1000:
        raise ValueError("bounded candidate list required")
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or not {"symbol", "type", "strength"} <= candidate.keys()
        ):
            raise ValueError("normalized candidate required")
        canonical(candidate)
        if {
            "event_id",
            "state",
            "episode_id",
            "revision",
            "since_ms",
            "last_seen_ms",
            "producer_context",
        } & candidate.keys():
            raise ValueError("reserved lifecycle fields")
        target, kind, strength = (
            symbol(candidate["symbol"]),
            identity(candidate["type"]),
            candidate["strength"],
        )
        direction = candidate.get("direction", "")
        if direction not in ("", "HIGH", "LOW") or kind == "EVENT_END":
            raise ValueError("invalid candidate kind/direction")
        if (
            target not in covered
            or type(strength) is not int
            or not 0 <= strength <= 100
        ):
            raise ValueError("candidate outside coverage or invalid strength")
        key = canonical({"symbol": target, "type": kind, "direction": direction})
        if key in seen:
            raise ValueError("duplicate candidate")
        seen.add(key)
        entry = active.get(key)
        state = "UPDATE"
        if entry is None:
            state = "ACTIVE"
            entry = {
                "symbol": target,
                "type": kind,
                "direction": direction,
                "episode": str(
                    uuid5(
                        NAMESPACE_URL,
                        canonical(
                            {
                                "source": "v2:s3",
                                "environment": environment,
                                "frame_id": frame_id,
                                "key": key,
                            }
                        ),
                    )
                ),
                "started_at": observed_at,
                "revision": 0,
                "last_emitted": observed_at,
                "strength": strength,
            }
        entry["last_seen"] = observed_at
        active[key] = entry
        if (
            state == "ACTIVE"
            or observed_at - entry["last_emitted"] >= cooldown_ms
            or abs(strength - entry["strength"]) >= strength_delta
        ):
            entry.update(last_emitted=observed_at, strength=strength)
            emit(
                key,
                entry,
                state,
                {k: v for k, v in candidate.items() if k not in ("symbol", "type")},
            )
    for key, entry in list(active.items()):
        explicit = entry["symbol"] in removed
        absent = (
            entry["symbol"] in covered
            and key not in seen
            and observed_at - entry["last_seen"] > absence_ms
        )
        if explicit or absent:
            emit(
                key,
                entry,
                "END",
                {
                    "strength": entry["strength"],
                    "direction": entry["direction"],
                    "reason": "symbol_removed" if explicit else "candidate_absent",
                },
            )
            del active[key]
    if len(active) > 10000:
        raise ValueError("active lifecycle capacity exceeded")
    return active, emitted


class S3FrameProcessor:
    def __init__(
        self,
        publisher,
        *,
        evaluate,
        detector_version,
        cooldown_ms=30000,
        absence_ms=300000,
        strength_delta=20,
    ):
        if publisher.source != "s3":
            raise ValueError("S3 publisher required")
        identity(detector_version)
        if (
            any(
                type(v) is not int or v < 1
                for v in (cooldown_ms, absence_ms, strength_delta)
            )
            or strength_delta > 100
        ):
            raise ValueError("positive bounded lifecycle policy required")
        self.publisher, self.evaluate = publisher, evaluate
        self.config = {
            "detector_version": detector_version,
            "cooldown_ms": cooldown_ms,
            "absence_ms": absence_ms,
            "strength_delta": strength_delta,
            "lifetime_ms": publisher.lifetime_ms,
        }
        self.key = StateKey(
            "BINANCE",
            "market-data",
            publisher.environment,
            "FUTURES",
            "s3-lifecycle",
            "global",
        )

    def process(
        self, *, frame_id, observed_at, contexts, raw_windows, removed_symbols=()
    ):
        identity(frame_id)
        milliseconds(observed_at)
        if (
            not isinstance(contexts, dict)
            or len(contexts) > 1000
            or not isinstance(raw_windows, dict)
            or not raw_windows.keys() <= contexts.keys()
        ):
            raise ValueError("bounded covered contexts and raw windows required")
        if (
            not isinstance(removed_symbols, (list, tuple))
            or len(removed_symbols) > 1000
        ):
            raise ValueError("bounded explicit removals required")
        removed = sorted({symbol(item) for item in removed_symbols})
        for target in contexts:
            symbol(target)
        if set(removed) & contexts.keys():
            raise ValueError("removed symbols cannot also be covered")
        encoded = canonical(
            {
                "contexts": contexts,
                "raw_windows": raw_windows,
                "removed": removed,
                "observed_at": observed_at,
            }
        )
        if len(encoded.encode()) > 8_000_000:
            raise ValueError("frame input exceeds size limit")
        inputs, fingerprint = json.loads(encoded), digest(encoded)
        publisher = self.publisher
        with publisher._connect() as conn:
            publisher.lock(conn)
            prior = conn.execute(
                "SELECT input_digest,config,emitted_events FROM v2_s3_frames WHERE environment=%s AND frame_id=%s",
                (publisher.environment, frame_id),
            ).fetchone()
            if prior is not None:
                if prior[0] != fingerprint:
                    raise SignalConflict("S3 frame input conflict")
                config, events = prior[1:]
            else:
                now = milliseconds(publisher.clock_ms())
                if (
                    not observed_at <= now < observed_at + publisher.lifetime_ms
                    or now - observed_at > publisher.max_age_ms
                ):
                    raise ValueError("S3 frame stale or future")
                state = BusinessState(lambda: nullcontext(conn))
                snapshot = state.read(self.key)
                payload = (
                    json.loads(snapshot.payload_json)
                    if snapshot
                    else {"last_observed_at": -1, "events": {}, "breakouts": {}}
                )
                if observed_at <= payload["last_observed_at"]:
                    raise ValueError("S3 observation must advance monotonically")
                config = deepcopy(self.config)
                candidates, breakouts = self.evaluate(
                    deepcopy(inputs["contexts"]),
                    deepcopy(inputs["raw_windows"]),
                    deepcopy(payload["breakouts"]),
                    observed_at,
                )
                if not isinstance(breakouts, dict) or not breakouts.keys() <= (
                    payload["breakouts"].keys() | inputs["contexts"].keys()
                ):
                    raise ValueError("breakout state outside frame scope")
                for target in (
                    payload["breakouts"].keys()
                    - inputs["contexts"].keys()
                    - set(removed)
                ):
                    if breakouts.get(target) != payload["breakouts"][target]:
                        raise ValueError("uncovered breakout state cannot change")
                for target in removed:
                    breakouts.pop(target, None)
                active, events = advance_events(
                    payload["events"],
                    candidates,
                    environment=publisher.environment,
                    frame_id=frame_id,
                    observed_at=observed_at,
                    covered=set(inputs["contexts"]),
                    removed=set(removed),
                    **{
                        k: config[k]
                        for k in ("cooldown_ms", "absence_ms", "strength_delta")
                    },
                )
                updated = {
                    "last_observed_at": observed_at,
                    "events": active,
                    "breakouts": breakouts,
                }
                if len(canonical(updated).encode()) > 8_000_000:
                    raise ValueError("S3 state exceeds size limit")
                written = state.change(
                    self.key,
                    expected_version=snapshot.version if snapshot else 0,
                    request_key=frame_id,
                    payload=updated,
                    reason=config["detector_version"],
                )
                if written.code != "APPLIED":
                    raise RuntimeError("S3 state serialization conflict")
                conn.execute(
                    """INSERT INTO v2_s3_frames(environment,frame_id,input_digest,config,emitted_events,state_id,state_version)
                             VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)""",
                    (
                        publisher.environment,
                        frame_id,
                        fingerprint,
                        canonical(config),
                        json.dumps(events, allow_nan=False),
                        self.key.identity,
                        written.version,
                    ),
                )
            frozen, batch_digest = publisher.prepare(
                frame_id=frame_id,
                observed_at=observed_at,
                contexts=inputs["contexts"],
                events=events,
                lifetime_ms=config["lifetime_ms"],
            )
            signal_ids = publisher.record(conn, frame_id, frozen, batch_digest)
        return {**publisher.project(frozen, signal_ids), "emitted_events": events}
