"""Consume *persisted* typed candidates without evaluation or venue submission.

The store is a persistence port, not an evaluator: ``read`` must prove its
immutable candidate/reference and any receipt against the original signal,
account, intent and decision evidence. ``terminal`` owns an atomic signal-locked
transaction and must recheck those facts before writing a negative outcome.
The PostgreSQL implementation and its acceptance tests prove those guarantees;
the value objects below do not claim that hashing an object makes it durable.

No service constructs this adapter. No model, context builder, dispatcher or
controller is installed here. The existing scheduler may call ``run_once`` only
with context acquisition disabled; its bare-receipt expiry sweep is forbidden.
"""

import json
import re
from dataclasses import asdict, dataclass, fields
from types import SimpleNamespace
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.state import normalized
from v2_core.strategy import StrategyScope
from v2_core.strategy_execution_context import validate_execution_context
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_admission import (
    StrategyPlanAdmissionError,
    opportunity_intent_id,
)
from v2_core.strategy_prepare import StrategyPrepareRecovery
from v2_core.strategy_registry import DeploymentBinding, StrategyRegistry, StrategySpec
from v2_core.strategy_signal_admission import DUPLICATE_REASON, StrategySignalAdmission

CANDIDATE_SCHEMA = "strategy-entry-candidate-v1"
EXPIRED_REASON = "STRATEGY_CANDIDATE_EXPIRED"
_SOURCES = frozenset({"s2", "s3", "tv_bridge", "quant"})


def _binding(payload):
    if type(payload) is not dict or set(payload) != {
        field.name for field in fields(DeploymentBinding)
    }:
        raise ValueError("STRATEGY_CANDIDATE_BINDING_INVALID")
    return DeploymentBinding(
        **{
            **payload,
            "scope": AccountScope(**payload["scope"]),
            "spec": StrategySpec(**payload["spec"]),
        }
    )


@dataclass(frozen=True)
class FrozenEntryCandidate:
    """Detached proposal; only the store can prove it was durably frozen.

    OPEN already contains final sizing, exit settings and typed safety inputs.
    This adapter neither infers direction from S6/S8 nor fabricates their scores.
    IGNORED has no plan and cannot carry an executable intent.
    """

    binding: DeploymentBinding
    signal_id: str
    source: str
    signal_digest: str
    action: str
    rationale: str
    observed_at_ms: int
    decided_at_ms: int
    expires_at_ms: int
    features_json: str = "{}"
    plan: TradePlan | None = None
    schema_version: str = CANDIDATE_SCHEMA

    def __post_init__(self):
        if type(self.binding) is not DeploymentBinding:
            raise TypeError("STRATEGY_CANDIDATE_BINDING_REQUIRED")
        binding = _binding(asdict(self.binding))
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "signal_id", str(UUID(self.signal_id)))
        if (
            self.schema_version != CANDIDATE_SCHEMA
            or self.source not in _SOURCES
            or self.action not in {"OPEN", "IGNORED"}
            or binding.mode != "ACTIVE"
            or type(self.signal_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.signal_digest) is None
        ):
            raise ValueError("STRATEGY_CANDIDATE_CONTRACT_INVALID")
        normalized(self.rationale)
        if len(self.rationale.encode()) > 2048:
            raise ValueError("STRATEGY_CANDIDATE_RATIONALE_BOUND")
        for value in (self.observed_at_ms, self.decided_at_ms, self.expires_at_ms):
            milliseconds(value)
        if not (
            self.observed_at_ms
            <= self.decided_at_ms
            < self.expires_at_ms
            <= self.observed_at_ms + 86400000
        ):
            raise ValueError("STRATEGY_CANDIDATE_LIFETIME_INVALID")
        if (
            type(self.features_json) is not str
            or len(self.features_json.encode()) > 65536
        ):
            raise ValueError("STRATEGY_CANDIDATE_FEATURE_BOUND")
        features = json.loads(self.features_json)
        encoded = canonical(features)
        if len(encoded.encode()) > 65536:
            raise ValueError("STRATEGY_CANDIDATE_FEATURE_BOUND")
        object.__setattr__(self, "features_json", encoded)
        if self.action == "IGNORED":
            if self.plan is not None:
                raise ValueError("STRATEGY_REJECTED_CANDIDATE_HAS_PLAN")
            return
        if type(self.plan) is not TradePlan:
            raise TypeError("STRATEGY_CANDIDATE_TYPED_PLAN_REQUIRED")
        plan = TradePlan.from_payload(self.plan.payload())
        object.__setattr__(self, "plan", plan)
        if (
            plan.scope != binding.scope
            or plan.instance_id != binding.instance_id
            or plan.strategy_id != binding.spec.strategy_id
            or plan.code_version != binding.spec.code_version
            or plan.schema_version != binding.spec.plan_schema
            or plan.deployment_generation != binding.generation
            or plan.config_digest != binding.config_digest
            or plan.exit_handler != binding.spec.exit_handler
            or (plan.observed_at_ms, plan.decided_at_ms, plan.expires_at_ms)
            != (self.observed_at_ms, self.decided_at_ms, self.expires_at_ms)
        ):
            raise ValueError("STRATEGY_CANDIDATE_PLAN_MISMATCH")
        validate_execution_context(plan, features.get("execution"))

    @property
    def features(self):
        return json.loads(self.features_json)

    @property
    def candidate_digest(self):
        return digest(canonical(self.payload()))

    @property
    def execution_authorized(self):
        return False

    def payload(self):
        return asdict(self)

    @classmethod
    def from_payload(cls, payload):
        if type(payload) is not dict or set(payload) != {
            field.name for field in fields(cls)
        }:
            raise ValueError("STRATEGY_CANDIDATE_FIELDS_REQUIRED")
        try:
            copied = json.loads(canonical(payload))
            copied["binding"] = _binding(copied["binding"])
            if copied["plan"] is not None:
                copied["plan"] = TradePlan.from_payload(copied["plan"])
            return cls(**copied)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("STRATEGY_CANDIDATE_PAYLOAD_INVALID") from None


