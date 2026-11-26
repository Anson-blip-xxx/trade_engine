"""Audited terminal outcomes for duplicate typed signal opportunities.

The strict plan adapter remains unchanged. Only a verified, permanently claimed
opportunity may turn a different signal/consumer into IGNORED. This adapter owns
its transaction, not a scheduler lease, and grants no order execution authority.
It is not wired into any running service.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid5

from v2_core.account_risk import AccountRiskDenied
from v2_core.account_risk import _lock as lock_account
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.signals import Signals
from v2_core.strategy_deployment import deployment_snapshot
from v2_core.strategy_plan_admission import (
    _EVIDENCE_BYTES,
    StrategyPlanAdmission,
    StrategyPlanAdmissionError,
    opportunity_intent_id,
    require_plan_admission,
)
from v2_core.strategy_plan_store import read_trade_plan

RESOLUTION_SCHEMA = "strategy-signal-resolution-v1"
DUPLICATE_REASON = "STRATEGY_OPPORTUNITY_ALREADY_CLAIMED"
_DECISION_NAMESPACE = UUID("d1c8d303-c42d-4635-b01b-905e3e5aa171")


def _conflict():
    raise StrategyPlanAdmissionError("STRATEGY_SIGNAL_OUTCOME_CONFLICT")


def _outcome(conn, consumer, signal_id):
    decision = conn.execute(
        """SELECT d.decision_id::text,d.evidence_ref,d.action,d.side,
        d.quantity::text,e.strategy_version,e.config_digest,e.config,e.snapshot
        FROM v2_strategy_decisions d JOIN v2_decision_evidence e USING(evidence_ref)
        WHERE d.consumer=%s AND d.signal_id=%s""",
        (consumer, signal_id),
    ).fetchone()
    receipt = conn.execute(
        "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
        (consumer, signal_id),
    ).fetchone()
    return decision, receipt


def _signal_intent(conn, plan, signal_id):
    return conn.execute(
        """SELECT intent_id::text FROM v2_trade_intents WHERE
        exchange=%s AND account_id=%s AND environment=%s AND product=%s
        AND producer=%s AND signal_id=%s""",
        (*asdict(plan.scope).values(), plan.instance_id, signal_id),
    ).fetchone()


def _lock_signal(conn, signal_id):
    conn.execute("SET LOCAL lock_timeout='3s'")
    conn.execute("SET LOCAL statement_timeout='5s'")
    row = conn.execute(
        "SELECT source,snapshot FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE",
        (signal_id,),
    ).fetchone()
    if row is None:
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_SIGNAL_MISMATCH")
    return row


def _ignored_proof(plan, binding, proposal, winner, signal_id, signal, decided_at):
    proposed = json.loads(proposal.snapshot_json)
    snapshot = {
        "signal_id": signal_id,
        "observed_at": signal[1]["observed_at"],
        "decided_at": decided_at,
        "expires_at_ms": signal[1]["expires_at_ms"],
        "source": signal[0],
        "symbol": plan.symbol,
        "rationale": DUPLICATE_REASON,
        "features": {
            "strategy_signal_resolution": {
                "schema": RESOLUTION_SCHEMA,
                "scope": asdict(plan.scope),
                "consumer": binding.consumer,
                "proposal_evidence_ref": proposal.evidence_ref,
                "candidate_plan": plan.payload(),
                "candidate_deployment": deployment_snapshot(binding),
                "proposal_rationale": proposed["rationale"],
                "proposal_features": proposed["features"],
                "winner": winner,
            }
        },
        # Deliberately no top-level strategy_plan or OPEN result: this record
        # cannot be mistaken for the winner's opening decision.
        "result": {"action": "IGNORED", "side": None, "quantity": None},
        "execution_authorized": False,
    }
    proof = DecisionEvidence(
        plan.code_version, binding.config_json, canonical(snapshot)
    )
    if len((proof.config_json + proof.snapshot_json).encode()) > _EVIDENCE_BYTES:
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_EVIDENCE_BOUND")
    return proof


class StrategySignalAdmission:
    def __init__(self, connect, registry):
        self.strict = StrategyPlanAdmission(connect, registry)
        self.connect, self.registry = connect, registry

    def admit(self, plan, binding, *, signal_id, rationale, features=None):
        plan, binding, features, signal_id = self.strict._inputs(
            plan, binding, rationale, features, signal_id
        )
        if signal_id is None:
            raise StrategyPlanAdmissionError("STRATEGY_SIGNAL_REQUIRED")
        try:
            with self.connect() as conn:
                signal = _lock_signal(conn, signal_id)
                proposal = self.strict._proof(
                    conn, plan, binding, rationale, features, signal_id
                )
                decision, receipt = _outcome(conn, binding.consumer, signal_id)
                if decision is not None:
                    # A foreign/incomplete record is a conflict, never an excuse
                    # to overwrite it or let strict admission bypass it.
                    if decision[2:5] != ("IGNORED", None, None) or receipt != (
                        "IGNORED",
                        None,
                        DUPLICATE_REASON,
                    ):
                        _conflict()
                    result = self._duplicate(
                        conn, plan, binding, proposal, signal_id, signal
                    )
                else:
                    intent = _signal_intent(conn, plan, signal_id)
                    if (receipt is not None or intent is not None) and (
                        receipt
                        != ("INTENT", opportunity_intent_id(plan), "signal admitted")
                        or intent != (opportunity_intent_id(plan),)
                    ):
                        _conflict()
                    result = StrategyPlanAdmission(
                        lambda: nullcontext(conn), self.registry
                    ).admit(
                        plan,
                        binding,
                        signal_id=signal_id,
                        rationale=rationale,
                        features=features,
                    )
                    result = {**result, "signal_id": signal_id}
            return result
        except StrategyPlanAdmissionError as exc:
            if str(exc) not in {DUPLICATE_REASON, "STRATEGY_ADMISSION_INTENT_CONFLICT"}:
                raise
        # The first transaction must have rolled back before checking a durable
        # winner. A uniqueness race alone is not proof of a committed claim.
        with self.connect() as conn:
            signal = _lock_signal(conn, signal_id)
            proposal = self.strict._proof(
                conn, plan, binding, rationale, features, signal_id
            )
            result = self._duplicate(conn, plan, binding, proposal, signal_id, signal)
        return result

    @staticmethod
    def _duplicate(conn, plan, binding, proposal, signal_id, signal):
        identity = opportunity_intent_id(plan)
        if (
            conn.execute(
                "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (identity,),
            ).fetchone()
            is None
        ):
            raise StrategyPlanAdmissionError("STRATEGY_OPPORTUNITY_WINNER_UNPROVEN")
        lock_account(conn, plan.scope.key)
        # No winner-signal lock here: the lock order is loser signal -> intent
        # root -> account. Winner evidence/receipts are immutable.
        row = conn.execute(
            """SELECT e.evidence_ref,e.strategy_version,e.config_digest,e.config,e.snapshot
            FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE i.intent_id=%s""",
            (identity,),
        ).fetchone()
        try:
            proof = DecisionEvidence(row[1], canonical(row[3]), canonical(row[4]))
            if proof.evidence_ref != row[0] or proof.config_digest != row[2]:
                raise ValueError("winner evidence digest mismatch")
            winner_plan = read_trade_plan(conn, identity)
            winner = require_plan_admission(
                conn, identity, deployment=row[4]["strategy_deployment"]
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise AccountRiskDenied("STRATEGY_OPPORTUNITY_RECEIPT_INVALID") from None
        if (
            winner_plan.scope != plan.scope
            or winner_plan.opportunity_id != plan.opportunity_id
            or winner_plan.symbol != plan.symbol
            or winner_plan.side != plan.side
        ):
            raise StrategyPlanAdmissionError("STRATEGY_OPPORTUNITY_IDENTITY_CONFLICT")
        if _signal_intent(conn, plan, signal_id) is not None:
            _conflict()
        decision, receipt = _outcome(conn, binding.consumer, signal_id)
        decision_id = str(
            uuid5(_DECISION_NAMESPACE, binding.consumer + ":" + signal_id)
        )
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        if decision is None and receipt is None:
            proof = _ignored_proof(
                plan, binding, proposal, winner, signal_id, signal, now
            )
            bound = lambda: nullcontext(conn)
            EvidenceStore(bound).put(proof)
            conn.execute(
                """INSERT INTO v2_strategy_decisions
                (decision_id,consumer,signal_id,evidence_ref,action,side,quantity)
                VALUES(%s,%s,%s,%s,'IGNORED',NULL,NULL)""",
                (decision_id, binding.consumer, signal_id, proof.evidence_ref),
            )
            Signals(bound).complete(
                consumer=binding.consumer,
                signal_id=signal_id,
                outcome="IGNORED",
                reason=DUPLICATE_REASON,
            )
        else:
            if decision is None or receipt != ("IGNORED", None, DUPLICATE_REASON):
                _conflict()
            try:
                decided_at = decision[8]["decided_at"]
                if type(decided_at) is not int or decided_at > now:
                    _conflict()
                proof = _ignored_proof(
                    plan, binding, proposal, winner, signal_id, signal, decided_at
                )
                expected = (
                    decision_id,
                    proof.evidence_ref,
                    "IGNORED",
                    None,
                    None,
                    proof.strategy_version,
                    proof.config_digest,
                    json.loads(proof.config_json),
                    json.loads(proof.snapshot_json),
                )
                if decision != expected:
                    _conflict()
            except (ValueError, TypeError, KeyError, RecursionError):
                _conflict()
        return {
            "status": "IGNORED",
            "signal_id": signal_id,
            "consumer": binding.consumer,
            "decision_id": decision_id,
            "reason": DUPLICATE_REASON,
            "winner_intent_id": identity,
            "opportunity_id": plan.opportunity_id,
            "plan_digest": plan.plan_digest,
            "evidence_ref": proof.evidence_ref,
            "execution_authorized": False,
        }
