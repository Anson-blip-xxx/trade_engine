"""Characterize the shared anchor read without a database or service fixture."""

import ast
import inspect
from dataclasses import replace
from decimal import Decimal

import pytest

from services import v2_entry_structure
from v2_core import entry_controls, entry_history
from v2_core.account_risk import AccountScope

SCOPE = AccountScope("BINANCE", "entry-history-qa", "SANDBOX", "FUTURES")
NOW = 1_000_000

# Preserve the exact query contract, including the actual-fill ordering and
# settlement revision. Whitespace is irrelevant; predicates and expressions
# are not. This is a static expectation, not a moving Git-baseline assertion.
EXPECTED_QUERY = """
SELECT i.intent_id::text,i.payload->>'side',ep.status,f.oq,f.cq,
  f.closed_ms,f.known_ms,terminal.price,terminal.orders,
  cl.request_evidence->>'reason',
  EXISTS(SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id
    AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')),
  settled.evidence->>'net_pnl',cl.request_evidence->>'origin',
  cl.request_evidence->>'kind'
FROM v2_trade_intents i JOIN v2_episodes ep ON ep.episode_id=i.intent_id
LEFT JOIN v2_settlements settled ON settled.episode_id=i.intent_id
  AND settled.revision=ep.accounting_revision AND settled.currency='USDT'
JOIN LATERAL (
  SELECT sum(x.quantity) FILTER(WHERE o.leg='OPEN') oq,
    coalesce(sum(x.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
    max(x.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
    (extract(epoch FROM max(greatest(x.created_at,o.updated_at)) FILTER(WHERE o.leg='CLOSE'))*1000)::bigint known_ms,
    max(x.occurred_at_ms) last_fill
  FROM v2_orders o JOIN v2_fills x USING(order_id) WHERE o.episode_id=i.intent_id
) f ON f.oq>0
LEFT JOIN LATERAL (
  SELECT round(sum(x.price*x.quantity)/nullif(sum(x.quantity),0),18)::text price,
    count(DISTINCT o.order_id) orders
  FROM v2_orders o JOIN v2_fills x USING(order_id)
  WHERE o.episode_id=i.intent_id AND o.leg='CLOSE' AND x.occurred_at_ms=f.closed_ms
) terminal ON true
LEFT JOIN LATERAL (
  SELECT request_evidence FROM v2_orders o
  JOIN LATERAL (SELECT max(x.occurred_at_ms) latest_fill FROM v2_fills x
    WHERE x.order_id=o.order_id) cf ON cf.latest_fill IS NOT NULL
  WHERE o.episode_id=i.intent_id AND leg='CLOSE'
  ORDER BY cf.latest_fill DESC,o.order_id DESC LIMIT 1
) cl ON true
WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
  AND i.payload->>'symbol'=%s AND i.producer IN ('s6','s8')
ORDER BY f.last_fill DESC,i.intent_id DESC LIMIT 1
"""


def row(**changes):
    values = {
        "identity": "previous-episode",
        "side": "BUY",
        "status": "SETTLED",
        "oq": Decimal("2.5"),
        "cq": Decimal("2.5"),
        "closed": NOW - 200,
        "known": NOW - 100,
        "price": "0.123456789123456789",
        "count": 1,
        "reason": "PEAK_DRAWDOWN",
        "pending": False,
        "net": "-1.23456789",
        "origin": None,
        "kind": None,
    }
    values.update(changes)
    return tuple(values.values())


class Connection:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, query, parameters):
        self.calls.append((query, parameters))
        return self

    def fetchone(self):
        return self.result


def test_service_export_is_the_same_shared_repository_function():
    assert v2_entry_structure.prior_close is entry_history.prior_close
    assert entry_history.prior_close.__module__ == "v2_core.entry_history"


@pytest.mark.parametrize(
    "scope,target,expected",
    [
        (
            SCOPE,
            "QUSDT",
            ("BINANCE", "entry-history-qa", "SANDBOX", "FUTURES", "QUSDT"),
        ),
        (
            replace(SCOPE, account_id="different-account", environment="LIVE"),
            "AKEUSDT",
            ("BINANCE", "different-account", "LIVE", "FUTURES", "AKEUSDT"),
        ),
        (
            replace(SCOPE, account_id="third-account"),
            "BTCUSDT",
            ("BINANCE", "third-account", "SANDBOX", "FUTURES", "BTCUSDT"),
        ),
    ],
)
def test_query_and_account_scope_parameter_order_are_unchanged(scope, target, expected):
    conn = Connection(None)
    assert entry_history.prior_close(conn, scope, target, NOW) == (
        None,
        "NO_PREVIOUS_FILLED_EPISODE",
    )
    assert len(conn.calls) == 1
    query, parameters = conn.calls[0]
    assert " ".join(query.split()) == " ".join(EXPECTED_QUERY.split())
    assert parameters == expected


