"""Pure typed-consumer contracts and fault transitions; no PostgreSQL or venue.

The fake persistence port makes faults observable, not database durability claims.
Real freeze/receipt/locking and scheduler evidence is tested separately with PG.
"""

import json
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_plan_admission import contracts

from v2_core import entry_controls, liquidity_controls
from v2_core.account_risk import AccountRiskDenied
from v2_core.evidence import canonical, digest
from v2_core.strategy import StrategyScope
from v2_core.strategy_entry_worker import (
    EXPIRED_REASON,
    EntryReceipt,
    EntrySignalState,
    FrozenEntryCandidate,
    TypedEntryWorker,
)
from v2_core.strategy_execution_context import execution_context
from v2_core.strategy_plan_admission import (
    StrategyPlanAdmissionError,
    opportunity_intent_id,
)
from v2_core.strategy_signal_admission import DUPLICATE_REASON


def candidate_scene(*, action="OPEN", side="BUY"):
    registry, binding, plan = contracts(now=1000)
    if side == "SELL":
        plan = replace(plan, side="SELL", stop_price="108")
    signal_id = str(uuid4())
    snapshot = {
        "observed_at": plan.observed_at_ms,
        "expires_at_ms": plan.expires_at_ms,
        "symbol": plan.symbol,
        "signal": "TREND_UP_LONG" if side == "BUY" else "TREND_DOWN_SHORT",
        "features": {"strength": 71},
    }

    def controls(module):
        values = module.resolve({})
        return {
            "version": 0,
            "values": values,
            "digest": digest(
                canonical({"scope": asdict(plan.scope), "version": 0, "values": values})
            ),
        }

    context = execution_context(
        plan,
        entry_controls=controls(entry_controls),
        liquidity_controls=controls(liquidity_controls),
        policy={
            "entry.reentry_enabled": False,
            "entry.price_guard_enabled": False,
            "entry.max_adverse_price_fraction": "0.01",
            "entry.max_stop_risk_increase": "1.10",
        },
    )
    candidate = FrozenEntryCandidate(
        binding=binding,
        signal_id=signal_id,
        source="tv_bridge",
        signal_digest=digest(canonical(snapshot)),
        action=action,
        rationale="QA_FROZEN_OPEN" if action == "OPEN" else "QA_FROZEN_REJECTION",
        observed_at_ms=plan.observed_at_ms,
        decided_at_ms=plan.decided_at_ms,
        expires_at_ms=plan.expires_at_ms,
        features_json=canonical({"execution": context}) if action == "OPEN" else "{}",
        plan=plan if action == "OPEN" else None,
    )
    state = EntrySignalState(
        signal_id, "tv_bridge", "SANDBOX", canonical(snapshot), 1000, candidate
    )
    return registry, candidate, state


