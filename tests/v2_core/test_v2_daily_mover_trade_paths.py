import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

import pytest

from services.v2_daily_mover_trade_paths import (
    SOURCE,
    enrich_case_paths,
    load_case_fills,
)
from v2_core.account_risk import AccountScope
from v2_core.quant_indicators import MINUTE

START = 1792857600000
END = START + 1440 * MINUTE
SCOPE = AccountScope("BINANCE", "private-account", "SANDBOX", "FUTURES")


def trade(identity="private-episode", side="BUY"):
    return {
        "id": identity,
        "symbol": "BTCUSDT",
        "side": side,
        "opened_ms": START + MINUTE,
        "closed_ms": START + 6 * MINUTE,
        "opening_quantity": "2",
        "closing_quantity": "2",
        "opening_notional": "200",
        "closing_notional": "220",
        "net_pnl": "18.5" if side == "BUY" else "-21.5",
        "settlement_state": "SETTLED",
        "settled_ms": START + 7 * MINUTE,
        "settlement_revision": 1,
        "status": "FILLED_SETTLED",
    }


def fill_tuples():
    return [
        ("open", START + MINUTE, "OPEN", "2", "100", False),
        ("partial", START + 3 * MINUTE, "CLOSE", "1", "110", False),
        ("close", START + 6 * MINUTE, "CLOSE", "1", "110", False),
    ]


def case(identity="private-episode", side="BUY"):
    return {
        "trade": trade(identity, side),
        "fills": [
            dict(
                zip(("identity", "t", "leg", "quantity", "price"), row[:5], strict=True)
            )
            for row in fill_tuples()
        ],
        "source": SOURCE,
        "load_error": None,
    }


def loaded(*cases):
    return {
        "cases": list(cases or [case()]),
        "source": SOURCE,
        "as_of_ms": END,
        "selection_truncated": False,
        "eligible_count": len(cases) or 1,
        "exclusions": [],
    }


def evidence(*trades):
    return {
        "account_scope": asdict(SCOPE),
        "window_start_ms": START,
        "window_end_ms": END,
        "as_of_ms": END,
        "symbols": {"BTCUSDT": {"trades": list(trades or [trade()])}},
    }


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, batches=None, readonly=True):
        self.batches = list(batches if batches is not None else [fill_tuples()])
        self.readonly, self.queries, self.transactions = readonly, [], 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if sql.startswith("SHOW"):
            return Cursor([("on" if self.readonly else "off",)])
        if sql.startswith("SET LOCAL"):
            return Cursor([])
        result = self.batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return Cursor(result)


def read(_symbol, start, end):
    return [
        {"t": stamp, "o": "110", "h": "120", "l": "90", "c": "110", "v": "1"}
        for stamp in range(start, end, MINUTE)
    ]


def test_read_only_scope_and_timing_query_are_mandatory():
    conn = Connection()
    result = load_case_fills(conn, SCOPE, evidence(), as_of_ms=END)
    assert result["cases"][0]["fills"] == case()["fills"]
    assert result["source"] == SOURCE
    sql, params = conn.queries[-1]
    assert "i.exchange,i.account_id,i.environment,i.product" in sql
    assert "f.occurred_at_ms<=%s" in sql and "f.created_at<=to_timestamp" in sql
    assert "i.payload->>'symbol'=%s" in sql and "i.payload->>'side'=%s" in sql
    assert "LIMIT 1001" in sql
    assert params == (
        "private-episode",
        *asdict(SCOPE).values(),
        "BTCUSDT",
        "BUY",
        END,
        END,
        END,
    )
    assert conn.transactions == 1
    assert "private-account" not in json.dumps(result)


def test_live_scope_wrong_account_and_mutable_connection_rejected_before_fill_read():
    with pytest.raises(ValueError, match="SANDBOX_REQUIRED"):
        load_case_fills(
            Connection(),
            AccountScope("BINANCE", "private-account", "LIVE", "FUTURES"),
            evidence(),
            as_of_ms=END,
        )
    wrong = evidence()
    wrong["account_scope"]["account_id"] = "another-account"
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        load_case_fills(Connection(), SCOPE, wrong, as_of_ms=END)
    conn = Connection(readonly=False)
    with pytest.raises(ValueError, match="READ_ONLY_REQUIRED"):
        load_case_fills(conn, SCOPE, evidence(), as_of_ms=END)
    assert len(conn.queries) == 1


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"status": "FILLED_UNSETTLED"}, "UNSETTLED_OR_UNFILLED"),
        ({"maintenance": True}, "MAINTENANCE_EXCLUDED"),
        ({"exit_origin": "APPROVED_TESTNET_MAINTENANCE"}, "MAINTENANCE_EXCLUDED"),
        ({"closed_ms": END}, "NOT_CLOSED_IN_REVIEW_DAY"),
        ({"settled_ms": END + 1}, "SETTLEMENT_TIMING_UNKNOWN"),
        ({"net_pnl": None}, "SETTLED_NET_UNKNOWN"),
        ({"symbol": "ETHUSDT"}, "SYMBOL_EVIDENCE_MISMATCH"),
    ],
)
def test_ineligible_episodes_remain_explicit_exclusions(changes, reason):
    row = trade()
    row.update(changes)
    conn = Connection()
    result = load_case_fills(conn, SCOPE, evidence(row), as_of_ms=END)
    assert result["cases"] == []
    assert result["exclusions"][0]["reason"] == reason
    assert len(conn.queries) == 1


