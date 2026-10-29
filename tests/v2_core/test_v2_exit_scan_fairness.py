"""Durable bounded exit scans use isolated PG and never contact a venue.

The scan tests substitute pure evaluation, not order authorization. Existing
symbol-exit integration tests cover the actual fresh reduce-only preflight.
"""

import json
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_directional_lifecycle import SCOPE, opening
from test_v2_directional_replay import worker
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_exit import DirectionalExitStage
from v2_core.runtime_policy import PolicyStore
from v2_core.session_lock import session_advisory_lock
from v2_core.state import StateWrite

database = database_fixture


def forbidden(*_, **__):
    pytest.fail("exit scan QA must not contact a venue or submit an order")


class ForbiddenScopedRequest:
    account_id = SCOPE.account_id
    environment = SCOPE.environment

    def __call__(self, *args, **kwargs):
        return forbidden(*args, **kwargs)


def population(database, count):
    _, runtime = worker(database)
    identifiers = []
    for index in range(count):
        runtime, result = opening(database, symbol=f"COIN{index}USDT")
        identifiers.append(result["intent_id"])
    runtime.execution.submit = forbidden
    runtime.execution.query = forbidden
    return runtime, sorted(identifiers)


def stage(database, runtime, *, limit=10):
    service = DirectionalExitStage(
        database,
        ForbiddenScopedRequest(),
        runtime=runtime,
        scope=SCOPE,
        observe=forbidden,
        clock_ms=lambda: 600003,
        reference=forbidden,
        allow_writes=True,
        limit=limit,
    )
    attempted, recovered = [], []

    def trace(episode):
        recovered.append(episode)
        return ([],)

    def evaluate(episode):
        attempted.append(episode)
        return {"status": "WAIT", "reason": "QA_NO_EXIT_CONDITION"}

    service._trace = trace
    service.evaluate = evaluate
    service.audit = SimpleNamespace(
        run_once=lambda: {"status": "ACCOUNT_COVERAGE_CLEAR", "blockers": []}
    )
    return service, attempted, recovered


def close_ledger(runtime, episode):
    """Authoritative synthetic QA fills; no venue or unsafe fixture deletion."""
    oid, _ = runtime.data.orders.prepare(
        episode,
        leg="CLOSE",
        quantity="1.25",
        request_key="qa-scan-closed",
        evidence={"source": "explicit-QA-fill"},
    )
    assert runtime.data.orders.transition(
        oid, expected_version=1, status="SUBMITTING", evidence={}
    )
    runtime.data.ledger.record_fill(
        order_id=oid,
        exchange_fill_id="2",
        quantity="1.25",
        price="100",
        fee="0.05",
        fee_currency="USDT",
        occurred_at_ms=4,
        evidence={"source": "explicit-QA-fill"},
    )
    assert runtime.data.orders.transition(
        oid,
        expected_version=2,
        status="FILLED",
        exchange_order_id="20",
        evidence={"fills_complete": True},
    )


