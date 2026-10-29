"""Bounded post-risk JEV entry filter; account policy defaults OFF.

Called only AFTER the original pure admission returned an authorized OPEN
candidate. It does not repeat strategy/risk evaluation. Existing PG business
state binds one public snapshot/deadline to a scoped signal; the existing JEV
background queue/coordinator performs the external request. No second queue,
ledger, model client, credentials, exchange calls or order authority live here.

An integrating StrategyWorker handles PENDING as a normal DEFERRED return
before saving any immutable OPEN decision. Its existing scheduler may poll PG
within the same lease and remaining budget, then acquire a new account context
and rerun admission once; otherwise the next normal attempt remains available.
Every non-pending assessment supplies a normal decision. Freeze
``evidence_json`` alongside it. Existing submit-time risk checks still apply.
Do NOT turn PENDING into global UNAVAILABLE.
An explicit BASELINE availability policy can retain the original fresh OPEN
with FALLBACK evidence. Model vetoes, ambiguous claims, corrupt state and stale
signals never acquire that authority. No provider error message is inspected.

Only PG context reads/writes occur synchronously (50ms lock/100ms statement
limits); the connection factory must also be bounded. These SQL limits do not
magically bound connection setup or an entire multi-statement transaction. The
same-lease polling budget is at most the model timeout (hard policy ceiling 2s),
not a hard-real-time wall-clock guarantee. A late return must pass the
scheduler's remaining-budget/lease check before any fresh consume. Model
results are read from PG, NOT the reference's 5s in-memory control cache.
"""

import json
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from uuid import UUID, uuid4

from services.v2_jev_coordinator import (
    DAY_NAMESPACE,
    MINUTE_NAMESPACE,
    REQUEST_NAMESPACE,
    _budget,
    _budget_key,
    _token_budget_reason,
    _window_starts,
)
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE, public_features
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.jev_advisory import (
    RESERVE_TOKENS,
    AdvisoryPolicy,
    _integer,
    prepare_request,
)
from v2_core.jev_entry_gate import (
    GateAssessment,
    JevEntryGatePolicy,
    evaluate_entry_gate,
)
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyDecision

BINDING_NAMESPACE = "jev-entry-check-v1"
POLICY_NAMESPACE = "jev-entry-gate-policy-v1"
_SCHEMA = "jev-fixed-entry-check-v1"


class _InvalidBinding(ValueError):
    pass


def _require(condition):
    if not condition:
        raise _InvalidBinding()


def policy_key(scope):
    """Account-scoped runtime policy, separate from provider-wide kill switch."""
    if not isinstance(scope, AccountScope):
        raise TypeError("JEV_ENTRY_ACCOUNT_SCOPE_REQUIRED")
    return StateKey(**asdict(scope), namespace=POLICY_NAMESPACE, key="current")


def _skip(decision, reason):
    evidence = {
        "schema": _SCHEMA,
        "status": "SKIP",
        "reason": reason,
        "execution_authorized": False,
        "risk_override": False,
        "admission_authorized": False,
    }
    return GateAssessment(
        "SKIP",
        reason,
        StrategyDecision(
            "IGNORED",
            reason,
            features_json=canonical(
                {
                    **evidence,
                    "original_admission": json.loads(decision.features_json),
                }
            ),
        ),
        canonical(evidence),
    )