def test_latest_closed_selection_bounded_to_six_and_duplicate_episode_excluded():
    rows = [trade(f"episode-{i}") for i in range(8)]
    for i, row in enumerate(rows):
        row["closed_ms"] += i
    result = load_case_fills(
        Connection([fill_tuples()] * 6), SCOPE, evidence(*rows), as_of_ms=END
    )
    assert len(result["cases"]) == 6 and result["eligible_count"] == 8
    assert result["selection_truncated"] is True
    assert result["cases"][0]["trade"]["id"] == "episode-7"
    duplicates = load_case_fills(
        Connection(), SCOPE, evidence(trade(), trade()), as_of_ms=END
    )
    assert duplicates["cases"] == []


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([], "SCOPED_FILLS_UNAVAILABLE"),
        (fill_tuples() * 334, "FILL_COUNT_BOUND"),
        ([(*fill_tuples()[0][:5], True)], "MAINTENANCE_EXCLUDED"),
        (RuntimeError("private DSN password"), "SCOPED_FILL_READ_UNAVAILABLE"),
    ],
)
def test_missing_excess_maintenance_or_failed_read_sanitized(rows, reason):
    result = load_case_fills(Connection([rows]), SCOPE, evidence(), as_of_ms=END)
    assert result["cases"][0]["load_error"] == reason
    assert result["cases"][0]["fills"] == []
    assert "password" not in json.dumps(result)


def test_query_failure_does_not_abort_remaining_cases():
    result = load_case_fills(
        Connection([RuntimeError("timeout"), fill_tuples()]),
        SCOPE,
        evidence(trade("a"), trade("b")),
        as_of_ms=END,
    )
    assert result["cases"][0]["load_error"] == "SCOPED_FILL_READ_UNAVAILABLE"
    assert result["cases"][1]["load_error"] is None


@pytest.mark.parametrize(
    "side,gross", [("BUY", "20.000000000000000000"), ("SELL", "-20.000000000000000000")]
)
def test_same_source_actual_net_separate_from_fill_aware_gross_and_postflat(
    side, gross
):
    result = enrich_case_paths(loaded(case(side=side)), read, as_of_ms=END)
    row = result["cases"][0]
    assert row["status"] == "OBSERVED_LOWER_BOUND"
    assert row["source"] == SOURCE
    assert row["actual_net_pnl"] == trade(side=side)["net_pnl"]
    assert row["gross_path"]["realized_gross_usdt"] == gross
    assert (
        row["gross_path"]["excursion_interpretation"]
        == "SAMPLED_LOWER_BOUND_NOT_EXECUTABLE_PROFIT"
    )
    assert row["post_flat"]["market_return_not_trade_pnl"] is True
    assert row["post_flat"]["basis"] == "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE"
    assert result["budget"]["requested_minutes"] == 65
    assert result["execution_authorized"] is False


@pytest.mark.parametrize(
    "defect",
    [
        "quantity",
        "notional",
        "window",
        "future_fill",
        "same_time_mixed",
        "overclose",
        "duplicate",
        "source",
        "settlement",
    ],
)
def test_invalid_fill_case_never_reads_market_or_emits_derived_values(defect):
    item = case()
    if defect == "quantity":
        item["trade"]["opening_quantity"] = "3"
    elif defect == "notional":
        item["trade"]["closing_notional"] = "221"
    elif defect == "window":
        item["trade"]["opened_ms"] += 1
    elif defect == "future_fill":
        item["fills"][-1]["t"] = END + 1
    elif defect == "same_time_mixed":
        item["fills"][1]["t"] = item["fills"][0]["t"]
    elif defect == "overclose":
        item["fills"][1]["quantity"] = "3"
    elif defect == "duplicate":
        item["fills"][1]["identity"] = "open"
    elif defect == "source":
        item["source"] = "BINANCE_USDM_LIVE"
    else:
        item["trade"]["settled_ms"] = END + 1
    calls = []
    result = enrich_case_paths(
        loaded(item), lambda *args: calls.append(args), as_of_ms=END
    )
    row = result["cases"][0]
    assert (
        row["status"] == "UNKNOWN"
        and row["gross_path"] is row["post_flat"] is row["actual_net_pnl"] is None
    )
    assert calls == []


