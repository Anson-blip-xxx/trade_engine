"""Initial typed subscription: real isolated PG, no venue/service operations."""

import json
import time
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from services.v2_strategy_entry_job import run_entry_work
from tests.v2_core.test_v2_settlement_execution_fence import database, fenced
from tests.v2_core.test_v2_strategy_entry_job import ROOT, case, run
from tests.v2_core.test_v2_strategy_plan_admission import counts
from tests.v2_core.test_v2_strategy_signal_maintenance import (
    adapter,
    clock,
    migration,
    raw,
    read,
    sweep,
)
from v2_core import strategy_signal_window as window
from v2_core.evidence import canonical, digest
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_entry_worker import FrozenEntryCandidate
from v2_core.strategy_signal_window import (
    HistoryPage,
    _lane_page,
    deployment_history_page,
    history_progress,
    initial_signal_cutoff,
)

__all__ = ["database", "fenced"]


@pytest.fixture(autouse=True)
def seek_index(fenced):
    with fenced.admin() as conn:
        conn.execute(
            (ROOT / "db/migrations/20261127_strategy_history_seek.sql").read_text()
        )


def cutoff(connect, selected):
    with connect() as conn:
        return initial_signal_cutoff(conn, selected.worker.scope)


def page(connect, selected, *, source="tv_bridge", cursor=None, size=100):
    with connect() as conn:
        return deployment_history_page(
            conn, scope=selected.worker.scope, source=source, cursor=cursor, size=size
        )


def sweep_pages(connect, selected, *, source="tv_bridge", size=100):
    cursor, collected = None, {}
    for _ in range(1000):
        result = page(connect, selected, source=source, cursor=cursor, size=size)
        collected.update(result)
        cursor = result.next_cursor
        if cursor is None:
            return list(collected.items())
    pytest.fail("bounded fixture sweep never completed")


def explain_lane(conn, **values):
    class RecordingConnection:
        def execute(self, query, args):
            self.query, self.args = query, args
            return conn.execute(query, args)

    recorded = RecordingConnection()
    _lane_page(recorded, **values)
    return conn.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + recorded.query, recorded.args
    ).fetchone()[0]


def plan_nodes(value):
    if isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            yield from plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_nodes(child)


def seed_history(connect, size=20000):
    with connect() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
            "SELECT md5('typed-window-history-'||n)::uuid,'tv_bridge','SANDBOX',"
            "'!old-'||lpad(n::text,8,'0'),jsonb_build_object("
            "'observed_at',1,'expires_at_ms',2,'symbol','BTCUSDT',"
            "'signal','TREND_UP_LONG','features','{}'::jsonb) "
            "FROM generate_series(1,%s) n",
            (size,),
        )
        conn.execute("ANALYZE v2_inbound_signals")


def test_large_old_inbox_does_not_create_tasks_expiry_audits_or_ledger(fenced):
    selected = case(fenced)
    migration(fenced.admin)
    seed_history(fenced.admin)
    live, _ = raw(fenced.admin, expires_at_ms=clock(fenced.admin) + 60000)
    before = counts(fenced.admin)
    result = sweep(fenced, selected, deployment_window=True)
    assert set(result["signals"]) == {live}
    assert result["signals"][live]["status"] == "DEFERRED"
    assert counts(fenced.admin) == before
    result = run(fenced, selected, deployment_window=True)
    assert result["signals"][selected.identity] == "PREPARED"
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks t JOIN v2_inbound_signals s USING(signal_id) "
            "WHERE s.request_key LIKE '!old-%%'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_signal_receipts r JOIN v2_inbound_signals s USING(signal_id) "
            "WHERE s.request_key LIKE '!old-%%'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_cash_adjustments").fetchone() == (
            0,
        )


