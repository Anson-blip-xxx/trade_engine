"""Frozen close attribution must not invent the trigger of external adoption."""

from decimal import Decimal

import pytest

from services.v2_profit_capture_review import strict_trade_review
from services.v2_trade_review import recorded_exit_reason, trade_review
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope

SCOPE = AccountScope("BINANCE", "exit-attribution-test", "SANDBOX", "FUTURES")
database = database_fixture


@pytest.mark.parametrize(
    ("reason", "origin", "kind", "expected"),
    [
        (
            None,
            "BINANCE_EXTERNAL_CLOSE",
            "EXTERNAL_REDUCE_ONLY",
            "RECONCILED_EXTERNAL_CLOSE",
        ),
        (None, "BINANCE_EXTERNAL_CLOSE", "LIQUIDATION", "EXCHANGE_LIQUIDATION"),
        (None, "BINANCE_EXTERNAL_CLOSE", None, "RECONCILED_EXTERNAL_CLOSE"),
        (None, "BINANCE_EXTERNAL_CLOSE", "UNRECOGNIZED", "RECONCILED_EXTERNAL_CLOSE"),
        (None, "BINANCE_ALGO_CHILD", None, "VENUE_PROTECTION_CHILD"),
        (None, None, "LIQUIDATION", None),
        (None, "UNRECOGNIZED", "LIQUIDATION", None),
        (None, None, None, None),
        ("", None, None, None),
        ("UNCONFIRMED_PROTECTION", None, None, "UNCONFIRMED_PROTECTION"),
        ("PEAK_DRAWDOWN", "BINANCE_ALGO_CHILD", None, "PEAK_DRAWDOWN"),
        (
            "RECORDED_REASON",
            "BINANCE_EXTERNAL_CLOSE",
            "EXTERNAL_REDUCE_ONLY",
            "RECORDED_REASON",
        ),
    ],
)
def test_only_frozen_evidence_supports_attribution(reason, origin, kind, expected):
    assert recorded_exit_reason(reason, origin, kind) == expected


def _row(identity, *, reason=None, origin=None, kind=None, net="-1.25"):
    # Exact projection of trade_review's one bounded SQL statement. The helper
    # deliberately contains no current exit state or notification fallback.
    return (
        identity,
        "BTCUSDT",
        "BUY",
        1000,
        "s3",
        {},
        {},
        {},
        "digest",
        "v1",
        1100,
        2000,
        2100,
        "1",
        "1",
        "100",
        "99",
        "0.25",
        net,
        reason,
        origin,
        kind,
        None,
        None,
        False,
    )


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class _ReadOnlyConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if query == "SHOW transaction_read_only":
            return _Rows([("on",)])
        assert query.startswith("WITH candidates AS MATERIALIZED")
        assert "cl.request_evidence->>'kind'" in query
        # Preserve existing scope, as-of, filled-close and bounded query gates.
        assert "(exchange,account_id,environment,product)=(%s,%s,%s,%s)" in query
        assert "o.leg='CLOSE'" in query and "JOIN v2_fills z USING(order_id)" in query
        assert "ORDER BY z.occurred_at_ms DESC,z.fill_key DESC LIMIT 1) cl" in query
        assert "z.created_at<=to_timestamp(%s/1000.0)" in query
        assert "z.occurred_at_ms<=%s" in query
        assert "LIMIT %s" in query
        assert params[:4] == ("BINANCE", "exit-attribution-test", "SANDBOX", "FUTURES")
        assert params[8] == 501
        return _Rows(self.rows)


def test_query_projects_external_kind_without_additional_reads_or_pnl_changes():
    conn = _ReadOnlyConnection(
        [
            _row(
                "ordinary", origin="BINANCE_EXTERNAL_CLOSE", kind="EXTERNAL_REDUCE_ONLY"
            ),
            _row("liquidation", origin="BINANCE_EXTERNAL_CLOSE", kind="LIQUIDATION"),
            _row("unknown", kind="LIQUIDATION"),
        ]
    )
    result = trade_review(conn, SCOPE, now_ms=10000)
    rows = {row["id"]: row for row in result["trades"]}
    assert rows["ordinary"]["exit_reason"] == "RECONCILED_EXTERNAL_CLOSE"
    assert rows["liquidation"]["exit_reason"] == "EXCHANGE_LIQUIDATION"
    assert rows["unknown"]["exit_reason"] is None
    assert len(conn.queries) == 2  # read-only check plus unchanged bounded query
    assert Decimal(result["summary"]["net_pnl"]) == Decimal("-3.75")
    assert all(row["held_minutes"] == "0.015" for row in rows.values())
    assert not result["execution_authorized"]
    assert not result["automatic_parameter_updates"]
    assert any("不证明人工、止损" in text for text in result["limitations"])