@pytest.mark.parametrize("count", [11, 25])
def test_every_episode_is_attempted_in_bounded_durable_slices(database, count):
    runtime, identifiers = population(database, count)
    seen = []
    rounds = (count + 9) // 10
    for index in range(rounds):
        # New object each cycle deliberately discards all process-local state.
        service, attempted, recovered = stage(database, runtime)
        result = service.run_once()
        expected = [identifiers[(index * 10 + n) % count] for n in range(10)]
        assert attempted == recovered == expected
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "EXIT_SCAN_INCOMPLETE"
        assert result["execution_authorized"] is False
        assert result["scan"] == {
            "limit": 10,
            "selected": 10,
            "active_count_lower_bound": 11,
            "remaining_count_lower_bound": 1,
            "count_is_lower_bound": True,
            "complete": False,
            "cursor_advanced": True,
            "cursor_invalid": False,
            "cursor_rebuilt": False,
        }
        checkpoint = service.store.read(service.cursor)
        assert checkpoint.version == index + 1
        assert json.loads(checkpoint.payload_json) == {"after": expected[-1]}
        seen.extend(attempted)
    assert set(seen) == set(identifiers)
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE leg='CLOSE'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("count", [0, 1, 10])
def test_at_or_below_slice_limit_can_be_clear_without_extra_round(database, count):
    runtime, identifiers = population(database, count)
    service, attempted, recovered = stage(database, runtime)
    result = service.run_once()
    assert result["status"] == "CLEAR"
    assert attempted == recovered == identifiers
    assert result["scan"]["complete"] is True
    assert result["scan"]["active_count_lower_bound"] == count
    assert result["scan"]["count_is_lower_bound"] is False
    assert result["scan"]["cursor_advanced"] is bool(count)
    assert result["execution_authorized"] is False
    if not count:
        assert service.store.read(service.cursor) is None


def test_repeated_full_scan_rechecks_exits_without_identical_cursor_history(database):
    runtime, identifiers = population(database, 2)
    service, attempted, _ = stage(database, runtime)
    assert service.run_once()["scan"]["cursor_advanced"] is True
    original = service.store.read(service.cursor)
    attempted.clear()
    result = service.run_once()
    assert result["status"] == "CLEAR"
    assert attempted == identifiers
    assert result["scan"]["cursor_advanced"] is False
    assert service.store.read(service.cursor) == original
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_state_history WHERE state_id=%s",
            (service.cursor.identity,),
        ).fetchone() == (1,)


def test_removed_cursor_episode_and_new_episode_do_not_starve_survivors(database):
    runtime, identifiers = population(database, 11)
    service, attempted, _ = stage(database, runtime, limit=3)
    service.run_once()
    after = attempted[-1]
    close_ledger(runtime, after)
    _, inserted = opening(database, symbol="ADDEDUSDT")
    remaining = sorted(set(identifiers) - {after} | {inserted["intent_id"]})
    seen = []
    for _ in range(4):
        service, attempted, _ = stage(database, runtime, limit=3)
        expected = [item for item in remaining if item > after]
        expected += [item for item in remaining if item <= after]
        result = service.run_once()
        assert attempted == expected[:3]
        assert result["scan"]["complete"] is False
        seen.extend(attempted)
        after = attempted[-1]
    assert set(seen) == set(remaining)


def test_poison_and_pending_slice_advance_without_skipping_other_episodes(database):
    runtime, identifiers = population(database, 11)
    service, attempted, recovered = stage(database, runtime)
    pending_order = "qa-bound-close"
    calls = []

    def trace(episode):
        recovered.append(episode)
        if episode == identifiers[0]:
            raise ValueError("QA_POISON_EPISODE")
        return ([{"order_id": pending_order}],)

    service._trace = trace
    runtime.execution.recover = lambda oid: calls.append(oid) or "UNKNOWN"
    service.audit.run_once = forbidden
    first = service.run_once()
    assert first["status"] == "BLOCKED"
    assert first["reason"] == "CLOSE_RECOVERY_PENDING"
    assert first["scan"]["complete"] is False
    assert first["scan"]["cursor_advanced"] is True
    assert recovered == identifiers[:10] and attempted == []
    assert calls == [pending_order] * 9
    restarted, attempted, recovered = stage(database, runtime)
    restarted.run_once()
    assert attempted[0] == recovered[0] == identifiers[10]


def test_one_bad_evaluation_does_not_skip_healthy_peers_or_pin_cursor(database):
    runtime, identifiers = population(database, 11)
    service, attempted, _ = stage(database, runtime)

    def evaluate(episode):
        attempted.append(episode)
        if episode == identifiers[0]:
            raise ValueError("QA_BAD_EXIT_DATA")
        return {"status": "WAIT"}

    service.evaluate = evaluate
    result = service.run_once()
    assert attempted == identifiers[:10]
    assert result["exits"][identifiers[0]]["status"] == "BLOCKED"
    assert result["scan"]["cursor_advanced"] is True
    restarted, attempted, _ = stage(database, runtime)
    restarted.run_once()
    assert attempted[0] == identifiers[10]


