"""Isolated PG bridge acceptance; synthetic facts, no venue sockets or writes."""

import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_candidate_producer import CandidateProducer
from services.v2_strategy_entry_job import run_entry_work
from services.v2_strategy_market_reader import ClosedMarketBars
from tests.v2_core import test_v2_strategy_deployment_execution as financial
from tests.v2_core.test_v2_settlement_execution_fence import (
    database,
    fenced,
    route_update,
)
from tests.v2_core.test_v2_strategy_candidate_builder import bars, policy, safety
from tests.v2_core.test_v2_strategy_entry_job import ROOT
from tests.v2_core.test_v2_strategy_sizing import sizing_policy, sizing_scene
from v2_core.evidence import canonical
from v2_core.shared_market import Catalog
from v2_core.signals import Signals

__all__ = ["database", "fenced"]


def now(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]


def scene(fenced, *, side="BUY", source="tv_bridge", builder_policy=None):
    with fenced.admin() as conn:
        for migration in (
            "20261126_strategy_entry_candidates.sql",
            "20261126_entry_discovery_cursors.sql",
            "20260929_shared_market.sql",
        ):
            conn.execute((ROOT / "db/migrations" / migration).read_text())
    sizing = sizing_policy(
        single_margin_fraction=".01",
        cost_fraction=".002",
        max_fact_age_ms=60000,
        max_quote_age_ms=30000,
    )
    _, _, _, template = sizing_scene(side=side, policy=sizing, selected_leverage=2)
    deployment = financial.binding(
        config={
            "candidate_builder": (
                builder_policy or policy(max_structure_age_ms=1800000)
            ).binding_config(),
            "sizing": sizing.binding_config(),
            "capital": sizing.capital_config(),
            "producer": {
                "schema": "strategy-candidate-producer-v1",
                "catalog_max_age_ms": 60000,
                "exit_version": "v1",
                "exit_config": {"mode": "FIXED_STOP"},
            },
        }
    )
    financial.publish(fenced.admin, deployment)
    financial.risk(fenced.admin)
    args = dict(fenced.args)
    args.pop("environment")
    binding = ManagementBinding(financial.SCOPE, **args)
    tick = now(fenced.admin)
    snapshot = {
        "observed_at": tick // 900000 * 900000,
        "expires_at_ms": tick + 60000,
        "symbol": "BTCUSDT",
        "signal": "TREND_DOWN_SHORT",
        "features": {"strength": 99, "price": "99999", "setup": "IGNORE_THIS"},
    }
    identity = Signals(fenced.admin).admit(
        source=source,
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot=snapshot,
    )
    row = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "filters": [
            {
                "filterType": "MARKET_LOT_SIZE",
                "stepSize": template.quantity_step,
                "minQty": template.min_quantity,
                "maxQty": template.max_quantity,
            },
            {
                "filterType": "PRICE_FILTER",
                "tickSize": template.price_tick,
                "minPrice": template.min_price,
                "maxPrice": template.max_price,
            },
            {"filterType": "MIN_NOTIONAL", "notional": template.min_notional},
        ],
    }
    Catalog(fenced.admin, "SANDBOX", lambda: now(fenced.admin)).save({"symbols": [row]})
    calls = []

    def market(target, *, closed_at_ms):
        calls.append("market")
        completed = closed_at_ms // 900000 * 900000
        rows = bars(side=side)
        offset = completed - len(rows) * 60000
        rows = [{**r, "t": r["t"] + offset} for r in rows]
        rows += [
            {**rows[-1], "t": stamp} for stamp in range(completed, closed_at_ms, 60000)
        ]
        return ClosedMarketBars(
            target, "SANDBOX", closed_at_ms, now(fenced.admin), json.dumps(rows)
        )

    def account(state, selected, frame):
        calls.append("account")
        tick = now(fenced.admin)
        proof = template.leverage_proof
        proof["venue"].update(observed_at_ms=tick, book_at_ms=tick)
        return replace(
            template,
            scope=financial.SCOPE,
            observed_at_ms=tick,
            available_at_ms=tick,
            account_at_ms=tick,
            quote_at_ms=tick,
            filters_at_ms=tick,
            leverage_proof_json=canonical(proof),
        )

    def execution(plan):
        calls.append("execution")
        return safety(plan)

    producer = CandidateProducer(
        fenced.admin,
        binding=binding,
        registry=financial.REGISTRY,
        instance_id=deployment.instance_id,
        source=source,
        market_reader=SimpleNamespace(read=market),
        account_reader=account,
        execution_reader=execution,
        stop=Event(),
    )
    return SimpleNamespace(
        producer=producer,
        identity=identity,
        binding=binding,
        deployment=deployment,
        calls=calls,
        catalog=row,
        snapshot=snapshot,
    )


