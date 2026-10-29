import time
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from test_v2_directional_admission import formal
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE, scenario
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture
from test_v2_reentry_observer import installed as installed_fixture
from test_v2_reentry_observer import observed

from services.v2_reentry_evaluation import coverage_report, evaluation_summary, outcomes
from services.v2_reentry_observation_report import observation_report
from services.v2_reentry_observer import ObserverLimits, persist, read_only
from v2_core.strategy import StrategyScope

database = database_fixture
installed = installed_fixture
closed = closed_fixture


def now():
    return time.time_ns() // 1000000 + 1


def get_outcome(connect, episode, *, scope=SCOPE, at=None):
    with connect() as c:
        read_only(c)
        return outcomes(c, scope, [episode], now=now() if at is None else at)[episode]


def test_actual_close_requires_final_settlement_and_future_is_not_visible(
    installed, closed
):
    connect, route = installed
    runtime, episode, income = closed
    before = now()
    assert get_outcome(connect, episode)["state"] == "AWAITING_SETTLEMENT"
    baseline(connect, runtime, episode)
    assert service(connect, income).run_once()["status"] == "CLEAR"
    result = get_outcome(connect, episode)
    assert result["state"] == "SETTLED" and Decimal(result["net_pnl"]) == Decimal(
        "-10.106"
    )
    assert get_outcome(connect, episode, at=before)["net_pnl"] is None
    assert (
        get_outcome(connect, episode, scope=replace(SCOPE, account_id="foreign"))[
            "state"
        ]
        == "UNLINKED_DECISION"
    )
    # Attaching a later outcome must never alter the immutable shadow payload.
    observation = observed()
    observation["candidate"]["decision_id"] = episode
    observation["observed_ms"] = now()
    with connect() as c:
        persist(c, route, observation, limits=ObserverLimits())
    with connect() as c:
        read_only(c)
        saved = c.execute("SELECT payload FROM v2_reentry_observations").fetchone()[0]
        report = observation_report(c, route["registry_id"], SCOPE, days=90, now=now())
        assert len(report["rows"]) == 1
        assert report["rows"][0]["actual"]["state"] == "SETTLED"
        assert Decimal(report["evaluation"]["groups"][0]["actual_net_pnl"]) == Decimal(
            "-10.106"
        )
        assert outcomes(c, SCOPE, [episode], now=now())[episode]["state"] == "SETTLED"
        assert (
            c.execute("SELECT payload FROM v2_reentry_observations").fetchone()[0]
            == saved
        )


def test_unfilled_and_no_intent_decisions_are_not_losses(installed):
    connect, _ = installed
    _, runtime, signal, context = scenario(connect)
    result = formal(runtime).consume(signal, context=context)
    episode = result["intent_id"]
    assert get_outcome(connect, episode)["state"] == "UNFILLED"
    unadmitted = str(uuid4())
    other_consumer = StrategyScope(**asdict(SCOPE), producer="s8").consumer
    with connect() as c:
        c.execute(
            """INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,side,quantity)
          SELECT %s,%s,signal_id,evidence_ref,action,side,quantity FROM v2_strategy_decisions WHERE decision_id=%s""",
            (unadmitted, other_consumer, episode),
        )
    assert get_outcome(connect, unadmitted) == {
        "state": "NO_INTENT",
        "net_pnl": None,
        "intent_id": None,
        "closed_ms": None,
        "settlement_revision": None,
        "settled_ms": None,
    }


