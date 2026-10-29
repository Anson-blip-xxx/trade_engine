"""PostgreSQL versioned strategy state; no blind snapshot overwrite or deletion.

Use domain ledger tables for orders, exposure and accounting. A state CAS alone
does not reserve account risk atomically with order submission.
"""

import json
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4, uuid5

from v2_core.evidence import canonical

_NAMESPACE = UUID("6271d707-8bca-41a6-82cd-f48fa7efc4b6")
_HISTORY_ONLY_NAMESPACE = "account-coverage-evidence-v1"


def history_reference(state_id, version):
    return {"_v2_state_history_ref": str(UUID(state_id)), "version": version}


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
            if row is not None and "_v2_state_history_ref" in row[1]:
                if (
                    key.namespace != _HISTORY_ONLY_NAMESPACE
                    or row[0] != 1
                    or row[2]
                    or row[1] != history_reference(key.identity, row[0])
                    or row[3] != asdict(key)
                ):
                    raise ValueError("invalid state history reference")
                original = conn.execute(
                    "SELECT payload,deleted FROM v2_state_history WHERE state_id=%s AND version=%s",
                    (key.identity, row[0]),
                ).fetchone()
                if (
                    original is None
                    or original[1]
                    or "_v2_state_history_ref" in original[0]
                ):
                    raise ValueError("missing state history evidence")
                row = (row[0], original[0], row[2], row[3])
            if row is not None and "_v2_coverage_facts" in row[1]:
                if key.namespace != _HISTORY_ONLY_NAMESPACE or row[3] != asdict(key):
                    raise ValueError("invalid coverage storage scope")
                from v2_core.coverage_evidence import unpack_coverage

                row = (row[0], unpack_coverage(conn, key, row[1]), row[2], row[3])
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
            # Immutable coverage evidence already has a durable history row.
            # Keep identity/version and FK, without a second full TOAST copy.
            current_payload = (
                canonical(history_reference(key.identity, version))
                if key.namespace == _HISTORY_ONLY_NAMESPACE
                and version == 1
                and not deleted
                else encoded
            )
            conn.execute(
                "UPDATE v2_business_state SET version=%s,payload=%s::jsonb,deleted=%s WHERE state_id=%s",
                (version, current_payload, deleted, key.identity),
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