def candidate(selected):
    return selected.producer._state(selected.identity).candidate


def consume(fenced, selected):
    return run_entry_work(
        fenced.admin,
        binding=selected.binding,
        registry=financial.REGISTRY,
        producer=selected.deployment.instance_id,
        source=selected.producer.source,
        stop=Event(),
        clock_ms=financial.now,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_actual_sizing_to_freeze_to_prepared_without_signal_direction_authority(
    fenced, side, source
):
    selected = scene(fenced, side=side, source=source)
    result = selected.producer.prepare(selected.identity)
    assert result["status"] == "FROZEN" and result["execution_authorized"] is False
    saved = candidate(selected)
    assert saved.plan.side == side
    assert saved.plan.leverage == 2
    assert saved.features["sizing"]["status"] == "SIZED"
    assert saved.features["capital"]["leverage_decision"]["selected"] == 2
    assert selected.calls == ["market", "account", "execution"]
    assert consume(fenced, selected)["signals"] == {selected.identity: "PREPARED"}
    before = list(selected.calls)
    replay = selected.producer.prepare(selected.identity)
    assert replay["status"] == "RECOVERY_REQUIRED" and replay["receipt"] == "INTENT"
    assert selected.calls == before
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_orders").fetchall() == [
            ("PREPARED",)
        ]
        assert conn.execute("SELECT count(*) FROM v2_fills").fetchone() == (0,)


def test_frozen_recovery_precedes_disabled_deployment_and_revoked_route(fenced):
    selected = scene(fenced)
    result = selected.producer.prepare(selected.identity)
    route_update(fenced, "SET phase='DRAINING'")
    financial.publish(
        fenced.admin, replace(selected.deployment, mode="OFF", generation=2)
    )
    selected.calls.clear()
    replay = selected.producer.prepare(selected.identity)
    assert replay["status"] == "RECOVERY_REQUIRED"
    assert replay["candidate_digest"] == result["candidate_digest"]
    assert selected.calls == []


@pytest.mark.parametrize("trigger", ["stop", "route", "deployment", "error"])
def test_safety_read_cannot_escape_final_commit_fence(fenced, trigger):
    selected = scene(fenced)

    def execution(plan):
        if trigger == "stop":
            selected.producer.stop.set()
        elif trigger == "route":
            route_update(fenced, "SET phase='DRAINING'")
        elif trigger == "deployment":
            financial.publish(
                fenced.admin, replace(selected.deployment, mode="OFF", generation=2)
            )
        else:
            raise TimeoutError("synthetic bounded reader failure")
        return safety(plan)

    selected.producer.execution = execution
    with pytest.raises((ValueError, TimeoutError)):
        selected.producer.prepare(selected.identity)
    assert candidate(selected) is None
    with fenced.admin() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)


@pytest.mark.parametrize("mode", ["unknown", "no_setup", "no_budget"])
def test_non_candidates_do_not_invent_terminal_receipts(fenced, mode):
    selected = scene(
        fenced,
        builder_policy=policy(min_volume_ratio="100") if mode == "no_setup" else None,
    )
    if mode == "unknown":
        read = selected.producer.market.read

        def missing(*args, **kwargs):
            frame = read(*args, **kwargs)
            return replace(
                frame,
                bars_json=json.dumps(
                    [{**row, "v": "0", "tbv": "0"} for row in frame.bars]
                ),
            )

        selected.producer.market.read = missing
    if mode == "no_budget":
        account = selected.producer.account
        selected.producer.account = lambda *args: replace(
            account(*args), available_margin="0"
        )
    result = selected.producer.prepare(selected.identity)
    assert result["status"] == "DEFERRED"
    assert (
        result["observation"]
        == {"unknown": "UNKNOWN", "no_setup": "NO_SETUP", "no_budget": "NO_BUDGET"}[
            mode
        ]
    )
    assert selected.calls == (
        ["market", "account"] if mode == "no_budget" else ["market"]
    )
    assert candidate(selected) is None
    with fenced.admin() as conn:
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )


@pytest.mark.parametrize(
    "field,value", [("scope", None), ("symbol", "ETHUSDT"), ("quantity_step", ".01")]
)
def test_account_scope_and_catalog_filter_mismatch_cannot_freeze(fenced, field, value):
    from v2_core.account_risk import AccountScope

    selected = scene(fenced)
    account = selected.producer.account
    value = (
        AccountScope("BINANCE", "other", "SANDBOX", "FUTURES")
        if field == "scope"
        else value
    )
    selected.producer.account = lambda *args: replace(account(*args), **{field: value})
    with pytest.raises(ValueError, match="SCOPE_MISMATCH|CATALOG_RULE_MISMATCH"):
        selected.producer.prepare(selected.identity)
    assert candidate(selected) is None