@pytest.fixture
def scene():
    def build(*, action="OPEN", side="BUY"):
        registry, candidate, state = candidate_scene(action=action, side=side)
        case = SimpleNamespace(
            registry=registry,
            candidate=candidate,
            state=state,
            events=[],
            admissions=[],
            preparations=[],
            terminals=[],
        )
        case.intent_id = (
            opportunity_intent_id(candidate.plan) if candidate.plan else str(uuid4())
        )

        def read(_conn, **identity):
            case.events.append("read")
            assert identity == {
                "scope": case.worker.scope,
                "source": "tv_bridge",
                "signal_id": state.signal_id,
            }
            return case.state

        def terminal(**request):
            case.events.append("terminal")
            case.terminals.append(request)
            assert request["candidate_digest"] == case.state.candidate.candidate_digest
            case.state = replace(
                case.state,
                receipt=EntryReceipt(
                    request["outcome"], request["reason"], decision_id=str(uuid4())
                ),
                candidate=None,
            )

        def admit(plan, binding, **request):
            case.events.append("admit")
            case.admissions.append((plan, binding, request))
            case.state = replace(
                case.state,
                receipt=EntryReceipt("INTENT", "signal admitted", case.intent_id),
                candidate=None,
            )
            return {
                "status": "ACCEPTED",
                "intent_id": case.intent_id,
                "plan_digest": plan.plan_digest,
                "execution_authorized": False,
            }

        def recover(intent_id):
            case.events.append("prepare")
            case.preparations.append(intent_id)
            return {
                "status": "PREPARED",
                "intent_id": intent_id,
                "order_id": "qa-original-order",
                "execution_authorized": False,
            }

        case.store = SimpleNamespace(read=read, terminal=terminal)
        case.admission = SimpleNamespace(admit=admit)
        case.preparation = SimpleNamespace(recover=recover)
        case.worker = TypedEntryWorker(
            lambda: nullcontext(object()),
            registry,
            scope=StrategyScope(
                **asdict(candidate.binding.scope),
                producer=candidate.binding.instance_id,
            ),
            source="tv_bridge",
            store=case.store,
            clock_ms=lambda: 999999999,
            admission=case.admission,
            preparation=case.preparation,
        )
        case.consume = lambda: case.worker.consume(state.signal_id, context={})
        return case

    return build


def test_candidate_roundtrip_is_detached_immutable_and_not_execution_authority():
    _, candidate, _ = candidate_scene()
    original = candidate.payload()
    restored = FrozenEntryCandidate.from_payload(json.loads(canonical(original)))
    assert (
        restored == candidate
        and restored.candidate_digest == candidate.candidate_digest
    )
    assert restored is not candidate and restored.binding is not candidate.binding
    assert not candidate.execution_authorized
    with pytest.raises(FrozenInstanceError):
        candidate.rationale = "changed"
    payload = candidate.payload()
    payload["binding"]["scope"]["account_id"] = "another-account"
    candidate.features["execution"]["policy"]["entry.reentry_enabled"] = True
    assert candidate.payload() == original


@pytest.mark.parametrize(
    "change",
    [
        {"action": "WAIT"},
        {"action": "IGNORED"},
        {"plan": None},
        {"signal_id": "bad"},
        {"source": "unknown"},
        {"signal_digest": "x" * 64},
        {"schema_version": "future"},
        {"rationale": ""},
        {"rationale": "x" * 2049},
        {"observed_at_ms": True},
        {"decided_at_ms": 0},
        {"expires_at_ms": 900},
        {"features_json": "{}"},
        {"features_json": '{"secret":"forbidden"}'},
        {"features_json": '{"score":0.1}'},
        {"features_json": " " * 65537},
    ],
)
def test_invalid_candidate_is_rejected_before_ports(change):
    _, candidate, _ = candidate_scene()
    with pytest.raises((ValueError, TypeError)):
        replace(candidate, **change)


@pytest.mark.parametrize("mode", ["SHADOW", "OFF", "DRAINING"])
def test_research_or_inactive_binding_cannot_be_an_execution_candidate(mode):
    _, candidate, _ = candidate_scene()
    with pytest.raises(ValueError, match="CONTRACT_INVALID"):
        replace(candidate, binding=replace(candidate.binding, mode=mode))


@pytest.mark.parametrize(
    "field,value",
    [
        ("code_version", "entry-v2"),
        ("deployment_generation", 2),
        ("instance_id", "different"),
        ("exit_handler", "different"),
    ],
)
def test_plan_binding_mismatch_rejected(field, value):
    _, candidate, _ = candidate_scene()
    with pytest.raises(ValueError, match="PLAN_MISMATCH"):
        replace(candidate, plan=replace(candidate.plan, **{field: value}))


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_frozen_open_admits_then_reads_durable_receipt_then_prepares(scene, side):
    case = scene(side=side)
    result = case.consume()
    assert result["status"] == "PREPARED" and result["execution_authorized"] is False
    assert case.events == ["read", "admit", "read", "prepare"]
    assert case.admissions[0] == (
        case.candidate.plan,
        case.candidate.binding,
        {
            "signal_id": case.state.signal_id,
            "rationale": case.candidate.rationale,
            "features": case.candidate.features,
        },
    )
    assert case.admissions[0][0].side == side
    assert case.preparations == [case.intent_id] and not case.terminals
    assert case.worker.requires_audited_expiry is True
    assert case.worker.entries_enabled is False