def test_actual_page_uses_expiry_index_for_large_predeployment_history(fenced):
    selected = case(fenced)
    seed_history(fenced.admin)
    scope = selected.worker.scope
    with fenced.admin() as conn:
        boundary = initial_signal_cutoff(conn, scope)
        plan = explain_lane(
            conn,
            scope=scope,
            source="tv_bridge",
            cutoff=boundary,
            lane=0,
            position=None,
            size=100,
        )
    nodes = list(plan_nodes(plan))
    # With only one eligible row, either expiry index is a bounded valid plan;
    # requiring a particular index name would test planner costing, not scope.
    assert {"v2_signals_typed_history_seek", "v2_signals_live_discovery"} & {
        node.get("Index Name") for node in nodes
    }
    assert all(
        node["Actual Rows"] <= 100
        for node in nodes
        if node.get("Relation Name") == "v2_inbound_signals"
    )
    assert [row[0] for row in sweep_pages(fenced.admin, selected)] == [
        selected.identity
    ]


def test_initial_generation_not_restart_or_current_generation_owns_boundary(fenced):
    selected = case(fenced)
    first = cutoff(fenced.admin, selected)
    identity, _ = raw(fenced.admin, observed_at=first, expires_at_ms=first + 1)
    StrategyDeployments(fenced.admin, selected.registry).publish(
        replace(selected.deployment, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="window-paused-generation-two",
    )
    assert cutoff(fenced.admin, selected) == first
    assert identity in {row[0] for row in sweep_pages(fenced.admin, selected)}
    assert (
        adapter(fenced, selected, deployment_window=True).maintain(identity)["status"]
        == "EXPIRED"
    )
    assert read(fenced, selected, identity).receipt.outcome == "EXPIRED"


def test_late_arriving_predeployment_expiry_is_not_reaudited(fenced):
    selected = case(fenced)
    first = cutoff(fenced.admin, selected)
    identity, _ = raw(fenced.admin, observed_at=first - 2, expires_at_ms=first)
    before = counts(fenced.admin)
    result = adapter(fenced, selected, deployment_window=True).maintain(identity)
    assert result["status"] == "OUT_OF_WINDOW"
    assert result["initial_cutoff_ms"] == first
    assert counts(fenced.admin) == before
    assert identity not in {row[0] for row in sweep_pages(fenced.admin, selected)}
    # Legacy/generic opt-out preserves its pre-existing explicit audit contract.
    assert adapter(fenced, selected).maintain(identity)["status"] == "EXPIRED"
    assert identity in {row[0] for row in sweep_pages(fenced.admin, selected)}
    assert (
        adapter(fenced, selected, deployment_window=True).maintain(identity)["status"]
        == "RECOVERY_REQUIRED"
    )


def test_old_scheduled_work_remains_recoverable_and_terminal(fenced):
    selected = case(fenced)
    first = cutoff(fenced.admin, selected)
    identity, _ = raw(fenced.admin, observed_at=first - 2, expires_at_ms=first - 1)
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES(%s,%s)",
            (selected.worker.scope.consumer, identity),
        )
    assert identity in {row[0] for row in sweep_pages(fenced.admin, selected)}
    assert (
        adapter(fenced, selected, deployment_window=True).maintain(identity)["status"]
        == "EXPIRED"
    )


def test_frozen_old_candidate_recovers_before_window_and_not_as_raw_expiry(fenced):
    selected = case(fenced)
    first = cutoff(fenced.admin, selected)
    identity, snapshot = raw(
        fenced.admin, observed_at=first - 2, expires_at_ms=first - 1
    )
    candidate = FrozenEntryCandidate(
        binding=selected.deployment,
        signal_id=identity,
        source="tv_bridge",
        signal_digest=digest(canonical(snapshot)),
        action="IGNORED",
        rationale="isolated prior frozen candidate",
        observed_at_ms=first - 2,
        decided_at_ms=first - 2,
        expires_at_ms=first - 1,
    )
    selected.worker.store.freeze(candidate)
    assert identity in {row[0] for row in sweep_pages(fenced.admin, selected)}
    result = adapter(fenced, selected, deployment_window=True).maintain(identity)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason"] == "STRATEGY_FROZEN_CANDIDATE_REQUIRED"
    outcomes = {}
    for _ in range(7):
        outcomes.update(run(fenced, selected, deployment_window=True)["signals"])
        if identity in outcomes:
            break
    assert outcomes[identity] == "EXPIRED"
    assert (
        read(fenced, selected, identity).receipt.reason == "STRATEGY_CANDIDATE_EXPIRED"
    )