def test_partial_exit_not_settled_even_with_forged_early_profit(installed):
    connect, _ = installed
    _, runtime, signal, context = scenario(connect)
    result = formal(runtime).consume(signal, context=context)
    episode, opening = result["intent_id"], result["order_id"]
    orders, ledger = runtime.data.orders, runtime.data.ledger
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=opening,
        exchange_fill_id="open",
        quantity="1.25",
        price="100",
        fee="0.05",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={"source": "qa"},
    )
    orders.transition(
        opening, expected_version=2, status="FILLED", evidence={"fills_complete": True}
    )
    closing, _ = orders.prepare(
        episode, leg="CLOSE", quantity="1.25", request_key="partial", evidence={}
    )
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id="close",
        quantity="0.25",
        price="110",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=4,
        evidence={"source": "qa"},
    )
    with connect() as c:
        c.execute(
            "INSERT INTO v2_settlements VALUES(%s,1,'USDT',%s,clock_timestamp())",
            (
                episode,
                Jsonb(
                    {
                        "net_pnl": "999",
                        "exchange_flat": True,
                        "orders_terminal": True,
                        "fills_complete": True,
                        "cash_complete": True,
                    }
                ),
            ),
        )
    assert get_outcome(connect, episode)["state"] == "OPEN_OR_PARTIAL_EXIT"
    assert get_outcome(connect, episode)["net_pnl"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"cash_complete": False},
        {"exchange_flat": None},
        {"net_pnl": "NaN"},
        {"net_pnl": "1e100"},
        {"orders_terminal": "true"},
    ],
)
def test_invalid_settlement_proof_is_unknown(installed, closed, change):
    connect, _ = installed
    _, episode, _ = closed
    proof = {
        "exchange_flat": True,
        "orders_terminal": True,
        "fills_complete": True,
        "cash_complete": True,
        "net_pnl": "1",
        **change,
    }
    with connect() as c:
        c.execute(
            "INSERT INTO v2_settlements VALUES(%s,1,'USDT',%s,clock_timestamp())",
            (episode, Jsonb(proof)),
        )
    result = get_outcome(connect, episode)
    assert (
        result["state"] == "SETTLEMENT_EVIDENCE_INCOMPLETE"
        and result["net_pnl"] is None
    )


def test_nonterminal_and_maintenance_are_excluded(installed, closed):
    connect, _ = installed
    _, episode, _ = closed
    # Storage fixture represents an older overlapping close left UNKNOWN when
    # another close filled; do not bypass the live prepare() position guard.
    with connect() as c:
        c.execute(
            """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,quantity,status)
          VALUES (%s,%s,'CLOSE',%s,'qa-overlap',0.01,'UNKNOWN')""",
            (uuid4(), episode, str(uuid4())),
        )
    assert get_outcome(connect, episode)["state"] == "AWAITING_TERMINAL_ORDERS"
    # Read-side exclusion must be conservative even with an incomplete imported
    # maintenance record. This is QA data, not a maintenance-authority test.
    with connect() as c:
        c.execute(
            """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,quantity,request_evidence)
          VALUES (%s,%s,'CLOSE',%s,'qa-maintenance',0.01,%s)""",
            (
                uuid4(),
                episode,
                str(uuid4()),
                Jsonb({"origin": "APPROVED_TESTNET_MAINTENANCE"}),
            ),
        )
    assert get_outcome(connect, episode)["state"] == "MAINTENANCE_EXCLUDED"


def test_coverage_detects_pending_overdue_and_does_not_mix_versions(installed):
    connect, route = installed
    _, runtime, signal, context = scenario(connect)
    result = formal(runtime).consume(signal, context=context)
    identity = result["decision_id"]
    record = observed()
    record["candidate"]["decision_id"] = identity
    with connect() as c:
        persist(c, route, record, limits=ObserverLimits())
        fingerprint = c.execute(
            "SELECT policy_digest FROM v2_reentry_observations"
        ).fetchone()[0]
    health = {
        "registry_id": route["registry_id"],
        "policy_digest": "f" * 64,
        "started_ms": 1,
        "limits": {"max_lag_ms": 120000},
    }

    def coverage(at, info=health):
        with connect() as c:
            read_only(c)
            return coverage_report(c, route["registry_id"], SCOPE, health=info, now=at)

    at = now()
    pending = coverage(at)
    assert (pending["decisions"], pending["recorded"], pending["pending"]) == (1, 0, 1)
    overdue = coverage(at + 120001)
    assert overdue["unobserved_over_lag"] == 1
    assert coverage(at, {**health, "policy_digest": fingerprint})["recorded"] == 1
    assert coverage(at, {**health, "stale": True})["status"] == "UNAVAILABLE"
    assert (
        coverage(at, {**health, "registry_id": str(uuid4())})["status"] == "UNAVAILABLE"
    )
    assert coverage(at + 301000)["status"] == "NO_NEW_OPEN"


