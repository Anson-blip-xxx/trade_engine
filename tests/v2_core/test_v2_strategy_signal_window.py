"""Initial typed subscription: real isolated PG, no venue/service operations."""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from tests.v2_core.test_v2_settlement_execution_fence import database, fenced
from tests.v2_core.test_v2_strategy_entry_job import case, run
from tests.v2_core.test_v2_strategy_plan_admission import counts
from tests.v2_core.test_v2_strategy_signal_maintenance import (
    adapter,
    clock,
    migration,
    raw,
    read,
    sweep,
)
from v2_core.evidence import canonical, digest
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_entry_worker import FrozenEntryCandidate
from v2_core.strategy_signal_window import (
    _PAGE_SQL,
    deployment_history_page,
    initial_signal_cutoff,
)

__all__ = ["database", "fenced"]


def cutoff(connect, selected):
    with connect() as conn:
        return initial_signal_cutoff(conn, selected.worker.scope)


def page(connect, selected, *, source="tv_bridge", cursor=None, size=100):
    with connect() as conn:
        return deployment_history_page(
            conn, scope=selected.worker.scope, source=source, cursor=cursor, size=size
        )


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
        plan = conn.execute(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + _PAGE_SQL,
            (
                "tv_bridge",
                scope.environment,
                boundary,
                scope.consumer,
                scope.consumer,
                scope.consumer,
                scope.consumer,
                *asdict(scope).values(),
                "tv_bridge",
                scope.environment,
                None,
                None,
                100,
            ),
        ).fetchone()[0]
    indexes = []

    def walk(value):
        if isinstance(value, dict):
            if "Index Name" in value:
                indexes.append(value["Index Name"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(plan)
    assert "v2_signals_live_discovery" in indexes
    assert [row[0] for row in page(fenced.admin, selected)] == [selected.identity]


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
    assert identity in {row[0] for row in page(fenced.admin, selected)}
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
    assert identity not in {row[0] for row in page(fenced.admin, selected)}
    # Legacy/generic opt-out preserves its pre-existing explicit audit contract.
    assert adapter(fenced, selected).maintain(identity)["status"] == "EXPIRED"
    assert identity in {row[0] for row in page(fenced.admin, selected)}
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
    assert identity in {row[0] for row in page(fenced.admin, selected)}
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
    assert identity in {row[0] for row in page(fenced.admin, selected)}
    result = adapter(fenced, selected, deployment_window=True).maintain(identity)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason"] == "STRATEGY_FROZEN_CANDIDATE_REQUIRED"
    result = run(fenced, selected, deployment_window=True)
    assert result["signals"][identity] == "EXPIRED"
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
    assert identity in {row[0] for row in page(fenced.admin, selected)}
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
    first = page(fenced.admin, selected, size=1)
    second = page(fenced.admin, selected, cursor=first[0][1], size=1)
    assert [first[0][0], second[0][0]] == identities[:2]
    assert other not in {row[0] for row in page(fenced.admin, selected)}
    assert page(fenced.admin, selected, cursor="zzzz") == []
    assert page(fenced.admin, selected, size=1) == first


def test_missing_initial_publication_is_not_a_silent_empty_subscription(fenced):
    selected = case(fenced)
    scope = replace(selected.worker.scope, producer="not-published-instance")
    with fenced.admin() as conn, pytest.raises(ValueError, match="DEPLOYMENT_REQUIRED"):
        deployment_history_page(
            conn, scope=scope, source="tv_bridge", cursor=None, size=10
        )