def test_retry_starts_at_original_receipt_without_admission_or_candidate(scene):
    case = scene()
    first = case.consume()
    case.events.clear()
    second = case.consume()
    assert second == first
    assert case.events == ["read", "prepare"]
    assert len(case.admissions) == 1 and len(set(case.preparations)) == 1


@pytest.mark.parametrize("outcome", ["IGNORED", "EXPIRED"])
def test_terminal_receipt_is_returned_without_evaluation_or_preparation(scene, outcome):
    case = scene()
    case.state = replace(
        case.state,
        candidate=None,
        receipt=EntryReceipt(outcome, "QA_PROVEN_TERMINAL", decision_id=str(uuid4())),
    )
    result = case.consume()
    assert (
        result["status"] == outcome
        and result["decision_id"] == case.state.receipt.decision_id
    )
    assert case.events == ["read"]


@pytest.mark.parametrize("now", [0, 1000, 999999999])
def test_missing_frozen_candidate_never_invents_terminal_version(scene, now):
    case = scene()
    case.state = replace(case.state, candidate=None, now_ms=now)
    assert case.consume()["reason"] == "STRATEGY_FROZEN_CANDIDATE_REQUIRED"
    assert case.events == ["read"] and not case.terminals


def test_future_candidate_waits_without_extending_deadline(scene):
    case = scene()
    case.state = replace(case.state, now_ms=case.candidate.decided_at_ms - 1)
    assert case.consume()["reason"] == "STRATEGY_CANDIDATE_NOT_YET_CURRENT"
    assert case.events == ["read"]


@pytest.mark.parametrize("expired", [False, True])
def test_frozen_rejection_commits_then_rereads_terminal(scene, expired):
    case = scene(action="IGNORED")
    if expired:
        case.state = replace(case.state, now_ms=case.candidate.expires_at_ms)
    result = case.consume()
    assert result["status"] == ("EXPIRED" if expired else "IGNORED")
    assert case.events == ["read", "terminal", "read"]
    assert case.terminals[0]["reason"] == (
        EXPIRED_REASON if expired else case.candidate.rationale
    )
    assert not case.admissions and not case.preparations


def test_terminal_callback_return_does_not_prove_commit(scene):
    case = scene(action="IGNORED")
    case.store.terminal = lambda **_: {"status": "IGNORED"}
    with pytest.raises(ValueError, match="DURABLE_RECEIPT_REQUIRED"):
        case.consume()
    assert not case.preparations


def test_expired_open_checks_permanent_duplicate_before_expiring(scene):
    case = scene()
    case.state = replace(case.state, now_ms=case.candidate.expires_at_ms)

    def duplicate(*_args, **_kwargs):
        case.events.append("duplicate")
        case.state = replace(
            case.state,
            candidate=None,
            receipt=EntryReceipt("IGNORED", DUPLICATE_REASON),
        )
        return {
            "status": "IGNORED",
            "reason": DUPLICATE_REASON,
            "winner_intent_id": str(uuid4()),
            "execution_authorized": False,
        }

    case.admission.admit = duplicate
    assert case.consume()["reason"] == DUPLICATE_REASON
    assert case.events == ["read", "duplicate", "read"]
    assert not case.terminals and not case.preparations


def test_only_proven_not_current_open_can_be_expired(scene):
    case = scene()
    case.state = replace(case.state, now_ms=case.candidate.expires_at_ms)

    def no_longer_current(*_args, **_kwargs):
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_NOT_CURRENT")

    case.admission.admit = no_longer_current
    assert case.consume()["status"] == "EXPIRED"
    assert len(case.terminals) == 1 and not case.preparations


