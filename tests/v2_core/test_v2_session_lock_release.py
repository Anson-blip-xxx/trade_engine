"""Delayed backend termination must not create a spurious next-stage BUSY."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from threading import Event
from types import SimpleNamespace

import pytest
from test_v2_external_close_recovery import SCOPE
from test_v2_external_close_recovery import setup as setup_fixture
from test_v2_protection_child import database as database_fixture
from test_v2_protection_child import setup as original_setup_fixture

from services.v2_directional_lifecycle import DirectionalProtectionStage
from v2_core.protection import TestnetProtection
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState
from v2_core.transport import ExchangeTransportError

database = database_fixture
original_setup = original_setup_fixture
setup = setup_fixture
LOCK = "testnet-protocol-account:" + SCOPE.key


def _available(database, key=LOCK):
    with database() as conn:
        result = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
        ).fetchone()[0]
        if result:
            assert conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,)
            ).fetchone()[0]
        return result


def test_success_busy_and_separate_scopes_release_before_connection_close(database):
    owners = []

    @contextmanager
    def retained():
        # Hold the connection physically open after helper exit. Availability
        # must depend on acknowledged unlock, not socket termination.
        manager = database()
        owner = manager.__enter__()
        owners.append((manager, owner))
        yield owner

    try:
        with session_advisory_lock(retained, LOCK) as acquired:
            assert acquired is True
            assert not _available(database)
            with session_advisory_lock(database, LOCK) as competing:
                assert competing is False
            with session_advisory_lock(database, LOCK + ":other-account") as other:
                assert other is True
        assert owners[0][1].closed is False
        assert _available(database)
        with session_advisory_lock(database, LOCK) as subsequent:
            assert subsequent is True
    finally:
        for manager, _ in owners:
            manager.__exit__(None, None, None)


def test_body_exception_releases_lock_without_rolling_back_business_work(database):
    with database() as conn:
        conn.execute("CREATE TABLE qa_lock_business(value int NOT NULL)")
    original = RuntimeError("body failure")
    with (
        pytest.raises(RuntimeError) as caught,
        session_advisory_lock(database, LOCK) as acquired,
    ):
        assert acquired
        with database() as business:
            business.execute("INSERT INTO qa_lock_business VALUES (1)")
        raise original
    assert caught.value is original
    assert _available(database)
    with database() as conn:
        assert conn.execute("SELECT value FROM qa_lock_business").fetchall() == [(1,)]


def test_aborted_lock_connection_transaction_is_rolled_back_before_unlock(database):
    owners = []

    @contextmanager
    def connect():
        with database() as owner:
            owners.append(owner)
            yield owner

    with session_advisory_lock(connect, LOCK) as acquired:
        assert acquired
        from psycopg.errors import DivisionByZero

        with pytest.raises(DivisionByZero):
            owners[0].execute("SELECT 1/0")
    assert _available(database)


def test_parallel_scope_owner_is_exclusive_and_next_owner_can_start(database):
    acquired, release = Event(), Event()

    def first():
        with session_advisory_lock(database, LOCK) as held:
            assert held
            acquired.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert acquired.wait(5)
            with session_advisory_lock(database, LOCK) as held:
                assert held is False
            with session_advisory_lock(database, LOCK + ":other") as held:
                assert held is True
        finally:
            release.set()
        future.result(timeout=5)
    assert _available(database)


@pytest.mark.parametrize("body_error", [False, True])
@pytest.mark.parametrize("release_error", [False, True])
def test_unconfirmed_release_closes_connection_and_preserves_original_error(
    body_error, release_error
):
    class Owner:
        closed = False

        def execute(self, statement, *_params):
            if "pg_advisory_unlock(" in statement:
                if release_error:
                    raise RuntimeError("secret transport detail")
                return SimpleNamespace(fetchone=lambda: (False,))
            return SimpleNamespace(fetchone=lambda: (True,))

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    owner = Owner()

    @contextmanager
    def connect():
        yield owner

    original = RuntimeError("original body failure")
    with (
        pytest.raises(RuntimeError) as caught,
        session_advisory_lock(connect, LOCK) as held,
    ):
        assert held
        if body_error:
            raise original
    assert owner.closed
    if body_error:
        assert caught.value is original
    else:
        assert str(caught.value) == "SESSION_LOCK_RELEASE_UNCONFIRMED"
        assert "secret" not in str(caught.value)


def test_legacy_close_only_handoff_blocks_but_acknowledged_unlock_converges(
    database, setup, monkeypatch
):
    data, spec, venue, _worker = setup
    pending_sessions = []

    @contextmanager
    def delayed_backend_close():
        manager = database()
        owner = manager.__enter__()
        try:
            yield owner
        except BaseException as exc:
            manager.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            owner.commit()
            held = owner.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted)"
            ).fetchone()[0]
            owner.commit()
            if held:
                # Deterministically model the interval before the backend has
                # processed the client's Terminate/socket-close notification.
                pending_sessions.append((manager, owner))
            else:
                manager.__exit__(None, None, None)

    def finish_backend_termination():
        while pending_sessions:
            manager, owner = pending_sessions.pop()
            owner.rollback()
            owner.execute("SELECT pg_advisory_unlock_all()")
            owner.commit()
            manager.__exit__(None, None, None)

    @contextmanager
    def legacy_close_only(connect, key):
        with connect() as owner:
            held = owner.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
            ).fetchone()[0]
            owner.commit()
            yield held

    def request(method, path, params):
        if path == "/fapi/v1/algoOrder":
            assert method == "GET"
            raise ExchangeTransportError(
                "EXCHANGE_RESPONSE_ERROR", status=400, code=-2013
            )
        return venue(method, path, params)

    request.account_id, request.environment = SCOPE.account_id, SCOPE.environment
    stage = DirectionalProtectionStage(
        delayed_backend_close,
        request,
        scope=SCOPE,
        clock_ms=lambda: 100,
        reference=lambda _: pytest.fail("No market read for flat reconciliation"),
        allow_writes=False,
    )
    journal = stage.supervisor.child.protection
    snapshot, payload = journal.read(spec)
    assert journal.save(spec, snapshot, {**payload, "status": "UNKNOWN"})
    try:
        with monkeypatch.context() as patched:
            for module in (
                "v2_core.protection_supervisor",
                "v2_core.external_close_recovery",
                "v2_core.account_coverage",
                "services.v2_directional_lifecycle",
            ):
                patched.setattr(module + ".session_advisory_lock", legacy_close_only)
            legacy = stage.run_once()
        assert legacy["status"] == "BLOCKED"
        assert legacy["external_closes"]["results"][0]["status"] == "BUSY"
        assert legacy["coverage"]["status"] == "BUSY"
        assert pending_sessions
        assert len(data.trace(spec.episode)["fills"]) == 1
        finish_backend_termination()
        fixed = stage.run_once()
        assert fixed["status"] == "CLEAR", fixed
        assert fixed["external_closes"]["results"][0]["status"] == "RECONCILED"
        assert fixed["coverage"]["status"] == "ACCOUNT_COVERAGE_CLEAR"
        assert pending_sessions == []
        assert stage.run_once()["status"] == "CLEAR"
        assert len(data.trace(spec.episode)["orders"]) == 2
        assert len(data.trace(spec.episode)["fills"]) == 2
        assert all(method == "GET" for method, _, _ in venue.calls)
    finally:
        finish_backend_termination()


def test_flat_cleanup_releases_before_next_coverage_owner(database, setup):
    _data, original, _venue, external = setup
    assert external.reconcile(original.episode)["status"] == "RECONCILED"
    spec = replace(original, kind="TAKE_PROFIT_MARKET", trigger_price="101")
    parents, owners, writes = {}, [], []

    def request(method, path, params):
        if path.endswith(("openOrders", "positionRisk")):
            assert method == "GET"
            return []
        assert path.endswith("algoOrder")
        if method == "DELETE":
            writes.append(dict(params))
            parents["algoStatus"] = "CANCELED"
        else:
            assert method == "GET"
        return dict(parents)

    request.account_id, request.environment = SCOPE.account_id, SCOPE.environment
    protection = TestnetProtection(BusinessState(database), request, scope=SCOPE)
    parents.update(
        **protection.params(spec),
        orderType=spec.kind,
        algoId=101,
        algoStatus="NEW",
    )
    parents.update(closePosition=True, priceProtect=False)
    assert protection.save(spec, None, {"spec": asdict(spec), "status": "NEW"})

    @contextmanager
    def retained():
        manager = database()
        owner = manager.__enter__()
        owners.append((manager, owner))
        try:
            yield owner
        finally:
            owner.commit()

    stage = DirectionalProtectionStage(
        retained,
        request,
        scope=SCOPE,
        clock_ms=lambda: 100,
        reference=lambda _: pytest.fail("Flat cleanup has no market dependency"),
        allow_writes=True,
    )
    try:
        assert stage._cleanup_flat(protection.key(spec).identity) == {
            "status": "CLEANED",
            "parent_status": "CANCELED",
        }
        assert all(not owner.closed for _, owner in owners)
        assert _available(database)
        assert writes == [{"algoId": 101}]
    finally:
        for manager, _ in reversed(owners):
            manager.__exit__(None, None, None)