def test_missing_expiry_is_visible_corruption_not_silently_old_history(fenced):
    selected = case(fenced)
    identity = str(uuid4())
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
            "VALUES(%s,'tv_bridge','SANDBOX','!broken-window',%s)",
            (
                identity,
                Jsonb(
                    {"observed_at": 1, "signal": "TREND_UP_LONG", "symbol": "BTCUSDT"}
                ),
            ),
        )
    assert identity in {row[0] for row in sweep_pages(fenced.admin, selected)}
    before = counts(fenced.admin)
    with pytest.raises(ValueError):
        adapter(fenced, selected, deployment_window=True).maintain(identity)
    assert counts(fenced.admin) == before


def test_source_isolation_and_keyset_wrap_preserved(fenced):
    selected = case(fenced)
    tick = clock(fenced.admin)
    identities = [
        raw(fenced.admin, request_key=f"!current-{i}", expires_at_ms=tick + 60000)[0]
        for i in range(3)
    ]
    other, _ = raw(fenced.admin, source="s3", expires_at_ms=tick + 60000)
    actual = sweep_pages(fenced.admin, selected, size=1)
    assert {row[0] for row in actual} == {*identities, selected.identity}
    assert other not in {row[0] for row in actual}
    first = page(fenced.admin, selected, size=1)
    assert first.next_cursor is not None
    # Legacy request-key checkpoints safely replay, not translate across orders.
    assert page(fenced.admin, selected, cursor="zzzz", size=1) == first
    assert page(fenced.admin, selected, size=1) == first


def test_missing_initial_publication_is_not_a_silent_empty_subscription(fenced):
    selected = case(fenced)
    scope = replace(selected.worker.scope, producer="not-published-instance")
    with fenced.admin() as conn, pytest.raises(ValueError, match="DEPLOYMENT_REQUIRED"):
        deployment_history_page(
            conn, scope=scope, source="tv_bridge", cursor=None, size=10
        )


def test_large_postdeployment_inbox_is_index_seek_not_full_history_sort(fenced):
    selected = case(fenced)
    boundary = cutoff(fenced.admin, selected)
    scope = selected.worker.scope
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
            "SELECT md5('typed-window-live-'||n)::uuid,'tv_bridge','SANDBOX',"
            "'!live-'||lpad(n::text,8,'0'),jsonb_build_object("
            "'observed_at',%s,'expires_at_ms',%s,'symbol','BTCUSDT',"
            "'signal','TREND_UP_LONG','features','{}'::jsonb) "
            "FROM generate_series(1,20000) n",
            (boundary, boundary + 600000),
        )
        conn.execute("ANALYZE v2_inbound_signals")
        first = _lane_page(
            conn,
            scope=scope,
            source="tv_bridge",
            cutoff=boundary,
            lane=0,
            position=None,
            size=10,
        )
        plan = explain_lane(
            conn,
            scope=scope,
            source="tv_bridge",
            cutoff=boundary,
            lane=0,
            position=[first[-1][2], first[-1][0]],
            size=10,
        )
    nodes = list(plan_nodes(plan))
    assert any(
        node.get("Index Name") == "v2_signals_typed_history_seek" for node in nodes
    )
    assert not any(
        node["Node Type"] in {"Sort", "Incremental Sort", "Seq Scan"} for node in nodes
    )
    inbox = [
        node for node in nodes if node.get("Relation Name") == "v2_inbound_signals"
    ]
    assert len(inbox) == 1
    assert inbox[0]["Actual Rows"] == 10
    assert inbox[0].get("Rows Removed by Filter", 0) == 0


