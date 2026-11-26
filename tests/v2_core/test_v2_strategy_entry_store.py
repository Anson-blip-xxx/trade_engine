"""Frozen typed entry transactions on disposable PG; no venue or service I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4, uuid5

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.v2_core import test_v2_strategy_deployment_execution as execution_fixtures
from tests.v2_core.test_v2_strategy_plan_admission import (
    counts,
    generic_intent_without_claim,
    setup,
    signal,
)
from tests.v2_core.test_v2_strategy_prepare import at_database_time
from v2_core import entry_controls, liquidity_controls
from v2_core.account_risk import RiskReference
from v2_core.evidence import EvidenceStore, canonical, digest
from v2_core.orders import Orders
from v2_core.service import TradingData
from v2_core.signals import Signals
from v2_core.strategy import StrategyScope
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_entry_store import (
    _TERMINAL_NAMESPACE,
    EXPIRED_REASON,
    StrategyEntryStore,
    _terminal_proof,
)
from v2_core.strategy_entry_worker import FrozenEntryCandidate, TypedEntryWorker
from v2_core.strategy_execution_context import execution_context
from v2_core.strategy_signal_admission import DUPLICATE_REASON, StrategySignalAdmission

database = database_fixture
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/20261126_strategy_entry_candidates.sql"
)


@pytest.fixture
def entry_database(database):
    with database() as conn:
        conn.execute(MIGRATION.read_text())
    return database


def strategy_scope(binding):
    return StrategyScope(**asdict(binding.scope), producer=binding.instance_id)


def frozen(
    database, binding, plan, *, identity=None, source="tv_bridge", action="OPEN"
):
    identity = identity or signal(database, plan, source=source)
    with database() as conn:
        snapshot = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (identity,)
        ).fetchone()[0]
    features = {}
    if action == "OPEN":
        features = {
            "execution": execution_context(
                plan,
                entry_controls=entry_controls.read(database, plan.scope).snapshot(),
                liquidity_controls=liquidity_controls.read(
                    database, plan.scope
                ).snapshot(),
                policy={
                    "entry.reentry_enabled": False,
                    "entry.price_guard_enabled": False,
                    "entry.max_adverse_price_fraction": "0.01",
                    "entry.max_stop_risk_increase": "1.10",
                },
            )
        }
    return FrozenEntryCandidate(
        binding=binding,
        signal_id=identity,
        source=source,
        signal_digest=digest(canonical(snapshot)),
        action=action,
        rationale="QA frozen structure" if action == "OPEN" else "QA missing structure",
        observed_at_ms=plan.observed_at_ms,
        decided_at_ms=plan.decided_at_ms,
        expires_at_ms=plan.expires_at_ms,
        features_json=canonical(features),
        plan=plan if action == "OPEN" else None,
    )


def ready(database, *, action="OPEN"):
    _, registry, binding, plan = setup(database)
    candidate = frozen(database, binding, plan, action=action)
    return registry, candidate, StrategyEntryStore(database)


def read(store, candidate, *, scope=None, source=None):
    with store.connect() as conn:
        return store.read(
            conn,
            scope=scope or strategy_scope(candidate.binding),
            source=source or candidate.source,
            signal_id=candidate.signal_id,
        )


def worker(database, registry, candidate, *, store=None, **kwargs):
    return TypedEntryWorker(
        database,
        registry,
        scope=strategy_scope(candidate.binding),
        source=candidate.source,
        store=store or StrategyEntryStore(database),
        clock_ms=lambda: 0,  # Wall-clock cannot expire a durable candidate.
        **kwargs,
    )


def finish(store, candidate, **changes):
    return store.terminal(
        **{
            "scope": strategy_scope(candidate.binding),
            "source": candidate.source,
            "signal_id": candidate.signal_id,
            "candidate_digest": candidate.candidate_digest,
            "outcome": "IGNORED",
            "reason": candidate.rationale,
            **changes,
        }
    )


def facts(database):
    with database() as conn:
        return counts(database), conn.execute(
            "SELECT (SELECT count(*) FROM v2_strategy_entry_candidates),"
            "(SELECT count(*) FROM v2_strategy_decisions)"
        ).fetchone()


def test_freeze_is_durable_detached_evidence_not_a_legacy_open_decision(entry_database):
    _, candidate, store = ready(entry_database)
    before = counts(entry_database)
    assert store.freeze(candidate) == candidate
    saved = read(StrategyEntryStore(entry_database), candidate)
    assert saved.candidate == candidate
    assert saved.receipt is None
    assert saved.candidate.execution_authorized is False
    after = counts(entry_database)
    assert after == (*before[:2], before[2] + 1, *before[3:])
    assert facts(entry_database)[1] == (1, 0)
    with entry_database() as conn:
        proof = conn.execute(
            "SELECT e.snapshot FROM v2_strategy_entry_candidates c "
            "JOIN v2_decision_evidence e USING(evidence_ref)"
        ).fetchone()[0]
    assert "strategy_plan" not in proof
    assert proof["execution_authorized"] is False
    assert (
        proof["features"]["strategy_entry_candidate"]["candidate"]
        == candidate.payload()
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE v2_strategy_entry_candidates SET evidence_ref=evidence_ref",
        "DELETE FROM v2_strategy_entry_candidates",
    ],
)
def test_first_writer_row_is_immutable(entry_database, sql):
    import psycopg

    _, candidate, store = ready(entry_database)
    store.freeze(candidate)
    before = facts(entry_database)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="immutable business record"),
        entry_database() as conn,
    ):
        conn.execute(sql)
    assert facts(entry_database) == before


def test_first_writer_survives_new_proposal_and_recreated_store(entry_database):
    _, candidate, store = ready(entry_database, action="IGNORED")
    store.freeze(candidate)
    changed_binding = replace(candidate.binding, config_json='{"risk":"0.02"}')
    newer = replace(
        candidate, binding=changed_binding, rationale="QA different revision"
    )
    before = facts(entry_database)
    assert StrategyEntryStore(entry_database).freeze(newer) == candidate
    assert read(store, candidate).candidate == candidate
    assert facts(entry_database) == before


def test_concurrent_freeze_persists_exactly_one_winner(entry_database):
    _, candidate, store = ready(entry_database, action="IGNORED")
    other = replace(candidate, rationale="QA second writer")
    barrier = Barrier(2)

    def freeze(value):
        barrier.wait(timeout=10)
        return StrategyEntryStore(entry_database).freeze(value)

    before = counts(entry_database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(freeze, [candidate, other]))
    assert results[0] == results[1]
    assert results[0] in (candidate, other)
    assert read(store, candidate).candidate == results[0]
    assert counts(entry_database)[2] == before[2] + 1
    assert facts(entry_database)[1] == (1, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"signal_digest": "0" * 64},
        {"source": "s3"},
        {"observed_at_ms": -1},
        {"expires_at_ms": 1},
    ],
)
def test_candidate_cannot_relabel_original_signal(entry_database, change):
    _, candidate, store = ready(entry_database, action="IGNORED")
    if change.get("observed_at_ms") == -1:
        change = {"observed_at_ms": candidate.observed_at_ms - 1}
    if change.get("expires_at_ms") == 1:
        change = {"expires_at_ms": candidate.expires_at_ms + 1}
    changed = replace(candidate, **change)
    before = facts(entry_database)
    with pytest.raises(
        ValueError, match="STRATEGY_ENTRY_(CANDIDATE_SIGNAL|SIGNAL_SCOPE)_MISMATCH"
    ):
        store.freeze(changed)
    assert facts(entry_database) == before


def test_newer_observed_context_keeps_original_signal_deadline(entry_database):
    _, registry, binding, plan = setup(entry_database)
    identity = signal(entry_database, plan)
    later = replace(plan, observed_at_ms=plan.observed_at_ms + 1)
    candidate = frozen(entry_database, binding, later, identity=identity)
    store = StrategyEntryStore(entry_database)
    assert store.freeze(candidate) == candidate
    result = worker(entry_database, registry, candidate).consume(identity, context={})
    assert result["status"] == "PREPARED"


@pytest.mark.parametrize("signal_kind", ["TREND_UP_LONG", "EVENT_END"])
def test_no_frozen_candidate_remains_pending_even_after_expiry(
    entry_database, signal_kind
):
    registry, candidate, _ = ready(entry_database)
    identity = signal(entry_database, candidate.plan, signal=signal_kind)
    connection = at_database_time(entry_database, candidate.expires_at_ms + 1)
    candidate = replace(candidate, signal_id=identity)
    before = facts(entry_database)
    result = worker(connection, registry, candidate).consume(identity, context={})
    assert result["status"] == "DEFERRED"
    assert result["reason"] == "STRATEGY_FROZEN_CANDIDATE_REQUIRED"
    assert facts(entry_database) == before


def test_open_candidate_cannot_attach_to_lifecycle_end(entry_database):
    _, candidate, store = ready(entry_database)
    identity = signal(entry_database, candidate.plan, signal="EVENT_END")
    candidate = frozen(
        entry_database, candidate.binding, candidate.plan, identity=identity
    )
    before = facts(entry_database)
    with pytest.raises(ValueError, match="CANDIDATE_SIGNAL_MISMATCH"):
        store.freeze(candidate)
    assert facts(entry_database) == before


def test_frozen_open_admits_and_prepares_once_without_submission(entry_database):
    registry, candidate, store = ready(entry_database)
    store.freeze(candidate)
    first = worker(entry_database, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert first["status"] == "PREPARED"
    assert first["execution_authorized"] is False
    trace = TradingData(entry_database).trace(first["intent_id"])
    assert trace["decision"]["strategy_plan"] == candidate.plan.payload()
    assert trace["decision"]["features"] == candidate.features
    assert trace["status"] == "RECEIVED"
    assert [order["status"] for order in trace["orders"]] == ["PREPARED"]
    before = facts(entry_database)
    repeated = worker(entry_database, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert repeated["status"] == "EXISTING_ORDER"
    assert repeated["order_id"] == first["order_id"]
    assert repeated["client_order_id"] == first["client_order_id"]
    assert facts(entry_database) == before
    state = read(store, candidate)
    assert state.candidate is None
    assert state.receipt.intent_id == first["intent_id"]


def test_admission_commit_ack_loss_recovers_original_receipt(entry_database):
    registry, candidate, store = ready(entry_database)
    store.freeze(candidate)
    adapter = StrategySignalAdmission(entry_database, registry)

    class LoseAcknowledgement:
        def admit(self, *args, **kwargs):
            adapter.admit(*args, **kwargs)
            raise OSError("QA commit response lost")

    with pytest.raises(OSError, match="response lost"):
        worker(
            entry_database, registry, candidate, admission=LoseAcknowledgement()
        ).consume(candidate.signal_id, context={})
    assert counts(entry_database)[0] == counts(entry_database)[3] == 1
    assert counts(entry_database)[5] == 0
    result = worker(entry_database, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert result["status"] == "PREPARED"
    assert counts(entry_database)[0] == counts(entry_database)[5] == 1


def test_admitted_receipt_recovers_without_candidate_or_current_deployment(
    entry_database,
):
    registry, candidate, store = ready(entry_database)
    StrategySignalAdmission(entry_database, registry).admit(
        candidate.plan,
        candidate.binding,
        signal_id=candidate.signal_id,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    first = worker(entry_database, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    StrategyDeployments(entry_database, registry).publish(
        replace(candidate.binding, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-stop-after-prepare",
    )
    assert facts(entry_database)[1] == (0, 0)
    before = facts(entry_database)
    connection = at_database_time(entry_database, candidate.expires_at_ms + 1)
    replay = worker(connection, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert replay["status"] == "EXISTING_ORDER"
    assert replay["order_id"] == first["order_id"]
    assert facts(entry_database) == before
    with pytest.raises(ValueError, match="ALREADY_TERMINAL"):
        store.freeze(candidate)
    assert facts(entry_database) == before


def test_ignored_candidate_is_atomic_audited_terminal_without_intent(entry_database):
    registry, candidate, store = ready(entry_database, action="IGNORED")
    store.freeze(candidate)
    before = counts(entry_database)
    result = worker(entry_database, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert result["status"] == "IGNORED"
    assert result["reason"] == candidate.rationale
    assert result["execution_authorized"] is False
    assert facts(entry_database)[1] == (1, 1)
    after = counts(entry_database)
    assert after == (*before[:2], before[2] + 1, before[3] + 1, *before[4:])
    assert (
        worker(entry_database, registry, candidate).consume(
            candidate.signal_id, context={}
        )
        == result
    )
    assert counts(entry_database) == after


@pytest.mark.parametrize("action", ["OPEN", "IGNORED"])
def test_pg_proven_candidate_expiry_has_audited_terminal(entry_database, action):
    registry, candidate, store = ready(entry_database, action=action)
    store.freeze(candidate)
    connection = at_database_time(entry_database, candidate.expires_at_ms)
    result = worker(connection, registry, candidate).consume(
        candidate.signal_id, context={}
    )
    assert result["status"] == "EXPIRED"
    assert result["reason"] == EXPIRED_REASON
    assert counts(entry_database)[0] == counts(entry_database)[5] == 0
    assert facts(entry_database)[1] == (1, 1)
    assert read(StrategyEntryStore(connection), candidate).receipt.outcome == "EXPIRED"


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_digest": "0" * 64},
        {"reason": "QA not frozen reason"},
        {"outcome": "EXPIRED", "reason": EXPIRED_REASON},
    ],
)
def test_terminal_requires_exact_candidate_and_pg_proof(entry_database, change):
    _, candidate, store = ready(entry_database, action="IGNORED")
    store.freeze(candidate)
    before = facts(entry_database)
    with pytest.raises(ValueError):
        finish(store, candidate, **change)
    assert facts(entry_database) == before


@pytest.mark.parametrize("stage", ["rollback", "commit-ack-loss"])
def test_terminal_transaction_failure_or_lost_ack_is_retry_safe(entry_database, stage):
    _, candidate, store = ready(entry_database, action="IGNORED")
    store.freeze(candidate)
    before = facts(entry_database)

    @contextmanager
    def broken():
        with entry_database() as conn:
            yield conn
            if stage == "rollback":
                raise OSError("QA before commit")
        raise OSError("QA after commit")

    with pytest.raises(OSError):
        finish(StrategyEntryStore(broken), candidate)
    if stage == "rollback":
        assert facts(entry_database) == before
    else:
        assert read(store, candidate).receipt.outcome == "IGNORED"
    finish(store, candidate)
    stable = facts(entry_database)
    finish(store, candidate)
    assert facts(entry_database) == stable
    assert stable[1] == (1, 1)


def test_bare_legacy_terminal_receipt_is_not_typed_proof(entry_database):
    registry, candidate, store = ready(entry_database, action="IGNORED")
    store.freeze(candidate)
    Signals(entry_database).complete(
        consumer=candidate.binding.consumer,
        signal_id=candidate.signal_id,
        outcome="EXPIRED",
        reason="legacy scheduler expiry",
    )
    before = facts(entry_database)
    with pytest.raises(ValueError, match="DURABLE_STATE_CONFLICT"):
        worker(entry_database, registry, candidate).consume(
            candidate.signal_id, context={}
        )
    assert facts(entry_database) == before


def test_terminal_cannot_change_first_writer_open_into_a_different_ignore(
    entry_database,
):
    _, candidate, store = ready(entry_database)
    store.freeze(candidate)
    false_candidate = replace(
        candidate, action="IGNORED", plan=None, features_json="{}"
    )
    with entry_database() as conn:
        snapshot = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone()[0]
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        proof = _terminal_proof(
            false_candidate,
            snapshot,
            outcome="IGNORED",
            reason=false_candidate.rationale,
            decided_at=now,
        )
        local = lambda: nullcontext(conn)
        EvidenceStore(local).put(proof)
        conn.execute(
            "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action) "
            "VALUES(%s,%s,%s,%s,'IGNORED')",
            (
                str(
                    uuid5(
                        _TERMINAL_NAMESPACE,
                        candidate.binding.consumer + ":" + candidate.signal_id,
                    )
                ),
                candidate.binding.consumer,
                candidate.signal_id,
                proof.evidence_ref,
            ),
        )
        Signals(local).complete(
            consumer=candidate.binding.consumer,
            signal_id=candidate.signal_id,
            outcome="IGNORED",
            reason=false_candidate.rationale,
        )
    before = facts(entry_database)
    with pytest.raises(ValueError, match="TERMINAL_EVIDENCE_INVALID"):
        read(store, candidate)
    assert facts(entry_database) == before


def test_foreign_consumer_intent_receipt_cannot_be_recovered(entry_database):
    registry, candidate, store = ready(entry_database)
    admitted = StrategySignalAdmission(entry_database, registry).admit(
        candidate.plan,
        candidate.binding,
        signal_id=candidate.signal_id,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    foreign = replace(strategy_scope(candidate.binding), account_id="other-account")
    Signals(entry_database).complete(
        consumer=foreign.consumer,
        signal_id=candidate.signal_id,
        outcome="INTENT",
        reason="signal admitted",
        intent_id=admitted["intent_id"],
    )
    before = facts(entry_database)
    with pytest.raises(ValueError, match="DURABLE_STATE_CONFLICT"):
        read(store, candidate, scope=foreign)
    with pytest.raises(ValueError, match="SIGNAL_SCOPE_MISMATCH"):
        read(store, candidate, source="s3")
    assert facts(entry_database) == before


def test_orphan_typed_intent_is_never_hidden_by_expiry(entry_database):
    registry, binding, plan, episode, _ = generic_intent_without_claim(
        entry_database, with_signal=True
    )
    with entry_database() as conn:
        identity = conn.execute(
            "SELECT signal_id::text FROM v2_trade_intents WHERE intent_id=%s",
            (episode,),
        ).fetchone()[0]
    candidate = frozen(entry_database, binding, plan, identity=identity)
    connection = at_database_time(entry_database, plan.expires_at_ms + 1)
    before = facts(entry_database)
    with pytest.raises(ValueError, match="DURABLE_STATE_CONFLICT"):
        worker(connection, registry, candidate).consume(candidate.signal_id, context={})
    assert facts(entry_database) == before


def test_duplicate_opportunity_terminal_recovery_needs_no_new_freeze(entry_database):
    registry, candidate, store = ready(entry_database)
    adapter = StrategySignalAdmission(entry_database, registry)
    adapter.admit(
        candidate.plan,
        candidate.binding,
        signal_id=candidate.signal_id,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    duplicate = frozen(entry_database, candidate.binding, candidate.plan, source="s3")
    outcome = adapter.admit(
        duplicate.plan,
        duplicate.binding,
        signal_id=duplicate.signal_id,
        rationale=duplicate.rationale,
        features=duplicate.features,
    )
    assert outcome["status"] == "IGNORED"
    StrategyDeployments(entry_database, registry).publish(
        replace(candidate.binding, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-stop-before-duplicate-recovery",
    )
    before = facts(entry_database)
    result = worker(entry_database, registry, duplicate).consume(
        duplicate.signal_id, context={}
    )
    assert result["status"] == "IGNORED"
    assert result["reason"] == DUPLICATE_REASON
    assert read(store, duplicate).receipt.decision_id == outcome["decision_id"]
    assert facts(entry_database) == before


def test_foreign_legacy_open_decision_never_becomes_expired_receipt(entry_database):
    registry, candidate, store = ready(entry_database)
    store.freeze(candidate)
    with entry_database() as conn:
        evidence_ref = conn.execute(
            "SELECT evidence_ref FROM v2_strategy_entry_candidates WHERE consumer=%s AND signal_id=%s",
            (candidate.binding.consumer, candidate.signal_id),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,side,quantity) "
            "VALUES(%s,%s,%s,%s,'OPEN','BUY',1)",
            (
                str(uuid4()),
                candidate.binding.consumer,
                candidate.signal_id,
                evidence_ref,
            ),
        )
    before = facts(entry_database)
    connection = at_database_time(entry_database, candidate.expires_at_ms + 1)
    with pytest.raises(ValueError, match="DURABLE_STATE_CONFLICT"):
        worker(connection, registry, candidate).consume(candidate.signal_id, context={})
    assert facts(entry_database) == before


def test_unknown_order_is_recovered_not_reclassified_as_expired(entry_database):
    binding, plan, _, _ = execution_fixtures.material()
    execution_fixtures.publish(entry_database, binding)
    execution_fixtures.risk(entry_database)
    candidate = frozen(entry_database, binding, plan)
    store = StrategyEntryStore(entry_database)
    store.freeze(candidate)
    port = worker(entry_database, execution_fixtures.REGISTRY, candidate)
    first = port.consume(candidate.signal_id, context={})
    orders = Orders(entry_database)
    orders.transition(
        first["order_id"],
        expected_version=1,
        status="SUBMITTING",
        evidence={},
        risk_reference=RiskReference(
            "SANDBOX", "BTCUSDT", "qa", "100", execution_fixtures.now()
        ),
    )
    orders.transition(
        first["order_id"],
        expected_version=2,
        status="UNKNOWN",
        evidence={"source": "QA synthetic timeout"},
    )
    before = facts(entry_database)
    connection = at_database_time(entry_database, candidate.expires_at_ms + 1)
    result = worker(connection, execution_fixtures.REGISTRY, candidate).consume(
        candidate.signal_id, context={}
    )
    assert result["status"] == "EXISTING_ORDER"
    assert result["order_status"] == "UNKNOWN"
    assert result["order_id"] == first["order_id"]
    assert read(StrategyEntryStore(connection), candidate).receipt.outcome == "INTENT"
    assert facts(entry_database) == before
