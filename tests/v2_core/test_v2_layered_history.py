from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest

from services.v2_layered_history import LayeredHistory, summarize, validate
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.entry_controls import Controls, resolve
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical, digest
from v2_core.intents import IntentStore, OpenIntent

database = database_fixture
SCOPE = AccountScope("BINANCE", "history-qa", "SANDBOX", "FUTURES")
NOW = 1_800_000_000_000
PLAN = {"strategy": "S6", "side": "LONG", "entry_mode": "PULLBACK"}
SIGNAL = {"symbol": "BTCUSDT", "source": "s3"}


def controls(scope=SCOPE, **changes):
    values = resolve({"history.mode": "SHADOW", **changes})
    return Controls(
        1,
        values,
        digest(canonical({"scope": asdict(scope), "version": 1, "values": values})),
    )


def context(scope=SCOPE, now=NOW):
    return {
        "account_scope": asdict(scope),
        "environment": scope.environment,
        "symbol": "BTCUSDT",
        "assembled_at": now,
        "directional": {"regime": "neutral"},
    }


def row(*, scope=SCOPE, net="-2", symbol="BTCUSDT"):
    return {
        "episode_id": str(uuid4()),
        **asdict(scope),
        "producer": "s6",
        "symbol": symbol,
        "closed_at_ms": NOW - 10000,
        "settlement_revision": 2,
        "net_pnl": Decimal(net),
        "outcome_evidence": {"source": "directional-outcome-v1"},
        "recorded_at_ms": NOW - 1000,
        "settled_at_ms": NOW - 2000,
        "currency": "USDT",
        "settlement_evidence": {
            "exchange_flat": True,
            "orders_terminal": True,
            "fills_complete": True,
            "cash_complete": True,
            "net_pnl": net,
            "ledger_revision": 2,
            "observed_at_ms": NOW - 5000,
            "account_scope": asdict(scope),
        },
        "evidence_ref": "a" * 64,
        "source": "s3",
        "plan": deepcopy(PLAN),
        "regime": "neutral",
        "stop_price": "98",
        "decided_at_ms": str(NOW - 20000),
        "opened_quantity": Decimal(1),
        "closed_quantity": Decimal(1),
        "opening_notional": Decimal(100),
        "latest_fill_ms": NOW - 10000,
        "latest_fill_known_at_ms": NOW - 7000,
        "maintenance": False,
    }


def summary(rows, *, scope=SCOPE, settings=None, **kwargs):
    return summarize(
        rows,
        scope=scope,
        producer="s6",
        signal=SIGNAL,
        context=context(scope),
        plan=PLAN,
        controls=settings or controls(scope),
        **kwargs,
    ).as_dict()


def test_missing_history_is_unknown_not_positive():
    result = summary([])
    assert result["status"] == "UNKNOWN_INSUFFICIENT_HISTORY"
    assert result["selected_stats"]["mean_r"] is None
    assert result["selected_stats"]["win_rate"] is None
    assert result["suggested_factor"] == result["applied_factor"] == "1"


def test_symbol_sparse_pools_only_same_family_and_uses_actual_r():
    rows = [row(net="-2", symbol="ETHUSDT") for _ in range(30)]
    rows += [row(net="4")]
    proof = summary(rows)
    assert proof["selected_level"] == "FAMILY"
    assert proof["family_stats"]["count"] == 31
    assert proof["symbol_stats"]["count"] == 1
    assert proof["samples"][0]["initial_stop_risk"] == "2"
    assert proof["samples"][0]["net_r"] == "-1"
    assert proof["selected_stats"]["net_pnl"] == "-56"
    assert proof["suggested_factor"] == "0.75"
    assert proof["applied_factor"] == "1"


def test_enough_symbol_shrinks_towards_separate_family_prior():
    rows = [row(net="-2") for _ in range(30)]
    rows += [row(net="1", symbol="ETHUSDT") for _ in range(30)]
    proof = summary(rows, settings=controls(**{"history.mode": "SOFT"}))
    assert proof["selected_level"] == "SYMBOL"
    assert proof["prior_mean_r"] == "0.5"
    assert proof["posterior_mean_r"] == "-0.4"
    assert proof["selected_stats"]["count"] == 30
    assert proof["applied_factor"] == "0.75"