@pytest.mark.parametrize("isolated", [False, True])
def test_backlog_never_bypasses_account_safety_or_authorizes_an_order(
    database, isolated
):
    runtime, _ = population(database, 11)
    if isolated:
        policy = PolicyStore(database, SCOPE)
        policy.patch(
            {"exit.symbol_isolation_enabled": True},
            expected_version=policy.read().version,
            reason="QA scan keeps account-wide uncertainty blocked",
        )
    service, _, recovered = stage(database, runtime)
    service.audit.run_once = lambda: {
        "status": "ACCOUNT_COVERAGE_BLOCKED",
        "blockers": ["VENUE_LEDGER_POSITION_MISMATCH"],
    }
    service.evaluate = forbidden
    service._unprotected_exit = forbidden
    result = service.run_once()
    assert result["status"] == "BLOCKED"
    assert result["execution_authorized"] is False
    assert result["coverage"]["blockers"] == ["VENUE_LEDGER_POSITION_MISMATCH"]
    assert len(recovered) == 10
    assert result["scan"]["cursor_advanced"] is True
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE leg='CLOSE'"
        ).fetchone() == (0,)


def test_crash_before_checkpoint_replays_slice_after_restart(database):
    runtime, identifiers = population(database, 11)
    service, _, recovered = stage(database, runtime)

    def crash():
        raise ConnectionError("QA coverage dependency unavailable")

    service.audit.run_once = crash
    with pytest.raises(ConnectionError):
        service.run_once()
    assert recovered == identifiers[:10]
    assert service.store.read(service.cursor) is None
    restarted, attempted, _ = stage(database, runtime)
    restarted.run_once()
    assert attempted == identifiers[:10]


def test_cursor_cas_failure_is_not_reported_as_success(database, monkeypatch):
    runtime, identifiers = population(database, 11)
    service, attempted, _ = stage(database, runtime)
    monkeypatch.setattr(
        service.store, "change", lambda *_, **__: StateWrite("STALE", 1)
    )
    with pytest.raises(ValueError, match="EXIT_SCAN_CURSOR_CONFLICT"):
        service.run_once()
    assert attempted == identifiers[:10]
    assert service.store.read(service.cursor) is None


def test_process_interruption_mid_slice_cannot_skip_unattempted_exits(database):
    runtime, identifiers = population(database, 11)
    service, attempted, _ = stage(database, runtime)

    def interrupted(episode):
        attempted.append(episode)
        if len(attempted) == 3:
            raise KeyboardInterrupt("QA interrupted worker")
        return {"status": "WAIT"}

    service.evaluate = interrupted
    with pytest.raises(KeyboardInterrupt):
        service.run_once()
    assert attempted == identifiers[:3]
    assert service.store.read(service.cursor) is None
    restarted, retried, _ = stage(database, runtime)
    restarted.run_once()
    assert retried == identifiers[:10]


def test_busy_stage_does_not_move_cursor_or_touch_inventory(database):
    runtime, _ = population(database, 11)
    service, attempted, recovered = stage(database, runtime)
    with session_advisory_lock(
        database, "directional-exit-stage:" + SCOPE.key
    ) as acquired:
        assert acquired
        assert service.run_once() == {"status": "BLOCKED", "reason": "BUSY"}
    assert attempted == recovered == []
    assert service.store.read(service.cursor) is None


