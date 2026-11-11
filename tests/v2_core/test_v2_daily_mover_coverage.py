"""Coverage facts cannot be promoted to historical absence or trade permission."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from services.v2_daily_mover_coverage import (
    _CATALOG_SQL,
    build_coverage_review,
    load_execution_catalog_snapshot,
    unavailable_snapshot,
)
from tests.support.v2_admission import database as database_fixture

database = database_fixture

NOW = 1_800_000_000_000
SYMBOLS = ["BTCUSDT", "牛来USDT"]


def record(symbol="BTCUSDT", **changes):
    data = {
        "environment": "SANDBOX",
        "observed": NOW - 1000,
        "root": "object",
        "symbol": symbol,
        "present": True,
        "exact": symbol,
        "status": "TRADING",
        "supported": True,
        "kind": "object",
    }
    data.update(changes)
    return tuple(data.values())


class QueryError(Exception):
    def __init__(self, code):
        super().__init__("DO_NOT_EXPORT_SECRET_OR_DATABASE_DETAIL")
        self.sqlstate = code


class Connection:
    def __init__(self, rows=None, *, readonly="on", error=None):
        self.rows = [record(symbol) for symbol in SYMBOLS] if rows is None else rows
        self.readonly, self.error = readonly, error
        self.calls, self.transactions, self.rollbacks, self.active = [], 0, 0, False

    @contextmanager
    def transaction(self):
        self.transactions += 1
        self.active = True
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise
        finally:
            self.active = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == _CATALOG_SQL and self.error:
            raise QueryError(self.error)
        return self

    def fetchone(self):
        return (self.readonly,)

    def fetchall(self):
        return self.rows


def load(conn=None, **changes):
    return load_execution_catalog_snapshot(
        conn or Connection(),
        **{
            "environment": "SANDBOX",
            "symbols": SYMBOLS,
            "as_of_ms": NOW,
            **changes,
        },
    )


def report(**changes):
    return {
        "account_environment": "SANDBOX",
        "market_source": "BINANCE_USDM_LIVE",
        "as_of_ms": NOW,
        "window": {"start_ms": NOW - 86_400_000, "end_ms": NOW - 60_000},
        "rows": [
            {
                "symbol": SYMBOLS[0],
                "direction": "BUY",
                "evidence": {"primary_reason": "MATCHED_DIRECTION_FILLED"},
            },
            {
                "symbol": SYMBOLS[1],
                "direction": "SELL",
                "evidence": {
                    "primary_reason": "NO_RETAINED_SIGNAL_UNKNOWN",
                    "signal_count": 0,
                },
            },
        ],
        **changes,
    }


def test_exact_readonly_projection_unicode_scope_and_no_mutation():
    conn = Connection()
    result = load(conn)
    assert result["status"] == "OBSERVED"
    assert result["observed_at_ms"] == NOW - 1000
    assert result["rows"][1] == {
        "symbol": "牛来USDT",
        "present": True,
        "status": "TRADING",
        "supported": True,
    }
    assert conn.calls[-1] == (_CATALOG_SQL, ("SANDBOX", SYMBOLS))
    assert conn.transactions == 1 and conn.rollbacks == 0
    assert "LIMIT 20" in _CATALOG_SQL and "WHERE environment=%s LIMIT 1" in _CATALOG_SQL
    assert all("UPDATE " not in sql and "INSERT " not in sql for sql, _ in conn.calls)


@pytest.mark.parametrize("count", [0, 1, 19, 20])
def test_zero_to_twenty_symbols_is_bounded(count):
    symbols = [f"A{i}USDT" for i in range(count)]
    conn = Connection([record(symbol) for symbol in symbols])
    result = load(conn, symbols=symbols)
    assert len(result["rows"]) == count
    assert result["status"] == ("OBSERVED" if count else "UNKNOWN")
    if not count:
        assert conn.calls == []


@pytest.mark.parametrize(
    "symbols",
    [
        ["BTCUSDT"] * 2,
        [f"A{i}USDT" for i in range(21)],
        ["A';DROP TABLE x;--USDT"],
        ["btcusdt"],
        ["BTC/USDT"],
        ["ＡUSDT"],
        [None],
        [True],
        "BTCUSDT",
        {"secret": "DO_NOT_EXPORT"},
    ],
)
def test_invalid_symbol_request_never_queries_or_echoes(symbols):
    conn = Connection()
    result = load(conn, symbols=symbols)
    assert result["reason"] == "CATALOG_INPUT_INVALID"
    assert result["rows"] == [] and conn.calls == []
    assert "DO_NOT_EXPORT" not in json.dumps(result)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"observed": NOW + 1}, "CATALOG_FROM_FUTURE"),
        ({"observed": NOW - 900001}, "CATALOG_STALE"),
        ({"observed": True}, "CATALOG_DATA_INVALID"),
        ({"observed": "1800000000000"}, "CATALOG_DATA_INVALID"),
        ({"environment": "LIVE"}, "CATALOG_ENVIRONMENT_MISMATCH"),
        ({"root": "array"}, "CATALOG_DATA_INVALID"),
        ({"exact": "DO_NOT_EXPORT_SECRET"}, "CATALOG_DATA_INVALID"),
        ({"status": "DO_NOT_EXPORT_SECRET"}, "CATALOG_DATA_INVALID"),
        ({"supported": "true"}, "CATALOG_DATA_INVALID"),
        ({"kind": "array"}, "CATALOG_DATA_INVALID"),
    ],
)
def test_bad_stale_future_or_wrong_environment_catalog_is_unknown(changes, reason):
    result = load(Connection([record(**changes)]), symbols=["BTCUSDT"])
    assert result["reason"] == reason
    assert result["observed_at_ms"] is None
    assert result["rows"][0]["present"] is None
    assert "DO_NOT_EXPORT_SECRET" not in json.dumps(result)


def test_freshness_boundary_and_explicit_zero_age():
    assert (
        load(Connection([record(observed=NOW - 900000)]), symbols=["BTCUSDT"])["status"]
        == "OBSERVED"
    )
    assert (
        load(Connection([record(observed=NOW)]), symbols=["BTCUSDT"], max_age_ms=0)[
            "status"
        ]
        == "OBSERVED"
    )
    assert (
        load(Connection([record(observed=NOW - 1)]), symbols=["BTCUSDT"], max_age_ms=0)[
            "reason"
        ]
        == "CATALOG_STALE"
    )


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("42501", "CATALOG_READ_PERMISSION_MISSING"),
        ("42P01", "CATALOG_SCHEMA_UNAVAILABLE"),
        ("57014", "CATALOG_QUERY_TIMEOUT"),
        ("XX000", "CATALOG_READ_UNAVAILABLE"),
    ],
)
def test_query_failure_rolls_back_savepoint_without_leaking_or_poisoning(code, reason):
    conn = Connection(error=code)
    result = load(conn)
    assert result["reason"] == reason
    assert conn.rollbacks == 1 and not conn.active
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    conn.error = None
    assert load(conn)["status"] == "OBSERVED"


def test_write_transaction_is_refused_and_missing_catalog_is_not_absence():
    conn = Connection(readonly="off")
    assert load(conn)["reason"] == "CATALOG_READ_ONLY_REQUIRED"
    assert not any(sql == _CATALOG_SQL for sql, _ in conn.calls)
    missing = load(Connection([]))
    assert missing["reason"] == "CATALOG_NOT_OBSERVED"
    assert all(row["present"] is None for row in missing["rows"])


def test_current_absence_is_not_historical_absence_and_preserves_fill():
    snapshot = load(
        Connection(
            [
                record(
                    symbol,
                    present=False,
                    exact=None,
                    status=None,
                    supported=None,
                    kind=None,
                )
                for symbol in SYMBOLS
            ]
        )
    )
    source = report()
    before = deepcopy((source, snapshot))
    result = build_coverage_review(source, snapshot)
    assert (source, snapshot) == before
    assert result["market_execution_environment_match"] is False
    assert result["snapshot_relation_to_review_day"] == "AT_OR_AFTER_DAY_END"
    assert result["rows"][0]["historical_evidence"] == "MATCHED_DIRECTION_FILLED"
    assert (
        result["rows"][0]["evidence_conflict"]
        == "HISTORICAL_FILL_WITH_CURRENT_ABSENCE_NOT_A_CONTRADICTION"
    )
    assert result["rows"][1]["historical_evidence"] == "NO_RETAINED_SIGNAL_UNKNOWN"
    assert all(
        row["historical_listing"] == "NOT_PROVEN_BY_CURRENT_CATALOG"
        for row in result["rows"]
    )
    assert result["counts"]["MATCHED_DIRECTION_FILLED"] == 1
    assert result["current_listing_counts"] == {"ABSENT_FROM_CURRENT_CATALOG": 2}
    assert (
        result["historical_current_intersections"][
            "NO_RETAINED_SIGNAL_UNKNOWN:ABSENT_FROM_CURRENT_CATALOG"
        ]
        == 1
    )
    assert not any(
        result[key]
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert build_coverage_review(source, snapshot) == result


@pytest.mark.parametrize(
    ("primary", "expected"),
    [
        ("MATCHED_DIRECTION_FILLED", "MATCHED_DIRECTION_FILLED"),
        ("OTHER_DIRECTION_ONLY", "ONLY_OTHER_DIRECTION_OR_NON_ENTRY"),
        ("NO_DIRECTIONAL_ENTRY_SIGNAL", "ONLY_OTHER_DIRECTION_OR_NON_ENTRY"),
        ("NO_RETAINED_SIGNAL_UNKNOWN", "NO_RETAINED_SIGNAL_UNKNOWN"),
        ("SIGNAL_PREFILTER_REJECTED", "RECORDED_REJECTION"),
        ("JEV_VETO", "RECORDED_REJECTION"),
        ("JEV_UNAVAILABLE_OR_SKIPPED", "UNKNOWN"),
        ("EXPIRED_NOT_STRATEGY_REJECTION", "UNKNOWN"),
        ("ACCOUNT_DECISION_NOT_OBSERVED", "UNKNOWN"),
        ("ENGINE_SYMBOL_UNSUPPORTED", "UNKNOWN"),
        ("CANDIDATE_NOT_FILLED", "ENTRY_CANDIDATE_NOT_FILLED"),
        ("CANDIDATE_EXECUTION_UNKNOWN", "UNKNOWN"),
        ("DO_NOT_EXPORT_SECRET", "UNKNOWN"),
    ],
)
def test_history_categories_do_not_reinterpret_missing_or_opposite_records(
    primary, expected
):
    source = report()
    source["rows"][0]["evidence"] = {"primary_reason": primary, "signal_count": 0}
    result = build_coverage_review(source, load())
    assert result["rows"][0]["historical_evidence"] == expected
    assert "DO_NOT_EXPORT_SECRET" not in json.dumps(result)


def test_zero_signal_count_alone_is_unknown_not_no_signal_generated():
    source = report()
    source["rows"][0]["evidence"] = {"signal_count": 0}
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "UNKNOWN"
    )


def test_observed_fill_overrides_rejection_primary_but_not_other_direction_fill():
    source = report()
    source["rows"][0]["evidence"] = {
        "primary_reason": "JEV_VETO",
        "trades": [{"side": "BUY", "opening_quantity": "1"}],
    }
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "MATCHED_DIRECTION_FILLED"
    )
    source["rows"][0]["evidence"]["trades"][0]["side"] = "SELL"
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "RECORDED_REJECTION"
    )


def test_mixed_reasons_require_an_actual_positive_rejection_count():
    source = report()
    ev = source["rows"][0]["evidence"] = {
        "primary_reason": "MIXED_RECORDED_REASONS",
        "stage_counts": {"EXPIRED_NOT_STRATEGY_REJECTION": 10},
    }
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "UNKNOWN"
    )
    ev["stage_counts"]["MARKET_FILTER_REJECTED"] = True
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "UNKNOWN"
    )
    ev["stage_counts"]["MARKET_FILTER_REJECTED"] = 1
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "RECORDED_REJECTION"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"environment": "LIVE"}, "CATALOG_ENVIRONMENT_MISMATCH"),
        ({"as_of_ms": NOW + 1}, "CATALOG_FROM_FUTURE"),
        ({"observed_at_ms": NOW + 1}, "CATALOG_FROM_FUTURE"),
        ({"observed_at_ms": NOW - 900001}, "CATALOG_STALE"),
        ({"observed_at_ms": False}, "CATALOG_DATA_INVALID"),
        ({"rows": []}, "CATALOG_SCOPE_MISMATCH"),
        ({"version": "wrong"}, "CATALOG_DATA_INVALID"),
    ],
)
def test_pure_review_revalidates_catalog_even_if_claimed_observed(change, reason):
    snapshot = {**load(), **change}
    result = build_coverage_review(report(), snapshot)
    assert result["reason"] == reason
    assert result["status"] == "PARTIAL_UNKNOWN"
    assert all(row["current_listing"] == "UNKNOWN" for row in result["rows"])
    assert result["rows"][0]["historical_evidence"] == "MATCHED_DIRECTION_FILLED"


def test_dangerous_extra_payload_not_copied_or_hashed_into_output():
    source, snapshot = report(), load()
    baseline = build_coverage_review(source, snapshot)
    source["credentials"] = {"secret": "DO_NOT_EXPORT_SECRET"}
    source["rows"][0]["evidence"]["config"] = {"key": "DO_NOT_EXPORT_SECRET"}
    snapshot["rows"][0]["filters"] = {"token": "DO_NOT_EXPORT_SECRET"}
    snapshot["account_id"] = "DO_NOT_EXPORT_ACCOUNT"
    assert build_coverage_review(source, snapshot) == baseline
    assert "DO_NOT_EXPORT" not in json.dumps(baseline)


def test_unavailable_snapshot_is_safe_and_review_still_has_history():
    snapshot = unavailable_snapshot(
        environment="SANDBOX",
        symbols=SYMBOLS,
        as_of_ms=NOW,
        reason="DO_NOT_EXPORT_SECRET",
    )
    result = build_coverage_review(report(), snapshot)
    assert result["reason"] == "CATALOG_READ_UNAVAILABLE"
    assert result["counts"]["MATCHED_DIRECTION_FILLED"] == 1
    assert result["current_listing_counts"] == {"UNKNOWN": 2}


@pytest.mark.parametrize(
    "changes",
    [
        {"account_environment": "PROD"},
        {"as_of_ms": True},
        {"window": {}},
        {"rows": [{}]},
        {"rows": [{"symbol": "SECRET!", "direction": "BUY"}]},
    ],
)
def test_invalid_report_is_safe_unknown(changes):
    result = build_coverage_review(report(**changes), load())
    assert result["reason"] == "REPORT_INPUT_INVALID" and result["rows"] == []


def test_same_environment_and_empty_report_are_explicit():
    result = build_coverage_review(
        report(market_source="BINANCE_USDM_SANDBOX", rows=[]), load(symbols=[])
    )
    assert result["market_execution_environment_match"] is True
    assert result["rows"] == [] and sum(result["counts"].values()) == 0


@pytest.mark.parametrize("bad", [[], {}, ["SANDBOX"]])
def test_unhashable_payloads_are_unknown_not_exceptions(bad):
    assert load(environment=bad)["reason"] == "CATALOG_INPUT_INVALID"
    assert (
        unavailable_snapshot(environment=bad, symbols=[], as_of_ms=NOW, reason=bad)[
            "reason"
        ]
        == "CATALOG_READ_UNAVAILABLE"
    )
    assert (
        build_coverage_review(report(account_environment=bad), load())["status"]
        == "UNKNOWN"
    )
    source = report()
    source["rows"][0]["evidence"]["primary_reason"] = bad
    assert (
        build_coverage_review(source, load())["rows"][0]["historical_evidence"]
        == "UNKNOWN"
    )
    source["rows"][0]["direction"] = bad
    assert build_coverage_review(source, load())["status"] == "UNKNOWN"
    snapshot = load()
    snapshot["rows"][0]["status"] = bad
    assert build_coverage_review(report(), snapshot)["reason"] == "CATALOG_DATA_INVALID"


@pytest.fixture
def catalog_db(database):
    from psycopg.types.json import Jsonb

    with database() as conn:
        conn.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260929_shared_market.sql"
            ).read_text()
        )
        for environment, symbols in (("SANDBOX", ["BTCUSDT"]), ("LIVE", SYMBOLS)):
            contracts = {
                symbol: {
                    "symbol": symbol,
                    "status": "TRADING",
                    "supported": True,
                    "filters": [{"DO_NOT_EXPORT_SECRET": "PRIVATE"}],
                }
                for symbol in symbols
            }
            conn.execute(
                "INSERT INTO v2_market_catalog(environment,observed_at_ms,digest,contracts) VALUES (%s,%s,%s,%s)",
                (environment, NOW - 1000, "a" * 64, Jsonb(contracts)),
            )
    return database


@contextmanager
def research_role(database, *, columns=True):
    from psycopg import sql

    role = "qa_mover_catalog_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        if columns:
            conn.execute(
                sql.SQL(
                    "GRANT SELECT(environment,observed_at_ms,contracts) ON v2_market_catalog TO {}"
                ).format(sql.Identifier(role))
            )
    try:
        yield role
    finally:
        with database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_pg_column_only_grant_projects_bounded_exact_environment(catalog_db):
    from psycopg import errors, sql

    with research_role(catalog_db) as role, catalog_db() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        result = load(conn)
        assert result["status"] == "OBSERVED"
        assert result["rows"][0]["present"] is True
        assert result["rows"][1]["present"] is False
        assert "DO_NOT_EXPORT_SECRET" not in json.dumps(result)
        with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT digest FROM v2_market_catalog").fetchall()
        assert conn.execute("SELECT 1").fetchone() == (1,)
        live = load(conn, environment="LIVE")
        assert all(row["present"] for row in live["rows"])


def test_pg_missing_column_grant_isolated_and_does_not_abort_other_evidence(catalog_db):
    from psycopg import sql

    with research_role(catalog_db, columns=False) as role, catalog_db() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        assert load(conn)["reason"] == "CATALOG_READ_PERMISSION_MISSING"
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_pg_missing_schema_isolated_and_does_not_abort_other_evidence(database):
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        assert load(conn)["reason"] == "CATALOG_SCHEMA_UNAVAILABLE"
        assert conn.execute("SELECT 1").fetchone() == (1,)