@pytest.mark.parametrize("mode", ["SHADOW", "SOFT"])
def test_positive_history_never_increases_risk(mode):
    proof = summary(
        [row(net="4") for _ in range(30)], settings=controls(**{"history.mode": mode})
    )
    assert proof["status"] == "SUFFICIENT"
    assert proof["suggested_factor"] == proof["applied_factor"] == "1"
    assert proof["family_stats"]["win_rate"] == "1"
    assert proof["family_stats"]["profit_factor"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "another"),
        ("environment", "LIVE"),
        ("producer", "s8"),
        ("source", "tv_bridge"),
        ("regime", "weak_bear"),
        ("plan", {**PLAN, "side": "SHORT"}),
        ("plan", {**PLAN, "entry_mode": "BREAKOUT"}),
        ("plan", {"strategy": "S6"}),
        ("maintenance", True),
        ("maintenance", None),
        ("outcome_evidence", {"strategy_learning_eligible": False}),
        ("outcome_evidence", None),
        ("stop_price", "100"),
        ("stop_price", "101"),
        ("stop_price", "NaN"),
        ("stop_price", None),
        ("closed_quantity", Decimal("0.5")),
        ("net_pnl", Decimal(-1)),
        ("currency", "USDC"),
        ("closed_at_ms", NOW + 1),
        ("settled_at_ms", NOW + 1),
        ("recorded_at_ms", NOW + 1),
        ("latest_fill_ms", NOW + 1),
        ("latest_fill_known_at_ms", NOW + 1),
        ("latest_fill_known_at_ms", None),
        ("latest_fill_known_at_ms", -1),
        ("decided_at_ms", str(NOW + 1)),
        ("closed_at_ms", NOW - 15 * 86400000),
        ("settlement_evidence", {}),
        ("settlement_evidence", None),
    ],
)
def test_unknown_cross_scope_future_maintenance_or_bad_facts_are_excluded(field, value):
    bad = row()
    bad[field] = value
    proof = summary([bad])
    assert proof["eligible_count"] == 0
    assert sum(proof["excluded"].values()) == 1
    assert proof["applied_factor"] == "1"


@pytest.mark.parametrize(
    "field", ["cash_complete", "fills_complete", "orders_terminal", "exchange_flat"]
)
def test_requires_all_complete_settlement_checks(field):
    bad = row()
    bad["settlement_evidence"][field] = False
    assert summary([bad])["excluded"] == {"INCOMPLETE_SETTLEMENT": 1}


def test_short_initial_risk_uses_direction_and_actual_quantity():
    short_plan = {"strategy": "S8", "side": "SHORT", "entry_mode": "PULLBACK"}
    sample = row()
    sample.update(
        producer="s8",
        plan=short_plan,
        stop_price="103",
        opening_notional=Decimal(202),
        opened_quantity=Decimal(2),
        closed_quantity=Decimal(2),
    )
    proof = summarize(
        [sample],
        scope=SCOPE,
        producer="s8",
        signal=SIGNAL,
        context=context(),
        plan=short_plan,
        controls=controls(),
    ).as_dict()
    assert proof["samples"][0]["initial_stop_risk"] == "4"
    assert proof["samples"][0]["net_r"] == "-0.5"


def test_duplicate_samples_do_not_inflate_count_or_sample_size():
    sample = row()
    proof = summary([sample] * 30, settings=controls(**{"history.mode": "SOFT"}))
    assert proof["eligible_count"] == 1
    assert proof["excluded"] == {"DUPLICATE_EPISODE": 29}
    assert proof["status"] == "UNKNOWN_INSUFFICIENT_HISTORY"
    assert proof["applied_factor"] == "1"


