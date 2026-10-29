"""Durable strategy evaluation -> intent admission -> PREPARED, never submission.

The first committed decision wins per scoped consumer/signal. Evaluation may run
more than once under contention, so the injected evaluator MUST be side-effect
free. Once stored, retries reuse its configuration/context/deadline verbatim.
"""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from uuid import UUID, uuid5

from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.intents import OpenIntent
from v2_core.ledger import amount
from v2_core.state import normalized

_NAMESPACE = UUID("e1871725-a730-45d8-9c79-7d4044c9e530")


@dataclass(frozen=True)
class StrategyScope:
    exchange: str
    account_id: str
    environment: str
    product: str
    producer: str

    def __post_init__(self):
        for field in (
            self.exchange,
            self.account_id,
            self.environment,
            self.product,
            self.producer,
        ):
            normalized(field)

    @property
    def consumer(self):
        return json.dumps(
            [
                self.exchange,
                self.account_id,
                self.environment,
                self.product,
                self.producer,
            ],
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class StrategyDecision:
    action: str
    rationale: str
    side: str | None = None
    quantity: str | None = None
    features_json: str = "{}"

    def __post_init__(self):
        if self.action not in {"OPEN", "IGNORED"}:
            raise ValueError("explicit OPEN or IGNORED decision required")
        normalized(self.rationale)
        if self.action == "OPEN":
            if self.side not in {"BUY", "SELL"}:
                raise ValueError("explicit opening side required")
            amount(self.quantity, positive=True)
        elif self.side is not None or self.quantity is not None:
            raise ValueError("ignored signal cannot carry an order")
        object.__setattr__(
            self, "features_json", canonical(json.loads(self.features_json))
        )


class StrategyWorker:
    def __init__(
        self,
        runtime,
        scope,
        *,
        source,
        strategy_version,
        config,
        decide,
        max_delay_ms,
        entry_filter=None,
    ):
        if not isinstance(scope, StrategyScope):
            raise TypeError("typed strategy scope required")
        for value in (source, strategy_version):
            normalized(value)
        if not callable(decide):
            raise TypeError("explicit pure evaluator required")
        if type(max_delay_ms) is not int or not 1 <= max_delay_ms <= 86400000:
            raise ValueError("bounded decision lifetime required")
        self.runtime, self.scope, self.source = runtime, scope, source
        self.strategy_version, self.config_json, self.decide, self.max_delay_ms = (
            strategy_version,
            canonical(config),
            decide,
            max_delay_ms,
        )
        self._connect = runtime.data._connect
        if entry_filter is not None and not callable(
            getattr(entry_filter, "assess", None)
        ):
            raise TypeError("explicit entry filter required")
        self.entry_filter = entry_filter

    def _read(self, conn, signal_id):
        signal = conn.execute(
            "SELECT source,environment,snapshot FROM v2_inbound_signals WHERE signal_id=%s",
            (signal_id,),
        ).fetchone()
        if signal is None or signal[:2] != (self.source, self.scope.environment):
            raise ValueError("signal outside bound source/environment")
        row = conn.execute(
            """SELECT d.decision_id::text,d.action,d.side,d.quantity::text,
            e.strategy_version,e.config,e.snapshot FROM v2_strategy_decisions d
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE d.consumer=%s AND d.signal_id=%s""",
            (self.scope.consumer, signal_id),
        ).fetchone()
        receipt = conn.execute(
            "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (self.scope.consumer, signal_id),
        ).fetchone()
        return signal[2], row, receipt

    def decision(self, signal_id):
        with self._connect() as conn:
            _, row, _ = self._read(conn, str(UUID(signal_id)))
        if row is None:
            return None
        return dict(
            zip(
                (
                    "decision_id",
                    "action",
                    "side",
                    "quantity",
                    "strategy_version",
                    "config",
                    "snapshot",
                ),
                row,
                strict=True,
            )
        )

    def reject_candidate_context(
        self, signal_id, *, cause, lease_token, attempt, minimum_attempts=2
    ):
        """Durably abstain after repeated, proven symbol-data failure.

        This method cannot evaluate a strategy, consult JEV, reserve risk or
        prepare an order. The original signal row lock serializes it with the
        first decision, receipt, expiry and intent admission. A prior OPEN wins
        untouched and is left to the ordinary recovery path.
        """
        from v2_core.scheduling import TERMINAL_CONTEXT_FAILURES
        from v2_core.signals import Signals

        if (
            not isinstance(cause, str)
            or cause not in TERMINAL_CONTEXT_FAILURES
            or type(attempt) is not int
            or type(minimum_attempts) is not int
            or not 2 <= minimum_attempts <= 20
            or attempt < minimum_attempts
        ):
            raise ValueError("CANDIDATE_CONTEXT_RESOLUTION_INVALID")
        signal_id, lease_token = str(UUID(signal_id)), str(UUID(lease_token))
        with self._connect() as conn:
            conn.execute(
                "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE",
                (signal_id,),
            )
            signal, stored, receipt = self._read(conn, signal_id)
            if receipt is not None and receipt[0] != "INTENT":
                return {
                    "status": receipt[0],
                    "signal_id": signal_id,
                    "decision_id": None if stored is None else stored[0],
                    "reason": receipt[2],
                }
            if stored is not None or receipt is not None:
                return None
            # An orphan admitted intent is not permission to discard a possible
            # live request. Corruption stays visible as a shared failure.
            if conn.execute(
                "SELECT 1 FROM v2_trade_intents WHERE signal_id=%s "
                "AND (exchange,account_id,environment,product,producer)=(%s,%s,%s,%s,%s) LIMIT 1",
                (signal_id, *asdict(self.scope).values()),
            ).fetchone():
                raise ValueError("CANDIDATE_CONTEXT_INTENT_WITHOUT_DECISION")
            task = conn.execute(
                "SELECT attempts,error_code,lease_token::text,"
                "lease_until>=clock_timestamp(),completed_at IS NULL "
                "FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s FOR UPDATE",
                (self.scope.consumer, signal_id),
            ).fetchone()
            if task != (attempt, cause, lease_token, True, True):
                return None
            now = self.runtime._now()
            if signal["observed_at"] > now:
                return None
            expired = now >= signal["expires_at_ms"]
            action = "EXPIRED" if expired else "IGNORED"
            reason = (
                "signal expired before evaluation"
                if expired
                else "CANDIDATE_DATA_UNAVAILABLE_AFTER_RETRY"
            )
            identity = str(uuid5(_NAMESPACE, self.scope.consumer + ":" + signal_id))
            snapshot = {
                "signal_id": signal_id,
                "observed_at": signal["observed_at"],
                "decided_at": now,
                "expires_at_ms": signal["expires_at_ms"],
                "source": self.source,
                "symbol": signal["symbol"],
                "rationale": reason,
                "features": {
                    "signal": signal["features"],
                    # This is absent evidence, not fabricated valid market or
                    # account data. No ordinary evaluator consumes this object.
                    "context": {},
                    "evaluation": {
                        "candidate_context_resolution": {
                            "scope": "CANDIDATE",
                            "cause": cause,
                            "attempt": attempt,
                            "minimum_attempts": minimum_attempts,
                            "same_previous_cause": True,
                            "context_available": False,
                            "execution_authorized": False,
                        }
                    },
                },
                "result": {"action": action, "side": None, "quantity": None},
            }
            proof = DecisionEvidence(
                self.strategy_version, self.config_json, canonical(snapshot)
            )
            if len((proof.config_json + proof.snapshot_json).encode()) > 1_000_000:
                raise ValueError("decision evidence exceeds size limit")
            bound = lambda: nullcontext(conn)
            EvidenceStore(bound).put(proof)
            conn.execute(
                "INSERT INTO v2_strategy_decisions "
                "(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
                "VALUES (%s,%s,%s,%s,%s,NULL,NULL)",
                (identity, self.scope.consumer, signal_id, proof.evidence_ref, action),
            )
            Signals(bound).complete(
                consumer=self.scope.consumer,
                signal_id=signal_id,
                outcome=action,
                reason=reason,
            )
            return {
                "status": action,
                "signal_id": signal_id,
                "decision_id": identity,
                "reason": reason,
            }

    def consume(self, signal_id, *, context):
        signal_id = str(UUID(signal_id))
        with self._connect() as conn:
            signal, stored, receipt = self._read(conn, signal_id)
        if stored is None and receipt is None:
            now = self.runtime._now()
            if signal["observed_at"] > now:
                return {"status": "DEFERRED", "signal_id": signal_id}
            context_json = canonical(context)
            if len(context_json.encode()) > 1_000_000:
                raise ValueError("decision context exceeds size limit")
            expired = now >= signal["expires_at_ms"]
            ended = signal["signal"] == "EVENT_END"
            deadline = min(signal["expires_at_ms"], now + self.max_delay_ms)
            frozen_context = json.loads(context_json)
            context_fields = {
                "assembled_at",
                "valid_until_ms",
                "environment",
                "symbol",
                "sources",
            }
            if (
                not expired
                and not ended
                and context_fields.intersection(frozen_context)
            ):
                if (
                    not context_fields <= frozen_context.keys()
                    or frozen_context["environment"] != self.scope.environment
                    or frozen_context["symbol"] != signal["symbol"]
                    or type(frozen_context["assembled_at"]) is not int
                    or type(frozen_context["valid_until_ms"]) is not int
                    or not 0
                    <= frozen_context["assembled_at"]
                    <= now
                    < frozen_context["valid_until_ms"]
                    or not isinstance(frozen_context["sources"], dict)
                    or not frozen_context["sources"]
                    or (
                        "liquidity_as_of_ms" in frozen_context
                        and (
                            type(frozen_context["liquidity_as_of_ms"]) is not int
                            or not frozen_context["assembled_at"]
                            <= frozen_context["liquidity_as_of_ms"]
                            <= now
                        )
                    )
                ):
                    raise ValueError(
                        "context is stale, incomplete or outside strategy scope"
                    )
                deadline = min(deadline, frozen_context["valid_until_ms"])
            result = (
                StrategyDecision("IGNORED", "signal expired before evaluation")
                if expired
                else StrategyDecision("IGNORED", "producer lifecycle ended")
                if ended
                else self.decide(
                    deepcopy(signal),
                    json.loads(context_json),
                    json.loads(self.config_json),
                )
            )
            if not isinstance(result, StrategyDecision):
                raise TypeError("typed strategy decision required")
            if result.action == "OPEN":
                fusion = json.loads(result.features_json).get("signal_fusion")
                if fusion is not None:
                    from v2_core.signal_fusion import validate_fusion

                    checked = validate_fusion(
                        signal,
                        source=self.source,
                        proof=frozen_context.get("signal_fusion"),
                        now_ms=frozen_context["assembled_at"],
                    )
                    if (
                        checked is None
                        or any(
                            fusion.get(key) != value for key, value in checked.items()
                        )
                        or checked["veto"]
                    ):
                        raise ValueError("SIGNAL_FUSION_DECISION_MISMATCH")
                    until = checked["valid_until_ms"]
                    if until is not None:
                        deadline = min(deadline, until)
                        if self.runtime._now() >= until:
                            result = StrategyDecision(
                                "IGNORED",
                                "SIGNAL_FUSION_EVIDENCE_EXPIRED",
                                features_json=result.features_json,
                            )
            if result.action == "OPEN" and self.entry_filter is not None:
                # Only fresh candidates reach the asynchronous second opinion.
                # Already committed decisions are immutable and skip this path.
                from v2_core.jev_entry_gate import GateAssessment

                try:
                    assessment = self.entry_filter.assess(
                        result,
                        signal_id=signal_id,
                        signal=deepcopy(signal),
                        context=json.loads(context_json),
                        config=json.loads(self.config_json),
                        consumer=self.scope.consumer,
                    )
                    if not isinstance(assessment, GateAssessment):
                        raise TypeError("typed entry assessment required")
                    filter_evidence = json.loads(assessment.evidence_json)
                    if not isinstance(filter_evidence, dict):
                        raise TypeError("entry filter evidence must be an object")
                    if assessment.status == "PENDING":
                        if assessment.decision is not None:
                            raise ValueError("pending filter cannot authorize entry")
                        return {
                            "status": "DEFERRED",
                            "signal_id": signal_id,
                            "reason": assessment.reason,
                        }
                    if assessment.status == "FALLBACK":
                        from v2_core.jev_entry_gate import BASELINE_FALLBACK_CAUSES

                        valid_until = filter_evidence.get("valid_until_ms")
                        fallback_policy = filter_evidence.get("policy", {})
                        if (
                            assessment.decision != result
                            or assessment.reason != "JEV_BASELINE_FALLBACK"
                            or filter_evidence.get("status") != "FALLBACK"
                            or fallback_policy.get("enabled") is not True
                            or fallback_policy.get("unavailable_action") != "BASELINE"
                            or filter_evidence.get("fallback_cause")
                            not in BASELINE_FALLBACK_CAUSES
                            or filter_evidence.get("execution_authorized") is not False
                            or filter_evidence.get("risk_override") is not False
                            or type(valid_until) is not int
                            or not self.runtime._now()
                            < valid_until
                            <= min(
                                signal["expires_at_ms"],
                                frozen_context.get("valid_until_ms", 0),
                            )
                        ):
                            raise ValueError("baseline fallback proof invalid")
                        deadline = min(deadline, valid_until)
                    elif assessment.status == "PASS":
                        if assessment.decision != result:
                            raise ValueError(
                                "entry filter cannot replace risk decision"
                            )
                        if assessment.reason == "JEV_GATE_DISABLED":
                            if (
                                filter_evidence.get("policy", {}).get("enabled")
                                is not False
                                or filter_evidence.get("valid_until_ms") is not None
                            ):
                                raise ValueError("disabled entry filter proof invalid")
                        else:
                            valid_until = filter_evidence.get("valid_until_ms")
                            if (
                                type(valid_until) is not int
                                or not self.runtime._now() < valid_until <= 2**53 - 1
                            ):
                                raise ValueError("entry filter validity unavailable")
                            # Existing prepare/dispatch and PG account-risk
                            # freshness checks now enforce the same JEV bound.
                            deadline = min(deadline, valid_until)
                    elif assessment.status in {"VETO", "SKIP"}:
                        if (
                            not isinstance(assessment.decision, StrategyDecision)
                            or assessment.decision.action != "IGNORED"
                        ):
                            raise ValueError(
                                "entry veto must be terminal and non-trading"
                            )
                    else:
                        raise ValueError("unknown entry assessment")
                    result = assessment.decision
                    evaluation = json.loads(result.features_json)
                    evaluation["jev_entry_gate"] = filter_evidence
                    result = StrategyDecision(
                        result.action,
                        result.rationale,
                        side=result.side,
                        quantity=result.quantity,
                        features_json=canonical(evaluation),
                    )
                except Exception:  # noqa: BLE001 - isolate model/reference failure to this signal
                    result = StrategyDecision("IGNORED", "JEV_GATE_UNAVAILABLE")
            snapshot = {
                "signal_id": signal_id,
                "observed_at": signal["observed_at"],
                "decided_at": now,
                "expires_at_ms": deadline,
                "source": self.source,
                "symbol": signal["symbol"],
                "rationale": result.rationale,
                "features": {
                    "signal": signal["features"],
                    "context": json.loads(context_json),
                    "evaluation": json.loads(result.features_json),
                },
                "result": {
                    "action": "EXPIRED" if expired else result.action,
                    "side": result.side,
                    "quantity": result.quantity,
                },
            }
            proof = DecisionEvidence(
                self.strategy_version, self.config_json, canonical(snapshot)
            )
            if len((proof.config_json + proof.snapshot_json).encode()) > 1_000_000:
                raise ValueError("decision evidence exceeds size limit")
            identity = str(uuid5(_NAMESPACE, self.scope.consumer + ":" + signal_id))
            with self._connect() as conn:
                conn.execute(
                    "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE",
                    (signal_id,),
                )
                _, stored, receipt = self._read(conn, signal_id)
                if stored is None and receipt is None:
                    # Cross-source candidates share one rolling opportunity window.
                    # The lock covers check + first immutable decision, including
                    # crashes before the winning decision is admitted as an intent.
                    if (
                        json.loads(self.config_json).get("dynamic_universe")
                        and result.action == "OPEN"
                    ):
                        fusion = frozen_context.get("signal_fusion", {})
                        window = fusion.get("window_ms")
                        if type(window) is not int or not 1000 <= window <= 300000:
                            raise ValueError("SIGNAL_FUSION_POLICY_REQUIRED")
                        lock = (
                            "fusion:"
                            + self.scope.consumer
                            + ":"
                            + signal["symbol"]
                            + ":"
                            + result.side
                        )
                        conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                            (lock,),
                        )
                        fusion_now = max(now, self.runtime._now())
                        winner = conn.execute(
                            "SELECT d.decision_id::text,d.signal_id::text FROM v2_strategy_decisions d "
                            "JOIN v2_decision_evidence e USING(evidence_ref) WHERE d.consumer=%s AND d.action='OPEN' AND d.side=%s "
                            "AND e.snapshot->>'symbol'=%s AND (e.snapshot->>'source'<>%s OR %s) "
                            "AND (e.snapshot->>'decided_at')::bigint BETWEEN %s AND %s ORDER BY e.created_at LIMIT 1",
                            (
                                self.scope.consumer,
                                result.side,
                                signal["symbol"],
                                self.source,
                                self.source == "quant",
                                fusion_now - window,
                                fusion_now,
                            ),
                        ).fetchone()
                        if winner:
                            result = StrategyDecision(
                                "IGNORED", "CORRELATED_SIGNAL_ALREADY_ADMITTED"
                            )
                            snapshot["rationale"] = result.rationale
                            snapshot["result"] = {
                                "action": result.action,
                                "side": None,
                                "quantity": None,
                            }
                            snapshot["features"]["evaluation"] = {
                                "winning_decision": winner[0],
                                "winning_signal": winner[1],
                            }
                            proof = DecisionEvidence(
                                self.strategy_version,
                                self.config_json,
                                canonical(snapshot),
                            )
                    EvidenceStore(lambda: nullcontext(conn)).put(proof)
                    conn.execute(
                        """INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,side,quantity)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            identity,
                            self.scope.consumer,
                            signal_id,
                            proof.evidence_ref,
                            snapshot["result"]["action"],
                            result.side,
                            result.quantity,
                        ),
                    )
                    _, stored, receipt = self._read(conn, signal_id)
        if receipt is not None and (stored is None or receipt[0] != "INTENT"):
            return {
                "status": receipt[0],
                "intent_id": receipt[1],
                "reason": receipt[2],
                "signal_id": signal_id,
                "decision_id": None if stored is None else stored[0],
            }
        identity, action, side, quantity, version, config, snapshot = stored
        if action != "OPEN":
            self.runtime.data.signals.complete(
                consumer=self.scope.consumer,
                signal_id=signal_id,
                outcome=action,
                reason=snapshot["rationale"],
            )
            return {
                "status": action,
                "signal_id": signal_id,
                "decision_id": identity,
                "reason": snapshot["rationale"],
            }
        proof = DecisionEvidence(version, canonical(config), canonical(snapshot))
        intent = OpenIntent(
            identity,
            self.scope.exchange,
            self.scope.account_id,
            self.scope.environment,
            self.scope.product,
            self.scope.producer,
            "signal:" + signal_id,
            snapshot["symbol"],
            side,
            quantity,
            version,
            proof.config_digest,
            proof.evidence_ref,
        )
        result = self.runtime.accept_open(intent, proof)
        return {**result, "signal_id": signal_id, "decision_id": identity}
