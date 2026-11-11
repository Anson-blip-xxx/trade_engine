"""Durable causal-trend bar consumption, not risk reservation or trade permission.

The first immutable OPEN decision consumes an account/symbol/side/profile/bar.
Its claim commits in the SAME transaction as that decision. Later intent failure
does not release it; transaction rollback does. No cooldown, source, producer,
signal revision or changing 1m input digest can create another same-bar claim.
Replay, shadow evaluation and rejected/pending candidates never write claims.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.runtime_policy import resolve
from v2_core.state import BusinessState, StateKey
from v2_core.trend_structure import validate_trend_structure

PROFILE = "causal-trend-entry-v1"
NAMESPACE = "trend-opportunity-v1"
SEMANTICS = "FIRST_IMMUTABLE_OPEN_DECISION_NOT_TRADE"


def opportunity_identity(scope, config, snapshot):
    """Derive only from frozen server evaluation/context, never TV metadata.

    Admission already binds and evaluates the market envelope. Repeat its
    identity/scope checks here so malformed live profile proof cannot silently
    fall back to the older cross-source rolling window.
    """
    if (
        snapshot["result"]["action"] != "OPEN"
        or config.get("mode") != "TESTNET_ADMISSION_ONLY"
        or config.get("source") == "quant"
        or not resolve(config.get("policy"))["entry.trend_structure_enabled"]
    ):
        return None
    account = {
        name: getattr(scope, name)
        for name in ("exchange", "account_id", "environment", "product")
    }
    features = snapshot["features"]
    context, evaluation = features["context"], features["evaluation"]
    proof = evaluation.get("source_scoring", {})
    side = {"S6": "BUY", "S8": "SELL"}.get(config.get("strategy"))
    if (
        account["environment"] != "SANDBOX"
        or config.get("account_scope") != account
        or context.get("account_scope") != account
        or context.get("environment") != account["environment"]
        or context.get("symbol") != snapshot["symbol"]
        or snapshot["source"] not in {"s3", "tv_bridge"}
        or config.get("source", "s3") != snapshot["source"]
        or side is None
        or snapshot["result"]["side"] != side
        or evaluation.get("admission_authorized") is not True
        or evaluation.get("execution_authorized") is not False
        or proof.get("profile") != PROFILE
        or proof.get("version") != PROFILE
        or proof.get("source") != snapshot["source"]
        or proof.get("execution_authorized") is not False
        or proof.get("setup") not in {"BREAKOUT", "PULLBACK_RESUME"}
    ):
        raise ValueError("TREND_OPPORTUNITY_SCOPE_OR_PROOF")
    target = symbol(snapshot["symbol"])
    market = context["directional"]["market"]
    source = context.get("sources", {}).get("s3", {})
    evidence = market.get("input_evidence", {})
    closed = evidence.get("closed_at")
    if (
        evidence.get("window_version") != "s3-closed-1m-v6"
        or source.get("source") != "s3"
        or source.get("environment") != account["environment"]
        or source.get("symbol") != target
        or source.get("observed_at") != closed
        or source.get("features") != market
        or proof.get("structure") != market.get("trend_structure")
    ):
        raise ValueError("TREND_OPPORTUNITY_UNBOUND_STRUCTURE")
    structure = validate_trend_structure(
        proof.get("structure"),
        symbol=target,
        environment=account["environment"],
        closed_at_ms=closed,
    )
    if structure["status"] != "READY":
        raise ValueError("TREND_OPPORTUNITY_NOT_READY")
    identity = {
        "profile_version": proof["profile"],
        "symbol": target,
        "side": side,
        "last_closed_at_ms": structure["last_closed_at_ms"],
    }
    key = StateKey(**account, namespace=NAMESPACE, key=digest(canonical(identity)))
    return key, identity


def claim_trend_opportunity(conn, *, scope, config, snapshot, decision_id):
    """Call only inside the transaction that persists the immutable decision.

    The scoped state primary key plus transaction lock serializes cross-source
    workers. Claims are never deleted or released after downstream rejection.
    A replay/non-profile decision returns None before any SQL is executed.
    """
    selected = opportunity_identity(scope, config, snapshot)
    if selected is None:
        return None
    key, identity = selected
    decision_id = str(UUID(decision_id))
    payload = {
        "identity": identity,
        "decision_id": decision_id,
        "signal_id": str(UUID(snapshot["signal_id"])),
        "source": snapshot["source"],
        "semantics": SEMANTICS,
        "execution_authorized": False,
    }
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (NAMESPACE + ":" + key.identity,),
    )
    store = BusinessState(lambda: nullcontext(conn))
    current = store.read(key)
    if current is None:
        written = store.change(
            key,
            expected_version=0,
            request_key="claim:" + decision_id,
            payload=payload,
            reason="TREND_FIRST_OPEN_DECISION_CLAIM",
        )
        if written.code != "APPLIED":
            raise ValueError("TREND_OPPORTUNITY_CONCURRENT_STATE")
        status, winner = "CLAIMED", payload
    else:
        winner = json.loads(current.payload_json)
        if (
            current.deleted
            or current.version != 1
            or set(winner) != set(payload)
            or winner.get("identity") != identity
            or winner.get("semantics") != SEMANTICS
            or winner.get("execution_authorized") is not False
        ):
            raise ValueError("TREND_OPPORTUNITY_STATE_INVALID")
        if winner["decision_id"] == decision_id:
            if winner != payload:
                raise ValueError("TREND_OPPORTUNITY_RETRY_CONFLICT")
            status = "ALREADY_CLAIMED"
        else:
            status = "DUPLICATE_OPPORTUNITY"
    return {
        "status": status,
        "scope": asdict(key),
        "identity": identity,
        "winning_decision": winner["decision_id"],
        "winning_signal": winner["signal_id"],
        "semantics": SEMANTICS,
        "execution_authorized": False,
    }
