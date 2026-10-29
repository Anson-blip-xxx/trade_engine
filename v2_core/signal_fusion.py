"""Bounded, replayable cross-source confirmation; no signal is neutral.

This is a score input, never an execution permit. Public evidence alone cannot
change account limits, position sizing, venue eligibility or protection rules.
"""

from dataclasses import asdict, dataclass

from v2_core.directional import supports_event
from v2_core.evidence import canonical, digest


@dataclass(frozen=True)
class FusionPolicy:
    enabled: bool = False
    confirmation_bonus: int = 5
    conflict_veto: bool = True
    window_ms: int = 60000

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or type(self.conflict_veto) is not bool
            or type(self.confirmation_bonus) is not int
            or not 0 <= self.confirmation_bonus <= 5
            or type(self.window_ms) is not int
            or not 1000 <= self.window_ms <= 300000
        ):
            raise ValueError("SIGNAL_FUSION_POLICY_INVALID")


def _direction(event):
    if supports_event("S6", event):
        return "LONG"
    if supports_event("S8", event):
        return "SHORT"
    return None


def _own(signal):
    value = {
        key: signal.get(key)
        for key in ("symbol", "signal", "observed_at", "expires_at_ms")
    }
    if (
        not isinstance(value["symbol"], str)
        or not value["symbol"]
        or len(value["symbol"]) > 80
        or _direction(value["signal"]) is None
        or type(value["observed_at"]) is not int
        or type(value["expires_at_ms"]) is not int
        or not 0 <= value["observed_at"] < value["expires_at_ms"] <= 2**53 - 1
    ):
        raise ValueError("SIGNAL_FUSION_SIGNAL_INVALID")
    return value


def build_fusion(signal, *, source, peers, policy, policy_version, now_ms):
    """Select the freshest opposite-source observation, never count duplicates.

    Invalid, future or expired peer observations are unknown, not disagreement.
    Selection is by observation time and stable ID, not by desired direction.
    A later contradictory observation supersedes an earlier confirmation.
    """
    own = _own(signal)
    if (
        source not in {"s3", "tv_bridge", "quant"}
        or not isinstance(policy, FusionPolicy)
        or type(policy_version) is not int
        or policy_version < 0
        or type(now_ms) is not int
        or not own["observed_at"] <= now_ms <= 2**53 - 1
        or not isinstance(peers, list)
        or len(peers) > 50
    ):
        raise ValueError("SIGNAL_FUSION_INPUT_INVALID")
    values = asdict(policy)
    fingerprint = digest(
        canonical(
            {"environment": "SANDBOX", "version": policy_version, "values": values}
        )
    )
    other = "tv_bridge" if source == "s3" else "s3" if source == "tv_bridge" else None
    selected = []
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        observed, expires = peer.get("observed_at"), peer.get("expires_at_ms")
        identifier = peer.get("signal_id")
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 100
            or peer.get("source") != other
            or peer.get("environment") != "SANDBOX"
            or peer.get("symbol") != own["symbol"]
            or _direction(peer.get("signal")) is None
            or type(observed) is not int
            or type(expires) is not int
            or not 0 <= observed <= now_ms < expires <= 2**53 - 1
            or not now_ms < observed + policy.window_ms
        ):
            continue
        selected.append(
            {
                key: peer[key]
                for key in (
                    "signal_id",
                    "source",
                    "environment",
                    "symbol",
                    "signal",
                    "observed_at",
                    "expires_at_ms",
                )
            }
        )
    direction = _direction(own["signal"])
    # Simultaneous contradictory events from one source are not confirmation.
    # Prefer the conflict at the same observation timestamp, then stable ID.
    selected.sort(
        key=lambda peer: (
            peer["observed_at"],
            _direction(peer["signal"]) != direction,
            peer["signal_id"],
        ),
        reverse=True,
    )
    selected = selected[:1]
    for peer in selected:
        peer["relationship"] = (
            "CONFIRMS" if _direction(peer["signal"]) == direction else "CONFLICTS"
        )
    live = now_ms < own["expires_at_ms"]
    status = (
        "OFF"
        if not policy.enabled or other is None
        else "ABSENT"
        if not live or not selected
        else selected[0]["relationship"]
    )
    used = status in {"CONFIRMS", "CONFLICTS"}
    valid_until = (
        min(
            own["expires_at_ms"],
            selected[0]["expires_at_ms"],
            selected[0]["observed_at"] + policy.window_ms,
        )
        if used
        else None
    )
    return {
        "version": "directional-fusion-v1",
        "environment": "SANDBOX",
        "source": source,
        "symbol": own["symbol"],
        "direction": direction,
        "own_signal": own,
        "observed_at_ms": now_ms,
        "window_ms": policy.window_ms,
        "policy": values,
        "policy_version": policy_version,
        "policy_digest": fingerprint,
        "peers": selected,
        "status": status,
        "score_bonus": policy.confirmation_bonus if status == "CONFIRMS" else 0,
        "veto": bool(status == "CONFLICTS" and policy.conflict_veto),
        "valid_until_ms": valid_until,
        "rule": "BOUNDED_SCORE_ONLY_NO_SIZE_OR_LEVERAGE_BOOST",
    }


def validate_fusion(signal, *, source, proof, now_ms):
    """Verify a frozen proof, then reevaluate its original timestamps at now.

    Old metadata-only fusion is neutral for rolling upgrades. A malformed new
    proof is an error; it cannot manufacture confirmation or weaken hard risk.
    """
    if proof is None or (
        isinstance(proof, dict) and proof.get("version") != "directional-fusion-v1"
    ):
        return None
    if not isinstance(proof, dict):
        raise TypeError("SIGNAL_FUSION_PROOF_INVALID")
    try:
        if proof["source"] != source or proof["own_signal"] != _own(signal):
            raise ValueError()
        policy = FusionPolicy(**proof["policy"])
        expected = build_fusion(
            signal,
            source=source,
            peers=proof["peers"],
            policy=policy,
            policy_version=proof["policy_version"],
            now_ms=proof["observed_at_ms"],
        )
        if expected != proof or now_ms < proof["observed_at_ms"]:
            raise ValueError()
        return build_fusion(
            signal,
            source=source,
            peers=proof["peers"],
            policy=policy,
            policy_version=proof["policy_version"],
            now_ms=now_ms,
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("SIGNAL_FUSION_PROOF_INVALID") from None


def validated_public_features(proof, now_ms):
    """Return only bounded public TV facts; absence is null, never bearish."""
    neutral = {"tv_confirmation": None, "tv_observed_at_ms": None}
    if not isinstance(proof, dict) or proof.get("version") != "directional-fusion-v1":
        return neutral
    checked = validate_fusion(
        proof.get("own_signal", {}),
        source=proof.get("source"),
        proof=proof,
        now_ms=now_ms,
    )
    if checked["status"] not in {"CONFIRMS", "CONFLICTS"}:
        return neutral
    tv = (
        checked["own_signal"]
        if checked["source"] == "tv_bridge"
        else checked["peers"][0]
    )
    return {
        "tv_confirmation": 1 if checked["status"] == "CONFIRMS" else -1,
        "tv_observed_at_ms": tv["observed_at"],
    }