def foreign_tasks(connect, scope, count=20000):
    with connect() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
            "SELECT md5('typed-window-foreign-'||n)::uuid,'s3','SANDBOX',"
            "'!foreign-'||lpad(n::text,8,'0'),jsonb_build_object("
            "'observed_at',1,'expires_at_ms',2,'symbol','BTCUSDT',"
            "'signal','TREND_UP_LONG','features','{}'::jsonb) "
            "FROM generate_series(1,%s) n",
            (count,),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) "
            "SELECT %s,signal_id FROM v2_inbound_signals WHERE source='s3'",
            (scope.consumer,),
        )
        conn.execute("ANALYZE v2_strategy_tasks")
        conn.execute("ANALYZE v2_inbound_signals")


def before_task_lane(connect, selected):
    cursor = None
    # Raw, missing expiry, frozen candidates, receipts, then tasks.
    for _ in range(4):
        cursor = page(connect, selected, cursor=cursor, size=100).next_cursor
        assert cursor is not None
    return cursor


def lookup_decisions(conn, consumer, identities):
    # Synthetic immutable index fixtures, not financial or strategy evidence.
    conn.execute(
        "INSERT INTO v2_decision_evidence(evidence_ref,config_digest,strategy_version,config,snapshot) "
        "SELECT md5('lookup-'||signal_id),md5('fixture'),'lookup-only','{}',"
        "jsonb_build_object('signal_id',signal_id) "
        "FROM unnest(%s::text[]) p(signal_id)",
        (identities,),
    )
    conn.execute(
        "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action) "
        "SELECT md5('lookup-'||signal_id)::uuid,%s,signal_id::uuid,"
        "md5('lookup-'||signal_id),'EXPIRED' FROM unnest(%s::text[]) p(signal_id)",
        (consumer, identities),
    )


def record_discovery(connect, captured):
    """Observe the actual INSERT's read plan, then execute/commit that INSERT."""

    @contextmanager
    def recording():
        with connect() as connection:

            class Record:
                def __getattr__(self, name):
                    return getattr(connection, name)

                def execute(self, query, args=None):
                    if (
                        isinstance(query, str)
                        and "WITH undiscovered AS MATERIALIZED" in query
                        and "unnest(%s::uuid[])" in query
                    ):
                        read_query = query.replace(
                            "INSERT INTO v2_strategy_tasks(consumer,signal_id)", ""
                        ).replace("ON CONFLICT DO NOTHING", "")
                        plan = connection.execute(
                            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + read_query,
                            args,
                        ).fetchone()[0]
                        order = connection.execute(read_query, args).fetchall()
                        captured.append((plan, [str(row[1]) for row in order]))
                    return connection.execute(query, args)

            yield Record()

    return recording