def test_history_gaps_leave_realized_net_but_no_fabricated_extrema():
    result = enrich_case_paths(loaded(), lambda *args: read(*args)[1:], as_of_ms=END)
    row = result["cases"][0]
    assert row["status"] == "UNKNOWN" and row["actual_net_pnl"] == "18.5"
    assert row["gross_path"]["status"] == "INCOMPLETE_HISTORY"
    assert row["gross_path"]["mfe_gross_usdt_lower_bound"] is None
    assert row["post_flat"]["status"] == "INCOMPLETE_OR_INVALID_MINUTE_PATH"


@pytest.mark.parametrize(
    "defect", ["duplicate", "outside", "oversize", "bad_ohlc", "exception"]
)
def test_bad_archive_isolated_and_secret_details_redacted(defect):
    def bad(symbol, start, end):
        rows = read(symbol, start, end)
        if defect == "duplicate":
            rows[-1] = rows[0]
        elif defect == "outside":
            rows[0]["t"] = end
        elif defect == "oversize":
            rows.append(rows[0])
        elif defect == "bad_ohlc":
            rows[0]["h"] = "1"
        else:
            raise RuntimeError("postgresql://private/password")
        return rows

    result = enrich_case_paths(loaded(), bad, as_of_ms=END)
    assert result["cases"][0]["status"] == "UNKNOWN"
    assert "password" not in json.dumps(result)


def test_budget_prevents_reads_and_failed_reads_count_minutes():
    calls = []
    result = enrich_case_paths(
        loaded(), lambda *args: calls.append(args), as_of_ms=END, max_minutes=1
    )
    assert calls == []
    assert result["cases"][0]["gross_path"]["reason"] == "ARCHIVE_MINUTE_BUDGET"

    def failed(*_args):
        raise RuntimeError("read failed")

    result = enrich_case_paths(loaded(), failed, as_of_ms=END)
    assert (
        result["budget"]["requested_minutes"] == 65 and result["budget"]["calls"] == 2
    )


def test_deadline_prevents_next_page_without_fake_fills():
    ticks = iter([0, 31, 32])
    calls = []
    result = enrich_case_paths(
        loaded(),
        lambda *args: calls.append(args),
        as_of_ms=END,
        monotonic=lambda: next(ticks),
    )
    assert calls == []
    assert result["cases"][0]["gross_path"]["reason"] == "ARCHIVE_DEADLINE"


def test_archive_pages_are_at_most720_minutes_with_fill_changing_minutes_excluded():
    item = case()
    item["fills"][-1]["t"] = START + 800 * MINUTE
    item["trade"]["closed_ms"] = START + 800 * MINUTE
    item["trade"]["settled_ms"] = START + 801 * MINUTE
    calls = []

    def observed(symbol, start, end):
        calls.append((start, end))
        return list(reversed(read(symbol, start, end)))

    result = enrich_case_paths(loaded(item), observed, as_of_ms=END)
    assert [int((end - start) / MINUTE) for start, end in calls] == [720, 79, 60]
    assert result["cases"][0]["status"] == "OBSERVED_LOWER_BOUND"


def test_not_yet_observable_postflat_never_requests_future_archive():
    item = case()
    item["fills"][-1]["t"] = END - 5 * MINUTE
    item["trade"]["closed_ms"] = END - 5 * MINUTE
    item["trade"]["settled_ms"] = END - 4 * MINUTE
    calls = []

    def observed(symbol, start, end):
        calls.append((start, end))
        return read(symbol, start, end)

    result = enrich_case_paths(loaded(item), observed, as_of_ms=END)
    assert all(stop <= END for _, stop in calls)
    assert result["cases"][0]["post_flat"]["status"] == "HORIZON_NOT_YET_OBSERVABLE"


def test_one_invalid_case_does_not_block_next_and_input_not_mutated():
    first, second = case("first"), case("second")
    first["trade"]["opening_quantity"] = "99"
    source = loaded(first, second)
    before = deepcopy(source)
    result = enrich_case_paths(source, read, as_of_ms=END)
    assert [row["status"] for row in result["cases"]] == [
        "UNKNOWN",
        "OBSERVED_LOWER_BOUND",
    ]
    assert source == before
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "bad_case",
    [
        None,
        {},
        {"trade": "invalid"},
        {"trade": trade(), "source": SOURCE, "fills": [None] * 1001},
    ],
)
def test_malformed_case_shape_and_excess_fills_cannot_block_other_cases(bad_case):
    result = enrich_case_paths(loaded(bad_case, case()), read, as_of_ms=END)
    assert [row["status"] for row in result["cases"]] == [
        "UNKNOWN",
        "OBSERVED_LOWER_BOUND",
    ]
