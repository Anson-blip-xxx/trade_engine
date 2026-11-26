"""Real isolated PG route fences and disposable typed entry batches; no venue."""

from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from services import v2_strategy_entry_job as module
from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_entry_job import (
    EntryJobSpec,
    entry_transaction,
    run_entry_work,
)
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_settlement_execution_fence import fenced as fenced_fixture
from tests.v2_core.test_v2_settlement_execution_fence import route_update
from tests.v2_core.test_v2_strategy_entry_scheduler import frozen, task
from tests.v2_core.test_v2_strategy_plan_admission import counts
from v2_core.account_risk import AccountScope
from v2_core.session_lock import session_advisory_lock
from v2_core.signals import Signals

database = database_fixture
fenced = fenced_fixture
ROOT = Path(__file__).resolve().parents[2]


def case(fenced, *, action="OPEN"):
    registry, deployment, plan, identity, worker = frozen(
        fenced.admin, action=action, financial=True
    )
    with fenced.admin() as conn:
        conn.execute(
            (ROOT / "db/migrations/20261126_entry_discovery_cursors.sql").read_text()
        )
    args = dict(fenced.args)
    args.pop("environment")
    binding = ManagementBinding(plan.scope, **args)
    return SimpleNamespace(
        registry=registry,
        deployment=deployment,
        plan=plan,
        identity=identity,
        worker=worker,
        binding=binding,
        stop=Event(),
    )


def run(fenced, selected, **overrides):
    return run_entry_work(
        fenced.admin,
        **{
            "binding": selected.binding,
            "registry": selected.registry,
            "producer": selected.deployment.instance_id,
            "source": "tv_bridge",
            "stop": selected.stop,
            "clock_ms": selected.worker.runtime._now,
            **overrides,
        },
    )


@pytest.mark.parametrize(
    "action,expected", [("OPEN", "PREPARED"), ("IGNORED", "IGNORED")]
)
def test_real_fenced_batch_and_replay(fenced, action, expected):
    selected = case(fenced, action=action)
    result = run(fenced, selected)
    assert result["status"] == "COMPLETED"
    assert result["signals"] == {selected.identity: expected}
    assert result["execution_authorized"] is False
    assert result["trading_health"] == "NOT_EVALUATED"
    assert task(fenced.admin, selected.worker, selected.identity)[1] is True
    before = counts(fenced.admin)
    assert run(fenced, selected)["signals"] == {}
    assert counts(fenced.admin) == before
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status<>'PREPARED'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("phase", ["DRAINING", "STARTING", "BLOCKED", "STOPPED"])
def test_revoked_route_cannot_even_prepare_or_discover(fenced, phase):
    selected = case(fenced)
    route_update(
        fenced,
        "SET phase=%s,worker_token=CASE WHEN %s IN ('BLOCKED','STOPPED') "
        "THEN NULL ELSE worker_token END",
        (phase, phase),
    )
    before = counts(fenced.admin)
    with pytest.raises(ValueError, match="EXECUTION_FENCED"):
        run(fenced, selected)
    assert counts(fenced.admin) == before
    assert task(fenced.admin, selected.worker, selected.identity) is None


@pytest.mark.parametrize("change", ["epoch", "account", "token"])
def test_whole_binding_checked_in_business_transaction(fenced, change):
    selected = case(fenced)
    changes = {
        "epoch": {"epoch": selected.binding.epoch + 1},
        "account": {
            "scope": AccountScope("BINANCE", "other-account", "SANDBOX", "FUTURES")
        },
        "token": {"worker_token": str(uuid4())},
    }
    with pytest.raises(
        ValueError, match="EXECUTION_FENCED|EXECUTION_ACCOUNT_SCOPE_MISMATCH"
    ):
        run(fenced, selected, binding=replace(selected.binding, **changes[change]))
    assert counts(fenced.admin)[0] == 0


def test_stop_before_work_is_no_database_access(fenced):
    selected = case(fenced)
    selected.stop.set()

    def forbidden():
        pytest.fail("stopped job must not connect")

    result = run_entry_work(
        forbidden,
        binding=selected.binding,
        registry=selected.registry,
        producer=selected.deployment.instance_id,
        source="tv_bridge",
        stop=selected.stop,
        clock_ms=selected.worker.runtime._now,
    )
    assert result["reason"] == "STRATEGY_ENTRY_STOPPED"