@dataclass(frozen=True)
class EntryReceipt:
    """Store-verified durable outcome; construction alone is not proof."""

    outcome: str
    reason: str
    intent_id: str | None = None
    decision_id: str | None = None

    def __post_init__(self):
        normalized(self.reason)
        if self.outcome not in {"INTENT", "IGNORED", "EXPIRED"} or (
            self.outcome == "INTENT"
        ) != (self.intent_id is not None):
            raise ValueError("STRATEGY_ENTRY_RECEIPT_INVALID")
        for name in ("intent_id", "decision_id"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, str(UUID(getattr(self, name))))


@dataclass(frozen=True)
class EntrySignalState:
    signal_id: str
    source: str
    environment: str
    snapshot_json: str
    now_ms: int
    candidate: FrozenEntryCandidate | None = None
    receipt: EntryReceipt | None = None

    def __post_init__(self):
        object.__setattr__(self, "signal_id", str(UUID(self.signal_id)))
        milliseconds(self.now_ms)
        if self.source not in _SOURCES or self.environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("STRATEGY_ENTRY_SIGNAL_SCOPE_INVALID")
        if (
            type(self.snapshot_json) is not str
            or len(self.snapshot_json.encode()) > 1000000
        ):
            raise ValueError("STRATEGY_ENTRY_SIGNAL_BOUND")
        snapshot = json.loads(self.snapshot_json)
        encoded = canonical(snapshot)
        if len(encoded.encode()) > 1000000:
            raise ValueError("STRATEGY_ENTRY_SIGNAL_BOUND")
        required = {"observed_at", "expires_at_ms", "symbol", "signal", "features"}
        if not required <= snapshot.keys() or not snapshot.keys() <= required | {
            "rationale"
        }:
            raise ValueError("STRATEGY_ENTRY_SIGNAL_INVALID")
        milliseconds(snapshot["observed_at"])
        milliseconds(snapshot["expires_at_ms"])
        if (
            snapshot["observed_at"] >= snapshot["expires_at_ms"]
            or type(snapshot["features"]) is not dict
        ):
            raise ValueError("STRATEGY_ENTRY_SIGNAL_INVALID")
        normalized(snapshot["symbol"])
        normalized(snapshot["signal"])
        object.__setattr__(self, "snapshot_json", encoded)
        if (
            self.candidate is not None
            and type(self.candidate) is not FrozenEntryCandidate
        ):
            raise TypeError("STRATEGY_ENTRY_FROZEN_CANDIDATE_REQUIRED")
        if self.receipt is not None and type(self.receipt) is not EntryReceipt:
            raise TypeError("STRATEGY_ENTRY_TYPED_RECEIPT_REQUIRED")

    @property
    def snapshot(self):
        return json.loads(self.snapshot_json)


