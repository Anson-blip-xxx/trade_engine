"""Isolated-PG signal consumption; duplicate opportunities never authorize orders."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier
from uuid import uuid4, uuid5

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_strategy_plan_admission import (
    SCOPE,
    contracts,
    counts,
    generic_intent_without_claim,
    setup,
    signal,
)
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.intents import Admission, AdmissionCode, IntentStore
from v2_core.service import TradingData
from v2_core.signals import Signals
from v2_core.state import BusinessState
from v2_core.strategy_deployment import StrategyDeployments, deployment_snapshot
from v2_core.strategy_plan_admission import (
    StrategyPlanAdmission,
    StrategyPlanAdmissionError,
    opportunity_key,
    require_plan_admission,
)
from v2_core.strategy_signal_admission import (
    _DECISION_NAMESPACE,
    StrategySignalAdmission,
    _ignored_proof,
)

database = database_fixture
REASON = "STRATEGY_OPPORTUNITY_ALREADY_CLAIMED"


def adapter_setup(database, **kwargs):
    strict, registry, binding, plan = setup(database, **kwargs)
    return StrategySignalAdmission(database, registry), strict, registry, binding, plan


def receipt(database, binding, identity):
    with database() as conn:
        return conn.execute(
            "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts "
            "WHERE consumer=%s AND signal_id=%s",
            (binding.consumer, identity),
        ).fetchone()


def decision(database, binding, identity):
    with database() as conn:
        return conn.execute(
            "SELECT d.decision_id::text,d.action,d.side,d.quantity::text,"
            "d.evidence_ref,e.snapshot FROM v2_strategy_decisions d "
            "JOIN v2_decision_evidence e USING(evidence_ref) "
            "WHERE d.consumer=%s AND d.signal_id=%s",
            (binding.consumer, identity),
        ).fetchone()


def facts(database):
    with database() as conn:
        number = conn.execute("SELECT count(*) FROM v2_strategy_decisions").fetchone()[
            0
        ]
    return counts(database), number


def consume(adapter, plan, binding, identity, **changes):
    return adapter.admit(
        plan, binding, **{"signal_id": identity, "rationale": "QA", **changes}
    )


def same_signal_times(plan, reference):
    return replace(
        plan,
        observed_at_ms=reference.observed_at_ms,
        decided_at_ms=reference.decided_at_ms,
        expires_at_ms=reference.expires_at_ms,
    )


def claimed(database):
    adapter, strict, registry, binding, plan = adapter_setup(database)
    original = signal(database, plan)
    first = consume(adapter, plan, binding, original)
    assert first["status"] == "ACCEPTED"
    return adapter, strict, registry, binding, plan, original, first


def assert_no_opening_authority(database, result, winner):
    assert result["status"] == "IGNORED"
    assert result["reason"] == REASON
    assert result["execution_authorized"] is False
    assert result["winner_intent_id"] == winner["intent_id"]
    assert "intent_id" not in result
    assert counts(database)[0] == counts(database)[4] == 1
    assert counts(database)[5:] == (0, 0)


def test_first_signal_uses_original_admission_without_preparing_an_order(database):
    adapter, _, _, binding, plan = adapter_setup(database)
    identity = signal(database, plan)
    result = consume(adapter, plan, binding, identity)
    assert result["status"] == "ACCEPTED"
    assert result["signal_id"] == identity
    assert result["execution_authorized"] is False
    assert receipt(database, binding, identity) == (
        "INTENT",
        result["intent_id"],
        "signal admitted",
    )
    assert counts(database) == (1, 1, 2, 1, 1, 0, 0)
    before = facts(database)
    assert consume(adapter, plan, binding, identity) == {
        **result,
        "status": "ALREADY_ACCEPTED",
    }
    assert facts(database) == before


def test_different_source_same_opportunity_gets_auditable_terminal_ignore(database):
    adapter, _, _, binding, plan, original, first = claimed(database)
    duplicate = signal(database, plan, source="s3")
    before = counts(database)
    result = consume(adapter, plan, binding, duplicate)
    assert_no_opening_authority(database, result, first)
    assert result["signal_id"] == duplicate
    assert result["consumer"] == binding.consumer
    assert result["opportunity_id"] == plan.opportunity_id
    assert result["plan_digest"] == plan.plan_digest
    assert receipt(database, binding, original) == (
        "INTENT",
        first["intent_id"],
        "signal admitted",
    )
    assert receipt(database, binding, duplicate) == ("IGNORED", None, REASON)
    stored = decision(database, binding, duplicate)
    assert stored[:5] == (
        result["decision_id"],
        "IGNORED",
        None,
        None,
        result["evidence_ref"],
    )
    assert "strategy_plan" not in stored[5]
    assert stored[5]["result"] == {"action": "IGNORED", "side": None, "quantity": None}
    resolution = stored[5]["features"]["strategy_signal_resolution"]
    assert resolution["schema"] == "strategy-signal-resolution-v1"
    assert resolution["candidate_plan"] == plan.payload()
    assert counts(database) == (
        before[0],
        before[1],
        before[2] + 1,
        before[3] + 1,
        before[4],
        before[5],
        before[6],
    )
    assert (
        Signals(database).pending(
            consumer=binding.consumer, environment="SANDBOX", source="s3"
        )
        == []
    )
    trace = TradingData(database).decision_trace(result["decision_id"])
    assert trace["receipt"] == {
        "outcome": "IGNORED",
        "intent_id": None,
        "reason": REASON,
    }


def test_ignored_exact_replay_is_read_only_and_keeps_original_evidence(database):
    adapter, _, _, binding, plan, _, first = claimed(database)
    duplicate = signal(database, plan)
    result = consume(adapter, plan, binding, duplicate, features={"input": "frozen"})
    before = facts(database)
    assert (
        consume(adapter, plan, binding, duplicate, features={"input": "frozen"})
        == result
    )
    assert facts(database) == before
    assert_no_opening_authority(database, result, first)


def test_opportunity_admitted_without_signal_still_consumes_later_duplicate(database):
    adapter, strict, _, binding, plan = adapter_setup(database)
    first = strict.admit(plan, binding, rationale="QA")
    duplicate = signal(database, plan)
    result = consume(adapter, plan, binding, duplicate)
    assert_no_opening_authority(database, result, first)
    assert receipt(database, binding, duplicate) == ("IGNORED", None, REASON)


@pytest.mark.parametrize(
    "changes", [{"rationale": "changed"}, {"features": {"new": True}}]
)
def test_same_consumer_original_signal_changed_evidence_remains_conflict(
    database, changes
):
    adapter, _, _, binding, plan, original, _ = claimed(database)
    before = facts(database)
    with pytest.raises(ValueError, match="CONFLICT"):
        consume(adapter, plan, binding, original, **changes)
    assert facts(database) == before
    assert decision(database, binding, original) is None


def test_same_consumer_original_signal_changed_plan_remains_conflict(database):
    adapter, _, _, binding, plan, original, _ = claimed(database)
    before = facts(database)
    with pytest.raises(ValueError, match="CONFLICT"):
        consume(adapter, replace(plan, quantity="1"), binding, original)
    assert facts(database) == before


@pytest.mark.parametrize(
    "changes", [{"rationale": "changed"}, {"features": {"new": True}}]
)
def test_ignored_receipt_cannot_relabel_a_changed_proposal(database, changes):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    consume(adapter, plan, binding, duplicate)
    before = facts(database)
    with pytest.raises(ValueError, match="CONFLICT"):
        consume(adapter, plan, binding, duplicate, **changes)
    assert facts(database) == before


def test_ignored_receipt_cannot_relabel_a_changed_plan(database):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    consume(adapter, plan, binding, duplicate)
    before = facts(database)
    with pytest.raises(ValueError, match="CONFLICT"):
        consume(adapter, replace(plan, quantity="1"), binding, duplicate)
    assert facts(database) == before


@pytest.mark.parametrize("same_signal", [False, True])
def test_other_instance_consumes_duplicate_without_overwriting_winner(
    database, same_signal
):
    adapter, _, _, binding, plan, original, first = claimed(database)
    other, _, _, other_binding, other_plan = adapter_setup(database, instance="trend-b")
    if same_signal:
        other_plan = same_signal_times(other_plan, plan)
    duplicate = original if same_signal else signal(database, other_plan)
    result = consume(other, other_plan, other_binding, duplicate)
    assert_no_opening_authority(database, result, first)
    assert receipt(database, binding, original) == (
        "INTENT",
        first["intent_id"],
        "signal admitted",
    )
    assert receipt(database, other_binding, duplicate) == ("IGNORED", None, REASON)
    before = facts(database)
    assert consume(adapter, plan, binding, original)["status"] == "ALREADY_ACCEPTED"
    assert facts(database) == before


def test_account_scope_does_not_share_an_opportunity_or_signal_receipt(database):
    adapter, _, _, binding, plan, original, first = claimed(database)
    other, _, _, other_binding, other_plan = adapter_setup(
        database, scope=replace(SCOPE, account_id="independent-account")
    )
    other_plan = same_signal_times(other_plan, plan)
    second = consume(other, other_plan, other_binding, original)
    assert second["status"] == "ACCEPTED"
    assert second["intent_id"] != first["intent_id"]
    assert counts(database)[0] == counts(database)[4] == 2
    assert counts(database)[5:] == (0, 0)
    assert receipt(database, binding, original)[0] == "INTENT"
    assert receipt(database, other_binding, original)[0] == "INTENT"
    assert consume(adapter, plan, binding, original)["status"] == "ALREADY_ACCEPTED"


@pytest.mark.parametrize("collision", ["symbol", "side"])
def test_opportunity_identity_collision_is_not_silently_ignored(database, collision):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    changed = (
        replace(plan, symbol="ETHUSDT")
        if collision == "symbol"
        else replace(plan, side="SELL", stop_price="108")
    )
    duplicate = signal(database, changed)
    before = facts(database)
    with pytest.raises(ValueError, match="STRATEGY_OPPORTUNITY_IDENTITY_CONFLICT"):
        consume(adapter, changed, binding, duplicate)
    assert facts(database) == before
    assert receipt(database, binding, duplicate) is None


@pytest.mark.parametrize("mode", ["OFF", "DRAINING", "SHADOW"])
def test_valid_historical_winner_allows_duplicate_cleanup_after_deployment_stop(
    database, mode
):
    adapter, _, registry, binding, plan, _, first = claimed(database)
    duplicate = signal(database, plan)
    StrategyDeployments(database, registry).publish(
        replace(binding, mode=mode, generation=2),
        expected_generation=1,
        request_id="qa-stop-duplicate",
    )
    result = consume(adapter, plan, binding, duplicate)
    assert_no_opening_authority(database, result, first)
    before = facts(database)
    assert consume(adapter, plan, binding, duplicate) == result
    assert facts(database) == before


def test_expired_duplicate_is_terminal_without_refreshing_winners_deadline(database):
    adapter, _, _, binding, plan, _, first = claimed(database)
    expired = replace(
        plan,
        observed_at_ms=plan.observed_at_ms - 120000,
        decided_at_ms=plan.decided_at_ms - 120000,
        expires_at_ms=plan.expires_at_ms - 120000,
    )
    duplicate = signal(database, expired)
    result = consume(adapter, expired, binding, duplicate)
    assert_no_opening_authority(database, result, first)
    assert (
        TradingData(database).trace(first["intent_id"])["decision"]["strategy_plan"]
        == plan.payload()
    )


def test_settled_winner_does_not_release_opportunity_for_another_signal(database):
    adapter, _, _, binding, plan, _, first = claimed(database)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key,status) VALUES (%s,%s,'SETTLED')",
            (first["intent_id"], "qa-duplicate-terminal"),
        )
    result = consume(adapter, plan, binding, signal(database, plan))
    assert_no_opening_authority(database, result, first)


@pytest.mark.parametrize("blocked", ["expired", "off"])
def test_nonduplicate_expiry_or_deployment_denial_is_not_converted_to_ignore(
    database, blocked
):
    adapter, _, registry, binding, plan = adapter_setup(database)
    if blocked == "expired":
        plan = replace(
            plan,
            observed_at_ms=plan.observed_at_ms - 120000,
            decided_at_ms=plan.decided_at_ms - 120000,
            expires_at_ms=plan.expires_at_ms - 120000,
        )
    else:
        StrategyDeployments(database, registry).publish(
            replace(binding, mode="OFF", generation=2),
            expected_generation=1,
            request_id="qa-stop-new-admission",
        )
    identity = signal(database, plan)
    before = facts(database)
    with pytest.raises(ValueError):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before
    assert receipt(database, binding, identity) is None


@pytest.mark.parametrize("mismatch", ["symbol", "environment", "unknown"])
def test_duplicate_resolution_cannot_consume_an_unrelated_signal(database, mismatch):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    if mismatch == "unknown":
        identity = str(uuid4())
    elif mismatch == "environment":
        identity = signal(
            database, replace(plan, scope=replace(plan.scope, environment="LIVE"))
        )
    else:
        identity = signal(database, plan, symbol="ETHUSDT")
    before = facts(database)
    with pytest.raises(ValueError):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before
    assert receipt(database, binding, identity) is None


@pytest.mark.parametrize("deleted", [False, True])
def test_rewritten_or_tombstoned_permanent_claim_never_completes_a_duplicate(
    database, deleted
):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    BusinessState(database).change(
        opportunity_key(plan),
        expected_version=1,
        request_key="qa-corrupt-claim",
        payload={} if deleted else {"corrupt": True},
        reason="QA",
        deleted=deleted,
    )
    before = facts(database)
    with pytest.raises(ValueError):
        consume(adapter, plan, binding, duplicate)
    assert facts(database) == before
    assert receipt(database, binding, duplicate) is None


@pytest.mark.parametrize("bad_history", [False, True])
def test_orphan_intent_or_unproven_claim_history_is_not_a_duplicate_winner(
    database, bad_history
):
    registry, binding, plan, episode, expected = generic_intent_without_claim(database)
    if bad_history:
        BusinessState(database).change(
            opportunity_key(plan),
            expected_version=0,
            request_key="admit:" + episode,
            payload=expected,
            reason="not-a-real-admission",
        )
    identity = signal(database, plan)
    before = facts(database)
    with pytest.raises(ValueError):
        consume(StrategySignalAdmission(database, registry), plan, binding, identity)
    assert facts(database) == before
    assert receipt(database, binding, identity) is None


def test_signal_owned_by_another_opportunity_cannot_be_reassigned_to_duplicate(
    database,
):
    adapter, strict, _, binding, plan, original, _ = claimed(database)
    other_plan = replace(plan, opportunity_id="another-closed-structure")
    strict.admit(other_plan, binding, rationale="QA")
    before = facts(database)
    with pytest.raises(ValueError, match="CONFLICT"):
        consume(adapter, other_plan, binding, original)
    assert facts(database) == before
    assert decision(database, binding, original) is None


@pytest.mark.parametrize("outcome", ["IGNORED", "EXPIRED"])
def test_existing_unrelated_terminal_receipt_cannot_be_overwritten(database, outcome):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    Signals(database).complete(
        consumer=binding.consumer,
        signal_id=duplicate,
        outcome=outcome,
        reason="another component completed this signal",
    )
    before = facts(database)
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_OUTCOME_CONFLICT"):
        consume(adapter, plan, binding, duplicate)
    assert facts(database) == before


def test_preexisting_open_decision_without_receipt_cannot_be_ignored(database):
    adapter, strict, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    with database() as conn:
        proof = strict._proof(conn, plan, binding, "QA", {}, duplicate)
    EvidenceStore(database).put(proof)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_decisions "
            "(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
            "VALUES (%s,%s,%s,%s,'OPEN',%s,%s)",
            (
                str(uuid4()),
                binding.consumer,
                duplicate,
                proof.evidence_ref,
                plan.side,
                plan.quantity,
            ),
        )
    before = facts(database)
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_OUTCOME_CONFLICT"):
        consume(adapter, plan, binding, duplicate)
    assert facts(database) == before
    assert receipt(database, binding, duplicate) is None


def test_concurrent_different_signals_have_one_winner_and_all_others_terminal(database):
    adapter, _, _, binding, plan = adapter_setup(database)
    identities = [signal(database, plan) for _ in range(4)]
    barrier = Barrier(len(identities))

    def run(identity):
        barrier.wait(timeout=5)
        return consume(adapter, plan, binding, identity)

    with ThreadPoolExecutor(max_workers=len(identities)) as pool:
        results = list(pool.map(run, identities))
    assert sorted(result["status"] for result in results) == [
        "ACCEPTED",
        "IGNORED",
        "IGNORED",
        "IGNORED",
    ]
    winner = next(result for result in results if result["status"] == "ACCEPTED")
    assert counts(database)[0] == counts(database)[4] == 1
    assert counts(database)[3] == 4
    assert counts(database)[5:] == (0, 0)
    for result in results:
        if result["status"] == "IGNORED":
            assert_no_opening_authority(database, result, winner)
    assert (
        Signals(database).pending(
            consumer=binding.consumer, environment="SANDBOX", source="tv_bridge"
        )
        == []
    )


@pytest.mark.parametrize("same_signal", [False, True])
def test_concurrent_instances_finish_both_receipts_with_one_opportunity(
    database, same_signal
):
    first, _, _, binding, plan = adapter_setup(database)
    second, _, _, other_binding, other_plan = adapter_setup(
        database, instance="trend-b"
    )
    if same_signal:
        other_plan = same_signal_times(other_plan, plan)
    first_signal = signal(database, plan)
    second_signal = first_signal if same_signal else signal(database, other_plan)
    barrier = Barrier(2)

    def run(item):
        port, candidate, selected, identity = item
        barrier.wait(timeout=5)
        return consume(port, candidate, selected, identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                run,
                [
                    (first, plan, binding, first_signal),
                    (second, other_plan, other_binding, second_signal),
                ],
            )
        )
    assert sorted(result["status"] for result in results) == ["ACCEPTED", "IGNORED"]
    assert counts(database)[0] == counts(database)[4] == 1
    assert counts(database)[3] == 2
    assert counts(database)[5:] == (0, 0)


def test_concurrent_ignored_retries_create_one_decision_and_receipt(database):
    adapter, _, _, binding, plan, _, first = claimed(database)
    duplicate = signal(database, plan)
    barrier = Barrier(4)

    def run(_):
        barrier.wait(timeout=5)
        return consume(adapter, plan, binding, duplicate)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert all(result == results[0] for result in results)
    assert_no_opening_authority(database, results[0], first)
    assert facts(database)[1] == 1
    assert counts(database)[3] == 2


def test_receipt_failure_rolls_back_duplicate_decision_and_evidence(
    database, monkeypatch
):
    adapter, _, _, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    before = facts(database)
    complete = Signals.complete

    def fail(self, **kwargs):
        result = complete(self, **kwargs)
        if kwargs["outcome"] == "IGNORED":
            raise RuntimeError("QA_DUPLICATE_RECEIPT_FAILURE")
        return result

    monkeypatch.setattr(Signals, "complete", fail)
    with pytest.raises(RuntimeError, match="QA_DUPLICATE_RECEIPT_FAILURE"):
        consume(adapter, plan, binding, duplicate)
    assert facts(database) == before
    assert receipt(database, binding, duplicate) is None


def test_unknown_admission_never_becomes_a_duplicate_terminal_decision(
    database, monkeypatch
):
    adapter, _, _, binding, plan = adapter_setup(database)
    identity = signal(database, plan)
    before = facts(database)
    admit = IntentStore.admit

    def unknown(self, intent):
        assert admit(self, intent).code is AdmissionCode.ACCEPTED
        return Admission(AdmissionCode.UNKNOWN)

    monkeypatch.setattr(IntentStore, "admit", unknown)
    with pytest.raises(ValueError, match="OUTCOME_UNKNOWN"):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before


def test_duplicate_outer_commit_failure_does_not_report_success_or_keep_evidence(
    database,
):
    _, _, registry, binding, plan, _, _ = claimed(database)
    duplicate = signal(database, plan)
    before = facts(database)

    @contextmanager
    def fail_commit():
        with database() as conn:
            yield conn
            written = conn.execute(
                "SELECT 1 FROM v2_strategy_decisions WHERE consumer=%s AND signal_id=%s",
                (binding.consumer, duplicate),
            ).fetchone()
            if written:
                raise RuntimeError("QA_DUPLICATE_COMMIT_FAILURE")

    with pytest.raises(RuntimeError, match="QA_DUPLICATE_COMMIT_FAILURE"):
        consume(
            StrategySignalAdmission(fail_commit, registry), plan, binding, duplicate
        )
    assert facts(database) == before


def test_lost_duplicate_commit_ack_has_one_durable_result_recovered_by_retry(database):
    adapter, _, registry, binding, plan, _, first = claimed(database)
    duplicate = signal(database, plan)

    @contextmanager
    def lose_ack():
        with database() as conn:
            yield conn
            written = conn.execute(
                "SELECT 1 FROM v2_strategy_decisions WHERE consumer=%s AND signal_id=%s",
                (binding.consumer, duplicate),
            ).fetchone()
        if written:
            raise RuntimeError("QA_DUPLICATE_ACK_LOST_AFTER_COMMIT")

    with pytest.raises(RuntimeError, match="QA_DUPLICATE_ACK_LOST_AFTER_COMMIT"):
        consume(StrategySignalAdmission(lose_ack, registry), plan, binding, duplicate)
    before = facts(database)
    result = consume(adapter, plan, binding, duplicate)
    assert_no_opening_authority(database, result, first)
    assert facts(database) == before
    assert facts(database)[1] == 1


def test_explicit_signal_identity_is_required_before_any_connection():
    registry, binding, plan = contracts()
    adapter = StrategySignalAdmission(
        lambda: pytest.fail("missing signal must not connect"), registry
    )
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_REQUIRED"):
        consume(adapter, plan, binding, None)


def test_scoped_signal_intent_without_receipt_is_not_a_terminal_duplicate(database):
    registry, binding, plan, episode, _ = generic_intent_without_claim(
        database, with_signal=True
    )
    identity = TradingData(database).trace(episode)["decision"]["signal_id"]
    before = facts(database)
    assert receipt(database, binding, identity) is None
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_OUTCOME_CONFLICT"):
        consume(StrategySignalAdmission(database, registry), plan, binding, identity)
    assert facts(database) == before
    assert receipt(database, binding, identity) is None


def test_uniqueness_conflict_without_committed_winner_is_not_duplicate_proof(
    database, monkeypatch
):
    adapter, _, _, binding, plan = adapter_setup(database)
    identity = signal(database, plan)
    before = facts(database)

    def conflict(*_args, **_kwargs):
        raise StrategyPlanAdmissionError("STRATEGY_ADMISSION_INTENT_CONFLICT")

    monkeypatch.setattr(StrategyPlanAdmission, "admit", conflict)
    with pytest.raises(ValueError, match="STRATEGY_OPPORTUNITY_WINNER_UNPROVEN"):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before
    assert receipt(database, binding, identity) is None


def test_foreign_open_decision_blocks_first_admission_not_only_duplicate_resolution(
    database,
):
    adapter, strict, _, binding, plan = adapter_setup(database)
    identity = signal(database, plan)
    with database() as conn:
        proof = strict._proof(conn, plan, binding, "QA", {}, identity)
    EvidenceStore(database).put(proof)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_decisions "
            "(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
            "VALUES (%s,%s,%s,%s,'OPEN',%s,%s)",
            (
                str(uuid4()),
                binding.consumer,
                identity,
                proof.evidence_ref,
                plan.side,
                plan.quantity,
            ),
        )
    before = facts(database)
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_OUTCOME_CONFLICT"):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before
    assert counts(database)[0] == 0
    assert receipt(database, binding, identity) is None


@pytest.mark.parametrize("changed", ["proposal", "candidate", "winner"])
def test_seeded_ignored_evidence_cannot_be_retrofitted_into_a_matching_proposal(
    database, changed
):
    adapter, strict, _, binding, plan, _, first = claimed(database)
    identity = signal(database, plan)
    with database() as conn:
        proposal = strict._proof(conn, plan, binding, "QA", {}, identity)
        winner = require_plan_admission(
            conn, first["intent_id"], deployment=deployment_snapshot(binding)
        )
        incoming = conn.execute(
            "SELECT source,snapshot FROM v2_inbound_signals WHERE signal_id=%s",
            (identity,),
        ).fetchone()
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
    expected = _ignored_proof(plan, binding, proposal, winner, identity, incoming, now)
    snapshot = json.loads(expected.snapshot_json)
    resolution = snapshot["features"]["strategy_signal_resolution"]
    if changed == "proposal":
        resolution["proposal_evidence_ref"] = "0" * 64
    elif changed == "candidate":
        resolution["candidate_plan"] = replace(plan, quantity="1").payload()
    else:
        resolution["winner"]["plan_digest"] = "0" * 64
    # This is valid immutable, content-addressed evidence with the expected
    # deterministic decision ID and receipt. Only its business claim is false;
    # no trigger disabling or mutation of an existing record is necessary.
    seeded = DecisionEvidence(
        expected.strategy_version, expected.config_json, canonical(snapshot)
    )
    EvidenceStore(database).put(seeded)
    identity_of_decision = str(
        uuid5(_DECISION_NAMESPACE, binding.consumer + ":" + identity)
    )
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_decisions "
            "(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
            "VALUES (%s,%s,%s,%s,'IGNORED',NULL,NULL)",
            (identity_of_decision, binding.consumer, identity, seeded.evidence_ref),
        )
    Signals(database).complete(
        consumer=binding.consumer, signal_id=identity, outcome="IGNORED", reason=REASON
    )
    before = facts(database)
    with pytest.raises(ValueError, match="STRATEGY_SIGNAL_OUTCOME_CONFLICT"):
        consume(adapter, plan, binding, identity)
    assert facts(database) == before
    assert decision(database, binding, identity)[4] == seeded.evidence_ref
