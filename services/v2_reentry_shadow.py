"""PG-only shadow opportunity consumption. Not connected to order admission.

Account/symbol/direction/previous trade/confirming bar identify an opportunity.
Source, signal revision, parameters and strategy names cannot create a second
claim for that same structure. A shadow claim never changes execution policy.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.reentry import VERSION
from v2_core.state import BusinessState, StateKey


class ShadowOpportunityBook:
    def __init__(self, connect, scope):
        if not isinstance(scope, AccountScope):
            raise TypeError("REENTRY_ACCOUNT_SCOPE_REQUIRED")
        self.connect, self.scope = connect, scope

    def claim(self, *, target, decision_id, assessment):
        symbol(target)
        decision = str(UUID(decision_id))
        if (
            assessment.get("execution_authorized") is not False
            or assessment.get("version") != VERSION
        ):
            raise ValueError("REENTRY_SHADOW_EVIDENCE_REQUIRED")
        if assessment.get("status") != "READY":
            return {"status": "NOT_READY", "execution_authorized": False}
        side = assessment.get("side")
        trigger = assessment.get("trigger_closed_ms")
        previous = str(UUID(assessment["previous_episode"]))
        if side not in {"BUY", "SELL"} or type(trigger) is not int or trigger <= 0:
            raise ValueError("REENTRY_SHADOW_IDENTITY")
        identity = {
            "symbol": target,
            "side": side,
            "previous_episode": previous,
            "trigger_closed_ms": trigger,
        }
        key = StateKey(
            **asdict(self.scope),
            namespace="reentry-shadow-claims-v1",
            key=digest(canonical(identity)),
        )
        payload = {
            "identity": identity,
            "decision_id": decision,
            "assessment": assessment,
            "execution_authorized": False,
        }
        with self.connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("reentry-shadow:" + key.identity,),
            )
            store = BusinessState(lambda: nullcontext(conn))
            current = store.read(key)
            if current is not None:
                import json

                old = json.loads(current.payload_json)
                if current.deleted or old.get("identity") != identity:
                    raise ValueError("REENTRY_SHADOW_STATE_INVALID")
                if old["decision_id"] == decision:
                    if old != payload:
                        raise ValueError("REENTRY_SHADOW_RETRY_CONFLICT")
                    status = "ALREADY_RECORDED"
                else:
                    status = "DUPLICATE_OPPORTUNITY"
                return {"status": status, "execution_authorized": False}
            written = store.change(
                key,
                expected_version=0,
                request_key="claim:" + decision,
                payload=payload,
                reason="REENTRY_SHADOW_CLAIM",
            )
            if written.code != "APPLIED":
                raise ValueError("REENTRY_SHADOW_CONCURRENT_STATE")
            return {"status": "RECORDED", "execution_authorized": False}
