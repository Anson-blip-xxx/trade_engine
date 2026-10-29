"""Terminal signal rejection using the existing persistent OPEN-only quarantine.

This adapter does not create, delete or release quarantine. An existing row,
including a tombstone, means denied, matching opening_halt.require_opening_allowed.
Place it outside expensive market/account/model context providers. The immutable
proof is a denial only, not an execution permit; prepare/dispatch must retain
their authoritative PostgreSQL checks. The digest detects inconsistent evidence,
not an independent authentication mechanism for untrusted external input.
"""

import re
from copy import deepcopy
from dataclasses import asdict

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyDecision

NAMESPACE = "symbol-opening-quarantine-v1"
CONTEXT_FIELD = "opening_quarantine"
PROOF_FIELDS = frozenset(
    {
        "account_scope",
        "symbol",
        "state_id",
        "version",
        "deleted",
        "payload_digest",
    }
)


def quarantine_key(scope, target):
    if not isinstance(scope, AccountScope):
        raise TypeError("TYPED_QUARANTINE_SCOPE_REQUIRED")
    return StateKey(**asdict(scope), namespace=NAMESPACE, key=symbol(target))


def _signal_key(signal, scope):
    if not isinstance(scope, AccountScope):
        raise TypeError("TYPED_QUARANTINE_SCOPE_REQUIRED")
    # StrategyWorker already verifies the inbound row's source/environment. Its
    # immutable snapshot need not duplicate that outer environment column.
    if not isinstance(signal, dict) or (
        "environment" in signal and signal["environment"] != scope.environment
    ):
        raise ValueError("QUARANTINE_SIGNAL_SCOPE_MISMATCH")
    return quarantine_key(scope, signal.get("symbol"))


class OpeningQuarantineContext:
    def __init__(self, connect, delegate, *, scope):
        if not isinstance(scope, AccountScope):
            raise TypeError("TYPED_QUARANTINE_SCOPE_REQUIRED")
        if not callable(connect) or not callable(delegate):
            raise TypeError("QUARANTINE_CONTEXT_PORTS_REQUIRED")
        self.store, self.delegate, self.scope = BusinessState(connect), delegate, scope

    def __call__(self, signal):
        key = _signal_key(signal, self.scope)
        row = self.store.read(key)
        if row is None:
            return self.delegate(signal)
        if (
            type(row.version) is not int
            or row.version < 1
            or type(row.deleted) is not bool
        ):
            raise ValueError("QUARANTINE_STATE_INVALID")
        proof = {
            "account_scope": asdict(self.scope),
            "symbol": key.key,
            "state_id": key.identity,
            "version": row.version,
            "deleted": row.deleted,
            "payload_digest": digest(row.payload_json),
        }
        return {CONTEXT_FIELD: {**proof, "digest": digest(canonical(proof))}}


def quarantine_rejection(signal, context, *, scope):
    """Pure typed, scope-bound terminal decision; never fabricates market data."""
    key = _signal_key(signal, scope)
    if not isinstance(context, dict):
        raise TypeError("QUARANTINE_CONTEXT_REQUIRED")
    if CONTEXT_FIELD not in context:
        return None
    proof = context[CONTEXT_FIELD]
    if (
        not isinstance(proof, dict)
        or set(proof) != PROOF_FIELDS | {"digest"}
        or proof["account_scope"] != asdict(scope)
        or proof["symbol"] != key.key
        or proof["state_id"] != key.identity
        or type(proof["version"]) is not int
        or proof["version"] < 1
        or type(proof["deleted"]) is not bool
        or any(
            not isinstance(proof[field], str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof[field])
            for field in ("payload_digest", "digest")
        )
    ):
        raise ValueError("QUARANTINE_PROOF_INVALID")
    body = {field: proof[field] for field in PROOF_FIELDS}
    if digest(canonical(body)) != proof["digest"]:
        raise ValueError("QUARANTINE_PROOF_INVALID")
    return StrategyDecision(
        "IGNORED",
        "SYMBOL_OPENING_QUARANTINED",
        features_json=canonical(
            {
                "stage": "SYMBOL_OPENING_QUARANTINE",
                "quarantine": deepcopy(proof),
                "execution_authorized": False,
                "admission_authorized": False,
            }
        ),
    )
