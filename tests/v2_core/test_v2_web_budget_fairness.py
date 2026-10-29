"""Real PG Web/market/context fairness and unchanged EXECUTE-only capabilities."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from test_v2_intent_admission import database as database_fixture
from test_v2_web_verification_quota import (
    HOST,
    PATH,
    counters,
    seed,
)
from test_v2_web_verification_quota import (
    policy as host_policy,
)
from test_v2_web_verification_quota import (
    quota as quota_fixture,
)

from v2_core.shared_market import AdmissionBudget, AdmissionBudgetPolicy, HostBudget
from v2_core.state import BusinessState, StateKey
from v2_core.web_verification_quota import WebVerificationPermit

database = database_fixture
quota = quota_fixture
LANE = "v2-host-lane:market:" + HOST
POLICY_KEY = StateKey(
    "BINANCE", "market-data", "SANDBOX", "FUTURES", "admission-budget-policy", "current"
)
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/20261024_admission_budget_fairness.sql"
)


def function_identity(conn):
    return conn.execute(
        "SELECT oid,proowner,proacl,prosecdef,proconfig FROM pg_proc "
        "WHERE oid='v2_web_testnet_verify_permit()'::regprocedure"
    ).fetchone()


@pytest.fixture
def fairness(quota):
    admin, web, role, ns = quota
    with admin() as conn:
        original = function_identity(conn)
        for _ in range(2):
            conn.execute(MIGRATION.read_text())
            assert function_identity(conn) == original
    host_policy(admin, shared_weight_limit=2000, market_weight_limit=550)
    return admin, web, role, ns


def configure(connect, payload=None, *, deleted=False):
    state = BusinessState(connect)
    previous = state.read(POLICY_KEY)
    result = state.change(
        POLICY_KEY,
        expected_version=previous.version if previous else 0,
        request_key=str(uuid4()),
        payload=(
            AdmissionBudgetPolicy(120, 100).values() if payload is None else payload
        ),
        deleted=deleted,
        reason="ISOLATED_WEB_FAIRNESS_QA",
    )
    assert result.code == "APPLIED"


def now(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]


def same_minute(connect, started):
    finished = now(connect)
    if started // 60000 != finished // 60000 or finished % 60000 >= 59500:
        pytest.skip("authoritative DB minute changed during bounded quota sample")


def spend(bucket, weight):
    while weight:
        chunk = min(weight, 100)
        assert bucket.permit(chunk)
        weight -= chunk


def lane(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT window_id,used_weight FROM v2_public_market_budgets WHERE scope=%s",
            (LANE,),
        ).fetchone()


def group(bucket, started):
    # No network: reserve a real adaptive bundle within this database minute.
    # Choosing its bounded deadline does not replace/inject the SQL clock.
    deadline = (started // 60000 + 1) * 60000 - 500
    return bucket.reserve_context(
        account_id="web-fairness-qa",
        symbol="BTCUSDT",
        adaptive=True,
        deadline_ms=deadline,
    )


def test_repeatable_replacement_keeps_acl_owner_and_fixed_state_identity(fairness):
    admin, web, role, _ = fairness
    assert POLICY_KEY.identity == "09f12c30-e371-596f-8f1a-1715b7c7e31b"
    with admin() as conn:
        assert conn.execute(
            "SELECT has_function_privilege(%s,'v2_web_testnet_verify_permit()','EXECUTE')",
            (role,),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT count(*) FROM pg_proc p,LATERAL aclexplode(p.proacl) a "
            "WHERE p.oid='v2_web_testnet_verify_permit()'::regprocedure AND a.grantee=0"
        ).fetchone() == (0,)
        for table in (
            "v2_egress_budgets",
            "v2_public_market_budgets",
            "v2_business_state",
        ):
            for right in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not conn.execute(
                    "SELECT has_table_privilege(%s,%s,%s)", (role, table, right)
                ).fetchone()[0]
    for statement in (
        "UPDATE v2_egress_budgets SET used_weight=0",
        "DELETE FROM v2_public_market_budgets",
        "SELECT payload FROM v2_business_state",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), web() as conn:
            conn.execute(statement)


def test_fixed_schema_defeats_temporary_shadow_and_live_remains_untouched(fairness):
    admin, web, _, ns = fairness
    configure(admin)
    with admin() as conn:
        conn.execute(
            "INSERT INTO v2_egress_budgets(host,used_weight) VALUES('fapi.binance.com',7)"
        )
    with web() as conn:
        conn.execute(
            "CREATE TEMP TABLE v2_egress_budgets(host text,used_weight integer)"
        )
        conn.execute("CREATE TEMP TABLE v2_public_market_budgets(scope text)")
        conn.execute("CREATE TEMP TABLE v2_business_state(payload jsonb)")
        conn.execute("SET LOCAL search_path=pg_temp,pg_catalog")
        assert conn.execute(
            sql.SQL("SELECT {}.v2_web_testnet_verify_permit()").format(
                sql.Identifier(ns)
            )
        ).fetchone()[0]
        assert conn.execute(
            "SELECT count(*) FROM pg_temp.v2_egress_budgets"
        ).fetchone() == (0,)
    assert counters(admin) == (30, 30)
    assert lane(admin)[1] == 0
    with admin() as conn:
        assert conn.execute(
            "SELECT used_weight FROM v2_egress_budgets WHERE host='fapi.binance.com'"
        ).fetchone() == (7,)


@pytest.mark.parametrize("first", ["market", "web"])
def test_web_and_collection_preserve_both_lane_minima_and_management(fairness, first):
    admin, web, _, _ = fairness
    configure(admin)
    permit = WebVerificationPermit(web)
    market = HostBudget(admin, environment="SANDBOX", lane="market")
    started = now(admin)
    if first == "market":
        spend(market, 430)
        allowed = [permit("GET", PATH) for _ in range(5)]
        same_minute(admin, started)
        assert allowed == [True] * 4 + [False]
    else:
        allowed = [permit("GET", PATH) for _ in range(16)]
        same_minute(admin, started)
        assert allowed == [True] * 15 + [False]
        spend(market, 100)
    same_minute(admin, started)
    assert counters(admin) == (550, 550)
    before = counters(admin)
    allowed = permit("GET", PATH)
    same_minute(admin, started)
    assert not allowed and counters(admin) == before
    spend(HostBudget(admin, environment="SANDBOX"), 1450)
    same_minute(admin, started)
    assert counters(admin) == (2000, 550)


def test_concurrent_web_market_and_contexts_share_one_host_without_bypasses(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    permit = WebVerificationPermit(web)
    admission = AdmissionBudget(admin, environment="SANDBOX")
    market = HostBudget(admin, environment="SANDBOX", lane="market")
    started = now(admin)
    if started % 60000 >= 58000:
        pytest.skip("need a bounded interval before authoritative DB rollover")
    # Exercise each real writer once before mixed contention, including a bundle.
    group(admission, started)
    assert permit("GET", PATH) and market.permit(30)

    def attempt(index):
        if index % 3 == 0:
            return "market", market.permit(30)
        if index % 3 == 1:
            return "admission", permit("GET", PATH)
        try:
            group(admission, started)
        except ValueError as exc:
            assert str(exc) == "ADMISSION_BUDGET_UNAVAILABLE"
            return "admission", False
        return "admission", True

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(60)))
    same_minute(admin, started)
    market_weight = 30 + 30 * sum(ok for kind, ok in results if kind == "market")
    admission_weight = 60 + 30 * sum(ok for kind, ok in results if kind == "admission")
    assert market_weight <= 430 and admission_weight <= 450
    assert market_weight + admission_weight <= 550
    assert counters(admin) == (market_weight + admission_weight,) * 2
    assert lane(admin) == (started // 60000, market_weight)
    spend(HostBudget(admin, environment="SANDBOX"), 1450)
    same_minute(admin, started)
    assert counters(admin) == (
        market_weight + admission_weight + 1450,
        market_weight + admission_weight,
    )


@pytest.mark.parametrize(
    "field", ["admission_reserved_weight", "market_reserved_weight"]
)
@pytest.mark.parametrize("bad", [True, None, "100", -1, 1.5, 100000])
def test_bad_reserve_values_fail_closed_without_disabling_management(
    fairness, field, bad
):
    admin, web, _, _ = fairness
    if type(bad) is float:
        # BusinessState already rejects floats. Inject corrupt persisted JSON
        # only in this isolated database to exercise the SQL trust boundary.
        configure(admin, {field: 0})
        with admin() as conn:
            conn.execute(
                "UPDATE v2_business_state SET version=version+1,payload=%s WHERE state_id=%s",
                (Jsonb({field: bad}), POLICY_KEY.identity),
            )
            conn.execute(
                "INSERT INTO v2_state_history(event_id,state_id,request_key,"
                "expected_version,version,payload,deleted,reason) "
                "VALUES(%s,%s,%s,1,2,%s,false,'ISOLATED_CORRUPT_POLICY_QA')",
                (uuid4(), POLICY_KEY.identity, str(uuid4()), Jsonb({field: bad})),
            )
    else:
        configure(admin, {field: bad})
    with pytest.raises(
        psycopg.errors.RaiseException, match="ADMISSION_BUDGET_POLICY_BOUNDS"
    ):
        WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) is None
    assert HostBudget(admin, environment="SANDBOX").permit(5)
    assert counters(admin) == (5, 0)


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"unexpected": 1}, "BOUNDS"),
        ({"admission_reserved_weight": 1751}, "BOUNDS"),
        ({"market_reserved_weight": 1801}, "BOUNDS"),
        ({"admission_reserved_weight": 501}, "RESERVE_INVALID"),
        (
            {"admission_reserved_weight": 120, "market_reserved_weight": 431},
            "RESERVE_INVALID",
        ),
    ],
)
def test_unknown_fields_or_invalid_joint_allocation_fail_closed(
    fairness, payload, code
):
    admin, web, _, _ = fairness
    configure(admin, payload)
    with pytest.raises(
        psycopg.errors.RaiseException, match="ADMISSION_BUDGET_POLICY_" + code
    ):
        WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) is None
    assert HostBudget(admin, environment="SANDBOX").permit(5)


def test_disabled_admission_policy_does_not_disable_management(fairness):
    admin, web, _, _ = fairness
    configure(admin, {}, deleted=True)
    with pytest.raises(
        psycopg.errors.RaiseException, match="ADMISSION_BUDGET_POLICY_DISABLED"
    ):
        WebVerificationPermit(web)("GET", PATH)
    assert HostBudget(admin, environment="SANDBOX").permit(5)
    assert counters(admin) == (5, 0)


@pytest.mark.parametrize("payload", [None, {}, {"admission_reserved_weight": 120}])
def test_missing_or_legacy_single_field_policy_keeps_compatibility(fairness, payload):
    admin, web, _, _ = fairness
    if payload is not None:
        configure(admin, payload)
    assert WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) == (30, 30)
    assert lane(admin)[1] == 0


def test_existing_admission_usage_is_counted_before_web_verification(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    started = now(admin)
    spend(AdmissionBudget(admin, environment="SANDBOX"), 450)
    allowed = WebVerificationPermit(web)("GET", PATH)
    same_minute(admin, started)
    assert not allowed and counters(admin) == (450, 450)
    assert lane(admin)[1] == 0


def test_legacy_unclassified_usage_is_preserved_until_a_fresh_minute(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    started = now(admin)
    seed(admin, used=430, market=430)
    assert WebVerificationPermit(web)("GET", PATH)
    same_minute(admin, started)
    assert counters(admin) == (460, 460)
    assert lane(admin) == (started // 60000, 430)
    # Model old counters from the previous minute, never reset current quotas.
    seed(admin, used=550, market=550, offset=-1)
    with admin() as conn:
        conn.execute(
            "UPDATE v2_public_market_budgets SET window_id=%s,used_weight=430 WHERE scope=%s",
            (started // 60000 - 1, LANE),
        )
    assert WebVerificationPermit(web)("GET", PATH)
    spend(HostBudget(admin, environment="SANDBOX", lane="market"), 100)
    same_minute(admin, started)
    assert counters(admin) == (130, 130)
    assert lane(admin) == (started // 60000, 100)


@pytest.mark.parametrize("future", ["host", "lane"])
def test_future_ledger_window_fails_closed_without_reset(fairness, future):
    admin, web, _, _ = fairness
    configure(admin)
    started = now(admin)
    seed(admin, used=30, market=30, offset=1 if future == "host" else 0)
    if future == "lane":
        with admin() as conn:
            conn.execute(
                "INSERT INTO v2_public_market_budgets(scope,weight_limit,window_id,used_weight) VALUES(%s,2400,floor(extract(epoch FROM clock_timestamp())/60)::bigint+1,0)",
                (LANE,),
            )
    before = counters(admin)
    allowed = WebVerificationPermit(web)("GET", PATH)
    same_minute(admin, started)
    assert not allowed
    assert counters(admin) == before


def test_host_cooldown_cannot_be_bypassed_by_web_or_minute_rollover(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    seed(admin, used=550, market=550, offset=-1)
    WebVerificationPermit(web).penalize(60)
    before = counters(admin)
    assert not WebVerificationPermit(web)("GET", PATH)
    assert not HostBudget(admin, environment="SANDBOX").permit(5)
    assert counters(admin) == before and lane(admin) is None


def test_host_ceiling_applies_even_when_noncritical_has_room(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    started = now(admin)
    seed(admin, used=1975, market=0)
    allowed = WebVerificationPermit(web)("GET", PATH)
    same_minute(admin, started)
    assert not allowed and counters(admin) == (1975, 0)
    assert lane(admin) is None


def test_failed_host_update_rolls_back_new_lane_seed_and_quota(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    seed(admin)
    with admin() as conn:
        conn.execute(
            "CREATE FUNCTION qa_reject_host_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'QA_WRITE_FAILED'; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER qa_reject_host_update BEFORE UPDATE ON v2_egress_budgets FOR EACH ROW EXECUTE FUNCTION qa_reject_host_update()"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="QA_WRITE_FAILED"):
        WebVerificationPermit(web)("GET", PATH)
    assert counters(admin) == (0, 0) and lane(admin) is None


def test_outer_verification_rollback_cannot_refund_committed_quota(fairness):
    admin, web, _, _ = fairness
    configure(admin)
    with pytest.raises(RuntimeError), web() as conn:
        conn.execute("SELECT 1")
        assert WebVerificationPermit(web)("GET", PATH)
        raise RuntimeError("later verification receipt failed")
    assert counters(admin) == (30, 30)
    assert lane(admin)[1] == 0