def test_first_writer_during_safety_io_reuses_original(fenced):
    selected = scene(fenced)
    normal = selected.producer.execution

    def racing(plan):
        selected.producer.execution = normal
        other = selected.producer.prepare(selected.identity)
        assert other["status"] == "FROZEN"
        return safety(plan)

    selected.producer.execution = racing
    result = selected.producer.prepare(selected.identity)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["candidate_digest"] == candidate(selected).candidate_digest
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_entry_candidates"
        ).fetchone() == (1,)


def test_lost_freeze_ack_recovers_original_without_fact_reads(fenced):
    selected = scene(fenced)
    original = selected.producer.write

    @contextmanager
    def lost_ack():
        with original() as conn:
            yield conn
        # The initial probe writes nothing. Lose only the durable freeze ACK.
        if candidate(selected) is not None:
            raise ConnectionError("synthetic postcommit loss")

    selected.producer.write = lost_ack
    with pytest.raises(ConnectionError):
        selected.producer.prepare(selected.identity)
    selected.calls.clear()
    assert selected.producer.prepare(selected.identity)["status"] == "RECOVERY_REQUIRED"
    assert selected.calls == []


def test_freshness_deadline_covers_safety_reader_time(fenced):
    selected = scene(fenced)
    state = selected.producer._state
    elapsed = [False]

    def late(identity):
        result = state(identity)
        return replace(result, now_ms=result.now_ms + 31000) if elapsed[0] else result

    selected.producer._state = late

    def execution(plan):
        elapsed[0] = True
        return safety(plan)

    selected.producer.execution = execution
    result = selected.producer.prepare(selected.identity)
    assert result["reason"] == "STRATEGY_PRODUCER_FACTS_EXPIRED"
    assert candidate(selected) is None


def test_stopped_producer_does_not_open_database_or_fact_ports(fenced):
    selected = scene(fenced)
    selected.producer.stop.set()
    selected.producer.connect = lambda: pytest.fail("must not connect")
    assert (
        selected.producer.prepare(selected.identity)["reason"]
        == "STRATEGY_PRODUCER_STOPPED"
    )
    assert selected.calls == []


def test_expiry_after_transaction_lock_rolls_back_whole_freeze(fenced):
    from services.v2_execution_guard import create_execution_guard
    from services.v2_strategy_entry_job import entry_transaction
    from tests.v2_core.test_v2_strategy_prepare import at_database_time

    selected = scene(fenced)
    original = selected.producer._freeze

    with fenced.admin() as conn:
        original_evidence_count = conn.execute(
            "SELECT count(*) FROM v2_decision_evidence"
        ).fetchone()

    def delayed(proposal):
        selected.producer.write = entry_transaction(
            at_database_time(fenced.admin, proposal.expires_at_ms),
            binding=selected.binding,
            guard=create_execution_guard(fenced.admin, **fenced.args),
            stop=selected.producer.stop,
        )
        return original(proposal)

    selected.producer._freeze = delayed
    with pytest.raises(ValueError, match="EXPIRED_BEFORE_COMMIT"):
        selected.producer.prepare(selected.identity)
    assert candidate(selected) is None
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_entry_candidates"
        ).fetchone() == (0,)
        assert (
            conn.execute("SELECT count(*) FROM v2_decision_evidence").fetchone()
            == original_evidence_count
        )


def test_final_first_writer_race_recovers_even_after_new_deployment(fenced):
    selected = scene(fenced)
    original = selected.producer._freeze

    def race(proposal):
        original(proposal)
        financial.publish(
            fenced.admin, replace(selected.deployment, mode="OFF", generation=2)
        )
        return original(proposal)

    selected.producer._freeze = race
    result = selected.producer.prepare(selected.identity)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert candidate(selected).binding == selected.deployment


@pytest.mark.parametrize("stamp", ["book_at_ms", "observed_at_ms"])
def test_older_leverage_facts_bound_frozen_plan_expiry(fenced, stamp):
    selected = scene(fenced)
    account = selected.producer.account
    recorded = []

    def older(*args):
        facts = account(*args)
        proof = facts.leverage_proof
        # Never predate the current closed-minute proof anchor.
        proof["venue"][stamp] = args[2].closed_at_ms
        facts = replace(facts, leverage_proof_json=canonical(proof))
        recorded.append(facts)
        return facts

    selected.producer.account = older
    result = selected.producer.prepare(selected.identity)
    facts = recorded[0]
    age = 30000 if stamp == "book_at_ms" else 60000
    deadline = facts.leverage_proof["venue"][stamp] + age + 1
    if result["status"] == "FROZEN":
        assert candidate(selected).expires_at_ms <= deadline
    else:
        assert result["reason"] == "STRATEGY_PRODUCER_FACTS_EXPIRED"
        assert candidate(selected) is None


