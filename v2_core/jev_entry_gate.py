"""Pure experimental second filter for the opt-in Testnet entry path.

The caller first runs the existing admission/risk checks. This component can
only retain that OPEN candidate, veto it, or defer/skip that one signal. An
explicit BASELINE availability policy can retain the freshly validated OPEN
with a separate FALLBACK status; this is never model approval. Neither PASS nor
FALLBACK is an execution permit. It never changes quantity/leverage, submits work, reads
state, waits, or performs I/O. Existing submit-time risk checks remain mandatory.

The caller must durably bind symbol, direction, public snapshot, policy and the
first started/deadline pair to the same candidate. Reusing this pure function
does not itself prevent a caller from resetting that binding on every retry.
PENDING has no decision; the existing scheduler must revisit it without holding
the trading thread. SKIP means terminally skip this signal, NOT bypass JEV.

Thresholds are explicitly experimental, not calibrated win probabilities.
Liquidity hypotheses are deliberately unused, even when a model returns a
number: a measured spread alone does not provide real order-book depth.
"""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from v2_core.evidence import canonical
from v2_core.jev_advisory import (
    MODEL,
    PROMPT_VERSION,
    _cached,
    _decimal,
    _integer,
    _result,
    prepare_request,
)
from v2_core.strategy import StrategyDecision

SCHEMA = "jev-entry-gate-v1"
_MAX_TIMESTAMP = 2**53 - 1
_PENDING_REASONS = frozenset(
    {
        "CACHE_MISS",
        "BUSY",
        "CONTROL_UNAVAILABLE",
        "CONTROL_STALE",
        "LOCAL_CACHE_BUSY",
        "MODEL_IN_FLIGHT",
    }
)
_TERMINAL_REASONS = frozenset(
    {
        "BUDGET_EXHAUSTED",
        "MODEL_UNAVAILABLE",
        "MODEL_TIMEOUT",
        "TOKEN_RESERVATION_EXCEEDED",
        "DEADLINE_EXPIRED",
        "COORDINATOR_UNAVAILABLE",
        "PUBLICATION_UNAVAILABLE",
        "CLOCK_INVALID",
        "CACHE_UNAVAILABLE",
        "INVALID_OR_STALE_MARKET",
        "REFERENCE_UNAVAILABLE",
        "DISABLED",
        "STOPPED",
        "CONTROL_CHANGED",
        "MODEL_RESPONSE_INVALID",
        "MODEL_UNKNOWN_FAILURE",
        "MODEL_LATE_RESPONSE",
        "MODEL_CLAIM_UNCERTAIN",
        "REQUEST_BUDGET_EXHAUSTED",
        "TOKEN_BUDGET_EXHAUSTED",
        "TOKEN_BUDGET_PACED",
        "PROVIDER_REQUEST_REJECTED",
        "MODEL_CONFIGURATION_UNAVAILABLE",
    }
)
_BASELINE_REASONS = frozenset(
    {
        "BUDGET_EXHAUSTED",
        "REQUEST_BUDGET_EXHAUSTED",
        "TOKEN_BUDGET_EXHAUSTED",
        "TOKEN_BUDGET_PACED",
        "MODEL_UNAVAILABLE",
        "MODEL_TIMEOUT",
        "DEADLINE_EXPIRED",
        "PROVIDER_REQUEST_REJECTED",
        "MODEL_CONFIGURATION_UNAVAILABLE",
    }
)
BASELINE_FALLBACK_CAUSES = _BASELINE_REASONS | {"MODEL_QUEUE_WAIT_EXHAUSTED"}


@dataclass(frozen=True)
class JevEntryGatePolicy:
    """Opt-in test thresholds, not evidence that JEV improves trading results."""

    enabled: bool = False
    min_trend_support: str = "0.6"
    max_chase_risk: str = "0.7"
    max_wait_ms: int = 5000
    max_age_ms: int = 60000
    cache_ttl_ms: int = 30000
    unavailable_action: str = "SKIP"
    unavailable_wait_ms: int = 1500

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("JEV_GATE_EXPLICIT_SWITCH_REQUIRED")
        for name in ("min_trend_support", "max_chase_risk"):
            value = getattr(self, name)
            if type(value) is not str:
                raise ValueError("JEV_GATE_DECIMAL_THRESHOLD_REQUIRED")
            object.__setattr__(self, name, _decimal(value, "0", "1"))
        _integer(self.max_wait_ms, 100, 30000)
        _integer(self.max_age_ms, 1000, 300000)
        _integer(self.cache_ttl_ms, 1000, self.max_age_ms)
        if self.unavailable_action not in {"SKIP", "BASELINE"}:
            raise ValueError("JEV_UNAVAILABLE_ACTION_INVALID")
        _integer(self.unavailable_wait_ms, 100, self.max_wait_ms)


