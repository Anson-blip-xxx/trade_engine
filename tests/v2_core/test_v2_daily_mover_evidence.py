"""Daily mover facts, scoped immutable history and isolated-PG integration."""

import json
from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_v2_intent_admission import database as database_fixture

from services.v2_daily_mover_evidence import (
    DAY_MS,
    _stage,
    load_daily_mover_evidence,
    summarize_symbol_evidence,
)
from v2_core.account_risk import AccountScope
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.strategy import StrategyScope

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-mover-account", "SANDBOX", "FUTURES")
START = 20000 * DAY_MS - 28800000
END = START + DAY_MS


def record(**changes):
    return {
        "signal_id": "signal1",
        "source": "tv_bridge",
        "observed_ms": START + 1,
        "action": "IGNORED",
        "reason": "strength_below_minimum",
        "evidence_ref": "f" * 64,
        "fields": {"stage": "SIGNAL_ONLY_PREFILTER"},
        "receipt_outcome": "IGNORED",
        "consumer_strategy": "s6",
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "SIGNAL_PREFILTER_REJECTED"),
        ({"action": "EXPIRED"}, "EXPIRED_NOT_STRATEGY_REJECTION"),
        ({"receipt_outcome": "EXPIRED"}, "EXPIRED_NOT_STRATEGY_REJECTION"),
        ({"action": "OPEN"}, "ENTRY_CANDIDATE"),
        ({"action": None, "receipt_outcome": None}, "ACCOUNT_DECISION_NOT_OBSERVED"),
        ({"action": None}, "RECEIPT_WITHOUT_DECISION_EVIDENCE"),
        ({"evidence_ref": None}, "DECISION_EVIDENCE_UNAVAILABLE"),
        ({"reason": None}, "DECISION_EVIDENCE_UNAVAILABLE"),
        ({"fields": {"jev_status": "VETO"}}, "JEV_VETO"),
        ({"fields": {"jev_status": "SKIP"}}, "JEV_UNAVAILABLE_OR_SKIPPED"),
        ({"reason": "JEV_GATE_UNAVAILABLE"}, "JEV_UNAVAILABLE_OR_SKIPPED"),
        ({"fields": {"context_cause": "MARKET_STALE"}}, "CANDIDATE_DATA_UNAVAILABLE"),
        ({"reason": "UNSUPPORTED_DIRECTIONAL_EVENT"}, "NO_ENTRY_CANDIDATE"),
        ({"fields": {"market_reason": "overextended"}}, "MARKET_FILTER_REJECTED"),
        ({"fields": {"sizing_reason": "risk_exhausted"}}, "SIZING_RISK_REJECTED"),
        (
            {"reason": "THIN", "fields": {"liquidity_reason": "THIN"}},
            "EXECUTION_MARKET_RISK_REJECTED",
        ),
        (
            {"reason": "LOW_EDGE", "fields": {"execution_gate": "LOW_EDGE"}},
            "EXECUTION_MARKET_RISK_REJECTED",
        ),
        ({"reason": "JEV_FUTURE_CODE", "fields": {}}, "OTHER_RECORDED_REJECTION"),
    ],
)
def test_stages_need_explicit_evidence(changes, expected):
    assert _stage(record(**changes)) == expected


def test_shared_signal_is_not_two_signals_and_fallback_is_not_veto():
    evidence = [
        record(),
        record(
            action="OPEN",
            fields={
                "jev_status": "FALLBACK",
                "jev_reason": "JEV_BASELINE_FALLBACK",
                "jev_fallback": "TOKEN_BUDGET_EXHAUSTED",
            },
        ),
    ]
    result = summarize_symbol_evidence(evidence)
    assert result["signal_count"] == 1
    assert result["signal_sources"] == {"tv_bridge": 1}
    assert result["stage_counts"] == {
        "SIGNAL_PREFILTER_REJECTED": 1,
        "ENTRY_CANDIDATE": 1,
    }
    assert result["jev_outcomes"][0]["status"] == "FALLBACK"
    assert result["jev_outcomes"][0]["fallback_cause"] == "TOKEN_BUDGET_EXHAUSTED"


