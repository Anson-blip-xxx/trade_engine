"""Atomic, account-scoped plan admission; never prepare or dispatch an order.

An opportunity's execution receipt is permanent and shared by all strategy
instances. A successful admission is only durable intent, not trading authority.
Callers retry an ambiguous transaction with the exact frozen input, never a new
opportunity identity. This adapter is not installed in any running service.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid5

from v2_core.account_risk import AccountRiskDenied
from v2_core.account_risk import _lock as lock_account
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.service import TradingData
from v2_core.signals import signal_consumer
from v2_core.state import BusinessState, StateKey, normalized
from v2_core.strategy_deployment import (
    deployment_snapshot,
    require_strategy_opening,
)
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.strategy_registry import DeploymentBinding, StrategyRegistry

NAMESPACE = "strategy-opportunity-execution-v1"
_INTENT_NAMESPACE = UUID("22f09582-ad9e-4f1b-b311-5984ce6813fc")
_FEATURE_BYTES = 65536
_EVIDENCE_BYTES = 262144
_REASON = "strategy plan admitted"


class StrategyPlanAdmissionError(ValueError):
    """A fixed-code rejection; the transaction must not be committed."""


def opportunity_intent_id(plan):
    """No producer, code version, generation or signal identity in this key."""
    if type(plan) is not TradePlan:
        raise TypeError("STRATEGY_TYPED_PLAN_REQUIRED")
    return str(
        uuid5(
            _INTENT_NAMESPACE,
            canonical(
                {"scope": asdict(plan.scope), "opportunity_id": plan.opportunity_id}
            ),
        )
    )


def opportunity_key(plan):
    return StateKey(
        **asdict(plan.scope),
        namespace=NAMESPACE,
        key=plan.opportunity_id,
    )


def require_plan_admission(conn, episode, *, deployment):
    """Verify a permanent claim before generic order preparation/submission.

    The caller owns the intent/account locks and has separately checked the
    deployment. This read never checks current entry time or changes a receipt;
    it also does not grant permission to send, manage or close any order.
    """
    try:
        episode = str(UUID(str(episode)))
        plan = read_trade_plan(conn, episode)
        if plan is None or opportunity_intent_id(plan) != episode:
            raise ValueError("not the account opportunity identity")
        row = conn.execute(
            """SELECT i.evidence_ref,i.producer,i.signal_id::text,i.payload,
            e.signal_id::text,e.snapshot FROM v2_trade_intents i
            JOIN v2_decision_evidence e USING(evidence_ref) WHERE i.intent_id=%s""",
            (episode,),
        ).fetchone()
        if (
            row is None
            or type(deployment) is not dict
            or row[1] != plan.instance_id
            or row[0] != row[3].get("evidence_ref")
            or row[2] != row[4]
            or row[2] != row[5].get("signal_id")
            or row[5].get("strategy_deployment") != deployment
            or deployment.get("scope") != asdict(plan.scope)
            or deployment.get("instance_id") != plan.instance_id
            or deployment.get("generation") != plan.deployment_generation
            or deployment.get("config_digest") != plan.config_digest
        ):
            raise ValueError("claim differs from frozen plan binding")
        key = opportunity_key(plan)
        expected = {
            "version": NAMESPACE,
            "scope": asdict(plan.scope),
            "opportunity_id": plan.opportunity_id,
            "intent_id": episode,
            "instance_id": plan.instance_id,
            "plan_digest": plan.plan_digest,
            "binding_digest": deployment["binding_digest"],
            "evidence_ref": row[0],
            "signal_id": row[2],
            "execution_authorized": False,
        }
        state = BusinessState(lambda: nullcontext(conn)).read(key)
        if (
            state is None
            or state.deleted
            or state.version != 1
            or json.loads(state.payload_json) != expected
        ):
            raise ValueError("permanent opportunity claim missing or changed")
        history = conn.execute(
            """SELECT expected_version,payload,deleted,reason,request_key
            FROM v2_state_history WHERE state_id=%s AND version=1""",
            (key.identity,),
        ).fetchone()
        if history != (0, expected, False, _REASON, "admit:" + episode):
            raise ValueError("claim history differs from admission")
        if row[2] is not None:
            consumer = json.dumps(
                [*asdict(plan.scope).values(), plan.instance_id], separators=(",", ":")
            )
            receipt = conn.execute(
                "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
                (consumer, row[2]),
            ).fetchone()
            if receipt != ("INTENT", episode, "signal admitted"):
                raise ValueError("signal receipt differs from admission")
        return expected
    except (ValueError, TypeError, KeyError, RecursionError):
        raise AccountRiskDenied("STRATEGY_OPPORTUNITY_RECEIPT_INVALID") from None


class StrategyPlanAdmission:
    def __init__(self, connect, registry):
        if not callable(connect) or type(registry) is not StrategyRegistry:
            raise TypeError("STRATEGY_ADMISSION_PORTS_REQUIRED")
        self.connect, self.registry = connect, registry

    def _inputs(self, plan, binding, rationale, features, signal_id):
        if type(plan) is not TradePlan or type(binding) is not DeploymentBinding:
            raise TypeError("STRATEGY_TYPED_PLAN_AND_BINDING_REQUIRED")
        # Own independently revalidated snapshots, including nested config.
        plan = TradePlan.from_payload(plan.payload())
        binding = DeploymentBinding(
            binding.scope,
            binding.instance_id,
            binding.spec,
            binding.config_json,
            binding.generation,
            binding.mode,
        )
        spec = self.registry.resolve_entry(binding)
        if binding.mode != "ACTIVE":
            raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_NOT_ACTIVE")
        if (
            plan.scope != binding.scope
            or plan.instance_id != binding.instance_id
            or plan.strategy_id != spec.strategy_id
            or plan.code_version != spec.code_version
            or plan.schema_version != spec.plan_schema
            or plan.deployment_generation != binding.generation
            or plan.config_digest != binding.config_digest
            or plan.exit_handler != spec.exit_handler
        ):
            raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_BINDING_MISMATCH")
        normalized(rationale)
        if len(rationale.encode("utf-8")) > 2048:
            raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_RATIONALE_BOUND")
        encoded = canonical({} if features is None else features)
        if len(encoded.encode("utf-8")) > _FEATURE_BYTES:
            raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_FEATURE_BOUND")
        signal = None if signal_id is None else str(UUID(signal_id))
        return plan, binding, json.loads(encoded), signal

    @staticmethod
    def _proof(conn, plan, binding, rationale, features, signal_id):
        source = "strategy-plan"
        if signal_id is not None:
            row = conn.execute(
                "SELECT source,environment,snapshot FROM v2_inbound_signals WHERE signal_id=%s",
                (signal_id,),
            ).fetchone()
            if (
                row is None
                or row[1] != plan.scope.environment
                or row[2].get("symbol") != plan.symbol
                or row[2]["observed_at"] > plan.decided_at_ms
                or row[2]["expires_at_ms"] < plan.expires_at_ms
            ):
                raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_SIGNAL_MISMATCH")
            source = row[0]
        snapshot = {
            "observed_at": plan.observed_at_ms,
            "decided_at": plan.decided_at_ms,
            "expires_at_ms": plan.expires_at_ms,
            "symbol": plan.symbol,
            "source": source,
            "rationale": rationale,
            "features": features,
            "strategy_plan": plan.payload(),
            "strategy_deployment": deployment_snapshot(binding),
            "result": {"action": "OPEN", "side": plan.side, "quantity": plan.quantity},
            "execution_authorized": False,
        }
        if signal_id is not None:
            snapshot["signal_id"] = signal_id
        proof = DecisionEvidence(
            plan.code_version, binding.config_json, canonical(snapshot)
        )
        if len((proof.config_json + proof.snapshot_json).encode()) > _EVIDENCE_BYTES:
            raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_EVIDENCE_BOUND")
        return proof

    @staticmethod
    def _receipt(plan, binding, proof, signal_id):
        return {
            "version": NAMESPACE,
            "scope": asdict(plan.scope),
            "opportunity_id": plan.opportunity_id,
            "intent_id": opportunity_intent_id(plan),
            "instance_id": plan.instance_id,
            "plan_digest": plan.plan_digest,
            "binding_digest": binding.binding_digest,
            "evidence_ref": proof.evidence_ref,
            "signal_id": signal_id,
            "execution_authorized": False,
        }

    @staticmethod
    def _replay(conn, state, expected, intent, proof, signal_id):
        if state.deleted or state.version != 1:
            raise StrategyPlanAdmissionError("STRATEGY_OPPORTUNITY_RECEIPT_INVALID")
        stored = json.loads(state.payload_json)
        if stored != expected:
            raise StrategyPlanAdmissionError(
                "STRATEGY_OPPORTUNITY_ALREADY_CLAIMED"
                if stored.get("instance_id") != expected["instance_id"]
                or stored.get("signal_id") != signal_id
                else "STRATEGY_ADMISSION_FROZEN_CONTENT_CONFLICT"
            )
        row = conn.execute(
            """SELECT i.exchange,i.account_id,i.environment,i.product,i.producer,
            i.request_key,i.payload,e.config,e.snapshot,e.strategy_version
            FROM v2_trade_intents i JOIN v2_decision_evidence e
              ON (e.evidence_ref,e.config_digest,e.strategy_version)=
                 (i.evidence_ref,i.config_digest,i.strategy_version)
            WHERE i.intent_id=%s""",
            (intent.intent_id,),
        ).fetchone()
        if row != (
            intent.exchange,
            intent.account_id,
            intent.environment,
            intent.product,
            intent.producer,
            intent.request_key,
            intent.payload,
            json.loads(proof.config_json),
            json.loads(proof.snapshot_json),
            proof.strategy_version,
        ):
            raise StrategyPlanAdmissionError("STRATEGY_OPPORTUNITY_INTENT_MISMATCH")
        if signal_id is not None and conn.execute(
            "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (signal_consumer(intent), signal_id),
        ).fetchone() != ("INTENT", intent.intent_id, "signal admitted"):
            raise StrategyPlanAdmissionError("STRATEGY_OPPORTUNITY_SIGNAL_MISMATCH")
        require_plan_admission(
            conn,
            intent.intent_id,
            deployment=json.loads(proof.snapshot_json)["strategy_deployment"],
        )

    def admit(self, plan, binding, *, rationale, features=None, signal_id=None):
        plan, binding, features, signal_id = self._inputs(
            plan, binding, rationale, features, signal_id
        )
        identity = opportunity_intent_id(plan)
        with self.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            proof = self._proof(conn, plan, binding, rationale, features, signal_id)
            intent = OpenIntent(
                identity,
                **asdict(plan.scope),
                producer=plan.instance_id,
                request_key="signal:" + signal_id
                if signal_id is not None
                else "opportunity:" + digest(canonical({"id": plan.opportunity_id})),
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                strategy_version=plan.code_version,
                config_digest=proof.config_digest,
                evidence_ref=proof.evidence_ref,
            )
            store = BusinessState(lambda: nullcontext(conn))
            key = opportunity_key(plan)
            expected = self._receipt(plan, binding, proof, signal_id)
            state = store.read(key)
            if state is not None:
                # Exact replay is read-only, even after expiry, settlement or a
                # deployment stop. It does not refresh any dispatch authority.
                self._replay(conn, state, expected, intent, proof, signal_id)
                status = "ALREADY_ACCEPTED"
            else:
                data = TradingData(lambda: nullcontext(conn))
                # Signal paths retain the existing signal -> intent root order;
                # every path acquires an intent root BEFORE the account lock.
                result = (
                    data.accept_signal(intent, proof)
                    if signal_id is not None
                    else data.accept(intent, proof)
                )
                if result.code is AdmissionCode.UNKNOWN:
                    raise StrategyPlanAdmissionError(
                        "STRATEGY_ADMISSION_OUTCOME_UNKNOWN"
                    )
                if result.code not in {
                    AdmissionCode.ACCEPTED,
                    AdmissionCode.ALREADY_ACCEPTED,
                }:
                    raise StrategyPlanAdmissionError(
                        "STRATEGY_ADMISSION_INTENT_CONFLICT"
                    )
                if result.intent_id != identity:
                    raise StrategyPlanAdmissionError(
                        "STRATEGY_ADMISSION_IDENTITY_CONFLICT"
                    )
                conn.execute(
                    "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                    (identity,),
                ).fetchone()
                lock_account(conn, plan.scope.key)
                state = store.read(key)
                if state is not None:
                    self._replay(conn, state, expected, intent, proof, signal_id)
                    status = "ALREADY_ACCEPTED"
                else:
                    if result.code is not AdmissionCode.ACCEPTED:
                        raise StrategyPlanAdmissionError(
                            "STRATEGY_OPPORTUNITY_RECEIPT_MISSING"
                        )
                    now = conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
                    ).fetchone()[0]
                    if not plan.entry_valid_at(now):
                        raise StrategyPlanAdmissionError(
                            "STRATEGY_ADMISSION_NOT_CURRENT"
                        )
                    require_strategy_opening(conn, identity)
                    changed = store.change(
                        key,
                        expected_version=0,
                        request_key="admit:" + identity,
                        payload=expected,
                        reason=_REASON,
                    )
                    if changed.code != "APPLIED":
                        raise StrategyPlanAdmissionError(
                            "STRATEGY_OPPORTUNITY_RECEIPT_CONFLICT"
                        )
                    status = "ACCEPTED"
        # The outer commit must succeed before any acceptance is returned.
        return {
            "status": status,
            "intent_id": identity,
            "opportunity_id": plan.opportunity_id,
            "plan_digest": plan.plan_digest,
            "evidence_ref": proof.evidence_ref,
            "execution_authorized": False,
        }