@dataclass(frozen=True)
class GateAssessment:
    """No OPEN decision exists for PENDING, VETO or SKIP assessments."""

    status: str
    reason: str
    decision: StrategyDecision | None
    evidence_json: str


def evaluate_entry_gate(
    decision,
    *,
    symbol,
    public_features,
    advisory,
    now_ms,
    started_at_ms,
    deadline_ms,
    signal_expires_at_ms,
    policy=None,
    baseline_valid_until_ms=None,
):
    """Evaluate one fixed candidate; model failure is never model approval.

    Default SKIP stays fail-closed. Explicit BASELINE permits only enumerated
    availability failures and a fresh baseline deadline supplied by the scoped
    context adapter. Invalid data, unknown failures and model vetoes stay closed.

    ``advisory`` must come from the trusted coordinator/reference, not arbitrary
    user input. Exact schema, model, prompt, digest, direction and TTL are still
    revalidated here. A digest proves binding, not the origin/authenticity of an
    otherwise forged model response. No model scores alter a hard-risk reject.
    """
    if not isinstance(decision, StrategyDecision):
        raise TypeError("JEV_GATE_ADMISSION_DECISION_REQUIRED")
    if decision.action != "OPEN":
        return GateAssessment(
            "VETO",
            "PREEXISTING_REJECTION",
            decision,
            canonical(
                {
                    "schema": SCHEMA,
                    "status": "VETO",
                    "reason": "PREEXISTING_REJECTION",
                    "execution_authorized": False,
                    "risk_override": False,
                }
            ),
        )
    policy = JevEntryGatePolicy() if policy is None else policy
    if not isinstance(policy, JevEntryGatePolicy):
        raise TypeError("JEV_GATE_POLICY_REQUIRED")
    prepared, checked, timing = None, None, None
    fallback_cause = None

    def result(status, reason):
        evidence = {
            "schema": SCHEMA,
            "status": status,
            "reason": reason,
            "fallback_cause": fallback_cause,
            "policy": asdict(policy),
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "request_key": prepared["request_key"] if prepared else None,
            "timing": timing,
            # Freeze the last usable instant into the OPEN decision. A PASS
            # checked just before cache expiry must not acquire a fresh order
            # lifetime while waiting for capacity or dispatch.
            "valid_until_ms": min(
                deadline_ms,
                signal_expires_at_ms,
                prepared["valid_until_ms"],
                checked["expires_at_ms"],
            )
            if status == "PASS" and policy.enabled and checked is not None
            else min(signal_expires_at_ms, baseline_valid_until_ms)
            if status == "FALLBACK"
            else None,
            "model_expires_at_ms": checked["expires_at_ms"] if checked else None,
            "hypotheses": checked["hypotheses"] if checked else None,
            "liquidity_use": "NOT_USED",
            "threshold_semantics": "EXPERIMENTAL_UNCALIBRATED_FILTER",
            "probability_semantics": "NOT_TRADE_WIN_RATE",
            "decision_effect": "ADMISSION_FILTER_ONLY",
            "execution_authorized": False,
            "risk_override": False,
        }
        evidence_json = canonical(evidence)
        selected = (
            decision
            if status in {"PASS", "FALLBACK"}
            else None
            if status == "PENDING"
            else StrategyDecision(
                "IGNORED",
                reason,
                features_json=canonical(
                    {
                        "original_admission": json.loads(decision.features_json),
                        "jev_entry_gate": evidence,
                        "admission_authorized": False,
                        "execution_authorized": False,
                    }
                ),
            )
        )
        return GateAssessment(status, reason, selected, evidence_json)

    def fallback(reason):
        nonlocal fallback_cause
        if policy.unavailable_action != "BASELINE":
            return result("SKIP", "JEV_RESULT_UNAVAILABLE")
        _integer(baseline_valid_until_ms, 0, _MAX_TIMESTAMP)
        if not now_ms < min(signal_expires_at_ms, baseline_valid_until_ms):
            return result("SKIP", "JEV_BASELINE_CONTEXT_EXPIRED")
        fallback_cause = reason
        return result("FALLBACK", "JEV_BASELINE_FALLBACK")

    if not policy.enabled:
        return result("PASS", "JEV_GATE_DISABLED")
    try:
        original = json.loads(decision.features_json)
        if (
            not isinstance(original, dict)
            or original.get("admission_authorized") is not True
            or original.get("execution_authorized") is not False
        ):
            return result("SKIP", "JEV_ORIGINAL_ADMISSION_INVALID")
        for value in (now_ms, started_at_ms, deadline_ms, signal_expires_at_ms):
            _integer(value, 0, _MAX_TIMESTAMP)
        if not started_at_ms <= now_ms or not (
            started_at_ms < deadline_ms <= signal_expires_at_ms
            and deadline_ms <= started_at_ms + policy.max_wait_ms
        ):
            return result("SKIP", "JEV_FIXED_DEADLINE_INVALID")
        prepared = prepare_request(
            public_features, now_ms=started_at_ms, max_age_ms=policy.max_age_ms
        )
        state = prepared["request"]["state"]
        if (
            state["symbol"] != symbol
            or state["direction"]
            != {
                "BUY": "LONG",
                "SELL": "SHORT",
            }[decision.side]
        ):
            return result("SKIP", "JEV_CANDIDATE_BINDING_MISMATCH")
        if deadline_ms > prepared["valid_until_ms"]:
            return result("SKIP", "JEV_FIXED_DEADLINE_INVALID")
        timing = {
            "started_at_ms": started_at_ms,
            "deadline_ms": deadline_ms,
            "checked_at_ms": now_ms,
            "signal_expires_at_ms": signal_expires_at_ms,
        }
        if now_ms >= signal_expires_at_ms:
            return result("SKIP", "JEV_REVIEW_DEADLINE_EXPIRED")
        # Only explicit client/provider availability failures or bounded missing work
        # may use the freshly revalidated baseline. Corrupt/late model results
        # never take this branch. The original model deadline remains recorded.
        if policy.unavailable_action == "BASELINE":
            if advisory is None:
                if now_ms - started_at_ms >= policy.unavailable_wait_ms:
                    return fallback("MODEL_QUEUE_WAIT_EXHAUSTED")
            elif type(advisory) is dict and advisory.get("status") == "FALLBACK":
                reason = advisory.get("reason")
                if set(advisory) != {"status", "reason"}:
                    expected = _result(
                        "FALLBACK",
                        reason,
                        prepared,
                        now_ms=advisory.get("produced_at_ms"),
                        expires_at_ms=advisory.get("expires_at_ms"),
                    )
                    if advisory != expected:
                        return result("SKIP", "JEV_RESULT_INVALID")
                    _integer(advisory["produced_at_ms"], started_at_ms, now_ms)
                    _integer(
                        advisory["expires_at_ms"],
                        advisory["produced_at_ms"],
                        _MAX_TIMESTAMP,
                    )
                if reason in _BASELINE_REASONS:
                    return fallback(reason)
                if (
                    reason in {"CACHE_MISS", "BUSY", "LOCAL_CACHE_BUSY"}
                    and now_ms - started_at_ms >= policy.unavailable_wait_ms
                ):
                    return fallback("MODEL_QUEUE_WAIT_EXHAUSTED")
        if now_ms >= deadline_ms:
            return result("SKIP", "JEV_REVIEW_DEADLINE_EXPIRED")
        if advisory is None:
            return result("PENDING", "JEV_RESULT_PENDING")
        if type(advisory) is not dict:
            return result("SKIP", "JEV_RESULT_INVALID")
        if advisory.get("status") != "ADVISORY_READY":
            # Non-ready records have no model authority. Any explicit baseline
            # availability policy was handled above; never mislabel it PASS.
            if advisory.get("status") == "FALLBACK":
                reason = advisory.get("reason")
                if reason in _PENDING_REASONS:
                    return result("PENDING", "JEV_RESULT_PENDING")
                if reason in _TERMINAL_REASONS:
                    return result("SKIP", "JEV_RESULT_UNAVAILABLE")
            if advisory.get("status") == "OFF":
                return result("SKIP", "JEV_RESULT_UNAVAILABLE")
            return result("SKIP", "JEV_RESULT_INVALID")
        checked = _cached(advisory, prepared, now_ms, policy.cache_ttl_ms)
        scores = checked["hypotheses"]
        if Decimal(scores["trend_supported"]) < Decimal(policy.min_trend_support):
            return result("VETO", "JEV_TREND_UNSUPPORTED")
        if Decimal(scores["chase_risk"]) > Decimal(policy.max_chase_risk):
            return result("VETO", "JEV_CHASE_RISK_HIGH")
        return result("PASS", "JEV_SECOND_FILTER_PASSED")
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return result("SKIP", "JEV_RESULT_INVALID")
