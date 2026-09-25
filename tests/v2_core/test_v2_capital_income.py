from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_capital_journal import setup as journal_setup
from test_v2_intent_admission import database as database_fixture

from v2_core.binance_income import BinanceIncomeImporter
from v2_core.capital_income import CapitalIncomeProjection
from v2_core.income import IncomeJournal, IncomeScope

database = database_fixture
setup = journal_setup


@pytest.fixture
def case(setup):
    j = setup[0]
    with j.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_capital_income.sql"
            ).read_text()
        )
    return CapitalIncomeProjection(j.connect), setup


def baseline(p, t, a, **overrides):
    args = {
        "currency": "USDT",
        "opening_amount": "1000",
        "through_ms": 100,
        "evidence_ref": str(uuid4()),
    }
    args.update(overrides)
    return p.enroll_baseline(t, a, **args)


def income(
    j, t, a, kind="REALIZED_PNL", value="10", at=101, source=None, currency="USDT"
):
    with j.connect() as c:
        scope = c.execute(
            "SELECT exchange,account_id,environment,product FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
            (t, a),
        ).fetchone()
    return IncomeJournal(j.connect).ingest(
        IncomeScope(*scope),
        income_type=kind,
        source_id=source or str(uuid4()),
        symbol="BTCUSDT",
        amount_text=value,
        currency=currency,
        occurred_at_ms=at,
        evidence={"source": "qa"},
    )


def project(p, t, a, **overrides):
    args = {"start_ms": 0, "end_ms": 200}
    args.update(overrides)
    return p.project_window(t, a, **args)


def test_baseline_cutoff_replay_and_late_income(case):
    p, (j, _, t, _, a, *_) = case
    ref = str(uuid4())
    opening = baseline(p, t, a, evidence_ref=ref)
    assert baseline(p, t, a, evidence_ref=ref, opening_amount="1000.00") == opening
    income(j, t, a, at=100)
    income(j, t, a)
    income(j, t, a, kind="COMMISSION", value="-1")
    income(j, t, a, kind="FUNDING_FEE", value="-2")
    income(j, t, a, value="0")
    first = project(p, t, a)
    assert (first["posted"], first["zero"], first["before_baseline"]) == (3, 1, 1)
    assert first["coverage_status"] == "NOT_PROVEN"
    assert first["execution_authorized"] is False
    assert project(p, t, a)["replayed"] == 3
    income(j, t, a, value="3", at=101)
    assert project(p, t, a)["posted"] == 1
    assert j.balances(t, a)["USDT"]["book_cash"] == "1010"
    with pytest.raises(ValueError, match="BASELINE_CONFLICT"):
        baseline(p, t, a)


def test_unsupported_missing_currency_and_positive_commission_are_visible(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a, kind="TRANSFER")
    income(j, t, a, kind="COMMISSION", value="1")
    income(j, t, a, currency="BTC")
    result = project(p, t, a)
    assert result["status"] == "NEEDS_REVIEW"
    assert {v["reason"] for v in result["blocked"]} == {
        "UNSUPPORTED_INCOME_TYPE",
        "INVALID_COMMISSION_DIRECTION",
        "BASELINE_MISSING",
    }
    assert result["posted"] == 0


def test_cursor_handles_same_timestamp_and_late_replay(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    for _ in range(5):
        income(j, t, a)
    cursor, total = None, 0
    for _ in range(3):
        result = project(p, t, a, limit=2, after=cursor)
        total += result["posted"]
        cursor = result["next_cursor"]
    assert total == 5 and cursor is None
    income(j, t, a)
    assert project(p, t, a)["posted"] == 1
    assert j.balances(t, a)["USDT"]["book_cash"] == "1060"


def test_concurrent_projection_posts_once(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: project(p, t, a), range(8)))
    assert sum(r["posted"] for r in results) == 1
    assert sum(r["replayed"] for r in results) == 7


def test_receipt_and_journal_rollback_together(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a)
    with j.connect() as c:
        c.execute(
            "CREATE FUNCTION qa_fail_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'qa failure'; END $$"
        )
        c.execute(
            "CREATE TRIGGER qa_fail BEFORE INSERT ON v2_capital_income_receipts FOR EACH ROW EXECUTE FUNCTION qa_fail_receipt()"
        )
    with pytest.raises(psycopg.Error):
        project(p, t, a)
    assert j.balances(t, a)["USDT"]["book_cash"] == "1000"
    with j.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_capital_projection_runs").fetchone()[0]
            == 0
        )
        c.execute("DROP TRIGGER qa_fail ON v2_capital_income_receipts")
    assert project(p, t, a)["posted"] == 1


def test_account_environment_and_tenant_isolation(case):
    p, (j, _, t, other, a, b, foreign, live) = case
    for account in (a, b, live):
        baseline(p, t, account)
        income(j, t, account, source="same-venue-id")
    assert project(p, t, a)["posted"] == 1
    assert j.balances(t, b)["USDT"]["book_cash"] == "1000"
    assert project(p, t, live)["posted"] == 1
    for operation in (lambda: project(p, other, a), lambda: baseline(p, t, foreign)):
        with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
            operation()


def test_reversed_projection_is_not_silently_reposted(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a)
    project(p, t, a)
    with j.connect() as c:
        original = c.execute(
            "SELECT journal_id::text FROM v2_capital_income_receipts"
        ).fetchone()[0]
    j.reverse(t, original, source="qa", source_id="undo", occurred_at_ms=102)
    result = project(p, t, a)
    assert result["blocked"][0]["reason"] == "PROJECTED_EVENT_REVERSED"
    assert result["posted"] == 0


