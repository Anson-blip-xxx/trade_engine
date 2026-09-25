from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from test_v2_capital_income import baseline, income, project
from test_v2_capital_income import case as income_case
from test_v2_capital_journal import setup as journal_setup
from test_v2_intent_admission import database as database_fixture

from v2_core.capital_income import CapitalIncomeProjection
from v2_core.capital_transfers import CapitalTransfers

database = database_fixture
setup = journal_setup
case = income_case


@pytest.fixture
def transfer_case(case):
    _, setup = case
    j = setup[0]
    with j.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_capital_transfers.sql"
            ).read_text()
        )
    p = CapitalIncomeProjection(j.connect, include_transfers=True)
    return CapitalTransfers(j.connect), p, setup


def pair(t, p, setup, **overrides):
    j, _, tenant, _, a, b, *_ = setup
    baseline(p, tenant, a)
    baseline(p, tenant, b)
    outgoing = income(j, tenant, a, kind="TRANSFER", value="-25", at=101)
    incoming = income(j, tenant, b, kind="TRANSFER", value="25", at=103, **overrides)
    return (
        tenant,
        a,
        b,
        {
            "outgoing_id": outgoing,
            "incoming_id": incoming,
            "evidence_ref": str(uuid4()),
        },
    )


def test_two_times_atomic_match_and_consolidated_elimination(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, a, b, args = pair(t, p, setup)
    assert (
        project(p, tenant, a)["blocked"][0]["reason"]
        == "TRANSFER_COUNTERPARTY_UNMATCHED"
    )
    result = t.match(tenant, **args)
    assert t.match(tenant, **args) == result
    assert (
        result["status"] == "ATTESTED_MATCH" and result["execution_authorized"] is False
    )
    assert j.balances(tenant, a)["USDT"]["book_cash"] == "975"
    assert j.balances(tenant, b, as_of_ms=102)["USDT"]["book_cash"] == "1000"
    assert j.balances(tenant, b)["USDT"]["book_cash"] == "1025"
    assert (
        j.consolidated(tenant, environment="SANDBOX", as_of_ms=102)["USDT"][
            "transfers_net"
        ]
        == "-25"
    )
    final = j.consolidated(tenant, environment="SANDBOX")["USDT"]
    assert final["transfers_net"] == "0" and final["net_contributed_capital"] == "2000"
    assert final["net_trading_pnl"] == "0"
    assert project(p, tenant, a)["replayed"] == 1
    assert project(p, tenant, b)["replayed"] == 1
    for account, balance in ((a, "975"), (b, "1025")):
        check = p.compare_wallet(
            tenant,
            account,
            currency="USDT",
            wallet_amount=balance,
            as_of_ms=200,
            observation_ref=uuid4(),
        )
        assert check["unresolved_local_facts"] == 0 and check["difference"] == "0"


def test_concurrent_replay_and_fact_reuse_conflict(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, _a, b, args = pair(t, p, setup)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: t.match(tenant, **args), range(8)))
    assert all(r == results[0] for r in results)
    with pytest.raises(ValueError, match="CONFLICT"):
        t.match(tenant, **dict(args, evidence_ref=uuid4()))
    another = income(j, tenant, b, kind="TRANSFER", value="25", at=103)
    with pytest.raises(ValueError, match="CONFLICT"):
        t.match(tenant, **dict(args, incoming_id=another, evidence_ref=uuid4()))
    with j.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_transfer_pairs").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "bad",
    [
        "amount",
        "currency",
        "same_account",
        "environment",
        "foreign",
        "time",
        "missing",
        "kind",
        "baseline",
    ],
)
def test_mismatches_do_not_change_cash(transfer_case, bad):
    t, p, setup = transfer_case
    j, _, tenant, other, a, b, foreign, live = setup
    baseline(p, tenant, a)
    if bad != "baseline":
        baseline(p, tenant, b)
    out = income(j, tenant, a, kind="TRANSFER", value="-25", at=101)
    target = (
        a
        if bad == "same_account"
        else live
        if bad == "environment"
        else foreign
        if bad == "foreign"
        else b
    )
    inc = income(
        j,
        other if bad == "foreign" else tenant,
        target,
        kind="REALIZED_PNL" if bad == "kind" else "TRANSFER",
        value="24" if bad == "amount" else "25",
        at=100 if bad == "time" else 103,
        currency="BTC" if bad == "currency" else "USDT",
    )
    with pytest.raises(ValueError):
        t.match(
            tenant,
            outgoing_id=out,
            incoming_id=uuid4() if bad == "missing" else inc,
            evidence_ref=uuid4(),
        )
    assert j.balances(tenant, a)["USDT"]["book_cash"] == "1000"


