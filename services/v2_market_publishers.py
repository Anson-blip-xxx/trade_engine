"""V2 adapters for computed S0 states / S3 feature frames, no legacy imports.

Callers must supply original observation time and durable frame/event identities.
These adapters do not turn scan wall time or array positions into identities.
"""

import math

from v2_core.evidence import canonical


def feature_values(value):
    """Preserve computed float observations as decimal strings, not money math."""
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite market feature")
        return str(value)
    if type(value) in (str, int, bool) or value is None:
        return value
    if isinstance(value, list):
        return [feature_values(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: feature_values(item) for key, item in value.items()}
    raise ValueError("unsupported market feature")


class S0Publisher:
    def __init__(self, publisher):
        if publisher.source != "s0":
            raise ValueError("S0 publisher binding required")
        self.publisher = publisher

    def publish_state(self, state, *, frame_id, observed_at):
        if not isinstance(state, dict) or not state:
            raise ValueError("nonempty S0 state required")
        features = feature_values(state)
        canonical(features)
        return self.publisher.publish(
            frame_id=frame_id,
            observed_at=observed_at,
            contexts={"*": features},
            events=[],
        )


class S3Publisher:
    def __init__(self, publisher):
        if publisher.source != "s3":
            raise ValueError("S3 publisher binding required")
        self.publisher = publisher

    def publish_frame(self, *, frame_id, observed_at, windows, events):
        if not isinstance(windows, dict) or not windows or not isinstance(events, list):
            raise ValueError("computed windows and managed events required")
        prepared = []
        for event in events:
            if (
                not isinstance(event, dict)
                or not {"event_id", "symbol", "type", "state"} <= event.keys()
            ):
                raise ValueError("managed S3 events require durable identities")
            if event["state"] not in {"ACTIVE", "UPDATE", "END"}:
                raise ValueError("explicit event lifecycle required")
            if not isinstance(event["type"], str) or not event["type"]:
                raise ValueError("event type required")
            features = feature_values(
                {
                    key: value
                    for key, value in event.items()
                    if key not in {"event_id", "symbol"}
                }
            )
            prepared.append(
                {
                    "event_id": event["event_id"],
                    "symbol": event["symbol"],
                    "signal": "EVENT_END" if event["state"] == "END" else event["type"],
                    "features": features,
                }
            )
        return self.publisher.publish(
            frame_id=frame_id,
            observed_at=observed_at,
            contexts=feature_values(windows),
            events=prepared,
        )
