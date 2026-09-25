from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from v2_core.account_registry import AccountRegistry
from v2_core.capital_journal import CapitalJournal

database = database_fixture


@pytest.fixture
def setup(database):
    with database() as c:
        for name in ("tenant_registry", "capital_journal"):
            c.execute(
                (
                    Path(__file__).resolve().parents[2]
                    / f"db/migrations/20260925_{name}.sql"
                ).read_text()
            )
    registry = AccountRegistry(database)
    tenant = registry.create_tenant(uuid4(), "Owner")
    other = registry.create_tenant(uuid4(), "Other")

    def enroll(owner, name, environment="SANDBOX"):
        return registry.enroll(
            owner,
            replace(SCOPE, account_id=name, environment=environment),
            display_name=name,
            credential_ref=uuid4(),
            request_id=uuid4(),
        )["registry_id"]

    return (
        CapitalJournal(database),
        registry,
        tenant,
        other,
        enroll(tenant, "A"),
        enroll(tenant, "B"),
        enroll(other, "C"),
        enroll(tenant, "D", "LIVE"),
    )


def post(j, t, a, kind="DEPOSIT", value="100", **overrides):
    args = {
        "kind": kind,
        "currency": "USDT",
        "cash_delta": value,
        "source": "qa",
        "source_id": str(uuid4()),
        "occurred_at_ms": 100,
    }
    args.update(overrides)
    return j.post(t, a, **args)


def test_cash_flows_are_not_profit_and_currencies_stay_separate(setup):
    j, _, t, _, a, *_ = setup
    for kind, value in [
        ("OPENING", "1000"),
        ("DEPOSIT", "100"),
        ("WITHDRAWAL", "-50"),
        ("REALIZED_PNL", "20"),
        ("FEE", "-2"),
        ("FEE_REBATE", "0.5"),
        ("FUNDING", "-1"),
    ]:
        post(j, t, a, kind, value)
    post(j, t, a, value="0.000000000000000001", currency="BTC")
    assert j.balances(t, a)["USDT"] == {
        "book_cash": "1067.5",
        "net_contributed_capital": "1050",
        "realized_pnl": "20",
        "fees_paid": "1.5",
        "funding_net": "-1",
        "net_trading_pnl": "17.5",
        "transfers_net": "0",
    }
    assert j.balances(t, a)["BTC"]["book_cash"] == "0.000000000000000001"
    assert j.balances(t, a, as_of_ms=99) == {}


def test_replay_is_scoped_and_conflicts_do_not_mutate(setup):
    j, _, t, _, a, b, *_ = setup
    first = post(j, t, a, source_id="same")
    assert post(j, t, a, value="100.00", source_id="same") == first
    assert post(j, t, b, source_id="same") != first
    assert post(j, t, a, kind="REALIZED_PNL", source_id="same") != first
    with pytest.raises(ValueError, match="CONFLICT"):
        post(j, t, a, value="101", source_id="same")
    assert j.balances(t, a)["USDT"]["book_cash"] == "200"


def test_transfer_atomic_consolidation_reversal_and_rotation(setup):
    j, registry, t, _, a, b, *_ = setup
    post(j, t, a)
    args = {
        "currency": "USDT",
        "quantity": "25",
        "source": "qa",
        "source_id": "transfer",
        "occurred_at_ms": 101,
    }
    moved = j.transfer(t, a, b, **args)
    assert j.transfer(t, a, b, **args) == moved
    assert j.balances(t, a)["USDT"]["book_cash"] == "75"
    assert j.balances(t, b)["USDT"]["transfers_net"] == "25"
    assert j.consolidated(t, environment="SANDBOX")["USDT"]["transfers_net"] == "0"
    assert j.consolidated(t, environment="LIVE") == {}
    reverse_args = {"source": "qa", "source_id": "undo", "occurred_at_ms": 102}
    undo = j.reverse(t, moved, **reverse_args)
    assert j.reverse(t, moved, **reverse_args) == undo
    assert j.balances(t, a)["USDT"]["book_cash"] == "100"
    assert j.balances(t, b)["USDT"]["book_cash"] == "0"
    with pytest.raises(ValueError, match="ALREADY_REVERSED"):
        j.reverse(t, moved, **dict(reverse_args, source_id="again"))
    with pytest.raises(ValueError, match="invalid reversal"):
        j.reverse(t, undo, **reverse_args)
    registry.rotate_credential(
        t, a, credential_ref=uuid4(), expected_version=1, request_id=uuid4()
    )
    assert j.balances(t, a)["USDT"]["book_cash"] == "100"


