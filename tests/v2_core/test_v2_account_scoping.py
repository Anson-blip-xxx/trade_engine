from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_v2_account_risk import POLICY, SCOPE, now, prepare, runner
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent

from v2_core.account_risk import AccountRisk
from v2_core.attention import RecoveryAttention
from v2_core.delivery import Projector
from v2_core.operational import OperationalProjector
from v2_core.runner import ExecutionRunner
from v2_core.runtime import DataRuntime
from v2_core.scoping import ScopeMismatch
from v2_core.service import TradingData

database = database_fixture
OTHER = replace(SCOPE, account_id="other")
LIVE = replace(SCOPE, environment="LIVE")


def runtime(database, scope=SCOPE, *, query=lambda _: None, projectors=()):
    return DataRuntime(
        database,
        submit=lambda _: pytest.fail("maintenance submitted"),
        query=query,
        risk_check=lambda _: True,
        clock_ms=now,
        scope=scope,
        projectors=projectors,
    )


def prepared(database, scope, *, expired=False):
    return prepare(
        database,
        account=scope.account_id,
        environment=scope.environment,
        expired=expired,
    )


def recoverable(database, scope):
    original, data, order, client = prepared(database, scope)
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    return original, data, order, client


def test_recovery_filters_before_limit_and_leaves_foreign_tasks_untouched(database):
    foreign = recoverable(database, OTHER)[2]
    live = recoverable(database, LIVE)[2]
    own = recoverable(database, SCOPE)[2]
    # Existing rows from a former global supervisor must also be filtered at claim.
    with database() as conn:
        for order in (foreign, live):
            conn.execute(
                "INSERT INTO v2_order_recovery(order_id) VALUES (%s)", (order,)
            )
    queried = []
    result = runtime(
        database, query=lambda req: queried.append(req["order_id"])
    ).recover_once(limit=1)
    assert result == {own: "UNKNOWN"} and queried == [own]
    with database() as conn:
        assert conn.execute(
            "SELECT attempts,lease_token,error_code FROM v2_order_recovery WHERE order_id=ANY(%s::uuid[]) ORDER BY order_id",
            ([foreign, live],),
        ).fetchall() == [(0, None, None), (0, None, None)]


def test_discovery_does_not_create_foreign_recovery_tasks(database):
    recoverable(database, OTHER)
    recoverable(database, LIVE)
    assert runtime(database).recover_once() == {}
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_order_recovery").fetchone()[0] == 0


def test_two_account_workers_recover_only_their_own_orders_concurrently(database):
    orders = {
        scope.account_id: recoverable(database, scope)[2] for scope in (SCOPE, OTHER)
    }
    seen = {SCOPE.account_id: [], OTHER.account_id: []}

    def run(scope):
        return runtime(
            database,
            scope,
            query=lambda req: seen[scope.account_id].append(req["account_id"]),
        ).recover_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (SCOPE, OTHER)))
    assert [set(result) for result in results] == [
        {orders[SCOPE.account_id]},
        {orders[OTHER.account_id]},
    ]
    assert seen == {
        SCOPE.account_id: [SCOPE.account_id],
        OTHER.account_id: [OTHER.account_id],
    }


def test_restart_and_expired_recovery_lease_remain_account_scoped(database):
    foreign = recoverable(database, OTHER)[2]
    own = recoverable(database, SCOPE)[2]
    with database() as conn:
        for order in (foreign, own):
            conn.execute(
                "INSERT INTO v2_order_recovery(order_id,lease_token,lease_until) VALUES (%s,gen_random_uuid(),clock_timestamp()-interval '1 second')",
                (order,),
            )
    assert runtime(database).recover_once() == {own: "UNKNOWN"}
    assert runtime(database, OTHER).recover_once() == {foreign: "UNKNOWN"}


@pytest.mark.parametrize("method", ["dispatch", "recover", "snapshot"])
def test_direct_foreign_order_access_rejected_without_cancellation(database, method):
    original, data, order, _ = prepared(database, OTHER)
    with pytest.raises(ScopeMismatch):
        getattr(runtime(database).execution, method)(order)
    assert data.trace(original.intent_id)["orders"][0]["status"] == "PREPARED"