class TypedEntryWorker:
    """Signal -> verified freeze -> atomic admission -> preparation, never POST.

    ``store.read(conn, scope=..., source=..., signal_id=...)`` verifies durable
    facts and returns EntrySignalState. ``store.terminal`` owns a fresh transaction
    and atomically rejects/expires only that exact frozen candidate. It must not
    replace existing receipts or consume an admitted/orphan intent. Exceptions,
    including lost commit acknowledgement, propagate; a retry starts with read.
    """

    requires_audited_expiry = True
    entries_enabled = False
    entry_filter = None
    entry_observer = None

    def __init__(
        self,
        connect,
        registry,
        *,
        scope,
        source,
        store,
        clock_ms,
        admission=None,
        preparation=None,
    ):
        if (
            not callable(connect)
            or type(registry) is not StrategyRegistry
            or type(scope) is not StrategyScope
            or scope.environment != "SANDBOX"
            or source not in _SOURCES
            or not callable(clock_ms)
            or not callable(getattr(store, "read", None))
            or not callable(getattr(store, "terminal", None))
        ):
            raise TypeError("STRATEGY_ENTRY_PORTS_REQUIRED")
        self._connect, self.registry, self.store = connect, registry, store
        self.scope, self.source = StrategyScope(**asdict(scope)), source
        self.runtime = SimpleNamespace(_now=clock_ms)
        self.admission = (
            StrategySignalAdmission(connect, registry)
            if admission is None
            else admission
        )
        account = AccountScope(
            **{
                name: getattr(scope, name)
                for name in ("exchange", "account_id", "environment", "product")
            }
        )
        self.preparation = (
            StrategyPrepareRecovery(connect, scope=account)
            if preparation is None
            else preparation
        )
        if not callable(getattr(self.admission, "admit", None)) or not callable(
            getattr(self.preparation, "recover", None)
        ):
            raise TypeError("STRATEGY_ENTRY_ADAPTERS_REQUIRED")

    def _state(self, conn, signal_id):
        state = self.store.read(
            conn, scope=self.scope, source=self.source, signal_id=signal_id
        )
        if type(state) is not EntrySignalState or (
            state.signal_id != signal_id
            or state.source != self.source
            or state.environment != self.scope.environment
        ):
            raise ValueError("STRATEGY_ENTRY_STORE_SCOPE_MISMATCH")
        # Receipt verification belongs to the store and precedes proposal reads.
        # Recovery cannot depend on a newer candidate or current configuration.
        if state.receipt is not None:
            return state
        candidate = state.candidate
        if candidate is not None:
            candidate = FrozenEntryCandidate.from_payload(candidate.payload())
            snapshot = state.snapshot
            if (
                candidate.signal_id != signal_id
                or candidate.source != self.source
                or candidate.binding.consumer != self.scope.consumer
                or candidate.signal_digest != digest(state.snapshot_json)
                or candidate.observed_at_ms < snapshot["observed_at"]
                or candidate.expires_at_ms > snapshot["expires_at_ms"]
                or (
                    candidate.plan is not None
                    and candidate.plan.symbol != snapshot["symbol"]
                )
                or (snapshot["signal"] == "EVENT_END" and candidate.action == "OPEN")
            ):
                raise ValueError("STRATEGY_ENTRY_CANDIDATE_SIGNAL_MISMATCH")
            self.registry.resolve_entry(candidate.binding)
        return state

    def _load(self, signal_id):
        with self._connect() as conn:
            return self._state(conn, signal_id)

    def _read(self, conn, signal_id):
        state = self._state(conn, str(UUID(signal_id)))
        # Scheduler only tests presence. This is NOT a legacy OPEN decision and
        # must never be inserted into v2_strategy_decisions before typed admit.
        frozen = (
            None
            if state.candidate is None
            else ("TYPED_FROZEN_CANDIDATE", state.candidate.candidate_digest)
        )
        receipt = (
            None
            if state.receipt is None
            else (state.receipt.outcome, state.receipt.intent_id, state.receipt.reason)
        )
        return state.snapshot, frozen, receipt

    def _receipt(self, state):
        receipt = state.receipt
        if receipt is None:
            raise ValueError("STRATEGY_ENTRY_DURABLE_RECEIPT_REQUIRED")
        if receipt.outcome != "INTENT":
            return {
                "status": receipt.outcome,
                "signal_id": state.signal_id,
                "decision_id": receipt.decision_id,
                "reason": receipt.reason,
                "execution_authorized": False,
            }
        result = self.preparation.recover(receipt.intent_id)
        if (
            type(result) is not dict
            or result.get("intent_id") != receipt.intent_id
            or result.get("status")
            not in {"PREPARED", "EXISTING_ORDER", "UNCHANGED", "EXPIRED"}
            or result.get("execution_authorized") is not False
        ):
            raise ValueError("STRATEGY_ENTRY_PREPARATION_RESULT_INVALID")
        return {**result, "signal_id": state.signal_id, "execution_authorized": False}

    def _terminal(self, state, outcome, reason):
        self.store.terminal(
            scope=self.scope,
            source=self.source,
            signal_id=state.signal_id,
            candidate_digest=state.candidate.candidate_digest,
            outcome=outcome,
            reason=reason,
        )
        # A return value is not proof of a committed receipt. Another valid
        # admission may have won; in that case recover its original identity.
        return self._receipt(self._load(state.signal_id))

    def consume(self, signal_id, *, context):
        signal_id = str(UUID(signal_id))
        if type(context) is not dict or context:
            raise ValueError("STRATEGY_ENTRY_FROZEN_CONTEXT_ONLY")
        state = self._load(signal_id)
        if state.receipt is not None:
            return self._receipt(state)
        candidate = state.candidate
        if candidate is None:
            return {
                "status": "DEFERRED",
                "signal_id": signal_id,
                "reason": "STRATEGY_FROZEN_CANDIDATE_REQUIRED",
                "execution_authorized": False,
            }
        if state.now_ms < candidate.decided_at_ms:
            return {
                "status": "DEFERRED",
                "signal_id": signal_id,
                "reason": "STRATEGY_CANDIDATE_NOT_YET_CURRENT",
                "execution_authorized": False,
            }
        if candidate.action == "IGNORED":
            if state.now_ms >= candidate.expires_at_ms:
                return self._terminal(state, "EXPIRED", EXPIRED_REASON)
            return self._terminal(state, "IGNORED", candidate.rationale)
        try:
            admitted = self.admission.admit(
                candidate.plan,
                candidate.binding,
                signal_id=signal_id,
                rationale=candidate.rationale,
                features=candidate.features,
            )
        except StrategyPlanAdmissionError as exc:
            if (
                type(exc) is not StrategyPlanAdmissionError
                or str(exc) != "STRATEGY_ADMISSION_NOT_CURRENT"
            ):
                raise
            latest = self._load(signal_id)
            if latest.receipt is not None:
                return self._receipt(latest)
            if (
                latest.candidate is None
                or latest.candidate.candidate_digest != candidate.candidate_digest
                or latest.now_ms < candidate.expires_at_ms
            ):
                raise
            return self._terminal(latest, "EXPIRED", EXPIRED_REASON)
        if (
            type(admitted) is not dict
            or admitted.get("execution_authorized") is not False
        ):
            raise ValueError("STRATEGY_ENTRY_ADMISSION_RESULT_INVALID")
        latest = self._load(signal_id)
        receipt = latest.receipt
        if admitted.get("status") == "IGNORED":
            if (
                admitted.get("reason") != DUPLICATE_REASON
                or "intent_id" in admitted
                or receipt is None
                or receipt.outcome != "IGNORED"
                or receipt.reason != DUPLICATE_REASON
            ):
                raise ValueError("STRATEGY_ENTRY_DUPLICATE_RESULT_INVALID")
        elif admitted.get("status") in {"ACCEPTED", "ALREADY_ACCEPTED"}:
            if (
                admitted.get("intent_id") != opportunity_intent_id(candidate.plan)
                or admitted.get("plan_digest") != candidate.plan.plan_digest
                or receipt is None
                or receipt.outcome != "INTENT"
                or receipt.intent_id != admitted["intent_id"]
            ):
                raise ValueError("STRATEGY_ENTRY_ADMISSION_RECEIPT_MISMATCH")
        else:
            raise ValueError("STRATEGY_ENTRY_ADMISSION_RESULT_INVALID")
        return self._receipt(latest)

    def reject_candidate_context(self, *_args, **_kwargs):
        # No context is acquired by this adapter. The legacy repeated-market-
        # failure terminalizer cannot reinterpret a frozen typed candidate.
        return None
