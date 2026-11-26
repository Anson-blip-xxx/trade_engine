"""Durable frozen typed candidates and audited non-admission outcomes.

No evaluation, venue request, order submission or production wiring lives here.
The first committed candidate wins per consumer/signal. Receipts and immutable
admission journals take precedence over a candidate on every recovery attempt.
"""

import json
import re
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid5

from v2_core.account_risk import AccountScope
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical, digest
from v2_core.signals import Signals
from v2_core.state import normalized
from v2_core.strategy import StrategyScope
from v2_core.strategy_deployment import deployment_snapshot
from v2_core.strategy_entry_worker import (
    EntryReceipt,
    EntrySignalState,
    FrozenEntryCandidate,
)
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_admission import (
    _EVIDENCE_BYTES,
    StrategyPlanAdmission,
    opportunity_intent_id,
    require_plan_admission,
)
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.strategy_registry import DeploymentBinding, StrategySpec
from v2_core.strategy_signal_admission import (
    _DECISION_NAMESPACE as DUPLICATE_NAMESPACE,
)
from v2_core.strategy_signal_admission import (
    DUPLICATE_REASON,
    RESOLUTION_SCHEMA,
    _ignored_proof,
    _outcome,
)

CANDIDATE_SCHEMA = "strategy-entry-candidate-v1"
TERMINAL_SCHEMA = "strategy-entry-terminal-v1"
EXPIRED_REASON = "STRATEGY_CANDIDATE_EXPIRED"
_TERMINAL_NAMESPACE = UUID("a4887e91-274f-4be8-86e9-4a2cf608bd01")
_CLOCK_SQL = "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"


def _conflict():
    raise ValueError("STRATEGY_ENTRY_DURABLE_STATE_CONFLICT")


def _scope(binding):
    return StrategyScope(**asdict(binding.scope), producer=binding.instance_id)


def _proof_size(proof):
    if len((proof.config_json + proof.snapshot_json).encode()) > _EVIDENCE_BYTES:
        raise ValueError("STRATEGY_ENTRY_EVIDENCE_BOUND")
    return proof


def _candidate_proof(candidate, signal):
    return _proof_size(
        DecisionEvidence(
            candidate.binding.spec.code_version,
            candidate.binding.config_json,
            canonical(
                {
                    "signal_id": candidate.signal_id,
                    "observed_at": candidate.observed_at_ms,
                    "decided_at": candidate.decided_at_ms,
                    "expires_at_ms": candidate.expires_at_ms,
                    "source": candidate.source,
                    "symbol": signal["symbol"],
                    "rationale": candidate.rationale,
                    "features": {
                        "strategy_entry_candidate": {
                            "schema": CANDIDATE_SCHEMA,
                            "candidate": candidate.payload(),
                            "candidate_digest": candidate.candidate_digest,
                        }
                    },
                    "execution_authorized": False,
                }
            ),
        )
    )


def _terminal_proof(candidate, signal, *, outcome, reason, decided_at):
    return _proof_size(
        DecisionEvidence(
            candidate.binding.spec.code_version,
            candidate.binding.config_json,
            canonical(
                {
                    "signal_id": candidate.signal_id,
                    "observed_at": signal["observed_at"],
                    "decided_at": decided_at,
                    "expires_at_ms": candidate.expires_at_ms,
                    "source": candidate.source,
                    "symbol": signal["symbol"],
                    "rationale": reason,
                    "features": {
                        "strategy_entry_resolution": {
                            "schema": TERMINAL_SCHEMA,
                            "scope": asdict(candidate.binding.scope),
                            "consumer": candidate.binding.consumer,
                            "candidate": candidate.payload(),
                            "candidate_digest": candidate.candidate_digest,
                        }
                    },
                    "result": {"action": outcome, "side": None, "quantity": None},
                    "execution_authorized": False,
                }
            ),
        )
    )


def _decision_matches(row, proof, *, identity, action):
    if row != (
        identity,
        proof.evidence_ref,
        action,
        None,
        None,
        proof.strategy_version,
        proof.config_digest,
        json.loads(proof.config_json),
        json.loads(proof.snapshot_json),
    ):
        _conflict()