def test_transaction_fence_holds_route_and_restores_role(fenced):
    selected = case(fenced)
    guard = create_execution_guard(fenced.restricted, **fenced.args)
    write = entry_transaction(
        fenced.restricted, binding=selected.binding, guard=guard, stop=selected.stop
    )
    with write() as conn:
        assert conn.execute("SELECT current_user").fetchone() == (fenced.worker,)
        conn.execute("INSERT INTO qa_fence_commits VALUES ('entry')")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced, "SET phase='DRAINING'")
    route_update(fenced)
    with fenced.admin() as conn:
        assert conn.execute("SELECT label FROM qa_fence_commits").fetchall() == [
            ("entry",)
        ]


def test_cooperative_stop_before_commit_rolls_back(fenced):
    selected = case(fenced)
    write = entry_transaction(
        fenced.admin,
        binding=selected.binding,
        guard=create_execution_guard(fenced.admin, **fenced.args),
        stop=selected.stop,
    )
    with pytest.raises(ValueError, match="STRATEGY_ENTRY_STOPPED"), write() as conn:
        conn.execute("INSERT INTO qa_fence_commits VALUES ('must rollback')")
        selected.stop.set()
    with fenced.admin() as conn:
        assert conn.execute("SELECT count(*) FROM qa_fence_commits").fetchone() == (0,)


@pytest.mark.parametrize("interrupt", ["STOP", "REVOKE"])
def test_admitted_but_not_prepared_resumes_original_after_interruption(
    fenced, monkeypatch, interrupt
):
    selected = case(fenced)
    original = module.StrategySignalAdmission.admit

    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if interrupt == "STOP":
            selected.stop.set()
        else:
            route_update(fenced, "SET phase='DRAINING'")
        return result

    monkeypatch.setattr(module.StrategySignalAdmission, "admit", interrupted)
    result = run(fenced, selected)
    assert result["status"] == "BLOCKED"
    assert counts(fenced.admin)[0] == 1
    assert counts(fenced.admin)[5] == 0
    assert task(fenced.admin, selected.worker, selected.identity)[1] is False
    with fenced.admin() as conn:
        first = conn.execute("SELECT intent_id FROM v2_trade_intents").fetchone()[0]
        conn.execute(
            "UPDATE v2_strategy_tasks SET next_attempt_at=clock_timestamp()-interval '1 second'"
        )
    selected.stop.clear()
    route_update(fenced, "SET phase='ACTIVE'")
    monkeypatch.setattr(module.StrategySignalAdmission, "admit", original)
    assert run(fenced, selected)["signals"] == {selected.identity: "PREPARED"}
    with fenced.admin() as conn:
        assert conn.execute("SELECT intent_id FROM v2_trade_intents").fetchall() == [
            (first,)
        ]
        assert conn.execute("SELECT episode_id FROM v2_orders").fetchall() == [(first,)]


def test_busy_job_keeps_other_instance_owner(fenced):
    selected = case(fenced)
    with session_advisory_lock(
        fenced.admin, "typed-entry-batch:" + selected.worker.scope.consumer
    ) as acquired:
        assert acquired
        assert run(fenced, selected)["status"] == "BUSY"
    assert counts(fenced.admin)[0] == 0


def expired_signal(fenced, selected, key, source="tv_bridge"):
    return Signals(fenced.admin).admit(
        source=source,
        environment="SANDBOX",
        request_key=key,
        snapshot={
            "observed_at": 1000,
            "expires_at_ms": 2000,
            "symbol": selected.plan.symbol,
            "signal": "TREND_UP_LONG",
            "features": {},
        },
    )


def test_disposable_batches_seek_and_wrap_cold_history(fenced):
    selected = case(fenced)
    identities = [
        expired_signal(fenced, selected, f"a:{number}") for number in range(5)
    ]
    for _ in range(4):
        run(fenced, selected, history_page_size=2)
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE signal_id=ANY(%s::uuid[])",
            (identities,),
        ).fetchone() == (5,)
        # Exactly one bounded checkpoint, not append-only cursor history.
        assert conn.execute(
            "SELECT count(*) FROM v2_entry_discovery_cursors"
        ).fetchone() == (1,)
    late = expired_signal(fenced, selected, "a:0-late")
    for _ in range(5):
        run(fenced, selected, history_page_size=2)
    assert task(fenced.admin, selected.worker, late) is not None
    assert counts(fenced.admin)[0] == 1


@pytest.mark.parametrize("lost_ack", [False, True])
def test_checkpoint_failure_repeats_page_does_not_skip(fenced, monkeypatch, lost_ack):
    selected = case(fenced)
    identities = [
        expired_signal(fenced, selected, f"0:{number}") for number in range(4)
    ]
    original = module._checkpoint

    def unavailable(*args):
        if lost_ack:
            original(*args)  # Commit the checkpoint, then lose its acknowledgement.
        raise RuntimeError("sensitive database details")

    monkeypatch.setattr(module, "_checkpoint", unavailable)
    first = run(fenced, selected, history_page_size=2)
    assert first["history_error"] == "RuntimeError"
    assert "sensitive" not in str(first)
    assert first["signals"][selected.identity] == "PREPARED"  # Hot work still advances.
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_entry_discovery_cursors"
        ).fetchone() == (1 if lost_ack else 0,)
    monkeypatch.setattr(module, "_checkpoint", original)
    for _ in range(3):
        run(fenced, selected, history_page_size=2)
    assert all(
        task(fenced.admin, selected.worker, identity) is not None
        for identity in identities
    )