@pytest.mark.parametrize(
    "error",
    [
        StrategyPlanAdmissionError("STRATEGY_ADMISSION_OUTCOME_UNKNOWN"),
        StrategyPlanAdmissionError("STRATEGY_ADMISSION_INTENT_CONFLICT"),
        AccountRiskDenied("STRATEGY_ENTRY_DISABLED"),
        AccountRiskDenied("STRATEGY_OPPORTUNITY_RECEIPT_INVALID"),
        ValueError("STRATEGY_ADMISSION_NOT_CURRENT"),
        TimeoutError("commit result unknown"),
    ],
)
def test_ambiguous_invalid_or_disabled_outcome_never_terminalized(scene, error):
    case = scene()
    case.state = replace(case.state, now_ms=case.candidate.expires_at_ms)

    def fail(*_args, **_kwargs):
        raise error

    case.admission.admit = fail
    with pytest.raises(type(error), match=str(error)):
        case.consume()
    assert not case.terminals and not case.preparations


def test_not_current_before_deadline_remains_unknown_not_expired(scene):
    case = scene()

    def fail(*_args, **_kwargs):
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_NOT_CURRENT")

    case.admission.admit = fail
    with pytest.raises(StrategyPlanAdmissionError):
        case.consume()
    assert not case.terminals


def test_admission_commit_lost_ack_is_recovered_without_reproposal(scene):
    case = scene()
    admit = case.admission.admit

    def lost_ack(*args, **kwargs):
        admit(*args, **kwargs)
        raise TimeoutError("lost acknowledgement")

    case.admission.admit = lost_ack
    with pytest.raises(TimeoutError):
        case.consume()
    assert not case.preparations
    assert case.consume()["status"] == "PREPARED"
    assert len(case.admissions) == 1 and case.preparations == [case.intent_id]


def test_preparation_lost_ack_retries_same_receipted_identity(scene):
    case = scene()
    recover = case.preparation.recover
    calls = []

    def lost_ack(intent_id):
        calls.append(intent_id)
        if len(calls) == 1:
            raise TimeoutError("unknown preparation commit")
        return recover(intent_id)

    case.preparation.recover = lost_ack
    with pytest.raises(TimeoutError):
        case.consume()
    assert case.consume()["status"] == "PREPARED"
    assert calls == [case.intent_id, case.intent_id] and len(case.admissions) == 1


def test_negative_commit_lost_ack_is_read_without_second_terminal_write(scene):
    case = scene(action="IGNORED")
    terminal = case.store.terminal

    def lost_ack(**kwargs):
        terminal(**kwargs)
        raise TimeoutError("negative commit acknowledgement lost")

    case.store.terminal = lost_ack
    with pytest.raises(TimeoutError):
        case.consume()
    assert case.consume()["status"] == "IGNORED"
    assert len(case.terminals) == 1


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"status": "UNKNOWN", "execution_authorized": False},
        {
            "status": "ACCEPTED",
            "intent_id": str(uuid4()),
            "execution_authorized": False,
        },
        {
            "status": "IGNORED",
            "reason": DUPLICATE_REASON,
            "intent_id": str(uuid4()),
            "execution_authorized": False,
        },
        {"status": "ACCEPTED", "execution_authorized": True},
    ],
)
def test_bad_admission_results_do_not_prepare_any_identity(scene, result):
    case = scene()
    case.admission.admit = lambda *_args, **_kwargs: result
    with pytest.raises(ValueError):
        case.consume()
    assert not case.preparations and not case.terminals