@pytest.mark.parametrize("side", ["BUY", "SELL", "UNKNOWN"])
@pytest.mark.parametrize(
    "closed,known", [(NOW - 200, NOW - 100), (NOW - 100, NOW - 200), (NOW, NOW)]
)
def test_return_values_preserve_actual_side_precision_and_latest_known_time(
    side, closed, known
):
    conn = Connection(row(side=side, closed=closed, known=known))
    assert entry_history.prior_close(conn, SCOPE, "QUSDT", NOW) == (
        {
            "previous_episode": "previous-episode",
            "side": side,
            "exit_reason": "PEAK_DRAWDOWN",
            "exit_known_ms": max(closed, known),
            "exit_price": "0.123456789123456789",
            "exit_net_pnl": "-1.23456789",
        },
        None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ACTIVE"},
        {"status": "CLOSED"},
        {"oq": Decimal("2.50000001")},
        {"cq": Decimal("2.50000001")},
        {"pending": True},
        {"closed": None},
        {"closed": 0},
        {"known": None},
        {"known": 0},
        {"closed": NOW + 1},
        {"known": NOW + 1},
        {"status": "ACTIVE", "count": 2, "price": None},
    ],
)
def test_unresolved_latest_episode_is_not_skipped_for_an_older_close(changes):
    conn = Connection(row(**changes))
    assert entry_history.prior_close(conn, SCOPE, "QUSDT", NOW) == (
        None,
        "PREVIOUS_EPISODE_UNRESOLVED",
    )
    assert len(conn.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [{"count": 0}, {"count": 2}, {"count": None}, {"price": None}, {"price": ""}],
)
def test_ambiguous_terminal_fill_rejection_is_unchanged(changes):
    conn = Connection(row(**changes))
    assert entry_history.prior_close(conn, SCOPE, "QUSDT", NOW) == (
        None,
        "PREVIOUS_CLOSE_PRICE_AMBIGUOUS",
    )


@pytest.mark.parametrize("reason", ["VENUE_PROTECTION_CHILD", None])
def test_nullable_settlement_pnl_and_exit_reason_are_not_invented(reason):
    conn = Connection(row(net=None, reason=reason))
    result, issue = entry_history.prior_close(conn, SCOPE, "QUSDT", NOW)
    assert issue is None
    assert result["exit_reason"] == reason
    assert result["exit_net_pnl"] is None


@pytest.mark.parametrize(
    "origin,kind,reason,expected",
    [
        (
            "BINANCE_EXTERNAL_CLOSE",
            "EXTERNAL_REDUCE_ONLY",
            None,
            "RECONCILED_EXTERNAL_CLOSE",
        ),
        (
            "BINANCE_EXTERNAL_CLOSE",
            "EXTERNAL_REDUCE_ONLY",
            "PEAK_DRAWDOWN",
            "RECONCILED_EXTERNAL_CLOSE",
        ),
        ("BINANCE_EXTERNAL_CLOSE", "LIQUIDATION", None, "EXCHANGE_LIQUIDATION"),
        (
            "BINANCE_EXTERNAL_CLOSE",
            "LIQUIDATION",
            "PEAK_DRAWDOWN",
            "EXCHANGE_LIQUIDATION",
        ),
        ("BINANCE_EXTERNAL_CLOSE", None, "PEAK_DRAWDOWN", None),
        ("BINANCE_EXTERNAL_CLOSE", "UNKNOWN", "PEAK_DRAWDOWN", None),
        ("BINANCE_ALGO_CHILD", None, None, "VENUE_PROTECTION_CHILD"),
        (None, None, "PEAK_DRAWDOWN", "PEAK_DRAWDOWN"),
        ("OTHER_EXTERNAL_SOURCE", "EXTERNAL_REDUCE_ONLY", None, None),
    ],
)
def test_verified_close_provenance_does_not_invent_strategy_cause(
    origin, kind, reason, expected
):
    conn = Connection(row(origin=origin, kind=kind, reason=reason))
    result, issue = entry_history.prior_close(conn, SCOPE, "QUSDT", NOW)
    assert issue is None
    assert result["exit_reason"] == expected
    assert result["exit_net_pnl"] == "-1.23456789"


@pytest.mark.parametrize(
    "changes", [{"status": "ACTIVE"}, {"pending": True}, {"cq": Decimal(0)}]
)
def test_external_close_binding_never_bypasses_settlement_or_flatness(changes):
    conn = Connection(
        row(
            origin="BINANCE_EXTERNAL_CLOSE",
            kind="EXTERNAL_REDUCE_ONLY",
            reason=None,
            **changes,
        )
    )
    assert entry_history.prior_close(conn, SCOPE, "QUSDT", NOW) == (
        None,
        "PREVIOUS_EPISODE_UNRESOLVED",
    )


def test_read_failure_propagates_to_the_callers_existing_fence():
    problem = RuntimeError("synthetic history read failure")

    class FailedConnection:
        def execute(self, *_):
            raise problem

    with pytest.raises(RuntimeError) as caught:
        entry_history.prior_close(FailedConnection(), SCOPE, "QUSDT", NOW)
    assert caught.value is problem


@pytest.mark.parametrize("module", [entry_controls, entry_history])
def test_core_history_and_submission_do_not_import_the_service_layer(module):
    tree = ast.parse(inspect.getsource(module))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert not any(
        name == "services" or name.startswith("services.") for name in imports
    )


def test_final_submission_resolves_history_from_core_inside_its_existing_fence():
    tree = ast.parse(inspect.getsource(entry_controls.validate_submission))
    history_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "v2_core.entry_history"
    ]
    assert len(history_imports) == 1
    assert [alias.name for alias in history_imports[0].names] == ["prior_close"]
    history_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "prior_close"
    ]
    assert len(history_calls) == 1
    transactions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "transaction"
            for item in node.items
        )
    ]
    assert any(history_calls[0] in list(ast.walk(node)) for node in transactions)
