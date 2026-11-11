"""Real isolated PostgreSQL ACL, contention and no-network boundary checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_transport import Connection

from services.v2_testnet_daemon_entry import PrivateRatePermit
from tests.support.v2_admission import database as database_fixture
from v2_core.shared_market import HostBudget, MarketPolicy
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError
from v2_core.web_verification_quota import WebVerificationPermit

database = database_fixture
HOST = "demo-fapi.binance.com"
PATH = "/fapi/v1/positionSide/dual"
KEY = StateKey(
    "BINANCE", "market-data", "SANDBOX", "FUTURES", "shared-market-policy", "current"
)


@pytest.fixture
def quota(database):
    root = Path(__file__).resolve().parents[2]
    role = "web_quota_" + uuid4().hex
    with database() as c:
        for migration in (
            "20260929_shared_market.sql",
            "20260930_web_verification_quota.sql",
        ):
            c.execute((root / "db/migrations" / migration).read_text())
        ns = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        c.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(ns), sql.Identifier(role)
            )
        )
        for signature in (
            "v2_web_testnet_verify_permit()",
            "v2_web_testnet_quota_cooldown(integer)",
        ):
            c.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                    sql.SQL(signature), sql.Identifier(role)
                )
            )

    @contextmanager
    def web():
        with database() as c:
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield c

    try:
        yield database, web, role, ns
    finally:
        with database() as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def seed(connect, *, used=0, market=0, offset=0):
    with connect() as c:
        c.execute(
            "INSERT INTO v2_egress_budgets(host,window_id,used_weight,market_weight) "
            "VALUES(%s,floor(extract(epoch FROM clock_timestamp())/60)::bigint+%s,%s,%s) "
            "ON CONFLICT(host) DO UPDATE SET window_id=EXCLUDED.window_id,"
            "used_weight=EXCLUDED.used_weight,market_weight=EXCLUDED.market_weight",
            (HOST, offset, used, market),
        )


def counters(connect):
    with connect() as c:
        return c.execute(
            "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
            (HOST,),
        ).fetchone()


def policy(connect, **changes):
    row = BusinessState(connect).read(KEY)
    BusinessState(connect).change(
        KEY,
        expected_version=row.version if row else 0,
        request_key=str(uuid4()),
        payload={**MarketPolicy().values(), **changes},
        reason="isolated quota QA",
    )


def test_fixed_cost_is_shared_and_live_is_untouched(quota):
    admin, web, _, _ = quota
    with admin() as c:
        c.execute(
            "INSERT INTO v2_egress_budgets(host,used_weight) VALUES('fapi.binance.com',7)"
        )
    assert WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) == (30, 30)
    assert PrivateRatePermit._READS[PATH] == 30
    with admin() as c:
        assert c.execute(
            "SELECT used_weight FROM v2_egress_budgets WHERE host='fapi.binance.com'"
        ).fetchone() == (7,)


def test_acl_functions_only_no_public_or_direct_table_access(quota):
    admin, web, role, _ = quota
    with admin() as c:
        for signature in (
            "v2_web_testnet_verify_permit()",
            "v2_web_testnet_quota_cooldown(integer)",
        ):
            assert c.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, signature)
            ).fetchone()[0]
            assert (
                c.execute(
                    "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a "
                    "WHERE p.oid=%s::regprocedure AND a.grantee=0",
                    (signature,),
                ).fetchone()[0]
                == 0
            )
        for table in ("v2_egress_budgets", "v2_business_state"):
            for right in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not c.execute(
                    "SELECT has_table_privilege(%s,%s,%s)", (role, table, right)
                ).fetchone()[0]
    for statement in (
        "UPDATE v2_egress_budgets SET used_weight=0",
        "DELETE FROM v2_egress_budgets",
        "UPDATE v2_business_state SET payload='{}'",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), web() as c:
            c.execute(statement)


def test_definer_ignores_caller_search_path_and_temp_shadow(quota):
    admin, web, _, ns = quota
    with web() as c:
        c.execute("CREATE TEMP TABLE v2_egress_budgets(host text,used_weight integer)")
        c.execute("CREATE TEMP TABLE v2_business_state(payload jsonb)")
        c.execute("SET LOCAL search_path=pg_temp,pg_catalog")
        assert c.execute(
            sql.SQL("SELECT {}.v2_web_testnet_verify_permit()").format(
                sql.Identifier(ns)
            )
        ).fetchone()[0]
        assert c.execute(
            "SELECT count(*) FROM pg_temp.v2_egress_budgets"
        ).fetchone() == (0,)
    assert counters(admin) == (30, 30)


def test_concurrent_web_and_market_preserve_management_reserve(quota):
    admin, web, _, _ = quota
    policy(admin, shared_weight_limit=120, market_weight_limit=60)
    seed(admin)
    permit = WebVerificationPermit(web)
    market = HostBudget(admin, environment="SANDBOX", lane="market")
    # The production database clock is deliberately not injectable by Web.
    with admin() as c:
        before = c.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())/60)"
        ).fetchone()[0]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda n: permit("GET", PATH) if n % 2 else market.permit(30), range(24)
            )
        )
    with admin() as c:
        after = c.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())/60)"
        ).fetchone()[0]
    if before != after:
        pytest.skip("database minute rolled during contention sample")
    assert sum(results) == 2
    assert counters(admin) == (60, 60)
    assert HostBudget(admin, environment="SANDBOX").permit(60)
    assert not permit("GET", PATH)
    assert counters(admin) == (120, 60)


def test_dynamic_policy_changes_without_web_restart(quota):
    admin, web, _, _ = quota
    policy(admin, shared_weight_limit=120, market_weight_limit=60)
    seed(admin, used=60, market=60)
    permit = WebVerificationPermit(web)
    assert not permit("GET", PATH)
    policy(admin, shared_weight_limit=150, market_weight_limit=90)
    assert permit("GET", PATH)
    assert counters(admin) == (90, 90)


@pytest.mark.parametrize("bad", [True, None, "900", -1, 2401])
def test_invalid_policy_fails_closed(quota, bad):
    admin, web, _, _ = quota
    policy(admin, shared_weight_limit=bad)
    with pytest.raises(psycopg.errors.RaiseException, match="MARKET_POLICY_BOUNDS"):
        WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) is None


def test_disabled_policy_fails_closed(quota):
    admin, web, _, _ = quota
    policy(admin)
    BusinessState(admin).change(
        KEY,
        expected_version=1,
        request_key=str(uuid4()),
        payload={},
        deleted=True,
        reason="QA disable",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="MARKET_POLICY_DISABLED"):
        WebVerificationPermit(web)("GET", PATH)


def test_rollover_uses_database_clock_and_keeps_cooldown(quota):
    admin, web, _, _ = quota
    permit = WebVerificationPermit(web)
    seed(admin, used=1800, market=900, offset=-1)
    assert permit("GET", PATH)
    assert counters(admin) == (30, 30)
    seed(admin, used=30, market=30, offset=1)
    assert not permit("GET", PATH)
    permit.penalize(120)
    with admin() as c:
        first = c.execute(
            "SELECT blocked_until FROM v2_egress_budgets WHERE host=%s", (HOST,)
        ).fetchone()[0]
    permit.penalize(1)
    seed(admin, used=30, market=30, offset=-1)
    assert not permit("GET", PATH)
    assert counters(admin) == (30, 30)
    with admin() as c:
        assert (
            c.execute(
                "SELECT blocked_until FROM v2_egress_budgets WHERE host=%s", (HOST,)
            ).fetchone()[0]
            == first
        )


@pytest.mark.parametrize("seconds", [None, 0, -1, 259201])
def test_sql_cooldown_rejects_invalid_values(quota, seconds):
    _, web, _, _ = quota
    with (
        pytest.raises(psycopg.errors.RaiseException, match="EGRESS_COOLDOWN_INVALID"),
        web() as c,
    ):
        c.execute("SELECT v2_web_testnet_quota_cooldown(%s)", (seconds,))


def test_outer_transaction_failure_cannot_refund_quota(quota):
    admin, web, _, _ = quota
    with pytest.raises(RuntimeError), web() as c:
        c.execute("SELECT 1")
        assert WebVerificationPermit(web)("GET", PATH)
        raise RuntimeError("receipt rolled back")
    assert counters(admin) == (30, 30)


@pytest.mark.parametrize(
    "method,path", [("POST", PATH), ("GET", "/fapi/v1/order"), ("GET", PATH + "?x=1")]
)
def test_unsupported_routes_never_connect(method, path):
    def unavailable():
        raise AssertionError("must not connect")

    assert not WebVerificationPermit(unavailable)(method, path)


def client(web, connection):
    return BinanceSignedTransport(
        account_id="qa",
        environment="SANDBOX",
        api_key="dummy",
        api_secret="dummy",
        clock_ms=lambda: 1000,
        permit=WebVerificationPermit(web),
        connection_factory=lambda *_, **_kwargs: connection,
    )


def test_quota_denial_prevents_network(quota):
    admin, web, _, _ = quota
    WebVerificationPermit(web).penalize(60)
    conn = Connection()
    with pytest.raises(ExchangeTransportError):
        client(web, conn)("GET", PATH, {})
    assert not conn.calls
    assert counters(admin) == (0, 0)


@pytest.mark.parametrize("status,minimum", [(429, 59), (418, 86399)])
def test_malformed_exchange_throttle_sets_shared_cooldown(quota, status, minimum):
    admin, web, _, _ = quota
    conn = Connection()
    conn.status, conn.raw = status, b"not-json"
    with pytest.raises(ExchangeTransportError):
        client(web, conn)("GET", PATH, {})
    assert len(conn.calls) == 1
    assert counters(admin) == (30, 30)
    with admin() as c:
        assert (
            c.execute(
                "SELECT extract(epoch FROM blocked_until-clock_timestamp()) FROM v2_egress_budgets WHERE host=%s",
                (HOST,),
            ).fetchone()[0]
            >= minimum
        )
    assert not HostBudget(admin, environment="SANDBOX").permit(1)