def test_opposing_consumer_rejection_does_not_dominate_entry_direction():
    result = summarize_symbol_evidence(
        [
            record(action="OPEN", reason="DIRECTIONAL_MARKET_GATES_PASSED", fields={}),
            record(
                consumer_strategy="s8",
                reason="UNSUPPORTED_DIRECTIONAL_EVENT",
                fields={},
            ),
        ]
    )
    assert result["by_strategy"]["s6"]["stage_counts"] == {"ENTRY_CANDIDATE": 1}
    assert result["by_strategy"]["s8"]["stage_counts"] == {"NO_ENTRY_CANDIDATE": 1}


def test_empty_is_not_proof_of_missing_strategy_and_examples_are_bounded():
    empty = summarize_symbol_evidence([])
    assert empty["first_signal_ms"] is None
    assert empty["coverage"]["retention_completeness"] == "NOT_PROVEN"
    records = [record(signal_id=str(i)) for i in range(80)]
    result = summarize_symbol_evidence(records, truncated=True)
    assert result["signal_count"] == 80
    assert len(result["evidence"]) == 50
    assert result["coverage"]["evidence_examples_truncated"] is True
    assert result["coverage"]["signals"] == "TRUNCATED"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_ms": START + 1},
        {"end_ms": END + DAY_MS},
        {"as_of_ms": END - 1},
        {"start_ms": True},
        {"symbols": []},
        {"symbols": ["BTCUSDT"] * 2},
        {"symbols": ["BTCUSDT;DROP TABLE"]},
        {"symbols": ["BTC\nUSDT"]},
        {"symbols": ["BTCUSD"]},
        {"max_signals_per_symbol": 251},
        {"max_trades": 501},
        {"max_trades": 0},
        {"max_read_seconds": 61},
        {"scope": asdict(SCOPE)},
    ],
)
def test_rejects_invalid_scope_window_and_query_bounds_before_sql(kwargs):
    with pytest.raises(ValueError):
        load_daily_mover_evidence(
            None,
            **{
                "scope": SCOPE,
                "start_ms": START,
                "end_ms": END,
                "symbols": ["BTCUSDT"],
                **kwargs,
            },
        )


def add_signal(
    conn,
    *,
    source="tv_bridge",
    symbol="BTCUSDT",
    observed=None,
    received=None,
    environment="SANDBOX",
):
    observed = START + 1000 if observed is None else observed
    received = observed if received is None else received
    identity = str(uuid4())
    snapshot = {
        "observed_at": observed,
        "expires_at_ms": observed + 300000,
        "symbol": symbol,
        "signal": "TREND_UP",
        "features": {"strength": 99},
    }
    conn.execute(
        """INSERT INTO v2_inbound_signals
      (signal_id,source,environment,request_key,snapshot,received_at)
      VALUES (%s,%s,%s,%s,%s,to_timestamp(%s/1000.0))""",
        (identity, source, environment, identity, Jsonb(snapshot), received),
    )
    return identity, snapshot