def test_actual_sizing_cross_source_dedup_preserves_single_prepared_order(fenced):
    selected = scene(fenced)
    assert selected.producer.prepare(selected.identity)["status"] == "FROZEN"
    first = candidate(selected)
    assert consume(fenced, selected)["signals"] == {selected.identity: "PREPARED"}
    second_id = Signals(fenced.admin).admit(
        source="s3",
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot=selected.snapshot,
    )
    selected.producer.source, selected.identity = "s3", second_id
    assert selected.producer.prepare(second_id)["status"] == "FROZEN"
    assert candidate(selected).plan.opportunity_id == first.plan.opportunity_id
    assert consume(fenced, selected)["signals"] == {second_id: "IGNORED"}
    with fenced.admin() as conn:
        assert conn.execute("SELECT status FROM v2_orders").fetchall() == [
            ("PREPARED",)
        ]
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)


@pytest.mark.parametrize("kind", ["expired", "future", "event_end"])
def test_source_boundary_never_collects_facts(fenced, kind):
    selected = scene(fenced)
    tick = now(fenced.admin)
    snapshot = dict(selected.snapshot)
    if kind == "expired":
        snapshot.update(observed_at=tick - 2000, expires_at_ms=tick - 1000)
    elif kind == "future":
        snapshot.update(observed_at=tick + 60000, expires_at_ms=tick + 120000)
    else:
        snapshot["signal"] = "EVENT_END"
    selected.identity = Signals(fenced.admin).admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key=str(uuid4()),
        snapshot=snapshot,
    )
    result = selected.producer.prepare(selected.identity)
    assert result["status"] == "DEFERRED"
    assert selected.calls == []
    assert candidate(selected) is None


@pytest.mark.parametrize("kind", ["missing", "expired", "corrupt"])
def test_catalog_failure_never_reads_market_or_account(fenced, kind):
    selected = scene(fenced)
    with fenced.admin() as conn:
        if kind == "missing":
            conn.execute("DELETE FROM v2_market_catalog WHERE environment='SANDBOX'")
        elif kind == "expired":
            conn.execute("UPDATE v2_market_catalog SET observed_at_ms=1")
        else:
            conn.execute("UPDATE v2_market_catalog SET digest=%s", ("0" * 64,))
    with pytest.raises(
        ValueError, match="CONTRACT_CATALOG_STALE|CONTRACT_CATALOG_CORRUPT"
    ):
        selected.producer.prepare(selected.identity)
    assert selected.calls == []
    assert candidate(selected) is None


def test_fact_io_holds_no_route_or_account_locks(fenced):
    from v2_core.account_risk import _lock

    selected = scene(fenced)
    read = selected.producer.market.read

    def probe(*args, **kwargs):
        route_update(fenced, "SET phase='DRAINING'")
        with fenced.admin() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            _lock(conn, financial.SCOPE.key)
        route_update(fenced, "SET phase='ACTIVE'")
        return read(*args, **kwargs)

    selected.producer.market.read = probe
    assert selected.producer.prepare(selected.identity)["status"] == "FROZEN"


def test_restricted_role_freeze_retains_route_lock_and_restores_business_role(fenced):
    import psycopg
    from psycopg import sql

    from services.v2_execution_guard import create_execution_guard

    selected = scene(fenced)
    with fenced.admin() as conn:
        tables = (
            "v2_inbound_signals",
            "v2_signal_receipts",
            "v2_strategy_decisions",
            "v2_trade_intents",
            "v2_decision_evidence",
            "v2_strategy_entry_candidates",
            "v2_business_state",
            "v2_state_history",
            "v2_market_catalog",
        )
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE ON {} TO {}").format(
                sql.SQL(",").join(map(sql.Identifier, tables)),
                sql.Identifier(fenced.worker),
            )
        )
    guard = create_execution_guard(fenced.restricted, **fenced.args)
    checks = []

    def checked(conn, **kwargs):
        phase = guard.require_transaction(conn, **kwargs)
        assert conn.execute("SELECT current_user").fetchone() == (fenced.worker,)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            route_update(fenced, "SET phase='DRAINING'")
        checks.append(phase)
        return phase

    producer = selected.producer
    selected.producer = CandidateProducer(
        fenced.restricted,
        binding=selected.binding,
        registry=financial.REGISTRY,
        instance_id=selected.deployment.instance_id,
        source="tv_bridge",
        market_reader=producer.market,
        account_reader=producer.account,
        execution_reader=producer.execution,
        stop=producer.stop,
        guard=SimpleNamespace(_args=guard._args, require_transaction=checked),
    )
    assert selected.producer.prepare(selected.identity)["status"] == "FROZEN"
    assert checks == ["ACTIVE", "ACTIVE"]
    assert candidate(selected) is not None
    route_update(fenced)