class StrategyEntryStore:
    def __init__(self, connect):
        if not callable(connect):
            raise TypeError("STRATEGY_ENTRY_CONNECTION_REQUIRED")
        self.connect = connect

    @staticmethod
    def _identity(scope, source, signal_id):
        if type(scope) is not StrategyScope:
            raise TypeError("STRATEGY_ENTRY_SCOPE_REQUIRED")
        AccountScope(scope.exchange, scope.account_id, scope.environment, scope.product)
        normalized(source)
        if len(scope.consumer.encode()) > 1024:
            raise ValueError("STRATEGY_ENTRY_CONSUMER_BOUND")
        return str(UUID(signal_id))

    @staticmethod
    def _signal(conn, *, scope, source, signal_id, lock="SHARE"):
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='5s'")
        row = conn.execute(
            "SELECT source,environment,snapshot FROM v2_inbound_signals "
            "WHERE signal_id=%s FOR " + lock,
            (signal_id,),
        ).fetchone()
        if row is None or row[:2] != (source, scope.environment):
            raise ValueError("STRATEGY_ENTRY_SIGNAL_SCOPE_MISMATCH")
        return row[2]

    @staticmethod
    def _candidate_matches(candidate, *, scope, source, signal_id, signal, now):
        if (
            type(candidate) is not FrozenEntryCandidate
            or candidate.binding.consumer != scope.consumer
            or candidate.signal_id != signal_id
            or candidate.source != source
            or candidate.signal_digest != digest(canonical(signal))
            or signal["observed_at"] > candidate.observed_at_ms
            or candidate.decided_at_ms > now
            or candidate.expires_at_ms > signal["expires_at_ms"]
            or (
                candidate.plan is not None and candidate.plan.symbol != signal["symbol"]
            )
            or (signal["signal"] == "EVENT_END" and candidate.action == "OPEN")
        ):
            raise ValueError("STRATEGY_ENTRY_CANDIDATE_SIGNAL_MISMATCH")

    @staticmethod
    def _scoped_intents(conn, scope, signal_id):
        return conn.execute(
            "SELECT intent_id::text FROM v2_trade_intents WHERE "
            "(exchange,account_id,environment,product,producer)=(%s,%s,%s,%s,%s) "
            "AND signal_id=%s",
            (*asdict(scope).values(), signal_id),
        ).fetchall()

    def _candidate(self, conn, *, scope, source, signal_id, signal, now):
        row = conn.execute(
            "SELECT c.evidence_ref,e.strategy_version,e.config_digest,e.config,e.snapshot "
            "FROM v2_strategy_entry_candidates c JOIN v2_decision_evidence e "
            "ON (e.evidence_ref,e.signal_id)=(c.evidence_ref,c.signal_id) "
            "WHERE c.consumer=%s AND c.signal_id=%s",
            (scope.consumer, signal_id),
        ).fetchone()
        if row is None:
            return None
        try:
            value = row[4]["features"]["strategy_entry_candidate"]
            candidate = FrozenEntryCandidate.from_payload(value["candidate"])
            self._candidate_matches(
                candidate,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                now=now,
            )
            proof = _candidate_proof(candidate, signal)
            if row != (
                proof.evidence_ref,
                proof.strategy_version,
                proof.config_digest,
                json.loads(proof.config_json),
                json.loads(proof.snapshot_json),
            ):
                _conflict()
            return candidate
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("STRATEGY_ENTRY_FROZEN_EVIDENCE_INVALID") from None

    @staticmethod
    def _intent_receipt(
        conn, *, scope, source, signal_id, signal, receipt, decision, intents
    ):
        if (
            decision is not None
            or receipt[2] != "signal admitted"
            or intents != [(receipt[1],)]
        ):
            _conflict()
        plan = read_trade_plan(conn, receipt[1])
        row = conn.execute(
            "SELECT e.evidence_ref,e.strategy_version,e.config_digest,e.config,e.snapshot "
            "FROM v2_decision_evidence e JOIN v2_trade_intents i "
            "USING(evidence_ref) WHERE i.intent_id=%s",
            (receipt[1],),
        ).fetchone()
        if (
            plan is None
            or row is None
            or _scope(plan).consumer != scope.consumer
            or plan.symbol != signal["symbol"]
            or row[4].get("source") != source
            or row[4].get("signal_id") != signal_id
        ):
            _conflict()
        proof = DecisionEvidence(row[1], canonical(row[3]), canonical(row[4]))
        if (proof.evidence_ref, proof.config_digest) != (row[0], row[2]):
            _conflict()
        require_plan_admission(
            conn, receipt[1], deployment=row[4].get("strategy_deployment")
        )
        return EntryReceipt("INTENT", receipt[2], intent_id=receipt[1])

    @staticmethod
    def _terminal_allowed(candidate, signal, *, outcome, reason, now):
        if candidate.decided_at_ms > now or signal["observed_at"] > now:
            _conflict()
        if outcome == "EXPIRED":
            allowed = now >= candidate.expires_at_ms and reason == EXPIRED_REASON
        else:
            allowed = (
                outcome == "IGNORED"
                and candidate.action == "IGNORED"
                and now < candidate.expires_at_ms
                and reason == candidate.rationale
            )
        if not allowed:
            raise ValueError("STRATEGY_ENTRY_TERMINAL_NOT_PROVEN")

    def _terminal_receipt(
        self, conn, *, scope, source, signal_id, signal, now, receipt, decision, intents
    ):
        if (
            decision is None
            or intents
            or receipt[1] is not None
            or receipt[0] not in {"IGNORED", "EXPIRED"}
        ):
            _conflict()
        try:
            snapshot = decision[8]
            decided = snapshot["decided_at"]
            if type(decided) is not int or decided > now:
                _conflict()
            if receipt == ("IGNORED", None, DUPLICATE_REASON):
                self._duplicate_receipt(
                    conn,
                    scope=scope,
                    source=source,
                    signal_id=signal_id,
                    signal=signal,
                    decision=decision,
                )
            else:
                candidate = FrozenEntryCandidate.from_payload(
                    snapshot["features"]["strategy_entry_resolution"]["candidate"]
                )
                self._candidate_matches(
                    candidate,
                    scope=scope,
                    source=source,
                    signal_id=signal_id,
                    signal=signal,
                    now=decided,
                )
                frozen = self._candidate(
                    conn,
                    scope=scope,
                    source=source,
                    signal_id=signal_id,
                    signal=signal,
                    now=decided,
                )
                if (
                    frozen is None
                    or frozen.candidate_digest != candidate.candidate_digest
                ):
                    _conflict()
                self._terminal_allowed(
                    candidate,
                    signal,
                    outcome=receipt[0],
                    reason=receipt[2],
                    now=decided,
                )
                proof = _terminal_proof(
                    candidate,
                    signal,
                    outcome=receipt[0],
                    reason=receipt[2],
                    decided_at=decided,
                )
                _decision_matches(
                    decision,
                    proof,
                    identity=str(
                        uuid5(_TERMINAL_NAMESPACE, scope.consumer + ":" + signal_id)
                    ),
                    action=receipt[0],
                )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("STRATEGY_ENTRY_TERMINAL_EVIDENCE_INVALID") from None
        return EntryReceipt(receipt[0], receipt[2], decision_id=decision[0])

    @staticmethod
    def _duplicate_receipt(conn, *, scope, source, signal_id, signal, decision):
        snapshot = decision[8]
        resolution = snapshot["features"]["strategy_signal_resolution"]
        if resolution["schema"] != RESOLUTION_SCHEMA:
            _conflict()
        plan = TradePlan.from_payload(resolution["candidate_plan"])
        deployment = resolution["candidate_deployment"]
        binding = DeploymentBinding(
            AccountScope(**deployment["scope"]),
            deployment["instance_id"],
            StrategySpec(**deployment["spec"]),
            canonical(decision[7]),
            deployment["generation"],
            deployment["mode"],
        )
        if (
            binding.consumer != scope.consumer
            or deployment_snapshot(binding) != deployment
            or binding.mode != "ACTIVE"
            or plan.scope != binding.scope
            or plan.instance_id != binding.instance_id
            or plan.strategy_id != binding.spec.strategy_id
            or plan.code_version != binding.spec.code_version
            or plan.schema_version != binding.spec.plan_schema
            or plan.deployment_generation != binding.generation
            or plan.config_digest != binding.config_digest
            or plan.exit_handler != binding.spec.exit_handler
        ):
            _conflict()
        proposal = StrategyPlanAdmission._proof(
            conn,
            plan,
            binding,
            resolution["proposal_rationale"],
            resolution["proposal_features"],
            signal_id,
        )
        identity = opportunity_intent_id(plan)
        winner_plan = read_trade_plan(conn, identity)
        row = conn.execute(
            "SELECT e.evidence_ref,e.strategy_version,e.config_digest,e.config,e.snapshot "
            "FROM v2_trade_intents i JOIN v2_decision_evidence e "
            "USING(evidence_ref) WHERE i.intent_id=%s",
            (identity,),
        ).fetchone()
        if winner_plan is None or row is None:
            _conflict()
        winner_proof = DecisionEvidence(row[1], canonical(row[3]), canonical(row[4]))
        if (winner_proof.evidence_ref, winner_proof.config_digest) != (row[0], row[2]):
            _conflict()
        winner = require_plan_admission(
            conn, identity, deployment=row[4]["strategy_deployment"]
        )
        if (
            plan.scope != binding.scope
            or plan.instance_id != binding.instance_id
            or winner_plan.scope != plan.scope
            or winner_plan.opportunity_id != plan.opportunity_id
            or winner_plan.symbol != plan.symbol
            or winner_plan.side != plan.side
        ):
            _conflict()
        proof = _ignored_proof(
            plan,
            binding,
            proposal,
            winner,
            signal_id,
            (source, signal),
            snapshot["decided_at"],
        )
        _decision_matches(
            decision,
            proof,
            identity=str(uuid5(DUPLICATE_NAMESPACE, scope.consumer + ":" + signal_id)),
            action="IGNORED",
        )

    def read(self, conn, *, scope, source, signal_id):
        signal_id = self._identity(scope, source, signal_id)
        signal = self._signal(conn, scope=scope, source=source, signal_id=signal_id)
        now = conn.execute(_CLOCK_SQL).fetchone()[0]
        decision, receipt = _outcome(conn, scope.consumer, signal_id)
        intents = self._scoped_intents(conn, scope, signal_id)
        candidate = verified = None
        if receipt is not None:
            verify = (
                self._intent_receipt
                if receipt[0] == "INTENT"
                else self._terminal_receipt
            )
            kwargs = {} if receipt[0] == "INTENT" else {"now": now}
            verified = verify(
                conn,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                receipt=receipt,
                decision=decision,
                intents=intents,
                **kwargs,
            )
        else:
            if decision is not None or intents:
                _conflict()
            candidate = self._candidate(
                conn,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                now=now,
            )
        return EntrySignalState(
            signal_id,
            source,
            scope.environment,
            canonical(signal),
            now,
            candidate,
            verified,
        )

    def freeze(self, candidate):
        if type(candidate) is not FrozenEntryCandidate:
            raise TypeError("STRATEGY_ENTRY_FROZEN_CANDIDATE_REQUIRED")
        candidate = FrozenEntryCandidate.from_payload(candidate.payload())
        scope, source, signal_id = (
            _scope(candidate.binding),
            candidate.source,
            candidate.signal_id,
        )
        self._identity(scope, source, signal_id)
        with self.connect() as conn:
            signal = self._signal(
                conn, scope=scope, source=source, signal_id=signal_id, lock="UPDATE"
            )
            now = conn.execute(_CLOCK_SQL).fetchone()[0]
            saved = self._candidate(
                conn,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                now=now,
            )
            if saved is not None:
                return saved
            current = self.read(conn, scope=scope, source=source, signal_id=signal_id)
            if current.receipt is not None:
                raise ValueError("STRATEGY_ENTRY_ALREADY_TERMINAL")
            self._candidate_matches(
                candidate,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                now=now,
            )
            proof = _candidate_proof(candidate, signal)
            EvidenceStore(lambda: nullcontext(conn)).put(proof)
            conn.execute(
                "INSERT INTO v2_strategy_entry_candidates(consumer,signal_id,evidence_ref) VALUES(%s,%s,%s)",
                (scope.consumer, signal_id, proof.evidence_ref),
            )
        return candidate

    def terminal(self, *, scope, source, signal_id, candidate_digest, outcome, reason):
        signal_id = self._identity(scope, source, signal_id)
        normalized(reason)
        if (
            not isinstance(candidate_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate_digest) is None
        ):
            raise ValueError("STRATEGY_ENTRY_FROZEN_DIGEST_REQUIRED")
        with self.connect() as conn:
            signal = self._signal(
                conn, scope=scope, source=source, signal_id=signal_id, lock="UPDATE"
            )
            current = self.read(conn, scope=scope, source=source, signal_id=signal_id)
            if current.receipt is not None:
                return
            candidate = current.candidate
            if candidate is None or candidate.candidate_digest != candidate_digest:
                raise ValueError("STRATEGY_ENTRY_FROZEN_CANDIDATE_CHANGED")
            self._terminal_allowed(
                candidate, signal, outcome=outcome, reason=reason, now=current.now_ms
            )
            proof = _terminal_proof(
                candidate,
                signal,
                outcome=outcome,
                reason=reason,
                decided_at=current.now_ms,
            )
            bound = lambda: nullcontext(conn)
            EvidenceStore(bound).put(proof)
            conn.execute(
                "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
                "VALUES(%s,%s,%s,%s,%s,NULL,NULL)",
                (
                    str(uuid5(_TERMINAL_NAMESPACE, scope.consumer + ":" + signal_id)),
                    scope.consumer,
                    signal_id,
                    proof.evidence_ref,
                    outcome,
                ),
            )
            Signals(bound).complete(
                consumer=scope.consumer,
                signal_id=signal_id,
                outcome=outcome,
                reason=reason,
            )