def test_strict_review_keeps_current_revision_label_contract(monkeypatch):
    conn = _ReadOnlyConnection(
        [
            _row(
                "a",
                origin="BINANCE_EXTERNAL_CLOSE",
                kind="EXTERNAL_REDUCE_ONLY",
                net="999",
            ),
            _row("b", origin="BINANCE_EXTERNAL_CLOSE", kind="LIQUIDATION", net="999"),
        ]
    )
    calls = []

    def labels(connection, scope, identities, *, now):
        calls.append((connection, scope, identities, now))
        return {
            "a": {
                "state": "SETTLED",
                "net_pnl": "-1.23456789123456789",
                "closed_ms": 2000,
                "settled_ms": 2200,
                "settlement_revision": 4,
            },
            "b": {"state": "SETTLEMENT_REVISION_CHANGED", "net_pnl": None},
        }

    monkeypatch.setattr("services.v2_profit_capture_review.outcomes", labels)
    result = strict_trade_review(conn, SCOPE, days=14, limit=500, now_ms=10000)
    rows = {row["id"]: row for row in result["trades"]}
    assert rows["a"]["exit_reason"] == "RECONCILED_EXTERNAL_CLOSE"
    assert rows["a"]["settlement_revision"] == 4
    assert rows["a"]["net_pnl"] == "-1.23456789123456789"
    assert rows["b"]["exit_reason"] == "EXCHANGE_LIQUIDATION"
    assert rows["b"]["net_pnl"] is None
    assert result["summary"]["settled_trades"] == 1
    assert len(conn.queries) == 2 and len(calls) == 1
    assert (
        result["settlement_label_contract"]
        == "CURRENT_ACCOUNTING_REVISION_COMPLETE_USDT_SETTLEMENT"
    )


@pytest.mark.parametrize(
    ("reason", "origin", "kind", "expected"),
    [
        (None, "BINANCE_ALGO_CHILD", None, "VENUE_PROTECTION_CHILD"),
        (
            None,
            "BINANCE_EXTERNAL_CLOSE",
            "EXTERNAL_REDUCE_ONLY",
            "RECONCILED_EXTERNAL_CLOSE",
        ),
        (None, "BINANCE_EXTERNAL_CLOSE", "LIQUIDATION", "EXCHANGE_LIQUIDATION"),
        ("UNCONFIRMED_PROTECTION", None, None, "UNCONFIRMED_PROTECTION"),
        (None, None, None, "UNKNOWN_RECORDED_REASON"),
        ("ARBITRARY_UNTRUSTED_CODE", None, None, "UNKNOWN_RECORDED_REASON"),
    ],
)
def test_mover_frozen_projection_preserves_supported_attribution(
    reason, origin, kind, expected
):
    from services.v2_daily_mover_evidence import _intent_records
    from services.v2_daily_mover_execution_review import build_execution_review
    from tests.v2_core.test_v2_daily_mover_execution_review import (
        END,
        START,
        report,
        trade,
    )

    row = (
        "episode",
        None,
        "evidence",
        "BTCUSDT",
        "BUY",
        "s6",
        START,
        "s3",
        "RIGHT_MOMENTUM",
        "digest",
        "v1",
        "1",
        "1",
        "100",
        "99",
        "0.25",
        START + 60_000,
        START + 120_000,
        1,
        2,
        None,
        None,
        None,
        None,
        "close-order",
        reason,
        origin,
        kind,
        [],
        "0.02",
        "100",
    )
    frozen = _intent_records([row], start_ms=START, end_ms=END, limit=500)[0]
    assert frozen["exit_reason"] == recorded_exit_reason(reason, origin, kind)
    value = report(trade(**{**frozen, "net_pnl": "-1.25", "gross_pnl_usdt": "-1"}))
    result = build_execution_review(value)
    assert result["status"] == "AVAILABLE"
    assert result["trades"][0]["exit_reason"] == expected
    assert result["by_exit_reason"][expected]["settled_count"] == 1
    assert result["totals"]["net_usdt"] == "-1.25"
    assert not result["execution_authorized"]