class JevEntryContext:
    """An optional StrategyWorker entry_filter port, never a context evaluator.

    Default ``policy=None`` reads dynamic account-scoped PG policy each attempt;
    absent policy means OFF. Explicit policy is an offline/QA override only.
    """

    def __init__(self, connect, reference, *, scope, policy=None, interval_seconds=10):
        if (
            not callable(connect)
            or not callable(getattr(reference, "submit", None))
            or not isinstance(scope, AccountScope)
            or (scope.exchange, scope.environment, scope.product)
            != ("BINANCE", "SANDBOX", "FUTURES")
        ):
            raise ValueError("JEV_ENTRY_BINDING_REQUIRED")
        if policy is not None and not isinstance(policy, JevEntryGatePolicy):
            raise TypeError("JEV_ENTRY_POLICY_REQUIRED")
        _integer(interval_seconds, 1, 60)
        if (
            policy is not None
            and policy.enabled
            and policy.max_wait_ms < interval_seconds * 2000
        ):
            raise ValueError("JEV_REVIEW_WINDOW_TOO_SHORT_FOR_SCHEDULER")
        if (
            policy is not None
            and policy.enabled
            and policy.cache_ttl_ms < policy.max_wait_ms
        ):
            raise ValueError("JEV_RESULT_TTL_SHORTER_THAN_REVIEW_WINDOW")
        self.connect, self.reference, self.scope, self.policy = (
            connect,
            reference,
            scope,
            policy,
        )
        self.interval_seconds = interval_seconds

    def binding_key(self, consumer, signal_id):
        return StateKey(
            **asdict(self.scope),
            namespace=BINDING_NAMESPACE,
            key=consumer + ":" + str(UUID(signal_id)),
        )

    def wait_pending(self, *, signal_id, consumer, max_wait_ms, stop_requested=None):
        """Bounded PG-only wake hint, never a decision or an execution permit.

        The scheduler retains its existing task lease and must acquire a new
        account context and rerun risk/gate checks after True. No transaction
        is held during sleep. Neither this wait nor a restart extends a model,
        signal or binding deadline; the external request stays in its worker.
        """
        try:
            _integer(max_wait_ms, 1, 2000)  # Existing model hard timeout ceiling.
            if stop_requested is not None and not callable(stop_requested):
                return False
            parts = json.loads(consumer)
            _require(
                isinstance(parts, list)
                and len(parts) == 5
                and parts[:4] == list(asdict(self.scope).values())
                and parts[4] in {"s6", "s8"}
            )
            signal_id = str(UUID(signal_id))
            key = self.binding_key(consumer, signal_id)
            started = time.monotonic()
            end = started + max_wait_ms / 1000
            fallback_allowed = False
            queue_unclaimed = False
            while time.monotonic() < end:
                if stop_requested is not None:
                    stopped = stop_requested()
                    if type(stopped) is not bool or stopped:
                        return False
                with self.connect() as conn, conn.transaction():
                    conn.execute("SET LOCAL lock_timeout='50ms'")
                    conn.execute("SET LOCAL statement_timeout='100ms'")
                    store = BusinessState(lambda: nullcontext(conn))
                    saved = store.read(key)
                    _require(
                        saved is not None and not saved.deleted and saved.version == 1
                    )
                    binding = json.loads(saved.payload_json)
                    _require(
                        binding["schema"] == _SCHEMA
                        and binding["consumer"] == consumer
                        and binding["signal_id"] == signal_id
                    )
                    policy = AdvisoryPolicy(**binding["model_policy"])
                    gate_policy = JevEntryGatePolicy(**binding["gate_policy"])
                    fallback_allowed = gate_policy.unavailable_action == "BASELINE"
                    end = min(end, started + policy.timeout_ms / 1000)
                    now = conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint"
                    ).fetchone()[0]
                    _integer(binding["deadline_ms"], 0, 2**53 - 1)
                    if not binding["started_at_ms"] <= now < binding["deadline_ms"]:
                        return False
                    request = self._read(
                        store,
                        StateKey(
                            **asdict(PUBLIC_SCOPE),
                            namespace=REQUEST_NAMESPACE,
                            key=binding["request_key"],
                        ),
                    )
                    queue_unclaimed = request is None
                    if queue_unclaimed and self._budget_exhausted(store, now, policy):
                        # Another candidate may consume quota after our first
                        # preflight. Wake for a fresh assessment instead of
                        # polling missing work until the queue timeout. This
                        # hint itself grants no fallback or order authority.
                        return time.monotonic() < end
                    if (
                        queue_unclaimed
                        and fallback_allowed
                        and now - binding["started_at_ms"]
                        >= gate_policy.unavailable_wait_ms
                    ):
                        return True
                    if request is not None:
                        _require(request.get("schema") == "jev-pg-coordinator-v1")
                        # Merely wake the original worker; assess() validates
                        # model/hash/direction/policy/TTL before any commit.
                        if request.get("status") == "COMPLETED":
                            return time.monotonic() < end
                        _require(request.get("status") == "CLAIMED")
                        _integer(request["deadline_ms"], 0, 2**53 - 1)
                        if now >= request["deadline_ms"]:
                            return time.monotonic() < end
                    remaining = min(
                        end - time.monotonic(), (binding["deadline_ms"] - now) / 1000
                    )
                if remaining > 0:
                    time.sleep(min(0.05, remaining))
            return fallback_allowed and queue_unclaimed
        except Exception:  # noqa: BLE001 - a wake hint has no trading authority
            return False

    @staticmethod
    def _read(store, key):
        row = store.read(key)
        if row is None:
            return None
        _require(not row.deleted)
        return json.loads(row.payload_json)

    def _budget_exhausted(self, store, now, control):
        minute, day = _window_starts(now)
        minutes = _budget(
            self._read(store, _budget_key(MINUTE_NAMESPACE, minute)),
            kind="minute",
            start=minute,
            limit=min(5, control.requests_per_minute),
        )
        if minutes["used"] + 1 > minutes["limit"]:
            return "REQUEST_BUDGET_EXHAUSTED"
        days = _budget(
            self._read(store, _budget_key(DAY_NAMESPACE, day)),
            kind="utc8-day",
            start=day,
            limit=min(500000, control.tokens_per_day),
        )
        return _token_budget_reason(
            days,
            now=now,
            pacing=control.token_budget_pacing,
            burst_tokens=control.token_pacing_burst_tokens,
        )

    def assess(self, decision, *, signal_id, signal, context, config, consumer):
        """No model submission until a fixed binding has committed successfully."""
        if not isinstance(decision, StrategyDecision):
            raise TypeError("JEV_GATE_ADMISSION_DECISION_REQUIRED")
        if decision.action != "OPEN" or (
            self.policy is not None and not self.policy.enabled
        ):
            return evaluate_entry_gate(
                decision,
                symbol=None,
                public_features=None,
                advisory=None,
                now_ms=None,
                started_at_ms=None,
                deadline_ms=None,
                signal_expires_at_ms=None,
                policy=self.policy,
            )
        try:
            signal_id = str(UUID(signal_id))
            parsed_consumer = json.loads(consumer)
            _require(
                type(parsed_consumer) is list
                and len(parsed_consumer) == 5
                and parsed_consumer[:4] == list(asdict(self.scope).values())
                and parsed_consumer[4] in {"s6", "s8"}
            )
            key = self.binding_key(consumer, signal_id)
            with self.connect() as conn, conn.transaction():
                conn.execute("SET LOCAL lock_timeout='50ms'")
                conn.execute("SET LOCAL statement_timeout='100ms'")
                store = BusinessState(lambda: nullcontext(conn))
                profile_key = policy_key(self.scope)
                if self.policy is None:
                    profile = store.read(profile_key)
                    try:
                        _require(profile is None or not profile.deleted)
                        policy = (
                            JevEntryGatePolicy(**json.loads(profile.payload_json))
                            if profile is not None
                            else JevEntryGatePolicy()
                        )
                        _require(
                            not policy.enabled
                            or (
                                policy.max_wait_ms >= self.interval_seconds * 2000
                                and policy.cache_ttl_ms >= policy.max_wait_ms
                            )
                        )
                    except (ValueError, TypeError):
                        return _skip(decision, "JEV_GATE_POLICY_INVALID")
                    policy_version = profile.version if profile is not None else 0
                    policy_source = "ACCOUNT_PG"
                else:
                    policy = self.policy
                    policy_version, policy_source = None, "EXPLICIT_OVERRIDE"
                bound = store.read(key)
                if not policy.enabled:
                    # OFF never creates work or reads provider control. A
                    # previously pending enabled candidate is retired instead
                    # of being silently reopened under the changed policy.
                    if bound is not None:
                        return _skip(decision, "JEV_CANDIDATE_POLICY_CHANGED")
                    assessment = evaluate_entry_gate(
                        decision,
                        symbol=None,
                        public_features=None,
                        advisory=None,
                        now_ms=None,
                        started_at_ms=None,
                        deadline_ms=None,
                        signal_expires_at_ms=None,
                        policy=policy,
                    )
                    return replace(
                        assessment,
                        evidence_json=canonical(
                            {
                                **json.loads(assessment.evidence_json),
                                "gate_policy_source": policy_source,
                                "gate_policy_version": policy_version,
                                "gate_policy_state_id": profile_key.identity,
                            }
                        ),
                    )
                # OFF is transparent to legacy-valid admission. JEV-only
                # evidence/context contracts are checked ONLY after the scoped
                # switch is known enabled, never while merely discovering OFF.
                original = json.loads(decision.features_json)
                if (
                    original.get("admission_authorized") is not True
                    or original.get("execution_authorized") is not False
                ):
                    return _skip(decision, "JEV_ORIGINAL_ADMISSION_INVALID")
                _require(
                    decision.side == {"s6": "BUY", "s8": "SELL"}[parsed_consumer[4]]
                    and config["strategy"] == parsed_consumer[4].upper()
                    and config["account_scope"] == asdict(self.scope)
                    and config["mode"] == "TESTNET_ADMISSION_ONLY"
                    and context["account_scope"] == asdict(self.scope)
                    and context["environment"] == "SANDBOX"
                    and context["symbol"] == signal["symbol"]
                )
                direction = "LONG" if decision.side == "BUY" else "SHORT"
                for value in (
                    signal["observed_at"],
                    signal["expires_at_ms"],
                    context["assembled_at"],
                    context["valid_until_ms"],
                ):
                    _integer(value, 0, 2**53 - 1)
                now = conn.execute(
                    "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint"
                ).fetchone()[0]
                _integer(now, 0, 2**53 - 1)
                _require(
                    signal["observed_at"] <= now < signal["expires_at_ms"]
                    and context["assembled_at"] <= now < context["valid_until_ms"]
                )
                features = public_features(
                    context["sources"]["s3"],
                    target=signal["symbol"],
                    direction=direction,
                    context=context,
                    now_ms=now,
                )
                from v2_core.signal_fusion import (
                    validate_fusion,
                    validated_public_features,
                )

                fusion = validate_fusion(
                    signal,
                    source=config["source"],
                    proof=context.get("signal_fusion"),
                    now_ms=now,
                )
                if fusion is not None:
                    features.update(validated_public_features(fusion, now))
                identity = {
                    "consumer": consumer,
                    "signal_id": signal_id,
                    "signal_digest": digest(canonical(signal)),
                    "config_digest": digest(canonical(config)),
                    "symbol": signal["symbol"],
                    "direction": direction,
                    "gate_policy": asdict(policy),
                    "gate_policy_source": policy_source,
                    "gate_policy_version": policy_version,
                }
                control_payload = self._read(store, CONTROL_KEY)
                control = (
                    AdvisoryPolicy(**control_payload)
                    if control_payload is not None
                    else AdvisoryPolicy()
                )
                if not control.enabled:
                    return _skip(decision, "JEV_RESULT_UNAVAILABLE")
                if control.cache_ttl_ms < policy.max_wait_ms:
                    return _skip(decision, "JEV_MODEL_TTL_TOO_SHORT")
                effective_policy = replace(
                    policy,
                    max_age_ms=min(policy.max_age_ms, control.max_age_ms),
                    cache_ttl_ms=min(
                        policy.cache_ttl_ms,
                        control.cache_ttl_ms,
                        control.max_age_ms,
                    ),
                )
                identity["model_policy"] = asdict(control)
                if bound is None:
                    try:
                        prepared = prepare_request(
                            features, now_ms=now, max_age_ms=effective_policy.max_age_ms
                        )
                    except ValueError as exc:
                        if str(exc) == "JEV_MARKET_SNAPSHOT_EXPIRED":
                            return _skip(decision, "JEV_MARKET_SNAPSHOT_EXPIRED")
                        raise
                    # The original ACCOUNT context expires quickly, but every
                    # next attempt must obtain fresh risk admission. Only the
                    # frozen PUBLIC model snapshot bounds this review window.
                    deadline = min(
                        now + effective_policy.max_wait_ms,
                        signal["expires_at_ms"],
                        prepared["valid_until_ms"],
                        fusion["valid_until_ms"]
                        if fusion is not None
                        and fusion.get("valid_until_ms") is not None
                        else signal["expires_at_ms"],
                    )
                    payload = {
                        "schema": _SCHEMA,
                        **identity,
                        "public_features": prepared["request"]["state"],
                        "request_key": prepared["request_key"],
                        "started_at_ms": now,
                        "deadline_ms": deadline,
                    }
                    written = store.change(
                        key,
                        expected_version=0,
                        request_key="bind:" + str(uuid4()),
                        payload=payload,
                        reason="JEV fixed candidate review binding",
                    )
                    _require(written.code in {"APPLIED", "STALE"})
                    bound = store.read(key)
                _require(bound is not None and not bound.deleted and bound.version == 1)
                binding = json.loads(bound.payload_json)
                _require(
                    type(binding) is dict
                    and set(binding)
                    == {
                        "schema",
                        *identity,
                        "public_features",
                        "request_key",
                        "started_at_ms",
                        "deadline_ms",
                    }
                    and binding["schema"] == _SCHEMA
                    and all(binding[name] == value for name, value in identity.items())
                )
                prepared = prepare_request(
                    binding["public_features"],
                    now_ms=binding["started_at_ms"],
                    max_age_ms=effective_policy.max_age_ms,
                )
                _require(prepared["request_key"] == binding["request_key"])
                advisory = None
                request_key = StateKey(
                    **asdict(PUBLIC_SCOPE),
                    namespace=REQUEST_NAMESPACE,
                    key=prepared["request_key"],
                )
                record = self._read(store, request_key)
                if record is not None:
                    _require(
                        type(record) is dict
                        and record.get("schema") == "jev-pg-coordinator-v1"
                        and record.get("request") == prepared["request"]
                        and record.get("status") in {"CLAIMED", "COMPLETED"}
                    )
                    _require(str(UUID(record["claim_id"])) == record["claim_id"])
                    _integer(record["claimed_at_ms"], prepared["observed_at_ms"], now)
                    _integer(
                        record["deadline_ms"],
                        record["claimed_at_ms"] + 1,
                        min(record["claimed_at_ms"] + 2000, prepared["valid_until_ms"]),
                    )
                    _require(record["reserved_input_tokens"] == RESERVE_TOKENS)
                    if record["status"] == "COMPLETED":
                        advisory = record.get("result")
                        _require(type(advisory) is dict)
                    else:
                        _integer(record["deadline_ms"], 0, 2**53 - 1)
                        if now >= record["deadline_ms"]:
                            # A crashed worker or failed publication is not a
                            # proven provider timeout. Do not turn a possibly
                            # rejected/corrupt result into baseline authority.
                            advisory = {
                                "status": "FALLBACK",
                                "reason": "MODEL_CLAIM_UNCERTAIN",
                            }
                        else:
                            # Queue age is not running-model age: a delayed
                            # claim keeps its own deadline and may still veto.
                            advisory = {
                                "status": "FALLBACK",
                                "reason": "MODEL_IN_FLIGHT",
                            }
                else:
                    exhaustion = self._budget_exhausted(store, now, control)
                    if exhaustion:
                        advisory = {"status": "FALLBACK", "reason": exhaustion}
                assessment = evaluate_entry_gate(
                    decision,
                    symbol=signal["symbol"],
                    public_features=binding["public_features"],
                    advisory=advisory,
                    now_ms=now,
                    started_at_ms=binding["started_at_ms"],
                    deadline_ms=binding["deadline_ms"],
                    signal_expires_at_ms=signal["expires_at_ms"],
                    policy=effective_policy,
                    baseline_valid_until_ms=context["valid_until_ms"],
                )
                evidence = {
                    **json.loads(assessment.evidence_json),
                    "binding_state_id": key.identity,
                    "binding_version": bound.version,
                    "gate_policy_source": policy_source,
                    "gate_policy_version": policy_version,
                    "gate_policy_state_id": profile_key.identity
                    if policy_source == "ACCOUNT_PG"
                    else None,
                    "public_snapshot_policy": "FIXED_MODEL_SNAPSHOT_FRESH_ACCOUNT_RECHECK",
                    "baseline_context": {
                        "assembled_at_ms": context["assembled_at"],
                        "valid_until_ms": context["valid_until_ms"],
                        "digest": digest(canonical(context)),
                    },
                }
                assessment = replace(assessment, evidence_json=canonical(evidence))
                queue = assessment.status == "PENDING" and record is None
            # Only after commit: another attempt or restart finds the SAME
            # snapshot/deadline. Duplicate enqueue cannot duplicate provider
            # calls; the existing PG coordinator owns that atomic claim.
            if queue:
                self.reference.submit(binding["public_features"])
            return assessment
        except _InvalidBinding:
            return _skip(decision, "JEV_CANDIDATE_BINDING_INVALID")
        except Exception:  # noqa: BLE001 - no DB/provider error text or global block
            return _skip(decision, "JEV_REVIEW_UNAVAILABLE")
