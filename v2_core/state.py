"""PostgreSQL versioned strategy state; no blind snapshot overwrite or deletion.

Use domain ledger tables for orders, exposure and accounting. A state CAS alone
does not reserve account risk atomically with order submission.
"""

import json
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4, uuid5

from v2_core.evidence import canonical

_NAMESPACE = UUID("6271d707-8bca-41a6-82cd-f48fa7efc4b6")


def normalized(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value != value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("normalized nonempty text required")
    return value


@dataclass(frozen=True)
class StateKey:
    exchange: str
    account_id: str
    environment: str
    product: str
    namespace: str
    key: str

    def __post_init__(self):
        for value in asdict(self).values():
            normalized(value)

    @property
    def identity(self):
        return str(uuid5(_NAMESPACE, canonical(asdict(self))))


@dataclass(frozen=True)
class StateSnapshot:
    version: int
    payload_json: str
    deleted: bool


@dataclass(frozen=True)
class StateWrite:
    code: str
    version: int | None


class BusinessState:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def read(self, key):
        if not isinstance(key, StateKey):
            raise TypeError("scoped state key required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s",
                (key.identity,),
            ).fetchone()
        if row is None:
            return None
        if row[3] != asdict(key):
            raise ValueError("state identity conflict")
        return StateSnapshot(row[0], canonical(row[1]), row[2])

    def change(
        self, key, *, expected_version, request_key, payload, reason, deleted=False
    ):
        if not isinstance(key, StateKey):
            raise TypeError("scoped state key required")
        if (
            type(expected_version) is not int
            or expected_version < 0
            or type(deleted) is not bool
        ):
            raise ValueError("explicit version and deletion flag required")
        normalized(request_key)
        normalized(reason)
        encoded = canonical(payload)
        if deleted and payload:
            raise ValueError("tombstone payload must be empty")
        with self._connect() as conn:
            if expected_version == 0:
                conn.execute(
                    """INSERT INTO v2_business_state(state_id,scope,version,payload,deleted)
                VALUES (%s,%s::jsonb,0,'{}',TRUE) ON CONFLICT DO NOTHING""",
                    (key.identity, canonical(asdict(key))),
                )
            current = conn.execute(
                "SELECT version,scope FROM v2_business_state WHERE state_id=%s FOR UPDATE",
                (key.identity,),
            ).fetchone()
            if current is None:
                return StateWrite("STALE", None)
            if current[1] != asdict(key):
                raise ValueError("state identity conflict")
            previous = conn.execute(
                """SELECT expected_version,payload,deleted,reason,version
                FROM v2_state_history WHERE state_id=%s AND request_key=%s""",
                (key.identity, request_key),
            ).fetchone()
            if previous is not None:
                if previous[:4] != (
                    expected_version,
                    json.loads(encoded),
                    deleted,
                    reason,
                ):
                    raise ValueError("state request content conflict")
                return StateWrite("ALREADY_APPLIED", previous[4])
            if current[0] != expected_version:
                return StateWrite("STALE", current[0])
            version = current[0] + 1
            conn.execute(
                "UPDATE v2_business_state SET version=%s,payload=%s::jsonb,deleted=%s WHERE state_id=%s",
                (version, encoded, deleted, key.identity),
            )
            conn.execute(
                """INSERT INTO v2_state_history(event_id,state_id,request_key,
                expected_version,version,payload,deleted,reason)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                (
                    str(uuid4()),
                    key.identity,
                    request_key,
                    expected_version,
                    version,
                    encoded,
                    deleted,
                    reason,
                ),
            )
        return StateWrite("APPLIED", version)