def test_discovery_commits_and_checkpoints_with_large_stale_decision_history(fenced):
    selected = case(fenced)
    scope = selected.worker.scope
    foreign_tasks(fenced.admin, scope)
    with fenced.admin() as conn:
        # Reproduce the competing runtime index and statistics predating the
        # new consumer. ANALYZE alone is not the fix for this query shape.
        conn.execute(
            "CREATE INDEX v2_decisions_analysis_recent "
            "ON v2_strategy_decisions(consumer,created_at DESC,decision_id)"
        )
        conn.execute("ANALYZE v2_strategy_decisions")
        identities = [
            row[0]
            for row in conn.execute(
                "SELECT signal_id::text FROM v2_inbound_signals WHERE source='s3'"
            )
        ]
        lookup_decisions(conn, scope.consumer, identities)
    now = clock(fenced.admin)
    fresh = {
        raw(
            fenced.admin,
            request_key=f"current-discovery-probe-{n}",
            expires_at_ms=now + 60000,
        )[0]
        for n in range(10)
    }
    captured = []
    result = run_entry_work(
        record_discovery(fenced.admin, captured),
        binding=selected.binding,
        registry=selected.registry,
        producer=selected.deployment.instance_id,
        source="tv_bridge",
        stop=selected.stop,
        clock_ms=selected.worker.runtime._now,
        deployment_window=True,
        limit=1,
    )
    assert result["history_error"] is None
    assert len(captured) == 1
    plan, discovered = captured[0]
    assert fresh <= set(discovered)
    lookups = [
        node
        for node in plan_nodes(plan)
        if node.get("Relation Name") == "v2_strategy_decisions"
    ]
    assert len(lookups) == 1
    assert lookups[0]["Index Name"] == "v2_strategy_decisions_consumer_signal_id_key"
    assert lookups[0]["Actual Rows"] <= 1
    assert 0 < lookups[0]["Actual Loops"] <= 100
    assert lookups[0].get("Rows Removed by Filter", 0) == 0
    with fenced.admin() as conn:
        assert (
            conn.execute(
                "SELECT request_cursor FROM v2_entry_discovery_cursors "
                "WHERE consumer=%s AND source='tv_bridge' AND environment='SANDBOX'",
                (scope.consumer,),
            )
            .fetchone()[0]
            .startswith("typed-history-v2:")
        )
        committed = {
            row[0]
            for row in conn.execute(
                "SELECT signal_id::text FROM v2_strategy_tasks "
                "WHERE consumer=%s AND signal_id=ANY(%s::uuid[])",
                (scope.consumer, list(fresh)),
            )
        }
    assert committed == fresh


def test_discovery_ranking_does_not_promote_successor_or_empty_tail(fenced):
    selected = case(fenced)
    now = selected.worker.runtime._now()
    low, member, high = (str(UUID(int=n)) for n in (0, 1, 2**128 - 1))
    with fenced.admin() as conn:
        for identity, expiry in (
            (low, now - 2),
            (member, now - 1),
            (high, now + 60000),
        ):
            conn.execute(
                "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
                "VALUES(%s,'tv_bridge','SANDBOX',%s,%s)",
                (
                    identity,
                    "ranking-" + identity,
                    Jsonb(
                        {
                            "observed_at": now - 100,
                            "expires_at_ms": expiry,
                            "symbol": "BTCUSDT",
                            "signal": "TREND_UP_LONG",
                        }
                    ),
                ),
            )
        lookup_decisions(conn, selected.worker.scope.consumer, [member])
    captured = []
    queue = StrategyScheduler(
        selected.worker,
        context_provider=lambda *_: pytest.fail("discovery cannot request context"),
        context_required=lambda _: False,
    )
    queue._connect = record_discovery(fenced.admin, captured)
    queue._history_page = lambda *_args, **_kwargs: [
        (identity, "ranking-" + identity) for identity in (low, high, member)
    ]
    assert queue.run_once(limit=3, discover_only=True, history_page_size=3) == {}
    # Actual member first even when expired, then fresh nonmember, then old
    # nonmember. A next-greater UUID or empty index tail must not be a decision.
    assert captured[0][1] == [member, high, low]
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE consumer=%s "
            "AND signal_id=ANY(%s::uuid[])",
            (selected.worker.scope.consumer, [low, member, high]),
        ).fetchone() == (3,)


def test_empty_source_match_advances_physical_seek_and_keeps_recovery_fair(fenced):
    selected = case(fenced)
    scope = selected.worker.scope
    foreign_tasks(fenced.admin, scope)
    cursor = before_task_lane(fenced.admin, selected)
    result = page(fenced.admin, selected, cursor=cursor, size=100)
    assert type(result) is HistoryPage
    assert result == []
    assert result.scanned == 100
    assert result.next_cursor not in {None, cursor}
    progress, metrics = history_progress(result, 100, time.perf_counter())
    assert progress == result.next_cursor
    assert metrics["history_scanned"] == 100
    assert metrics["history_sweep_complete"] is False
    with fenced.admin() as conn:
        plan = explain_lane(
            conn,
            scope=scope,
            source="tv_bridge",
            cutoff=cutoff(fenced.admin, selected),
            lane=4,
            position=None,
            size=100,
        )
    nodes = list(plan_nodes(plan))
    tasks = [node for node in nodes if node.get("Relation Name") == "v2_strategy_tasks"]
    assert len(tasks) == 1
    assert tasks[0]["Actual Rows"] == 100
    assert tasks[0]["Index Name"] == "v2_strategy_tasks_pkey"
    assert max(node["Actual Loops"] for node in nodes) <= 100


