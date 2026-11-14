"""The dashboard's cash classifier needs narrow SELECT, never income writes."""

import re
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from test_v2_cash_reporting_basis import save_historical_bridge
from test_v2_directional_settlement import baseline, service
from test_v2_liquidation_cash import liquidation as liquidation_fixture
from test_v2_precision_settlement import rounded_episode as rounded_fixture

from services.v2_performance_reporting import performance
from tests.support.v2_admission import database as database_fixture

database = database_fixture
rounded_episode = rounded_fixture
liquidation = liquidation_fixture
GRANT_PATH = (
    Path(__file__).resolve().parents[2]
    / "db/postgres_v2_dashboard_cash_basis_grants.sql"
)
COLUMNS = {
    "income_id",
    "exchange",
    "account_id",
    "environment",
    "product",
    "symbol",
    "income_type",
    "amount",
    "currency",
    "occurred_at_ms",
    "evidence",
}


def grant_statement():
    return "\n".join(
        line
        for line in GRANT_PATH.read_text().splitlines()
        if not line.startswith("--")
    ).strip()


def test_migration_is_one_exact_column_select_grant():
    statement = grant_statement()
    match = re.fullmatch(
        r"GRANT SELECT\s*\(([^)]+)\) ON trade_v2\.v2_exchange_income TO tradev2dash;",
        statement,
        flags=re.DOTALL,
    )
    assert match is not None
    columns = [column.strip() for column in match[1].split(",")]
    assert len(columns) == len(COLUMNS)
    assert set(columns) == COLUMNS
    assert "source_id" not in statement and "received_at" not in statement


@pytest.fixture
def restricted_dashboard(database):
    from psycopg import sql

    # The shared fixture has already verified a disposable Unix-socket QA
    # cluster. This unique NOLOGIN role cannot be a runtime/user account.
    role = "qa_dashboard_basis_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        # Existing reporting dependencies; intentionally no income grant yet.
        for table in (
            "v2_settlements",
            "v2_trade_intents",
            "v2_orders",
            "v2_fills",
            "v2_cash_adjustments",
        ):
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )

    @contextmanager
    def restricted():
        with database() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield conn

    def install():
        with database() as conn:
            # Execute the delivered SQL, changing only the isolated schema and
            # unique role identifiers. Keep its permission/column list intact.
            statement = grant_statement().replace(
                "trade_v2.", sql.Identifier(schema).as_string(conn) + "."
            )
            statement = statement.replace(
                " TO tradev2dash;", " TO " + sql.Identifier(role).as_string(conn) + ";"
            )
            conn.execute(statement)

    try:
        yield restricted, install
    finally:
        with database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_reporting_reproduces_missing_grant_then_recovers(restricted_dashboard):
    from psycopg.errors import InsufficientPrivilege

    restricted, install = restricted_dashboard
    with pytest.raises(InsufficientPrivilege), restricted() as conn:
        performance(conn)
    install()
    install()  # Redeploy is idempotent; no role or table replacement.
    with restricted() as conn:
        report = performance(conn)
        assert report["summary"]["settled_trades"] == 0
        assert report["daily"]["cash"] == []
        assert conn.execute(
            "SELECT has_table_privilege(current_user,'v2_exchange_income','SELECT')"
        ).fetchone() == (False,)


def test_restricted_report_keeps_legacy_precision_out_of_cash(
    rounded_episode, restricted_dashboard
):
    runtime, episode, _ = rounded_episode
    save_historical_bridge(runtime, episode, with_funding=True)
    restricted, install = restricted_dashboard
    install()
    with restricted() as conn:
        report = performance(conn, runtime.scope.account_id)
    assert Decimal(report["daily"]["cash"][0]["net_pnl"]) == Decimal("-10.106")
    assert report["daily"]["cash"][0]["unclassified_adjustments"] == 0


def test_restricted_report_verifies_real_insurance_cash(
    database, liquidation, restricted_dashboard
):
    runtime, episode, income = liquidation
    baseline(database, runtime, episode)
    assert (
        service(database, income, balance="989.05046733").run_once()["status"]
        == "CLEAR"
    )
    restricted, install = restricted_dashboard
    install()
    with restricted() as conn:
        cash = performance(conn, runtime.scope.account_id)["daily"]["cash"][0]
    assert Decimal(cash["net_pnl"]) == Decimal("-10.94953267")
    assert cash["unvalued_events"] == cash["unclassified_adjustments"] == 0


@pytest.mark.parametrize(
    "query",
    [
        (
            "INSERT INTO v2_exchange_income(income_id) "
            "VALUES ('11111111-1111-4111-8111-111111111111')"
        ),
        "UPDATE v2_exchange_income SET amount=amount WHERE false",
        "DELETE FROM v2_exchange_income WHERE false",
        "SELECT source_id FROM v2_exchange_income",
        "SELECT received_at FROM v2_exchange_income",
        "SELECT * FROM v2_exchange_income",
    ],
)
def test_column_grant_does_not_allow_writes_or_unneeded_columns(
    restricted_dashboard, query
):
    from psycopg.errors import InsufficientPrivilege

    restricted, install = restricted_dashboard
    install()
    with pytest.raises(InsufficientPrivilege), restricted() as conn:
        conn.execute(query)