class _CloseEvidenceConnection:
    """Choose immutable close evidence at fixture insertion, never rewrite it."""

    def __init__(self, conn, evidence):
        self.conn, self.evidence = conn, evidence

    def execute(self, query, params=None):
        if query.lstrip().startswith("INSERT INTO v2_orders") and params[2] == "CLOSE":
            from psycopg.types.json import Jsonb

            params = (*params[:-1], Jsonb(self.evidence))
        return self.conn.execute(query, params)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            {"origin": "BINANCE_EXTERNAL_CLOSE", "kind": "EXTERNAL_REDUCE_ONLY"},
            "RECONCILED_EXTERNAL_CLOSE",
        ),
        (
            {"origin": "BINANCE_EXTERNAL_CLOSE", "kind": "LIQUIDATION"},
            "EXCHANGE_LIQUIDATION",
        ),
        ({"origin": "BINANCE_ALGO_CHILD"}, "VENUE_PROTECTION_CHILD"),
        ({"kind": "LIQUIDATION"}, None),
    ],
)
def test_pg_both_readers_use_same_frozen_close_evidence(database, evidence, expected):
    from services.v2_daily_mover_evidence import load_daily_mover_evidence
    from tests.v2_core.test_v2_daily_mover_evidence import END, START, add_trade
    from tests.v2_core.test_v2_daily_mover_evidence import SCOPE as DB_SCOPE

    with database() as conn:
        identity = add_trade(_CloseEvidenceConnection(conn, evidence))
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        before = conn.execute("SELECT count(*) FROM v2_orders").fetchone()
        reviewed = strict_trade_review(conn, DB_SCOPE, days=14, limit=500, now_ms=END)
        row = next(row for row in reviewed["trades"] if row["id"] == identity)
        assert row["exit_reason"] == expected
        assert row["settlement_state"] == "SETTLED"
        assert Decimal(row["net_pnl"]) == Decimal("19.8")
        mover = load_daily_mover_evidence(
            conn,
            DB_SCOPE,
            start_ms=START,
            end_ms=END,
            as_of_ms=END,
            symbols=["BTCUSDT"],
        )
        assert mover["symbols"]["BTCUSDT"]["trades"][0]["exit_reason"] == expected
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == before


def test_pg_last_close_fill_wins_over_later_partial_update_and_future_or_unfilled_orders(
    database,
):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from tests.v2_core.test_v2_daily_mover_evidence import END, START, add_trade
    from tests.v2_core.test_v2_daily_mover_evidence import SCOPE as DB_SCOPE

    with database() as conn:
        identity = add_trade(
            _CloseEvidenceConnection(conn, {"reason": "PARTIAL_TAKE_PROFIT"}),
            closing="1",
            settle=False,
        )
        conn.execute(
            "UPDATE v2_orders SET updated_at=to_timestamp(%s/1000.0),version=version+1 "
            "WHERE episode_id=%s AND leg='CLOSE'",
            (END - 100, identity),
        )
        for reason, occurred, known, updated in [
            ("PEAK_DRAWDOWN", START + 20000, START + 20000, START + 20000),
            ("UNFILLED_ORDER", None, None, END - 50),
            ("FUTURE_EVENT", END + 1, END - 50, END - 50),
            ("FUTURE_KNOWN_FILL", END - 50, END + 1, END - 50),
            ("FUTURE_ORDER_UPDATE", END - 50, END - 50, END + 1),
        ]:
            order = str(uuid4())
            conn.execute(
                """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,
                request_key,quantity,status,request_evidence,updated_at)
                VALUES (%s,%s,'CLOSE',%s,%s,'1',%s,%s,to_timestamp(%s/1000.0))""",
                (
                    order,
                    identity,
                    order,
                    order,
                    "CANCELLED" if occurred is None else "FILLED",
                    Jsonb({"reason": reason}),
                    updated,
                ),
            )
            if occurred is not None:
                conn.execute(
                    """INSERT INTO v2_fills(fill_key,order_id,quantity,price,fee,
                    fee_currency,occurred_at_ms,payload,created_at)
                    VALUES (%s,%s,'1','110','0','USDT',%s,'{}',to_timestamp(%s/1000.0))""",
                    (str(uuid4()), order, occurred, known),
                )
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        reviewed = trade_review(conn, DB_SCOPE, now_ms=END)
    assert len(reviewed["trades"]) == 1
    assert reviewed["trades"][0]["exit_reason"] == "PEAK_DRAWDOWN"
    assert reviewed["trades"][0]["net_pnl"] is None  # no fabricated settlement