def test_tenant_and_environment_boundaries(setup):
    j, _, t, other, a, _, foreign, live = setup
    original = post(j, t, a)
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        j.balances(other, a)
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        post(j, other, a)
    with pytest.raises(ValueError, match="JOURNAL_NOT_FOUND"):
        j.reverse(other, original, source="qa", source_id="undo", occurred_at_ms=101)
    for target in (foreign, live, a):
        with pytest.raises(ValueError):
            j.transfer(
                t,
                a,
                target,
                currency="USDT",
                quantity="1",
                source="qa",
                source_id="move",
                occurred_at_ms=101,
            )
    with pytest.raises(ValueError):
        j.consolidated(t, environment="ALL")
    assert j.balances(t, a)["USDT"]["book_cash"] == "100"


@pytest.mark.parametrize("value", [0.1, "0", "NaN", "Infinity", "1e20", "1e-19", "-1"])
def test_invalid_deposit_amount(setup, value):
    j, _, t, _, a, *_ = setup
    with pytest.raises(ValueError):
        post(j, t, a, value=value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "TRANSFER"},
        {"currency": "usdt"},
        {"source": "a b"},
        {"source_id": ""},
        {"occurred_at_ms": True},
        {"occurred_at_ms": -1},
        {"kind": "FEE"},
        {"kind": "WITHDRAWAL"},
    ],
)
def test_invalid_event_fields(setup, overrides):
    j, _, t, _, a, *_ = setup
    with pytest.raises(ValueError):
        post(j, t, a, **overrides)


@pytest.mark.parametrize("operation", ["post", "transfer", "reverse"])
def test_concurrent_replay_has_one_effect(setup, operation):
    j, _, t, _, a, b, *_ = setup
    original = post(j, t, a)

    def execute(_):
        if operation == "post":
            return post(j, t, a, source_id="concurrent")
        if operation == "transfer":
            return j.transfer(
                t,
                a,
                b,
                currency="USDT",
                quantity="10",
                source="qa",
                source_id="concurrent",
                occurred_at_ms=101,
            )
        return j.reverse(
            t, original, source="qa", source_id="concurrent", occurred_at_ms=101
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(execute, range(8)))) == 1
    with j.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_capital_journals").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM v2_capital_entries").fetchone()[0] == 4


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE v2_capital_entries SET amount=1",
        "DELETE FROM v2_capital_entries",
        "UPDATE v2_capital_journals SET source='changed'",
        "DELETE FROM v2_capital_journals",
        "UPDATE v2_tenant_accounts SET account_id='changed'",
        "DELETE FROM v2_tenant_accounts",
    ],
)
def test_immutable_history_at_database_boundary(setup, mutation):
    j, _, t, _, a, *_ = setup
    post(j, t, a)
    with pytest.raises(psycopg.Error), j.connect() as c:
        c.execute(mutation)


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "unbalanced",
        "wrongbook",
        "direction",
        "foreign",
        "precision",
        "third",
    ],
)
def test_direct_sql_invalid_journal_rolls_back(setup, bad):
    j, _, t, _, a, _, foreign, *_ = setup
    journal = str(uuid4())
    with pytest.raises(psycopg.Error):  # noqa: SIM117 - emphasize commit-time rejection
        with j.connect() as c:
            c.execute(
                "INSERT INTO v2_capital_journals(journal_id,tenant_id,registry_id,kind,currency,source,source_id,request_digest,occurred_at_ms) "
                "VALUES (%s,%s,%s,'DEPOSIT','USDT','qa','raw',%s,100)",
                (journal, t, a, "a" * 64),
            )
            if bad != "missing":
                cash = (
                    "-1"
                    if bad == "direction"
                    else "1.0000000000000000001"
                    if bad == "precision"
                    else "1"
                )
                counter = (
                    "1" if bad == "direction" else "-2" if bad == "unbalanced" else "-1"
                )
                c.execute(
                    "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,'CASH',%s)",
                    (journal, t, foreign if bad == "foreign" else a, cash),
                )
                c.execute(
                    "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,%s,%s)",
                    (
                        journal,
                        t,
                        a,
                        "FEES" if bad == "wrongbook" else "EXTERNAL_CAPITAL",
                        counter,
                    ),
                )
                if bad == "third":
                    c.execute(
                        "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,'FUNDING',1)",
                        (journal, t, a),
                    )
    with j.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_capital_journals").fetchone()[0] == 0