def test_accepted_return_without_durable_receipt_is_not_preparation_authority(scene):
    case = scene()
    case.admission.admit = lambda *_args, **_kwargs: {
        "status": "ACCEPTED",
        "intent_id": case.intent_id,
        "plan_digest": case.candidate.plan.plan_digest,
        "execution_authorized": False,
    }
    with pytest.raises(ValueError, match="ADMISSION_RECEIPT_MISMATCH"):
        case.consume()
    assert not case.preparations


@pytest.mark.parametrize(
    "change",
    [
        {"status": "UNKNOWN"},
        {"intent_id": str(uuid4())},
        {"execution_authorized": True},
    ],
)
def test_invalid_preparation_result_cannot_be_reported_success(scene, change):
    case = scene()
    case.preparation.recover = lambda identity: {
        "status": "PREPARED",
        "intent_id": identity,
        "execution_authorized": False,
        **change,
    }
    with pytest.raises(ValueError, match="PREPARATION_RESULT_INVALID"):
        case.consume()


@pytest.mark.parametrize(
    "change",
    [
        {"signal_id": str(uuid4())},
        {"source": "s3"},
        {"environment": "LIVE"},
    ],
)
def test_foreign_store_scope_is_rejected(scene, change):
    case = scene()
    case.state = replace(case.state, **change)
    with pytest.raises(ValueError, match="STORE_SCOPE_MISMATCH"):
        case.consume()
    assert not case.admissions


@pytest.mark.parametrize(
    "change",
    [
        {"signal_digest": "a" * 64},
        {"signal_id": str(uuid4())},
        {"source": "s3"},
    ],
)
def test_candidate_signal_binding_mismatch_is_not_terminal(scene, change):
    case = scene()
    case.state = replace(case.state, candidate=replace(case.candidate, **change))
    with pytest.raises(ValueError, match="CANDIDATE_SIGNAL_MISMATCH"):
        case.consume()
    assert not case.admissions and not case.terminals


@pytest.mark.parametrize("offset", [-1, 1])
def test_plan_observation_may_follow_but_cannot_precede_bound_signal(scene, offset):
    case = scene()
    plan = replace(
        case.candidate.plan,
        observed_at_ms=case.candidate.observed_at_ms + offset,
    )
    features = case.candidate.features
    features["execution"]["plan_digest"] = plan.plan_digest
    candidate = replace(
        case.candidate,
        observed_at_ms=plan.observed_at_ms,
        plan=plan,
        features_json=canonical(features),
    )
    case.state = replace(case.state, candidate=candidate)
    if offset < 0:
        with pytest.raises(ValueError, match="CANDIDATE_SIGNAL_MISMATCH"):
            case.consume()
        assert not case.admissions
    else:
        assert case.consume()["status"] == "PREPARED"
        assert case.admissions[0][0] == plan


def test_scheduler_read_has_frozen_marker_not_legacy_open_decision(scene):
    case = scene()
    snapshot, frozen, receipt = case.worker._read(object(), case.state.signal_id)
    assert snapshot == case.state.snapshot
    assert frozen == ("TYPED_FROZEN_CANDIDATE", case.candidate.candidate_digest)
    assert receipt is None
    assert (
        case.worker.reject_candidate_context(case.state.signal_id, cause="failure")
        is None
    )
    assert not case.terminals


def test_no_fresh_context_can_override_persisted_candidate(scene):
    case = scene()
    with pytest.raises(ValueError, match="FROZEN_CONTEXT_ONLY"):
        case.worker.consume(case.state.signal_id, context={"new_price": "10000"})
    assert not case.events


def test_foreign_preparation_after_recovery_race_is_never_from_loser_plan(scene):
    case = scene()

    def lost_race(*_args, **_kwargs):
        case.state = replace(
            case.state,
            candidate=None,
            receipt=EntryReceipt("INTENT", "signal admitted", case.intent_id),
        )
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_NOT_CURRENT")

    case.admission.admit = lost_race
    assert case.consume()["status"] == "PREPARED"
    assert case.preparations == [case.intent_id] and not case.terminals