def test_both_callers_checkpoint_empty_physical_page_without_stopping_hot_work(fenced):
    selected = case(fenced)
    migration(fenced.admin)
    scope = selected.worker.scope
    foreign_tasks(fenced.admin, scope, 250)
    cursor = before_task_lane(fenced.admin, selected)
    with fenced.admin() as conn:
        conn.execute(
            "INSERT INTO v2_entry_discovery_cursors(consumer,source,environment,request_cursor) "
            "VALUES(%s,'tv_bridge','SANDBOX',%s)",
            (scope.consumer, cursor),
        )
        conn.execute(
            "INSERT INTO v2_strategy_maintenance_cursors(consumer,source,environment,request_cursor) "
            "VALUES(%s,'tv_bridge','SANDBOX',%s)",
            (scope.consumer, cursor),
        )
    maintained = sweep(fenced, selected, deployment_window=True)
    assert maintained["signals"] == {}
    assert maintained["request_cursor"] not in {None, cursor}
    result = run(fenced, selected, deployment_window=True)
    assert result["signals"][selected.identity] == "PREPARED"
    with fenced.admin() as conn:
        after = conn.execute(
            "SELECT request_cursor FROM v2_entry_discovery_cursors "
            "WHERE consumer=%s AND source='tv_bridge'",
            (scope.consumer,),
        ).fetchone()[0]
    assert after not in {None, cursor}


def test_maintenance_receipt_probes_never_expand_into_full_consumer_history(fenced):
    selected = case(fenced)
    scope = selected.worker.scope
    foreign_tasks(fenced.admin, scope)
    with fenced.admin() as conn:
        # Synthetic index/lookup fixtures, not a claim of financial settlement.
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "SELECT %s,signal_id,'EXPIRED','synthetic lookup-only fixture' "
            "FROM v2_inbound_signals WHERE source='s3'",
            (scope.consumer,),
        )
        conn.execute("ANALYZE v2_signal_receipts")
    now = clock(fenced.admin)
    for n in range(10):
        raw(
            fenced.admin,
            request_key=f"current-receipt-probe-{n}",
            expires_at_ms=now + 60000,
        )
    captured = []

    @contextmanager
    def recording():
        with fenced.admin() as connection:

            class Record:
                def execute(self, query, args=None):
                    if "WITH ORDINALITY" in query:
                        captured.append((query, args))
                    return connection.execute(query, args)

            yield Record()

    result = adapter(
        fenced,
        selected,
        connect=recording,
        deployment_window=True,
    )._signal_page(None, 10)
    assert len(result) == 10
    # Nearest-index seek is not membership: an unrelated later receipt/candidate
    # must not make a fresh signal appear completed/frozen.
    assert all(row[2] is (row[0] == selected.identity) for row in result)
    assert len(captured) == 1
    with fenced.admin() as conn:
        plan = conn.execute(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + captured[0][0],
            captured[0][1],
        ).fetchone()[0]
    lookups = [
        node
        for node in plan_nodes(plan)
        if node.get("Relation Name")
        in {"v2_signal_receipts", "v2_strategy_entry_candidates"}
    ]
    assert len(lookups) == 2
    assert all(
        node["Actual Rows"] <= 1 and node["Actual Loops"] <= 10 for node in lookups
    )
    assert (
        next(
            node for node in lookups if node["Relation Name"] == "v2_signal_receipts"
        ).get("Index Name")
        == "v2_signal_receipts_pkey"
    )


