"""Read-only access to the versioned SANDBOX public fusion policy."""

import json

from v2_core.signal_fusion import FusionPolicy
from v2_core.state import BusinessState, StateKey


def policy_key():
    return StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "signal-fusion-policy-v1",
        "current",
    )


def read_policy(connect):
    row = BusinessState(connect).read(policy_key())
    if row is None:
        return FusionPolicy(), 0
    if row.deleted:
        raise ValueError("SIGNAL_FUSION_POLICY_DELETED")
    return FusionPolicy(**json.loads(row.payload_json)), row.version