def test_wallet_difference_match_replay_and_unresolved_facts(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a)
    args = {
        "currency": "USDT",
        "wallet_amount": "1010",
        "as_of_ms": 200,
        "observation_ref": str(uuid4()),
    }
    before = p.compare_wallet(t, a, **args)
    assert before["difference"] == "10" and before["unresolved_local_facts"] == 1
    project(p, t, a)
    assert p.compare_wallet(t, a, **args) == before
    with pytest.raises(ValueError, match="OBSERVATION_CONFLICT"):
        p.compare_wallet(t, a, **dict(args, wallet_amount="1020"))
    after = p.compare_wallet(t, a, **dict(args, observation_ref=str(uuid4())))
    assert after["status"] == "AMOUNT_MATCH_UNVERIFIED"
    assert after["difference"] == "0" and after["unresolved_local_facts"] == 0
    assert after["execution_authorized"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": True},
        {"limit": 0},
        {"start_ms": -1},
        {"end_ms": 8 * 86400000},
        {"after": {"at_ms": 201, "income_id": str(uuid4())}},
        {"after": {}},
    ],
)
def test_invalid_projection_bounds(case, overrides):
    p, (_, _, t, _, a, *_) = case
    with pytest.raises(ValueError):
        project(p, t, a, **overrides)


def test_baseline_requires_empty_book_and_zero_is_supported(case):
    p, (j, _, t, _, a, b, *_) = case
    j.post(
        t,
        a,
        kind="DEPOSIT",
        currency="USDT",
        cash_delta="1",
        source="qa",
        source_id="old",
        occurred_at_ms=1,
    )
    with pytest.raises(psycopg.Error):
        baseline(p, t, a)
    assert j.balances(t, a)["USDT"]["book_cash"] == "1"
    assert baseline(p, t, b, opening_amount="0") is None
    income(j, t, b)
    assert project(p, t, b)["posted"] == 1


@pytest.mark.parametrize(
    "table",
    [
        "v2_capital_baselines",
        "v2_capital_income_receipts",
        "v2_capital_projection_runs",
        "v2_capital_wallet_checks",
    ],
)
def test_audit_is_immutable(case, table):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a)
    project(p, t, a)
    p.compare_wallet(
        t,
        a,
        currency="USDT",
        wallet_amount="1010",
        as_of_ms=200,
        observation_ref=uuid4(),
    )
    with pytest.raises(psycopg.Error), j.connect() as c:
        c.execute("DELETE FROM " + table)


def test_original_importer_to_cash_book_chain(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    calls = []

    def request(method, path, params):
        calls.append((method, path))
        return [
            {
                "tranId": 1,
                "incomeType": "COMMISSION",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "-0.25",
                "time": 101,
                "tradeId": "42",
            }
        ]

    importer = BinanceIncomeImporter(
        j.connect, request, account_id="A", environment="SANDBOX", clock_ms=lambda: 200
    )
    assert importer.import_window(start_ms=100, end_ms=200)["status"] == "FETCHED"
    assert project(p, t, a)["posted"] == 1
    assert importer.import_window(start_ms=100, end_ms=200)["status"] == "FETCHED"
    assert project(p, t, a)["replayed"] == 1
    assert calls == [("GET", "/fapi/v1/income")] * 2
    assert j.balances(t, a)["USDT"]["fees_paid"] == "0.25"
    with j.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_income_allocations").fetchone()[0] == 0
        )


def test_reversed_baseline_blocks_new_projection(case):
    p, (j, _, t, _, a, *_) = case
    opening = baseline(p, t, a)
    j.reverse(t, opening, source="qa", source_id="undo", occurred_at_ms=101)
    income(j, t, a, at=102)
    assert project(p, t, a)["blocked"][0]["reason"] == "BASELINE_REVERSED"
    check = p.compare_wallet(
        t, a, currency="USDT", wallet_amount="0", as_of_ms=200, observation_ref=uuid4()
    )
    assert check["baseline_reversed"] is True
    assert check["execution_authorized"] is False


@pytest.mark.parametrize("bad", ["wrong_account", "wrong_amount", "before_cutoff"])
def test_sql_receipt_binding_rejects_bypass(case, bad):
    p, (j, _, t, _, a, b, *_) = case
    baseline(p, t, a)
    baseline(p, t, b)
    at = 100 if bad == "before_cutoff" else 101
    source = income(j, t, a, at=at)
    account = b if bad == "wrong_account" else a
    posted = j.post(
        t,
        account,
        kind="REALIZED_PNL",
        currency="USDT",
        cash_delta="11" if bad == "wrong_amount" else "10",
        source="binance-income-v1",
        source_id=source,
        occurred_at_ms=at,
    )
    with pytest.raises(psycopg.Error), j.connect() as c:
        c.execute(
            "INSERT INTO v2_capital_income_receipts VALUES (%s,%s,%s,%s,'binance-cash-v1',clock_timestamp())",
            (source, t, account, posted),
        )


def test_same_transaction_id_across_income_types_is_not_deduplicated(case):
    p, (j, _, t, _, a, *_) = case
    baseline(p, t, a)
    income(j, t, a, source="123")
    income(j, t, a, source="123", kind="COMMISSION", value="-1")
    assert project(p, t, a)["posted"] == 2
    assert j.balances(t, a)["USDT"]["book_cash"] == "1009"