def test_membership_seek_rejects_next_uuid_and_empty_tail(fenced):
    selected = case(fenced)
    tick = clock(fenced.admin)
    low, high = str(UUID(int=0)), str(UUID(int=2**128 - 1))
    with fenced.admin() as conn:
        for identity in (low, high):
            conn.execute(
                "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
                "VALUES(%s,'tv_bridge','SANDBOX',%s,%s)",
                (
                    identity,
                    "membership-" + identity,
                    Jsonb(
                        {
                            "observed_at": tick,
                            "expires_at_ms": tick + 60000,
                            "symbol": "BTCUSDT",
                            "signal": "TREND_UP_LONG",
                        }
                    ),
                ),
            )
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "VALUES(%s,%s,'EXPIRED','synthetic lookup-only fixture')",
            (selected.worker.scope.consumer, selected.identity),
        )
    result = adapter(fenced, selected, deployment_window=True)._signal_page(None, 10)
    membership = {row[0]: row[2] for row in result}
    assert membership == {low: False, high: False, selected.identity: True}


@pytest.mark.parametrize(
    "bad_cursor", ["legacy-key", "typed-history-v2:not-json", "typed-history-v2:[]"]
)
def test_invalid_or_legacy_hint_replays_without_changing_admission(fenced, bad_cursor):
    selected = case(fenced)
    before = counts(fenced.admin)
    first = page(fenced.admin, selected, size=1)
    assert page(fenced.admin, selected, cursor=bad_cursor, size=1) == first
    assert counts(fenced.admin) == before


def test_missing_expiry_lane_and_per_item_checkpoints_are_independent(fenced):
    selected = case(fenced)
    identities = []
    with fenced.admin() as conn:
        for n in range(3):
            identity = str(uuid4())
            identities.append(identity)
            conn.execute(
                "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
                "VALUES(%s,'tv_bridge','SANDBOX',%s,%s)",
                (
                    identity,
                    f"!missing-{n}",
                    Jsonb({"symbol": "BTCUSDT", "observed_at": 1}),
                ),
            )
    first = page(fenced.admin, selected)
    missing = page(fenced.admin, selected, cursor=first.next_cursor, size=2)
    assert missing.scanned == 2
    assert {row[0] for row in missing} <= set(identities)
    checkpoint = missing.checkpoints[missing[0][0]]
    assert len(checkpoint) < 4096
    # Interrupted page resumes after the last attempted item, not the untried tail.
    cursor, recovered = checkpoint, set()
    for _ in range(30):
        current = page(fenced.admin, selected, cursor=cursor, size=2)
        recovered.update(row[0] for row in current)
        cursor = current.next_cursor
        if cursor is None:
            break
    assert set(identities) - {missing[0][0]} <= recovered
    state = json.loads(checkpoint.split(":", 1)[1])
    assert state[3][1] == missing[0][0]


def test_continuously_full_raw_lane_cannot_starve_late_original_recovery(
    fenced, monkeypatch
):
    selected = case(fenced)
    monkeypatch.setattr(window, "initial_signal_cutoff", lambda *_: 100)
    calls, lanes, sequence = 0, [], 0
    late = str(uuid4())

    def lane_page(_conn, *, lane, position, **_kwargs):
        nonlocal calls, sequence
        calls += 1
        lanes.append(lane)
        if lane == 0:
            sequence += 1
            return [(str(UUID(int=sequence)), f"live-{sequence}", 1000 + sequence)]
        # First receipt visit is empty, then an old expired signal commits.
        # Recovery is deliberately independent of the raw expiry frontier.
        if lane == 3 and calls >= 10 and position is None:
            return [(late, "old-original-receipt")]
        return []

    monkeypatch.setattr(window, "_lane_page", lane_page)
    cursor, found = None, set()
    for _ in range(21):
        result = deployment_history_page(
            None,
            scope=selected.worker.scope,
            source="tv_bridge",
            cursor=cursor,
            size=1,
        )
        found.update(row[0] for row in result)
        cursor = result.next_cursor
    assert late in found
    assert lanes == list(range(7)) * 3