def add_decision(
    conn,
    signal,
    *,
    scope=SCOPE,
    action="IGNORED",
    reason="overextended",
    fields=None,
    at=None,
    producer="s6",
    receipt=True,
):
    signal_id, snapshot = signal
    at = snapshot["observed_at"] + 100 if at is None else at
    identity = str(uuid4())
    proof = DecisionEvidence(
        "qa-mover-v1",
        "{}",
        canonical(
            {
                "signal_id": signal_id,
                "observed_at": snapshot["observed_at"],
                "decided_at": at,
                "source": "tv_bridge",
                "symbol": snapshot["symbol"],
                "rationale": reason,
                "features": {"evaluation": fields or {}},
                "result": {"action": action},
            }
        ),
    )
    consumer = StrategyScope(**asdict(scope), producer=producer).consumer
    conn.execute(
        """INSERT INTO v2_decision_evidence
      (evidence_ref,config_digest,strategy_version,config,snapshot,created_at)
      VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,to_timestamp(%s/1000.0))""",
        (
            proof.evidence_ref,
            proof.config_digest,
            proof.strategy_version,
            proof.config_json,
            proof.snapshot_json,
            at,
        ),
    )
    conn.execute(
        """INSERT INTO v2_strategy_decisions
      (decision_id,consumer,signal_id,evidence_ref,action,side,quantity,created_at)
      VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s/1000.0))""",
        (
            identity,
            consumer,
            signal_id,
            proof.evidence_ref,
            action,
            "BUY" if action == "OPEN" else None,
            "2" if action == "OPEN" else None,
            at,
        ),
    )
    if receipt and action != "OPEN":
        conn.execute(
            """INSERT INTO v2_signal_receipts
          (consumer,signal_id,outcome,reason,completed_at)
          VALUES (%s,%s,%s,%s,to_timestamp(%s/1000.0))""",
            (consumer, signal_id, action, reason, at),
        )
    return identity, proof, at


def add_trade(
    conn,
    *,
    scope=SCOPE,
    opened=None,
    closing="2",
    settle=True,
    settled_at=None,
    side="BUY",
    terminal_reason=None,
    intent_after_open=False,
):
    opened = START + 3000 if opened is None else opened
    signal = add_signal(conn, observed=opened - 1000)
    identity, proof, decided = add_decision(
        conn,
        signal,
        scope=scope,
        action="OPEN",
        reason="DIRECTIONAL_MARKET_GATES_PASSED",
        at=opened + 100 if intent_after_open else None,
    )
    conn.execute(
        """INSERT INTO v2_trade_intents
      (intent_id,exchange,account_id,environment,product,producer,request_key,
       payload,payload_digest,evidence_ref,config_digest,strategy_version,signal_id,created_at)
      VALUES (%s,%s,%s,%s,%s,'s6',%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s/1000.0))""",
        (
            identity,
            *asdict(scope).values(),
            identity,
            Jsonb({"symbol": "BTCUSDT", "side": side}),
            "a" * 64,
            proof.evidence_ref,
            proof.config_digest,
            proof.strategy_version,
            signal[0],
            decided,
        ),
    )
    if terminal_reason:
        conn.execute(
            """INSERT INTO v2_domain_outbox
          (event_id,intent_id,event_type,payload,created_at)
          VALUES (%s,%s,'INTENT_TERMINAL:2',%s,to_timestamp(%s/1000.0))""",
            (
                uuid4(),
                identity,
                Jsonb({"status": "EXPIRED", "reason": terminal_reason}),
                opened,
            ),
        )
        return identity
    conn.execute(
        """INSERT INTO v2_episodes(episode_id,slot_key,accounting_revision,status,created_at)
      VALUES (%s,%s,1,%s,to_timestamp(%s/1000.0))""",
        (identity, identity, "SETTLED" if settle else "ACTIVE", opened),
    )
    for leg, quantity, price, when in [
        ("OPEN", "2", "100", opened),
        ("CLOSE", closing, "110", START + 10000),
    ]:
        if quantity == "0":
            continue
        order = str(uuid4())
        conn.execute(
            """INSERT INTO v2_orders
          (order_id,episode_id,leg,client_order_id,request_key,quantity,status,updated_at,request_evidence)
          VALUES (%s,%s,%s,%s,%s,%s,'FILLED',to_timestamp(%s/1000.0),%s)""",
            (
                order,
                identity,
                leg,
                order,
                order,
                quantity,
                when,
                Jsonb({"reason": "TRAIL_STOP"}),
            ),
        )
        conn.execute(
            """INSERT INTO v2_order_events
          (event_id,order_id,version,status,evidence,created_at)
          VALUES (%s,%s,1,'FILLED','{}',to_timestamp(%s/1000.0))""",
            (uuid4(), order, when),
        )
        conn.execute(
            """INSERT INTO v2_fills
          (fill_key,order_id,quantity,price,fee,fee_currency,occurred_at_ms,payload,created_at)
          VALUES (%s,%s,%s,%s,'0.1','USDT',%s,'{}',to_timestamp(%s/1000.0))""",
            (str(uuid4()), order, quantity, price, when, when),
        )
    if settle:
        settled_at = START + 11000 if settled_at is None else settled_at
        conn.execute(
            """INSERT INTO v2_settlements(episode_id,revision,currency,evidence,settled_at)
          VALUES (%s,1,'USDT',%s,to_timestamp(%s/1000.0))""",
            (
                identity,
                Jsonb(
                    {
                        "net_pnl": "19.8",
                        "exchange_flat": True,
                        "orders_terminal": True,
                        "fills_complete": True,
                        "cash_complete": True,
                        "ledger_revision": 1,
                    }
                ),
                settled_at,
            ),
        )
    return identity