def test_summary_version_isolation_dedup_unknowns_and_counterexamples():
    def row(
        key, *, policy="a", state="SETTLED", net="5", status="WAIT", claim="NOT_READY"
    ):
        return {
            "decision_id": key,
            "policy_digest": policy,
            "status": status,
            "claim": claim,
            "source": "s3",
            "side": "BUY",
            "config_digest": "original",
            "strategy_version": "v1",
            "actual": {"state": state, "net_pnl": net},
        }

    a = row("a")
    b = row("b", status="READY", claim="RECORDED", net="-2")
    rows = [
        a,
        deepcopy(a),
        b,
        row("c", state="OPEN_OR_PARTIAL_EXIT", net=None),
        row("a", policy="b"),
    ]
    before = deepcopy(rows)
    report = evaluation_summary(rows)
    assert rows == before and not report["automated_promotion"]
    assert (
        report["duplicate_observations_excluded"] == 1
        and report["simulated_pnl"] is None
    )
    groups = {(r["policy_digest"], r["status"]): r for r in report["groups"]}
    assert (
        groups["a", "WAIT"]["settled"] == 1 and groups["a", "WAIT"]["observations"] == 2
    )
    assert groups["a", "WAIT"]["wait_with_actual_profit"] == 1
    assert groups["a", "READY"]["ready_with_actual_loss"] == 1
    assert groups["b", "WAIT"]["settled"] == 1
    empty = evaluation_summary([row("u", state="AWAITING_SETTLEMENT", net=None)])[
        "groups"
    ][0]
    assert empty["actual_net_pnl"] is None and empty["actual_win_rate_pct"] is None


def test_bounded_readonly_contract(installed):
    connect, _ = installed
    with pytest.raises(ValueError, match="READ_ONLY"), connect() as c:
        outcomes(c, SCOPE, [], now=now())
    with pytest.raises(ValueError, match="LIMIT"), connect() as c:
        read_only(c)
        outcomes(c, SCOPE, [str(uuid4())] * 101, now=now())


def test_late_cash_invalidates_old_settlement_label(installed, closed):
    connect, _ = installed
    runtime, episode, income = closed
    baseline(connect, runtime, episode)
    assert service(connect, income).run_once()["status"] == "CLEAR"
    assert get_outcome(connect, episode)["state"] == "SETTLED"
    runtime.data.ledger.adjustment(
        episode_id=episode,
        source_id="qa-late-funding",
        amount_text="-0.02",
        currency="USDT",
        kind="FUNDING",
        occurred_at_ms=70001,
        evidence={"source": "qa"},
    )
    result = get_outcome(connect, episode)
    assert (
        result["state"] == "SETTLEMENT_REVISION_CHANGED" and result["net_pnl"] is None
    )


def test_real_deployment_column_grants_are_sufficient(installed, closed):
    connect, _ = installed
    _, episode, _ = closed
    role = "qa_eval_" + uuid4().hex
    with connect() as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        c.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        c.execute(
            sql.SQL(
                "GRANT SELECT ON v2_strategy_decisions,v2_trade_intents,v2_orders,v2_fills,v2_settlements TO {}"
            ).format(sql.Identifier(role))
        )
        grants = (
            Path(__file__).resolve().parents[2]
            / "deploy/v2-testnet/reentry-evaluation-grants.sql"
        ).read_text()
        c.execute(
            grants.replace("trade_v2", sql.Identifier(schema).as_string(c)).replace(
                "tradev2accounts", sql.Identifier(role).as_string(c)
            )
        )
    try:
        with connect() as c:
            read_only(c)
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            assert (
                outcomes(c, SCOPE, [episode], now=now())[episode]["state"]
                == "AWAITING_SETTLEMENT"
            )
        for query in (
            "SELECT * FROM v2_episodes",
            "UPDATE v2_episodes SET status='ACTIVE'",
            "SELECT * FROM v2_business_state",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connect() as c:
                c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                c.execute(query)
    finally:
        with connect() as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_coverage_bound_is_explicit():
    class Query:
        def fetchall(self):
            return [(90000, False)] * 501

    class Conn:
        def execute(self, query, params=None):
            if query.startswith("SHOW"):
                return self
            assert "LIMIT 501" in query
            return Query()

        def fetchone(self):
            return ("on",)

    registry = str(uuid4())
    result = coverage_report(
        Conn(),
        registry,
        SCOPE,
        health={"registry_id": registry, "started_ms": 1, "policy_digest": "f" * 64},
        now=100000,
    )
    assert (
        result["truncated"] and result["decisions"] == 500 and result["pending"] == 500
    )