def test_exact_large_amount_and_event_time_reversal(setup):
    j, _, t, _, a, *_ = setup
    value = "99999999999999999999.123456789012345678"
    original = post(j, t, a, value=value)
    assert j.balances(t, a)["USDT"]["book_cash"] == value
    with pytest.raises(ValueError):
        j.reverse(t, original, source="qa", source_id="early", occurred_at_ms=99)
    j.reverse(t, original, source="qa", source_id="undo", occurred_at_ms=101)
    assert j.balances(t, a, as_of_ms=100)["USDT"]["book_cash"] == value
    assert j.balances(t, a)["USDT"]["book_cash"] == "0"


@pytest.mark.parametrize(
    "bad",
    ["live_transfer", "reversal_amount", "reversal_currency", "nan", "late_entry"],
)
def test_database_rejects_bypassed_scope_reversal_and_late_mutation(setup, bad):
    j, _, t, _, a, b, _, live = setup
    original = post(j, t, a)
    journal = str(uuid4())
    with pytest.raises(psycopg.Error), j.connect() as c:
        if bad == "late_entry":
            c.execute(
                "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,'FUNDING',1)",
                (original, t, a),
            )
        else:
            reversal = bad.startswith("reversal")
            c.execute(
                "INSERT INTO v2_capital_journals(journal_id,tenant_id,registry_id,kind,currency,source,source_id,request_digest,occurred_at_ms,reverses) "
                "VALUES (%s,%s,%s,%s,%s,'qa','bypass',%s,101,%s)",
                (
                    journal,
                    t,
                    a,
                    "REVERSAL" if reversal else "TRANSFER",
                    "BTC" if bad == "reversal_currency" else "USDT",
                    "a" * 64,
                    original if reversal else None,
                ),
            )
            c.execute(
                "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,'CASH',%s)",
                (
                    journal,
                    t,
                    a,
                    "NaN"
                    if bad == "nan"
                    else "-100"
                    if bad == "reversal_currency"
                    else "-1",
                ),
            )
            c.execute(
                "INSERT INTO v2_capital_entries VALUES (%s,%s,%s,%s,%s)",
                (
                    journal,
                    t,
                    a if reversal else live if bad == "live_transfer" else b,
                    "EXTERNAL_CAPITAL" if reversal else "CASH",
                    "100" if bad == "reversal_currency" else "1",
                ),
            )
    assert j.balances(t, a)["USDT"]["book_cash"] == "100"


def test_independent_concurrent_events_and_conflicting_reversals(setup):
    j, _, t, _, a, *_ = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: post(j, t, a, value="1", source_id=str(n)), range(12)))
    assert j.balances(t, a)["USDT"]["book_cash"] == "12"
    original = post(j, t, a, value="3")

    def reverse(n):
        try:
            j.reverse(t, original, source="qa", source_id=str(n), occurred_at_ms=101)
            return True
        except ValueError as exc:
            assert str(exc) == "ALREADY_REVERSED"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reverse, range(4))) == 1
    assert j.balances(t, a)["USDT"]["book_cash"] == "12"
