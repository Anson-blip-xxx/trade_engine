"""Explicit fresh-ledger capital enrollment; no venue/network writes."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import errors, sql
from test_v2_capital_journal import database, post
from test_v2_capital_journal import setup as capital_setup
from test_v2_intent_admission import evidence, intent

from services import v2_managed_testnet as managed
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.income import IncomeJournal, IncomeScope
from v2_core.runtime_policy import PolicySnapshot, PolicyStore, resolve
from v2_core.service import TradingData

__all__ = ["capital_setup", "database"]
SCOPE = AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
NOW = 1792540800000
BASELINE_FIELDS = {
    "capital.reference_wallet",
    "capital.started_at_ms",
    "capital.initial_equity",
}


def configured_values(**changes):
    return resolve(
        {
            "capital.enabled": True,
            "capital.model_enabled": True,
            "capital.initial_equity": "1000",
            "capital.reference_wallet": "5000",
            "capital.started_at_ms": 0,
            "capital.pool_fraction": "0.70",
            "sizing.pool_fraction": "0.70",
            "portfolio_risk.enabled": True,
            "recovery.enabled": True,
            "entry.min_score": 47,
            **changes,
        }
    )


class Connection:
    def __init__(self):
        self.calls = []
        self.residuals = (False,) * 6

    def execute(self, query, params=()):
        self.calls.append((query, params))
        result = (
            (not any(self.residuals),)
            if "v2_fresh_capital_scope_clear" in query
            else (1,)
        )
        return SimpleNamespace(fetchone=lambda: result)


@pytest.fixture
def fake(monkeypatch):
    conn = Connection()
    snapshots = {SCOPE.key: PolicySnapshot(7, canonical(configured_values()))}
    patches = []

    class Store:
        def __init__(self, connect, scope):
            self.scope = scope

        def read(self):
            return snapshots.get(
                self.scope.key, PolicySnapshot(0, canonical(resolve()))
            )

        def patch(self, changes, *, expected_version, reason):
            before = self.read()
            assert expected_version == before.version
            patches.append((self.scope, changes, expected_version, reason))
            result = PolicySnapshot(
                expected_version + 1, canonical(resolve({**before.values, **changes}))
            )
            snapshots[self.scope.key] = result
            return result

    monkeypatch.setattr(managed, "PolicyStore", Store)
    return conn, snapshots, patches


@pytest.mark.parametrize("wallet,initial", [("4000.50", "1000"), ("725.25", "725.25")])
def test_fresh_marker_only_changes_three_fields(fake, wallet, initial):
    conn, snapshots, patches = fake
    before = snapshots[SCOPE.key]
    assert managed._initialize_fresh_capital(
        conn, SCOPE, wallet=Decimal(wallet), started_at_ms=NOW
    )
    after = snapshots[SCOPE.key]
    assert after.version == before.version + 1
    assert after.values["capital.initial_equity"] == initial
    assert after.values["capital.reference_wallet"] == wallet
    assert after.values["capital.started_at_ms"] == NOW
    assert set(patches[0][1]) == BASELINE_FIELDS
    assert {k: v for k, v in before.values.items() if k not in BASELINE_FIELDS} == {
        k: v for k, v in after.values.items() if k not in BASELINE_FIELDS
    }
    queries = [query for query, _ in conn.calls]
    assert "pg_advisory_xact_lock" in queries[1]
    assert queries.index("SET LOCAL lock_timeout='1s'") < 1
    assert "v2_fresh_capital_scope_clear" in queries[-1]
    assert not any(query.startswith("LOCK TABLE") for query in queries)
    assert conn.calls[-1][1] == (
        SCOPE.exchange,
        SCOPE.account_id,
        SCOPE.environment,
        SCOPE.product,
    )


@pytest.mark.parametrize("residual", range(6))
def test_any_old_scope_evidence_refuses_reset(fake, residual):
    conn, snapshots, patches = fake
    conn.residuals = tuple(i == residual for i in range(6))
    before = snapshots[SCOPE.key]
    with pytest.raises(ValueError, match="FRESH_CAPITAL_LEDGER_NOT_EMPTY"):
        managed._initialize_fresh_capital(
            conn, SCOPE, wallet=Decimal(3000), started_at_ms=NOW
        )
    assert snapshots[SCOPE.key] == before
    assert patches == []


@pytest.mark.parametrize("version,started", [(0, 0), (7, 1), (7, NOW - 1000)])
def test_no_explicit_marker_never_rebaselines(fake, version, started):
    conn, snapshots, patches = fake
    snapshots[SCOPE.key] = PolicySnapshot(
        version, canonical(configured_values(**{"capital.started_at_ms": started}))
    )
    assert not managed._initialize_fresh_capital(
        conn, SCOPE, wallet=Decimal(800), started_at_ms=NOW
    )
    assert patches == []
    assert not any(query.startswith("LOCK TABLE") for query, _ in conn.calls)


def test_repeat_activation_does_not_reset_freshly_accrued_capital(fake):
    conn, snapshots, patches = fake
    managed._initialize_fresh_capital(
        conn, SCOPE, wallet=Decimal(3500), started_at_ms=NOW
    )
    once = snapshots[SCOPE.key]
    assert not managed._initialize_fresh_capital(
        conn, SCOPE, wallet=Decimal(3000), started_at_ms=NOW + 1000
    )
    assert snapshots[SCOPE.key] == once and len(patches) == 1


@pytest.mark.parametrize("wallet", ["NaN", "Infinity", "-Infinity", "0", "-1", "0.99"])
def test_invalid_or_under_minimum_wallet_is_not_initialized(fake, wallet):
    conn, _, patches = fake
    with pytest.raises(ValueError, match="FRESH_CAPITAL_INVALID_BASELINE"):
        managed._initialize_fresh_capital(
            conn, SCOPE, wallet=Decimal(wallet), started_at_ms=NOW
        )
    assert not patches


@pytest.mark.parametrize("started", [0, -1, True, "1000", 4133980800001])
def test_invalid_cutoff_is_rejected(fake, started):
    conn, _, patches = fake
    with pytest.raises(ValueError, match="FRESH_CAPITAL_INVALID_BASELINE"):
        managed._initialize_fresh_capital(
            conn, SCOPE, wallet=Decimal(4000), started_at_ms=started
        )
    assert not patches


def test_live_is_never_rebased(fake):
    conn, _, patches = fake
    with pytest.raises(ValueError, match="FRESH_CAPITAL_SANDBOX_REQUIRED"):
        managed._initialize_fresh_capital(
            conn,
            replace(SCOPE, environment="LIVE"),
            wallet=Decimal(4000),
            started_at_ms=NOW,
        )
    assert not conn.calls and not patches


def ports(fake, monkeypatch, *, blockers=()):
    conn, _, _ = fake
    monkeypatch.setattr(managed, "database", lambda: lambda: nullcontext(conn))
    monkeypatch.setattr(managed.time, "time_ns", lambda: NOW * 1000000)
    port = managed.TestnetPorts("qa-tenant", {})
    monkeypatch.setattr(
        port,
        "inventory",
        lambda _: (
            SCOPE,
            {"summary": {"balance": {"totalWalletBalance": "3500"}}},
            list(blockers),
        ),
    )
    monkeypatch.setattr(port, "ledger_clear", lambda _: [])
    return port


def test_preflight_existing_policy_uses_own_cap_without_template(fake, monkeypatch):
    port = ports(fake, monkeypatch)
    assert port.preflight({}) == []
    assert fake[1][SCOPE.key].values["capital.initial_equity"] == "1000"
    assert set(fake[2][0][1]) == BASELINE_FIELDS


def test_preflight_never_baselines_when_venue_is_not_clear(fake, monkeypatch):
    port = ports(fake, monkeypatch, blockers=["EXISTING_POSITION_REQUIRES_RECOVERY"])
    assert port.preflight({}) == ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    assert not fake[2]


def test_preflight_residual_or_driver_failure_remains_sanitized(fake, monkeypatch):
    port = ports(fake, monkeypatch)
    fake[0].residuals = (True,) * 6
    assert port.preflight({}) == ["TARGET_PREFLIGHT_FAILED"]
    assert not fake[2]


@pytest.mark.parametrize(
    "error,expected",
    [
        (errors.InsufficientPrivilege, "TARGET_PREFLIGHT_PERMISSION_DENIED"),
        (errors.UndefinedFunction, "FRESH_CAPITAL_SCHEMA_NOT_READY"),
    ],
)
def test_preflight_permission_and_schema_failures_have_safe_codes(
    fake, monkeypatch, error, expected
):
    port = ports(fake, monkeypatch)
    original = fake[0].execute

    def fail(query, params=()):
        if "v2_fresh_capital_scope_clear" in query:
            raise error("secret credential raw database detail")
        return original(query, params)

    monkeypatch.setattr(fake[0], "execute", fail)
    assert port.preflight({}) == [expected]
    assert not fake[2]


def test_version_zero_keeps_original_template_initialization(fake, monkeypatch):
    port = ports(fake, monkeypatch)
    fake[1].pop(SCOPE.key)
    template = replace(SCOPE, account_id="template")
    fake[1][template.key] = PolicySnapshot(3, canonical(configured_values()))
    port.values = {
        "V2_ACCOUNT_ID": template.account_id,
        "V2_CONTROLLER_INITIAL_EQUITY": "800",
    }
    assert port.preflight({}) == []
    assert fake[1][SCOPE.key].values["capital.initial_equity"] == "800"
    assert fake[1][SCOPE.key].values["entry.min_score"] == 47
    assert fake[2][0][3] == "MANAGED_TESTNET_INITIAL_PROFILE"
    assert not any(query.startswith("LOCK TABLE") for query, _ in fake[0].calls)


def install_policy(database):
    store = PolicyStore(database, SCOPE)
    store.patch(
        configured_values(), expected_version=0, reason="QA_EXPLICIT_FRESH_MARKER"
    )
    return store


@pytest.fixture
def setup(database, capital_setup):
    with database() as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='postgres'"
        ).fetchone():
            # This role exists only in the disposable Unix-socket QA cluster.
            conn.execute("CREATE ROLE postgres SUPERUSER NOLOGIN")
        conn.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20261021_fresh_capital_baseline.sql"
            ).read_text()
        )
    return capital_setup


@pytest.fixture
def worker(database, setup):
    role = "fresh_worker_" + uuid4().hex
    vault = "fresh_vault_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        for value in (role, vault):
            conn.execute(
                sql.SQL("CREATE ROLE {} NOSUPERUSER NOINHERIT NOLOGIN").format(
                    sql.Identifier(value)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(value)
                )
            )
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                sql.Identifier(vault), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON v2_tenant_accounts TO {}").format(
                sql.Identifier(vault)
            )
        )
        for table in (
            "v2_tenant_accounts",
            "v2_capital_journals",
            "v2_capital_entries",
        ):
            conn.execute(
                sql.SQL("ALTER TABLE {} OWNER TO postgres").format(
                    sql.Identifier(table)
                )
            )
        conn.execute(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON v2_business_state,v2_state_history,v2_trade_intents,v2_settlements,v2_exchange_income,v2_income_imports TO {}"
            ).format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION v2_fresh_capital_scope_clear(text,text,text,text) TO {}"
            ).format(sql.Identifier(role))
        )

    @contextmanager
    def connect():
        with database() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield conn

    try:
        yield connect
    finally:
        with database() as conn:
            for value in (role, vault):
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(value)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(value)))


def initialize(database, wallet="3500"):
    with database() as conn:
        return managed._initialize_fresh_capital(
            conn, SCOPE, wallet=Decimal(wallet), started_at_ms=NOW
        )


def test_pg_fresh_init_once_and_cross_scope_evidence_is_not_inherited(database, setup):
    journal, _, tenant, _, _, other, *_ = setup
    post(journal, tenant, other)
    store = install_policy(database)
    original = replace(intent(), account_id="another-account")
    TradingData(database).accept(original, evidence())
    assert initialize(database)
    assert not initialize(database, "3000")
    assert store.read().version == 2
    assert store.read().values["capital.reference_wallet"] == "3500"


def test_pg_even_unfilled_intent_prevents_fresh_reset(database, setup):
    store = install_policy(database)
    original = replace(intent(), account_id=SCOPE.account_id)
    TradingData(database).accept(original, evidence())
    with pytest.raises(ValueError, match="FRESH_CAPITAL_LEDGER_NOT_EMPTY"):
        initialize(database)
    assert store.read().version == 1


def test_pg_capital_journal_prevents_fresh_reset(database, setup):
    journal, _, tenant, _, account, *_ = setup
    store = install_policy(database)
    post(journal, tenant, account)
    with pytest.raises(ValueError, match="FRESH_CAPITAL_LEDGER_NOT_EMPTY"):
        initialize(database)
    assert store.read().version == 1


def test_pg_parallel_initialization_cannot_reset_twice(database, setup):
    store = install_policy(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: initialize(database), range(2)))
    assert sorted(outcomes) == [False, True]
    assert store.read().version == 2


@pytest.mark.parametrize("kind", ["income", "import"])
def test_pg_independent_cash_evidence_blocks_baseline(database, setup, kind):
    store = install_policy(database)
    if kind == "income":
        IncomeJournal(database).ingest(
            IncomeScope(
                SCOPE.exchange, SCOPE.account_id, SCOPE.environment, SCOPE.product
            ),
            income_type="COMMISSION",
            source_id="old-unallocated-cash",
            symbol="BTCUSDT",
            amount_text="-0.01",
            currency="USDT",
            occurred_at_ms=NOW - 1000,
            evidence={"source": "qa"},
        )
    else:
        with database() as conn:
            conn.execute(
                "INSERT INTO v2_income_imports(run_id,scope,start_ms,end_ms,status) "
                "VALUES (%s,%s::jsonb,0,1,'RUNNING')",
                (str(uuid4()), SCOPE.key),
            )
    with pytest.raises(ValueError, match="FRESH_CAPITAL_LEDGER_NOT_EMPTY"):
        initialize(database)
    assert store.read().version == 1


def test_pg_new_facts_cannot_enter_during_baseline_transaction(database, setup):
    install_policy(database)
    original = replace(intent(), account_id=SCOPE.account_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database() as conn:
            assert managed._initialize_fresh_capital(
                conn, SCOPE, wallet=Decimal(3500), started_at_ms=NOW
            )
            future = pool.submit(TradingData(database).accept, original, evidence())
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        future.result(timeout=5)


def test_pg_nonowner_noinherit_worker_initializes_without_private_table_access(
    database, worker
):
    store = install_policy(database)
    for table in ("v2_tenant_accounts", "v2_capital_journals", "v2_capital_entries"):
        with pytest.raises(errors.InsufficientPrivilege), worker() as conn:
            conn.execute(
                sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))
            )
    with worker() as conn:
        assert conn.execute(
            "SELECT rolsuper,rolinherit FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False)
    assert initialize(worker)
    assert store.read().version == 2
    with worker() as conn:
        assert not conn.execute(
            "SELECT has_table_privilege(current_user,'v2_capital_journals','SELECT')"
        ).fetchone()[0]


def test_pg_nonowner_worker_rejects_capital_residue(database, setup, worker):
    store = install_policy(database)
    journal, _, tenant, _, account, *_ = setup
    post(journal, tenant, account)
    with pytest.raises(ValueError, match="FRESH_CAPITAL_LEDGER_NOT_EMPTY"):
        initialize(worker)
    assert store.read().version == 1


def test_pg_nonowner_parallel_initialization_is_once(database, worker):
    store = install_policy(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: initialize(worker), range(2)))
    assert sorted(outcomes) == [False, True]
    assert store.read().version == 2


def test_pg_predicate_rejects_live_and_has_no_public_execute(worker):
    with (
        pytest.raises(
            errors.RaiseException, match="FRESH_CAPITAL_SANDBOX_SCOPE_REQUIRED"
        ),
        worker() as conn,
    ):
        conn.execute(
            "SELECT v2_fresh_capital_scope_clear('BINANCE','A','LIVE','FUTURES')"
        )
    with worker() as conn:
        row = conn.execute(
            "SELECT p.prosecdef,pg_get_userbyid(p.proowner),p.proconfig,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND p.proname='v2_fresh_capital_scope_clear'"
        ).fetchone()
        assert row[:2] == (True, "postgres")
        assert any(value.startswith("search_path=pg_catalog,") for value in row[2])
        assert "EXECUTE" not in row[3]
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE n.nspname=current_schema() AND p.proname='v2_fresh_capital_scope_clear' AND a.grantee=0"
            ).fetchone()[0]
            == 0
        )