def test_evidence_is_frozen_and_replay_verifies_scope_configuration_and_factors():
    settings = controls(**{"history.mode": "SOFT"})
    frozen = summarize(
        [row() for _ in range(30)],
        scope=SCOPE,
        producer="s6",
        signal=SIGNAL,
        context=context(),
        plan=PLAN,
        controls=settings,
    )
    proof = frozen.as_dict()
    assert validate(proof, SIGNAL, context(), PLAN, settings.snapshot()) == "0.75"
    proof["samples"].clear()
    assert len(frozen.as_dict()["samples"]) == 30
    with pytest.raises(ValueError, match="DIGEST_MISMATCH"):
        validate(proof, SIGNAL, context(), PLAN, settings)
    with pytest.raises(ValueError, match="SCOPE_OR_POLICY"):
        validate(frozen.as_dict(), SIGNAL, context(), PLAN, controls())
    with pytest.raises(ValueError, match="CONTROL_SCOPE"):
        validate(
            frozen.as_dict(),
            SIGNAL,
            context(),
            PLAN,
            controls(replace(SCOPE, account_id="different")),
        )
    with pytest.raises(ValueError, match="SCOPE_OR_POLICY"):
        validate(
            frozen.as_dict(),
            {**SIGNAL, "source": "tv_bridge"},
            context(),
            PLAN,
            settings,
        )


def test_stale_history_or_live_can_never_apply_soft():
    settings = controls(**{"history.mode": "SOFT"})
    proof = summary(
        [row() for _ in range(30)], settings=settings, data_asof_ms=NOW - 31000
    )
    assert proof["applied_factor"] == "1"
    live = replace(SCOPE, environment="LIVE")
    proof = summary(
        [row(scope=live) for _ in range(30)],
        scope=live,
        settings=controls(live, **{"history.mode": "SOFT"}),
    )
    assert proof["applied_factor"] == "1"


def test_default_off_does_not_read_and_failure_is_unknown_cached_sanitized():
    calls = []

    @contextmanager
    def connect():
        calls.append(1)
        raise ConnectionError("sensitive connection details")
        yield

    provider = LayeredHistory(connect, scope=SCOPE, producer="s6")
    assert provider.evaluate(SIGNAL, context(), PLAN).status == "DISABLED"
    assert calls == []
    settings = controls()
    first = provider.evaluate(SIGNAL, context(), PLAN, settings)
    second = provider.evaluate(SIGNAL, context(now=NOW + 1000), PLAN, settings)
    assert len(calls) == 1
    assert first.status == second.status == "UNKNOWN_READ_FAILED"
    assert "sensitive" not in first.evidence_json
    assert second.as_dict()["data_asof_ms"] == NOW
    provider.evaluate(SIGNAL, context(now=NOW + 31000), PLAN, settings)
    assert len(calls) == 2


def test_bounded_query_in_cache_and_immutable_read_timestamp():
    calls = []

    class Connection:
        def execute(self, sql, parameters=None):
            calls.append((sql, parameters))
            return self

        def fetchall(self):
            return []

    @contextmanager
    def connect():
        yield Connection()

    provider = LayeredHistory(connect, scope=SCOPE, producer="s6")
    provider.evaluate(SIGNAL, context(), PLAN, controls())
    proof = provider.evaluate(
        SIGNAL, context(now=NOW + 1000), PLAN, controls()
    ).as_dict()
    assert len(calls) == 2
    assert calls[0][0] == "SET LOCAL statement_timeout='2s'"
    assert "WITH recent AS MATERIALIZED" in calls[1][0]
    assert calls[1][1][-1] == 501
    assert proof["data_asof_ms"] == NOW
    provider.evaluate(SIGNAL, context(now=NOW - 1), PLAN, controls())
    assert (
        len(calls) == 4
    )  # A cached future observation can never satisfy a past query.