@pytest.mark.parametrize("scope", [OTHER, LIVE])
def test_admission_rejects_foreign_identity_before_persistence(scope):
    def forbidden():
        pytest.fail("foreign admission touched storage")

    original = replace(
        intent(), account_id=scope.account_id, environment=scope.environment
    )
    with pytest.raises(ScopeMismatch):
        runtime(forbidden).accept_open(original, evidence())


def test_expiry_filters_before_limit_and_does_not_cancel_foreign_orders(database):
    foreign, data, foreign_order, _ = prepared(database, OTHER, expired=True)
    live, _, live_order, _ = prepared(database, LIVE, expired=True)
    own, _, _, _ = prepared(database, SCOPE, expired=True)
    assert runtime(database).expire_once(limit=1) == {own.intent_id: "EXPIRED"}
    assert data.trace(foreign.intent_id)["orders"][0]["status"] == "PREPARED"
    assert data.trace(live.intent_id)["orders"][0]["status"] == "PREPARED"
    assert foreign_order != live_order


def test_attention_scan_only_records_own_overdue_orders(database):
    foreign = recoverable(database, OTHER)[0]
    own = recoverable(database, SCOPE)[0]
    scanner = RecoveryAttention(database, scope=SCOPE)
    assert scanner.scan(now_ms=now() + 60000, overdue_ms=1, limit=1) == 1
    assert scanner.scan(now_ms=now() + 60000, overdue_ms=1, limit=1) == 0
    with database() as conn:
        rows = conn.execute(
            "SELECT intent_id::text FROM v2_domain_outbox WHERE event_type LIKE 'ATTENTION_REQUIRED:%%'"
        ).fetchall()
        assert rows == [(own.intent_id,)] and rows != [(foreign.intent_id,)]


def test_release_sweep_does_not_free_other_accounts_capacity(database, monkeypatch):
    import v2_core.account_risk as module

    risk = AccountRisk(database)
    for scope in (OTHER, SCOPE):
        risk.configure(scope, POLICY, expected_version=0)
        _, data, order, _ = prepared(database, scope)
        assert runner(database).dispatch(order) == "ACKNOWLEDGED"
        with monkeypatch.context() as patch:
            patch.setattr(module, "release_terminal", lambda *_: False)
            data.orders.transition(
                order,
                expected_version=3,
                status="CANCELLED",
                evidence={"fills_complete": True},
            )
    assert runtime(database).tick(overdue_ms=60000, limit=1)["risk_releases"] == 1
    assert risk.usage(SCOPE)["positions"] == 0
    assert risk.usage(OTHER)["positions"] == 1
    assert runtime(database, OTHER).tick(overdue_ms=60000)["risk_releases"] == 1


@pytest.mark.parametrize("scheduled", [False, True])
def test_projection_filters_before_limit_for_shared_consumer(database, scheduled):
    foreign = prepared(database, OTHER)[0]
    own = prepared(database, SCOPE)[0]
    received = []
    p = Projector(
        database,
        "same-consumer",
        lambda event, subject, *_: received.append(subject) or True,
        scope=SCOPE,
    )
    action = p.run_scheduled_batch if scheduled else p.run_batch
    action(limit=1)
    assert received == [own.intent_id]
    assert foreign.intent_id not in received
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_consumer_receipts r JOIN v2_domain_outbox e USING(event_id) WHERE e.intent_id=%s",
                (foreign.intent_id,),
            ).fetchone()[0]
            == 0
        )


def test_projection_claim_filters_existing_foreign_retry_tasks(database):
    foreign = prepared(database, OTHER)[0]
    own = prepared(database, SCOPE)[0]
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_delivery_attempts(consumer,event_id) SELECT 'shared',event_id FROM v2_domain_outbox"
        )
    delivered = []
    result = Projector(
        database,
        "shared",
        lambda event, subject, *_: delivered.append(subject) or True,
        scope=SCOPE,
    ).run_scheduled_batch(limit=1)
    assert result["delivered"] == 1 and delivered == [own.intent_id]
    with database() as conn:
        assert (
            conn.execute(
                "SELECT sum(a.attempts) FROM v2_delivery_attempts a JOIN v2_domain_outbox e USING(event_id) WHERE e.intent_id=%s",
                (foreign.intent_id,),
            ).fetchone()[0]
            == 0
        )