def read(database, **kwargs):
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        before = conn.execute("SELECT count(*) FROM v2_orders").fetchone()
        result = load_daily_mover_evidence(
            conn,
            SCOPE,
            start_ms=START,
            end_ms=END,
            symbols=kwargs.pop("symbols", ["BTCUSDT"]),
            **kwargs,
        )
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == before
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "3s"
        return result


def test_pg_empty_unicode_and_readonly_contract(database):
    with database() as conn, pytest.raises(ValueError, match="READ_ONLY"):
        load_daily_mover_evidence(
            conn, SCOPE, start_ms=START, end_ms=END, symbols=["BTCUSDT"]
        )
    result = read(database, symbols=["BTCUSDT", "币安人生USDT"])
    assert result["symbols"]["币安人生USDT"]["engine_symbol_supported"] is False
    assert result["symbols"]["BTCUSDT"]["signal_count"] == 0
    assert result["symbols"]["BTCUSDT"]["trades"] == []


def test_pg_foreign_account_does_not_explain_our_missing_trade(database):
    with database() as conn:
        signal = add_signal(conn)
        add_decision(
            conn,
            signal,
            scope=replace(SCOPE, account_id="other-account"),
            reason="JEV_CHASE_RISK_HIGH",
            fields={"jev_entry_gate": {"status": "VETO"}},
        )
        add_trade(conn, scope=replace(SCOPE, account_id="other-account"))
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["signal_count"] == 2
    assert result["trades"] == []
    assert result["stage_counts"] == {"ACCOUNT_DECISION_NOT_OBSERVED": 4}
    assert result["jev_outcomes"] == []


def test_pg_immutable_rejections_expiry_fallback_and_window_boundaries(database):
    with database() as conn:
        add_decision(
            conn,
            add_signal(conn),
            reason="JEV_RESULT_UNAVAILABLE",
            fields={
                "jev_entry_gate": {"status": "SKIP", "reason": "JEV_RESULT_UNAVAILABLE"}
            },
        )
        add_decision(
            conn,
            add_signal(conn),
            action="EXPIRED",
            reason="signal expired before evaluation",
        )
        add_decision(
            conn,
            add_signal(conn),
            reason="overextended",
            fields={
                "market_plan": {"reason": "overextended"},
                "hidden_secret": "do-not-export",
            },
        )
        add_signal(conn, observed=END)
        add_signal(conn, observed=START - 1)
        add_signal(conn, received=END + 1)
        add_signal(conn, environment="LIVE")
        add_signal(conn, symbol="ETHUSDT")
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["signal_count"] == 3
    assert result["stage_counts"]["JEV_UNAVAILABLE_OR_SKIPPED"] == 1
    assert result["stage_counts"]["EXPIRED_NOT_STRATEGY_REJECTION"] == 1
    assert result["stage_counts"]["MARKET_FILTER_REJECTED"] == 1
    assert "do-not-export" not in json.dumps(result)
    assert all(r["evidence_ref"] is not None for r in result["evidence"] if r["action"])