def test_source_lanes_do_not_consume_each_others_signals(fenced):
    selected = case(fenced)
    other = expired_signal(fenced, selected, "0:s3", source="s3")
    assert run(fenced, selected)["signals"] == {selected.identity: "PREPARED"}
    assert task(fenced.admin, selected.worker, other) is None
    result = run(fenced, selected, source="s3")
    assert result["signals"] == {other: "DEFERRED"}
    assert counts(fenced.admin)[0] == 1


def test_explicit_job_spec_is_pickle_safe(fenced):
    import pickle

    selected = case(fenced)
    registry = selected.registry
    spec = EntryJobSpec(
        selected.binding,
        selected.deployment.instance_id,
        tuple(registry.specs.values()),
        tuple(registry.entry_handlers),
        tuple(registry.exit_handlers),
        tuple(registry.plan_schemas),
    )
    restored = pickle.loads(pickle.dumps(spec))
    assert restored == spec
    ports = module.entry_ports(spec, sources=("tv_bridge", "s3"))
    assert ports.binding == selected.binding
    assert ports.job_target is module.entry_job and ports.job_args == (spec,)
    assert (
        restored.registry().resolve_binding(selected.deployment)
        == selected.deployment.spec
    )
    with pytest.raises(ValueError, match="BOUNDS"):
        replace(spec, limit=1000)
    with pytest.raises(ValueError, match="REGISTRY"):
        replace(spec, specs=())


def test_live_unfrozen_signal_is_typed_wait_not_context_failure(fenced):
    selected = case(fenced)
    now = selected.worker.runtime._now()
    identity = Signals(fenced.admin).admit(
        source="s3",
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot={
            "observed_at": now,
            "expires_at_ms": now + 60000,
            "symbol": selected.plan.symbol,
            "signal": "TREND_UP_LONG",
            "features": {},
        },
    )
    result = run(fenced, selected, source="s3")
    assert result["status"] == "BLOCKED"
    assert result["signals"] == {identity: "DEFERRED"}
    assert result["pending"] == [
        {"signal_id": identity, "reason": "STRATEGY_FROZEN_CANDIDATE_REQUIRED"}
    ]
    assert not result["diagnostics"].get("failures")
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT completed_at,error_code FROM v2_strategy_tasks WHERE signal_id=%s",
            (identity,),
        ).fetchone() == (None, None)
    assert counts(fenced.admin)[0] == 0


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN"])
def test_terminal_unchanged_is_completed_but_unknown_stays_visible(fenced, status):
    from v2_core.intents import IntentStore

    selected = case(fenced)
    with fenced.admin() as conn:
        candidate = selected.worker.store.read(
            conn,
            scope=selected.worker.scope,
            source="tv_bridge",
            signal_id=selected.identity,
        ).candidate
    admitted = selected.worker.admission.admit(
        selected.plan,
        selected.deployment,
        signal_id=selected.identity,
        rationale=candidate.rationale,
        features=candidate.features,
    )
    if status == "UNKNOWN":
        # Synthetic ambiguous state exists only in the isolated QA database.
        with fenced.admin() as conn:
            conn.execute(
                "UPDATE v2_trade_intents SET status='UNKNOWN',version=version+1 WHERE intent_id=%s",
                (admitted["intent_id"],),
            )
    else:
        assert IntentStore(fenced.admin).terminate_unstarted(
            admitted["intent_id"], status=status, reason="QA"
        )
    before = counts(fenced.admin)
    result = run(fenced, selected)
    assert result["signals"] == {selected.identity: "UNCHANGED"}
    assert result["status"] == ("BLOCKED" if status == "UNKNOWN" else "COMPLETED")
    assert task(fenced.admin, selected.worker, selected.identity)[1] is (
        status != "UNKNOWN"
    )
    assert result["pending"] == (
        [
            {
                "signal_id": selected.identity,
                "reason": "STRATEGY_INTENT_RECONCILIATION_REQUIRED",
            }
        ]
        if status == "UNKNOWN"
        else []
    )
    assert counts(fenced.admin) == before