def seed(database, sample):
    """Real schema fixture, not a fake database cursor or a trading workflow."""
    snapshot = {
        "observed_at": NOW - 20000,
        "decided_at": NOW - 20000,
        "source": sample["source"],
        "symbol": sample["symbol"],
        "rationale": "qa",
        "features": {
            "context": {"directional": {"regime": sample["regime"]}},
            "evaluation": {
                "market_plan": sample["plan"],
                "sizing": {"stop_price": sample["stop_price"]},
            },
        },
    }
    proof = DecisionEvidence("layered-qa-v1", canonical({}), canonical(snapshot))
    EvidenceStore(database).put(proof)
    intent = OpenIntent(
        sample["episode_id"],
        sample["exchange"],
        sample["account_id"],
        sample["environment"],
        sample["product"],
        sample["producer"],
        str(uuid4()),
        sample["symbol"],
        "BUY",
        "1",
        proof.strategy_version,
        proof.config_digest,
        proof.evidence_ref,
    )
    IntentStore(database).admit(intent)
    with database() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET status='FILLED',version=version+1 "
            "WHERE intent_id=%s",
            (sample["episode_id"],),
        )
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key,status,accounting_revision) "
            "VALUES (%s,%s,'SETTLED',2)",
            (sample["episode_id"], str(uuid4())),
        )
        for leg in ("OPEN", "CLOSE"):
            order = str(uuid4())
            conn.execute(
                "INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,"
                "quantity,status,updated_at,request_evidence) VALUES (%s,%s,%s,%s,%s,1,'FILLED',"
                "to_timestamp(%s::numeric/1000),%s::jsonb)",
                (
                    order,
                    sample["episode_id"],
                    leg,
                    str(uuid4()),
                    str(uuid4()),
                    sample.get(
                        "order_updated_at_ms", sample["latest_fill_known_at_ms"]
                    ),
                    canonical({"reason": sample.get("close_reason", "QA_CLOSE")})
                    if leg == "CLOSE"
                    else "{}",
                ),
            )
            conn.execute(
                "INSERT INTO v2_fills(fill_key,order_id,quantity,price,fee,fee_currency,"
                "occurred_at_ms,payload,created_at) VALUES (%s,%s,1,100,0,'USDT',%s,'{}',"
                "to_timestamp(%s::numeric/1000))",
                (
                    str(uuid4()),
                    order,
                    sample["latest_fill_ms"],
                    sample.get("fill_created_at_ms", sample["latest_fill_known_at_ms"]),
                ),
            )
        conn.execute(
            "INSERT INTO v2_settlements(episode_id,revision,currency,evidence,settled_at) "
            "VALUES (%s,2,'USDT',%s::jsonb,to_timestamp(%s::numeric/1000))",
            (
                sample["episode_id"],
                canonical(sample["settlement_evidence"]),
                sample["settled_at_ms"],
            ),
        )
        conn.execute(
            "INSERT INTO v2_directional_outcomes(episode_id,exchange,account_id,"
            "environment,product,producer,symbol,event_type,closed_at_ms,"
            "settlement_revision,net_pnl,return_pct,quality_score,closing_price,"
            "evidence,recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'TREND_UP',%s,2,%s,"
            "-2,80,100,%s::jsonb,to_timestamp(%s::numeric/1000))",
            (
                sample["episode_id"],
                sample["exchange"],
                sample["account_id"],
                sample["environment"],
                sample["product"],
                sample["producer"],
                sample["symbol"],
                sample["closed_at_ms"],
                sample["net_pnl"],
                canonical(sample["outcome_evidence"]),
                sample["recorded_at_ms"],
            ),
        )


def test_real_postgres_scope_settlement_time_readonly_and_join(database):
    good = row()
    seed(database, good)
    seed(database, row(scope=replace(SCOPE, account_id="other")))
    seed(database, row(scope=replace(SCOPE, environment="LIVE")))
    future = row()
    future["settled_at_ms"] = NOW + 1
    seed(database, future)
    late_fill = row()
    late_fill["fill_created_at_ms"] = NOW + 1
    seed(database, late_fill)
    late_order = row()
    late_order["order_updated_at_ms"] = NOW + 1
    seed(database, late_order)
    maintenance = row()
    maintenance["outcome_evidence"]["strategy_learning_eligible"] = False
    seed(database, maintenance)
    with database() as conn:
        before = conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()

    @contextmanager
    def readonly():
        with database() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn

    result = (
        LayeredHistory(readonly, scope=SCOPE, producer="s6")
        .evaluate(SIGNAL, context(), PLAN, controls())
        .as_dict()
    )
    assert result["read_error"] is None
    assert result["queried_count"] == 5
    assert result["eligible_count"] == 1
    assert result["samples"][0]["episode_id"] == good["episode_id"]
    assert result["samples"][0]["net_r"] == "-1"
    assert result["excluded"] == {
        "FUTURE_OR_UNTIMED_FACT": 3,
        "MAINTENANCE_OR_UNKNOWN_ORIGIN": 1,
    }
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == before
        )