def test_pg_signal_cap_is_per_symbol_and_explicit(database):
    with database() as conn:
        add_signal(conn, source="s3")
        add_signal(conn, source="s3")
        add_signal(conn, symbol="ETHUSDT", source="s3")
    result = read(database, symbols=["BTCUSDT", "ETHUSDT"], max_signals_per_symbol=1)
    assert result["symbols"]["BTCUSDT"]["coverage"]["signals"] == "TRUNCATED"
    assert (
        result["symbols"]["ETHUSDT"]["coverage"]["signal_sources"]["s3"]
        == "BOUNDED_QUERY_COMPLETE"
    )
    assert (
        result["symbols"]["ETHUSDT"]["coverage"]["signal_sources"]["tv_bridge"]
        == "NOT_SAMPLED"
    )
    assert result["symbols"]["BTCUSDT"]["signal_count"] == 1


def test_pg_source_fair_sampling_keeps_tv_and_quant_visible(database):
    with database() as conn:
        for _ in range(4):
            add_signal(conn, source="s3", observed=START + 1)
        add_signal(conn, source="tv_bridge", observed=START + 1000)
        add_signal(conn, source="quant", observed=START + 2000)
    result = read(database, max_signals_per_symbol=3)["symbols"]["BTCUSDT"]
    assert result["signal_count"] == 3
    assert result["signal_sources"] == {"s3": 1, "tv_bridge": 1, "quant": 1}
    assert result["coverage"]["signal_sources"] == {
        "s3": "TRUNCATED",
        "tv_bridge": "BOUNDED_QUERY_COMPLETE",
        "quant": "BOUNDED_QUERY_COMPLETE",
    }


def test_pg_source_and_time_strata_cover_late_day_without_exceeding_budget(database):
    with database() as conn:
        for bucket in range(4):
            for n in range(5):
                add_signal(conn, source="s3", observed=START + bucket * 21600000 + n)
        add_signal(conn, source="tv_bridge", observed=END - 1)
    result = read(database, max_signals_per_symbol=12)["symbols"]["BTCUSDT"]
    assert result["signal_count"] == 5
    assert result["signal_sources"] == {"s3": 4, "tv_bridge": 1}
    assert result["last_signal_ms"] == END - 1
    windows = result["coverage"]["signal_time_windows"]
    assert len(windows) == 4
    assert sum(x["sample_limit"] for w in windows for x in w["sources"].values()) == 12
    for bucket, window in enumerate(windows):
        assert window["start_ms"] == START + bucket * 21600000
        assert window["end_ms"] == START + (bucket + 1) * 21600000
        s3 = window["sources"]["s3"]
        assert s3 == {
            "status": "TRUNCATED",
            "sample_limit": 1,
            "examined_unique_signals": 1,
            "observed_lower_bound": 2,
            "sentinel_observed": True,
        }
    assert result["coverage"]["signals"] == "TRUNCATED"


def test_pg_truncated_single_window_is_not_hidden_by_empty_other_windows(database):
    with database() as conn:
        for n in range(24):
            add_signal(conn, source="s3", observed=START + 3 * 21600000 + n)
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["signal_count"] == 21
    assert result["coverage"]["signal_sources"]["s3"] == "TRUNCATED"
    assert (
        result["coverage"]["signal_time_windows"][0]["sources"]["s3"]["status"]
        == "BOUNDED_QUERY_COMPLETE"
    )
    assert (
        result["coverage"]["signal_time_windows"][3]["sources"]["s3"]["status"]
        == "TRUNCATED"
    )


def test_pg_complete_trade_and_carry_in_use_current_validated_labels(database):
    with database() as conn:
        add_trade(conn, opened=START - 5000)
    trade = read(database)["symbols"]["BTCUSDT"]["trades"][0]
    assert trade["carry_in"] is True
    assert trade["net_pnl"] == "19.8"
    assert trade["net_pnl_available"] is True
    assert trade["status"] == "FILLED_SETTLED"
    assert Decimal(trade["entry_vwap"]) == 100
    assert Decimal(trade["exit_fill_vwap"]) == 110
    assert Decimal(trade["gross_pnl_usdt"]) == 20
    assert trade["exit_reason"] == "TRAIL_STOP"
    assert trade["funding_usdt"] is None