def test_cursor_does_not_rewrite_idle_row(fenced):
    selected = case(fenced)
    run(fenced, selected, history_page_size=1)
    run(fenced, selected, history_page_size=1)  # Wrap after the single raw row.
    with fenced.admin() as conn:
        before = conn.execute(
            "SELECT updated_at FROM v2_entry_discovery_cursors"
        ).fetchone()
    # Empty source creates no row and cannot update another source's checkpoint.
    assert run(fenced, selected, source="s2")["signals"] == {}
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT updated_at FROM v2_entry_discovery_cursors"
        ).fetchall() == [before]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_server_bars_to_frozen_candidate_to_prepared_and_cross_source_dedup(
    fenced, side
):
    from dataclasses import asdict
    from decimal import Decimal

    from tests.v2_core import test_v2_strategy_deployment_execution as financial
    from tests.v2_core.test_v2_strategy_candidate_builder import bars, policy, safety
    from v2_core.evidence import canonical
    from v2_core.strategy_candidate_builder import (
        assemble_frozen_candidate,
        build_candidate,
    )
    from v2_core.strategy_entry_store import StrategyEntryStore
    from v2_core.strategy_entry_worker import EntrySignalState

    with fenced.admin() as conn:
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        for migration in (
            "20261126_strategy_entry_candidates.sql",
            "20261126_entry_discovery_cursors.sql",
        ):
            conn.execute((ROOT / "db/migrations" / migration).read_text())
    closed = now // 900000 * 900000
    rows = bars(side=side)
    offset = closed - len(rows) * 60000
    rows = [{**row, "t": row["t"] + offset} for row in rows]
    decision = build_candidate(
        rows,
        symbol="BTCUSDT",
        environment="SANDBOX",
        closed_at_ms=closed,
        available_at_ms=now,
        decided_at_ms=now,
        policy=policy(),
    )
    assert decision.status == "CANDIDATE" and decision.side == side
    deployment = financial.binding(
        config={
            **financial.binding().config,
            "candidate_builder": decision.policy.binding_config(),
        }
    )
    financial.publish(fenced.admin, deployment)
    financial.risk(fenced.admin)
    _, original, _, _ = financial.material(selected=deployment)
    entry = Decimal(decision.structure["values"]["close"])
    plan = replace(
        original,
        side=side,
        opportunity_id=decision.opportunity_id,
        entry_price=str(entry),
        stop_price=str(entry - 10 if side == "BUY" else entry + 10),
        observed_at_ms=closed,
        decided_at_ms=now,
        expires_at_ms=now + 60000,
    )
    args = dict(fenced.args)
    args.pop("environment")
    binding = ManagementBinding(plan.scope, **args)
    store = StrategyEntryStore(
        entry_transaction(
            fenced.admin,
            binding=binding,
            guard=create_execution_guard(fenced.admin, **fenced.args),
            stop=Event(),
        )
    )
    identities = {}
    for source in ("tv_bridge", "s3"):
        snapshot = {
            "observed_at": closed,
            "expires_at_ms": plan.expires_at_ms,
            "symbol": plan.symbol,
            "signal": "TREND_DOWN_SHORT",  # TV is only a trigger.
            "features": {"strength": 99 if source == "tv_bridge" else 20},
        }
        identity = Signals(fenced.admin).admit(
            source=source,
            environment="SANDBOX",
            request_key=str(uuid4()),
            snapshot=snapshot,
        )
        state = EntrySignalState(identity, source, "SANDBOX", canonical(snapshot), now)
        candidate = assemble_frozen_candidate(
            decision,
            signal=state,
            binding=deployment,
            plan=plan,
            execution=safety(plan),
        )
        assert store.freeze(candidate).candidate_digest == candidate.candidate_digest
        identities[source] = identity
    results = [
        run_entry_work(
            fenced.admin,
            binding=binding,
            registry=financial.REGISTRY,
            producer=deployment.instance_id,
            source=source,
            stop=Event(),
            clock_ms=financial.now,
        )
        for source in ("tv_bridge", "s3")
    ]
    assert results[0]["signals"] == {identities["tv_bridge"]: "PREPARED"}
    assert results[1]["signals"] == {identities["s3"]: "IGNORED"}
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT i.payload->>'side',o.status FROM v2_orders o "
            "JOIN v2_trade_intents i ON i.intent_id=o.episode_id"
        ).fetchall() == [(side, "PREPARED")]
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)
        assert conn.execute(
            "SELECT outcome,reason FROM v2_signal_receipts WHERE signal_id=%s",
            (identities["s3"],),
        ).fetchone() == ("IGNORED", "STRATEGY_OPPORTUNITY_ALREADY_CLAIMED")
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)",
            tuple(asdict(plan.scope).values()),
        ).fetchone() == (1,)