def test_pair_failure_rolls_back_both_legs(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, a, b, args = pair(t, p, setup)
    with j.connect() as c:
        c.execute(
            "CREATE FUNCTION qa_reject_pair() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'qa fail'; END $$"
        )
        c.execute(
            "CREATE TRIGGER qa_reject BEFORE INSERT ON v2_capital_transfer_pairs FOR EACH ROW EXECUTE FUNCTION qa_reject_pair()"
        )
    with pytest.raises(psycopg.Error):
        t.match(tenant, **args)
    for account in (a, b):
        assert j.balances(tenant, account)["USDT"]["book_cash"] == "1000"
    with j.connect() as c:
        c.execute("DROP TRIGGER qa_reject ON v2_capital_transfer_pairs")
    assert t.match(tenant, **args)["status"] == "ATTESTED_MATCH"


def test_reversal_is_visible_on_both_accounts_and_cannot_repost(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, a, b, args = pair(t, p, setup)
    matched = t.match(tenant, **args)
    j.reverse(
        tenant,
        matched["outgoing_journal"],
        source="qa",
        source_id="undo",
        occurred_at_ms=104,
    )
    assert t.match(tenant, **args)["status"] == "NEEDS_REVIEW"
    for account in (a, b):
        result = project(p, tenant, account)
        assert result["blocked"][0]["reason"] == "PROJECTED_EVENT_REVERSED"
        check = p.compare_wallet(
            tenant,
            account,
            currency="USDT",
            wallet_amount="1000",
            as_of_ms=200,
            observation_ref=uuid4(),
        )
        assert check["unresolved_local_facts"] == 1


def test_database_pair_guard_and_immutability(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, a, b, args = pair(t, p, setup)
    matched = t.match(tenant, **args)
    with pytest.raises(psycopg.Error), j.connect() as c:
        c.execute("DELETE FROM v2_capital_transfer_pairs")
    other_out = income(j, tenant, a, kind="TRANSFER", value="-24", at=101)
    other_in = income(j, tenant, b, kind="TRANSFER", value="24", at=103)
    with pytest.raises(psycopg.Error), j.connect() as c:
        c.execute(
            "INSERT INTO v2_capital_transfer_pairs VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
            (
                str(uuid4()),
                tenant,
                other_out,
                other_in,
                matched["outgoing_journal"],
                matched["incoming_journal"],
                str(uuid4()),
            ),
        )


def test_database_pair_binding_rejects_wrong_cash_without_unique_collision(
    transfer_case,
):
    _, p, setup = transfer_case
    j, _, tenant, _, a, b, *_ = setup
    baseline(p, tenant, a)
    baseline(p, tenant, b)
    out = income(j, tenant, a, kind="TRANSFER", value="-25", at=101)
    inc = income(j, tenant, b, kind="TRANSFER", value="25", at=103)
    oj = j.post(
        tenant,
        a,
        kind="TRANSFER_OUT",
        currency="USDT",
        cash_delta="-24",
        source="binance-transfer-v1",
        source_id=out,
        occurred_at_ms=101,
    )
    ij = j.post(
        tenant,
        b,
        kind="TRANSFER_IN",
        currency="USDT",
        cash_delta="24",
        source="binance-transfer-v1",
        source_id=inc,
        occurred_at_ms=103,
    )
    with (
        pytest.raises(psycopg.Error, match="invalid capital transfer pair"),
        j.connect() as c,
    ):
        c.execute(
            "INSERT INTO v2_capital_transfer_pairs VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
            (str(uuid4()), tenant, out, inc, oj, ij, str(uuid4())),
        )


def test_legacy_journal_operations_survive_extended_constraint(transfer_case):
    _, p, setup = transfer_case
    j, _, tenant, _, a, b, *_ = setup
    baseline(p, tenant, a)
    baseline(p, tenant, b)
    for kind, value in (
        ("DEPOSIT", "10"),
        ("WITHDRAWAL", "-5"),
        ("REALIZED_PNL", "2"),
        ("FEE", "-1"),
        ("FEE_REBATE", "0.5"),
        ("FUNDING", "-0.5"),
    ):
        j.post(
            tenant,
            a,
            kind=kind,
            currency="USDT",
            cash_delta=value,
            source="qa",
            source_id=kind,
            occurred_at_ms=101,
        )
    moved = j.transfer(
        tenant,
        a,
        b,
        currency="USDT",
        quantity="10",
        source="qa",
        source_id="legacy",
        occurred_at_ms=102,
    )
    j.reverse(tenant, moved, source="qa", source_id="undo", occurred_at_ms=103)
    assert j.balances(tenant, a)["USDT"]["book_cash"] == "1006"


def test_reversed_baseline_invalidates_pair_replay(transfer_case):
    t, p, setup = transfer_case
    j = setup[0]
    tenant, a, _, args = pair(t, p, setup)
    t.match(tenant, **args)
    with j.connect() as c:
        opening = c.execute(
            "SELECT journal_id::text FROM v2_capital_baselines WHERE registry_id=%s",
            (a,),
        ).fetchone()[0]
    j.reverse(
        tenant, opening, source="qa", source_id="baseline-undo", occurred_at_ms=104
    )
    assert t.match(tenant, **args)["status"] == "NEEDS_REVIEW"


def test_cross_utc8_midnight_retains_each_cash_event_day(transfer_case):
    t, p, setup = transfer_case
    j, _, tenant, _, a, b, *_ = setup
    baseline(p, tenant, a)
    baseline(p, tenant, b)
    midnight = int(
        datetime(2026, 9, 26, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    out = income(j, tenant, a, kind="TRANSFER", value="-25", at=midnight - 1)
    inc = income(j, tenant, b, kind="TRANSFER", value="25", at=midnight + 1)
    t.match(tenant, outgoing_id=out, incoming_id=inc, evidence_ref=uuid4())
    assert j.balances(tenant, a, as_of_ms=midnight - 1)["USDT"]["book_cash"] == "975"
    assert j.balances(tenant, b, as_of_ms=midnight - 1)["USDT"]["book_cash"] == "1000"
    assert (
        j.consolidated(tenant, environment="SANDBOX", as_of_ms=midnight + 1)["USDT"][
            "book_cash"
        ]
        == "2000"
    )