def test_pg_partial_exit_has_no_fabricated_settled_pnl(database):
    with database() as conn:
        add_trade(conn, closing="1", settle=False)
    trade = read(database)["symbols"]["BTCUSDT"]["trades"][0]
    assert trade["partial_exit_observed"] is True
    assert trade["closed_ms"] is None
    assert trade["net_pnl"] is None
    assert trade["settlement_state"] == "OPEN_OR_PARTIAL_EXIT"
    assert trade["status"] == "FILLED_UNSETTLED"


def test_pg_later_settlement_is_not_leaked_before_asof(database):
    with database() as conn:
        add_trade(conn, settled_at=END + 1000)
    result = read(database)["symbols"]["BTCUSDT"]["trades"][0]
    assert result["settlement_state"] == "AWAITING_SETTLEMENT"
    assert result["net_pnl"] is None
    later = read(database, as_of_ms=END + 1000)["symbols"]["BTCUSDT"]["trades"][0]
    assert later["net_pnl"] == "19.8"


def test_pg_terminal_expiry_is_not_strategy_risk_rejection(database):
    with database() as conn:
        add_trade(conn, terminal_reason="decision expired before submission")
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["trades"][0]["status"] == "NO_EXECUTION_FILL"
    assert result["execution_reason_counts"] == [
        {
            "stage": "EXECUTION_EXPIRED_NOT_STRATEGY_REJECTION",
            "reason": "decision expired before submission",
            "count": 1,
        }
    ]


def test_pg_intent_cap_does_not_claim_full_day_coverage(database):
    with database() as conn:
        add_trade(conn)
        add_trade(conn)
    result = read(database, max_trades=1)["symbols"]["BTCUSDT"]
    assert result["coverage"]["intents"] == "TRUNCATED"
    assert len(result["trades"]) == 1


def test_pg_one_read_failure_does_not_abort_other_evidence(database, monkeypatch):
    import services.v2_daily_mover_evidence as module

    with database() as conn:
        add_trade(conn)
    monkeypatch.setattr(
        module,
        "_SIGNALS_SQL",
        "SELECT * FROM missing_qa_table WHERE %s=%s AND %s=%s AND %s=%s AND %s=%s AND %s=%s AND %s=%s",
    )
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["signal_count"] is None
    assert result["coverage"]["signals"] == "UNAVAILABLE"
    assert result["coverage"]["signal_error"] == "READ_SCHEMA_UNAVAILABLE"
    assert result["coverage"]["intents"] == "BOUNDED_QUERY_COMPLETE"
    assert result["trades"][0]["net_pnl"] == "19.8"


def test_pg_read_budget_exhaustion_is_unknown_not_zero(database, monkeypatch):
    import services.v2_daily_mover_evidence as module

    clock = iter([0, 30, 31, 32, 33])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = read(database)["symbols"]["BTCUSDT"]
    assert result["signal_count"] is None
    assert result["coverage"]["signal_error"] == "READ_BUDGET_EXHAUSTED"
    assert result["coverage"]["intent_error"] == "READ_BUDGET_EXHAUSTED"
    assert (
        result["coverage"]["signal_time_windows"][0]["sources"]["s3"][
            "observed_lower_bound"
        ]
        is None
    )


def test_pg_stable_snapshot_required(database):
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        with pytest.raises(ValueError, match="STABLE_SNAPSHOT"):
            load_daily_mover_evidence(
                conn, SCOPE, start_ms=START, end_ms=END, symbols=["BTCUSDT"]
            )


def test_pg_impossible_fill_before_intent_is_not_valid_settled_profit(database):
    with database() as conn:
        add_trade(conn, intent_after_open=True)
    trade = read(database)["symbols"]["BTCUSDT"]["trades"][0]
    assert trade["settlement_state"] == "INVALID_FILL_TIMING"
    assert trade["net_pnl"] is None
    assert trade["net_pnl_available"] is False
    assert trade["fills_valid"] is False
    assert trade["gross_pnl_usdt"] is None