def test_two_scoped_projectors_share_consumer_without_stealing_receipts(database):
    original = {
        scope.account_id: prepared(database, scope)[0].intent_id
        for scope in (SCOPE, OTHER)
    }

    def project(scope):
        seen = []
        p = Projector(
            database,
            "shared",
            lambda event, subject, *_: seen.append(subject) or True,
            scope=scope,
        )
        assert p.run_scheduled_batch()["delivered"] > 0
        assert p.run_scheduled_batch()["claimed"] == 0
        return set(seen)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(project, (SCOPE, OTHER)))
    assert results == [{original[SCOPE.account_id]}, {original[OTHER.account_id]}]


@pytest.mark.parametrize("scope", [None, OTHER, LIVE])
def test_scoped_runtime_rejects_unbound_or_foreign_projector(database, scope):
    p = Projector(database, "projection", lambda *_: True, scope=scope)
    with pytest.raises(ValueError, match="projectors"):
        runtime(database, projectors=(p,))


def test_account_scope_cannot_be_applied_to_operational_outbox(database):
    with pytest.raises(ValueError, match="financial"):
        OperationalProjector(database, "ops", lambda *_: True, scope=SCOPE)


def test_tick_composes_only_scoped_maintenance(database):
    foreign, data, foreign_order, _ = recoverable(database, OTHER)
    own, _, own_order, _ = recoverable(database, SCOPE)
    received = []
    p = Projector(
        database,
        "scope-tick",
        lambda event, subject, *_: received.append(subject) or True,
        scope=SCOPE,
    )
    result = runtime(database, projectors=(p,)).tick(overdue_ms=60000)
    assert result["recovery"] == {own_order: "UNKNOWN"}
    assert set(received) == {own.intent_id}
    assert data.trace(foreign.intent_id)["orders"][0]["status"] == "SUBMITTING"
    with database() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM v2_order_recovery WHERE order_id=%s", (foreign_order,)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda c: runtime(c, scope={}),
        lambda c: ExecutionRunner(
            c,
            submit=lambda _: None,
            query=lambda _: None,
            risk_check=lambda _: True,
            scope={},
        ),
        lambda c: RecoveryAttention(c, scope={}),
        lambda c: Projector(c, "p", lambda *_: True, scope={}),
    ],
)
def test_scope_is_typed_before_any_io(factory):
    with pytest.raises(TypeError):
        factory(lambda: pytest.fail("constructor accessed storage"))


def test_unprepared_expiry_is_also_scoped(database):
    data = TradingData(database)
    foreign = replace(intent(), account_id=OTHER.account_id)
    own = intent()
    data.accept(foreign, evidence())
    data.accept(own, evidence())
    assert runtime(database).expire_once(limit=1) == {own.intent_id: "EXPIRED"}
    assert data.trace(foreign.intent_id)["status"] == "RECEIVED"


def test_same_account_workers_still_claim_only_one_recovery(database):
    own = recoverable(database, SCOPE)[2]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runtime(database).recover_once(), range(4)))
    assert sum(own in result for result in results) == 1


def test_projection_does_not_cross_live_sandbox_boundary(database):
    foreign = prepared(database, LIVE)[0]
    own = prepared(database, SCOPE)[0]
    seen = []
    p = Projector(
        database,
        "env",
        lambda event, subject, *_: seen.append(subject) or True,
        scope=SCOPE,
    )
    assert p.run_scheduled_batch()["delivered"] > 0
    assert set(seen) == {own.intent_id} and foreign.intent_id not in seen


@pytest.mark.parametrize("changes", [{"exchange": "OTHER"}, {"product": "SPOT"}])
def test_exchange_and_product_are_part_of_recovery_scope(database, changes):
    data = TradingData(database)
    foreign = replace(intent(), **changes)
    data.accept(foreign, evidence())
    order, _ = data.orders.prepare(foreign.intent_id)
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    assert runtime(database).recover_once() == {}
    with pytest.raises(ScopeMismatch):
        runtime(database).execution.recover(order)