def test_deleted_cursor_restarts_safely_at_monotonic_state_version(database):
    runtime, identifiers = population(database, 11)
    service, _, _ = stage(database, runtime)
    service.run_once()
    saved = service.store.read(service.cursor)
    assert (
        service.store.change(
            service.cursor,
            expected_version=saved.version,
            request_key="qa-tombstone",
            payload={},
            deleted=True,
            reason="QA cursor reset only",
        ).code
        == "APPLIED"
    )
    restarted, attempted, _ = stage(database, runtime)
    restarted.run_once()
    assert attempted == identifiers[:10]
    assert restarted.store.read(restarted.cursor).version == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"after": "not-a-uuid"},
        {"after": 1},
        {"after": None},
        {"after": "00000000-0000-0000-0000-000000000001", "extra": "invalid"},
        {"_v2_state_history_ref": "00000000-0000-0000-0000-000000000001"},
        {"_v2_coverage_facts": {}},
    ],
)
def test_bad_paging_cursor_is_explicitly_rebuilt_after_bounded_attempt(
    database, payload
):
    runtime, identifiers = population(database, 11)
    service, attempted, recovered = stage(database, runtime)
    assert (
        service.store.change(
            service.cursor,
            expected_version=0,
            request_key="qa-corrupt-cursor",
            payload=payload,
            reason="QA invalid paging state is not exposure authority",
        ).code
        == "APPLIED"
    )
    result = service.run_once()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "EXIT_SCAN_INCOMPLETE"
    assert result["execution_authorized"] is False
    assert result["scan"]["cursor_invalid"] is True
    assert result["scan"]["cursor_rebuilt"] is True
    assert attempted == recovered == identifiers[:10]
    saved = service.store.read(service.cursor)
    assert saved.version == 2
    assert json.loads(saved.payload_json) == {"after": identifiers[9]}
    restarted, attempted, _ = stage(database, runtime)
    result = restarted.run_once()
    assert result["scan"]["cursor_invalid"] is False
    assert result["scan"]["cursor_rebuilt"] is False
    assert attempted[0] == identifiers[10]


def test_cursor_database_read_failure_is_not_treated_as_bad_payload(
    database, monkeypatch
):
    runtime, _ = population(database, 1)
    service, attempted, recovered = stage(database, runtime)

    @contextmanager
    def failed_read():
        with database() as conn:

            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, query, params=None):
                    if query.startswith("SELECT version,payload,deleted,scope"):
                        raise ConnectionError("QA database unavailable")
                    return conn.execute(query, params)

            yield Proxy()

    monkeypatch.setattr(service, "connect", failed_read)
    with pytest.raises(ConnectionError):
        service.run_once()
    assert attempted == recovered == []


@pytest.mark.parametrize("field", ["account_id", "environment", "namespace", "key"])
def test_cursor_identity_conflict_is_never_rebuilt_or_used(database, field):
    runtime, _ = population(database, 1)
    service, attempted, recovered = stage(database, runtime)
    wrong_scope = {**asdict(service.cursor), field: "QA_OTHER_SCOPE"}
    # Deliberately inconsistent identity only inside the disposable QA schema.
    # Include immutable history so normal PG state integrity still applies.
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_business_state(state_id,scope,version,payload,deleted) "
            "VALUES (%s,%s::jsonb,1,'{}',FALSE)",
            (service.cursor.identity, json.dumps(wrong_scope)),
        )
        conn.execute(
            "INSERT INTO v2_state_history(event_id,state_id,request_key,expected_version,"
            "version,payload,deleted,reason) VALUES (%s,%s,'qa-scope-mismatch',0,1,'{}',FALSE,'QA')",
            (str(uuid4()), service.cursor.identity),
        )
    with pytest.raises(ValueError, match="EXIT_SCAN_CURSOR_IDENTITY_CONFLICT"):
        service.run_once()
    assert attempted == recovered == []
    with database() as conn:
        assert conn.execute(
            "SELECT version,scope FROM v2_business_state WHERE state_id=%s",
            (service.cursor.identity,),
        ).fetchone() == (1, wrong_scope)
