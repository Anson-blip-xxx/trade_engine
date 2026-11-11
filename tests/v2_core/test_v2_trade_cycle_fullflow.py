"""Trade-cycle vetoes are terminal candidate decisions, not queue failures.

Real providers, PostgreSQL receipts and guarded execution run only against the
explicit disposable QA fixture. All venue ports and candles are synthetic; no
test can contact Binance or infer real-world profitability from these cases.
"""

import ast
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_v2_entry_upgrade_fullflow as existing
import test_v2_trading_pipeline as pipeline_fixture
from test_v2_directional_exit_stage import setup_exit
from test_v2_directional_replay import SCOPE
from test_v2_layered_history import row as history_row
from test_v2_layered_history import seed as seed_history

from services.v2_entry_research_context import EntryResearchContext
from services.v2_entry_structure import EntryStructureContext
from services.v2_trading_pipeline import failed
from tests.support.v2_admission import database
from v2_core.capital_model import rehearsal_profile
from v2_core.entry_controls import configure
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import StrategyScheduler

__all__ = ["database"]
NOW, CUTOFF = existing.NOW, existing.CUTOFF
CYCLE = {
    "reentry.gate_scope": "TRADE_CYCLE",
    "reentry.enforce_sides": ["BUY", "SELL"],
    "reentry.cycle_policy": {"confirmation_bars": 2, "max_trigger_age_bars": 0},
}


def build_cycle(
    database, monkeypatch, *, mode="ENFORCE", seeded=True, shape="flat", strategy="S6"
):
    """Extend an existing real-PG fixture, never fabricate a sealed assessment."""
    captured = {}
    original_configure = existing.configure
    original_structure = EntryStructureContext
    original_inputs = existing.inputs
    original_replay = existing.create_directional_replay_worker
    original_admission = existing.create_directional_admission_worker
    original_plan = existing.market_plan

    def configured(connect, scope, changes, **kwargs):
        return original_configure(connect, scope, {**changes, **CYCLE}, **kwargs)

    def previous_buy_plan(signal, context, config):
        # Existing seed_history creates a BUY episode. Even for a new SELL
        # candidate, keep that previous episode's recorded plan internally long.
        prior_signal = deepcopy(signal)
        prior_signal["signal"] = "TREND_UP"
        prior_signal["features"]["type"] = "TREND_UP"
        return original_plan(prior_signal, context, {**config, "strategy": "S6"})

    def seeded_history(connect, sample):
        sample = deepcopy(sample)
        sample.update(
            closed_at_ms=CUTOFF - 180000,
            latest_fill_ms=CUTOFF - 180000,
            latest_fill_known_at_ms=CUTOFF - 180000,
        )
        # The fixture's settlement and outcome use the same positive actual net.
        # This is a synthetic profit exit, not a test of outcome attribution.
        sample["net_pnl"] = history_row(net="2")["net_pnl"]
        sample["settlement_evidence"]["net_pnl"] = "2"
        seed_history(connect, sample)

    def structure(connect, delegate, **kwargs):
        calls = []

        def read_bars(target, start, end):
            calls.append((target, start, end))
            if shape == "unavailable":
                raise TimeoutError("synthetic private archive failure")
            bars = []
            for opened in range(start, end, 60000):
                bar = {
                    "t": opened,
                    "o": "100",
                    "h": "101",
                    "l": "99",
                    "c": "100",
                    "v": "10",
                }
                if shape in {"confirmed", "single_confirmation"}:
                    bar.update(o="98", h="99", l="97", c="98")
                    if shape == "confirmed":
                        # Trigger plus subsequent closes stay outside the
                        # breakout boundary without excessive extension.
                        if opened == CUTOFF - 180000:
                            bar.update(o="98", h="100.5", l="98", c="100", v="20")
                        elif opened == CUTOFF - 120000:
                            bar.update(o="100", h="100.5", l="99.6", c="100.1", v="20")
                        elif opened == CUTOFF - 60000:
                            bar.update(o="100.1", h="100.4", l="99.6", c="100", v="20")
                    elif opened == CUTOFF - 60000:
                        bar.update(o="98", h="100.5", l="98", c="100", v="20")
                    if strategy == "S8":
                        bar.update(
                            o=str(Decimal(200) - Decimal(bar["o"])),
                            h=str(Decimal(200) - Decimal(bar["l"])),
                            l=str(Decimal(200) - Decimal(bar["h"])),
                            c=str(Decimal(200) - Decimal(bar["c"])),
                        )
                bars.append(bar)
            if shape == "gap":
                bars.pop(len(bars) // 2)
            return list(reversed(bars))

        def symbol_context(signal):
            context = delegate(signal)
            context["symbol"] = signal["symbol"]
            return context

        instance = original_structure(
            connect,
            symbol_context,
            **{**kwargs, "read_bars": read_bars, "strategy": strategy},
        )
        captured["provider"] = EntryResearchContext(
            connect,
            instance,
            scope=SCOPE,
            strategy=strategy,
            source="s3",
            clock_ms=lambda: NOW,
        )
        captured["archive_calls"] = calls
        return instance

    monkeypatch.setattr(existing, "configure", configured)
    monkeypatch.setattr(existing, "seed_history", seeded_history)
    monkeypatch.setattr(existing, "EntryStructureContext", structure)
    monkeypatch.setattr(existing, "inputs", lambda: original_inputs(strategy))
    monkeypatch.setattr(
        existing,
        "create_directional_replay_worker",
        lambda *args, **kwargs: original_replay(
            *args, **{**kwargs, "strategy": strategy}
        ),
    )
    monkeypatch.setattr(
        existing,
        "create_directional_admission_worker",
        lambda *args, **kwargs: original_admission(
            *args, **{**kwargs, "strategy": strategy}
        ),
    )
    monkeypatch.setattr(
        existing,
        "EntryResearchContext",
        lambda *args, **kwargs: EntryResearchContext(
            *args, **{**kwargs, "strategy": strategy}
        ),
    )
    monkeypatch.setattr(existing, "market_plan", previous_buy_plan)
    values = existing.build(database, mode=mode, seeded=seeded)
    return (*values, captured)


def assert_consumed_rejection(database, worker, signal_id):
    saved = worker.decision(signal_id)
    assert saved["action"] == "IGNORED"
    assert saved["snapshot"]["rationale"].startswith("REENTRY_")
    with database() as conn:
        receipt = conn.execute(
            "SELECT outcome,intent_id FROM v2_signal_receipts "
            "WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone()
        assert receipt == ("IGNORED", None)
        task = conn.execute(
            "SELECT completed_at IS NOT NULL,lease_token,lease_until,error_code "
            "FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone()
        assert task == (True, None, None, None)
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s", (signal_id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_orders o JOIN v2_trade_intents i "
            "ON i.intent_id=o.episode_id WHERE i.signal_id=%s",
            (signal_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "shape,status",
    [
        ("flat", "WAIT"),
        ("single_confirmation", "WAIT"),
        ("gap", "UNKNOWN"),
        ("unavailable", "UNKNOWN"),
    ],
)
def test_nonready_cycle_finishes_receipt_and_task_without_order(
    database, monkeypatch, shape, status
):
    _, _, worker, signal_id, context, _, _, captured = build_cycle(
        database, monkeypatch, shape=shape
    )
    proof = context["entry_structure"]
    assert proof["gate_scope"] == "TRADE_CYCLE"
    assert proof["cycle_policy"] == CYCLE["reentry.cycle_policy"]
    assert proof["assessment"]["status"] == status
    if shape == "single_confirmation":
        assert proof["assessment"]["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
        assert proof["assessment"]["confirmation_bars_observed"] == 1
    scheduler = StrategyScheduler(worker, context_provider=captured["provider"])
    outcome = scheduler.run_once(limit=1)
    assert outcome == {signal_id: "IGNORED"}
    assert not failed(outcome)
    assert_consumed_rejection(database, worker, signal_id)
    frozen = deepcopy(worker.decision(signal_id))
    reads = len(captured["archive_calls"])
    assert scheduler.run_once(limit=1) == {}
    assert len(captured["archive_calls"]) == reads
    assert worker.decision(signal_id) == frozen


@pytest.mark.parametrize("shape", ["flat", "unavailable"])
def test_cycle_rejection_does_not_starve_other_symbol_in_same_batch(
    database, monkeypatch, shape
):
    runtime, _, worker, first, _, _, _, captured = build_cycle(
        database, monkeypatch, shape=shape
    )
    with database() as conn:
        signal = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (first,)
        ).fetchone()[0]
    signal["symbol"] = "ETHUSDT"
    second = runtime.data.signals.admit(
        source="s3",
        environment="SANDBOX",
        request_key="cycle-other-symbol",
        snapshot=signal,
    )
    scheduler = StrategyScheduler(worker, context_provider=captured["provider"])
    result = scheduler.run_once(limit=2)
    assert result == {first: "IGNORED", second: "PREPARED"}
    assert_consumed_rejection(database, worker, first)
    accepted = worker.decision(second)
    assert accepted["action"] == "OPEN"
    assert accepted["snapshot"]["features"]["evaluation"]["entry_structure"][
        "assessment"
    ] == {"status": "NOT_APPLICABLE", "reason": "NO_PREVIOUS_FILLED_EPISODE"}
    assert scheduler.run_once(limit=2) == {}
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders o JOIN v2_trade_intents i "
            "ON i.intent_id=o.episode_id WHERE i.signal_id=%s",
            (second,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("shape", ["flat", "gap", "unavailable"])
def test_shadow_cycle_never_vetoes_nonready_candidate(database, monkeypatch, shape):
    _, _, worker, signal_id, context, _, _, _ = build_cycle(
        database, monkeypatch, mode="SHADOW", shape=shape
    )
    assert context["entry_structure"]["assessment"]["status"] in {"WAIT", "UNKNOWN"}
    prepared = worker.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    status, sent, proof = existing.fake_dispatch(
        database, worker, signal_id, prepared["order_id"]
    )
    assert status == "ACKNOWLEDGED" and len(sent) == 1
    assert proof["status"] == "READY_FOR_GUARDED_SUBMISSION"


@pytest.mark.parametrize(
    "seeded,shape,status",
    [(False, "flat", "NOT_APPLICABLE"), (True, "confirmed", "READY")],
)
def test_new_or_confirmed_cycle_reaches_guarded_fake_dispatch(
    database, monkeypatch, seeded, shape, status
):
    _, _, worker, signal_id, context, _, _, _ = build_cycle(
        database, monkeypatch, seeded=seeded, shape=shape
    )
    assert context["entry_structure"]["assessment"]["status"] == status, context[
        "entry_structure"
    ]["assessment"]
    prepared = worker.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    result, sent, proof = existing.fake_dispatch(
        database, worker, signal_id, prepared["order_id"]
    )
    assert result == "ACKNOWLEDGED" and len(sent) == 1
    assert proof["blockers"] == []
    assert proof["execution_authorized"] is False


def test_changing_cycle_controls_fences_prepared_opening(database, monkeypatch):
    _, _, worker, signal_id, context, controls, _, _ = build_cycle(
        database, monkeypatch, seeded=False
    )
    prepared = worker.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    configure(
        database,
        SCOPE,
        {"reentry.cycle_policy": {"confirmation_bars": 3, "max_trigger_age_bars": 0}},
        expected_version=controls.version,
        reason="QA cycle policy changed after preparation",
    )
    status, sent, proof = existing.fake_dispatch(
        database, worker, signal_id, prepared["order_id"]
    )
    assert status == "REJECTED" and sent == []
    assert "ENTRY_CONTROLS_CHANGED_REPROPOSE" in proof["blockers"]


def test_new_settled_anchor_fences_prepared_opening(database, monkeypatch):
    _, _, worker, signal_id, context, _, _, _ = build_cycle(
        database, monkeypatch, seeded=False
    )
    prepared = worker.consume(signal_id, context=context)
    assert prepared["status"] == "PREPARED"
    # A new settled episode can arrive while an old proposal is waiting to send.
    sample = history_row(scope=SCOPE, net="2")
    sample.update(
        closed_at_ms=CUTOFF - 60000,
        latest_fill_ms=CUTOFF - 60000,
        latest_fill_known_at_ms=CUTOFF - 60000,
        close_reason="PEAK_DRAWDOWN",
    )
    seed_history(database, sample)
    status, sent, proof = existing.fake_dispatch(
        database, worker, signal_id, prepared["order_id"]
    )
    assert status == "REJECTED" and sent == []
    assert "ENTRY_STRUCTURE_ANCHOR_CHANGED_REPROPOSE" in proof["blockers"]


def test_slow_final_anchor_query_cannot_hold_submit_lock_or_send_order(
    database, monkeypatch
):
    import v2_core.entry_history as structure

    _, _, worker, signal_id, context, _, _, _ = build_cycle(
        database, monkeypatch, seeded=False
    )
    prepared = worker.consume(signal_id, context=context)
    observed = []

    def slow_anchor(conn, *_):
        observed.append(
            (
                conn.execute("SHOW statement_timeout").fetchone()[0],
                conn.execute("SHOW lock_timeout").fetchone()[0],
            )
        )
        # Only the disposable QA server is reachable here; its 500ms limit must
        # cancel this query and roll back the savepoint, never send an order.
        conn.execute("SELECT pg_sleep(5)")
        pytest.fail("anchor read was not time bounded")

    monkeypatch.setattr(structure, "prior_close", slow_anchor)
    status, sent, proof = existing.fake_dispatch(
        database, worker, signal_id, prepared["order_id"]
    )
    assert observed == [("500ms", "100ms")]
    assert status == "REJECTED" and sent == []
    assert "ENTRY_STRUCTURE_ANCHOR_UNAVAILABLE" in proof["blockers"]
    # A persisted readiness proof also proves the timeout did not abort the
    # caller's outer transaction or poison unrelated lifecycle work.
    assert proof["status"] == "BLOCKED"


@pytest.mark.parametrize("shape,expected", [("flat", "WAIT"), ("confirmed", "READY")])
def test_buy_exit_to_sell_entry_requires_confirmed_reverse_structure(
    database, monkeypatch, shape, expected
):
    _, _, worker, signal_id, context, _, _, captured = build_cycle(
        database, monkeypatch, strategy="S8", shape=shape
    )
    proof = context["entry_structure"]
    assert proof["side"] == "SELL" and proof["previous_exit"]["side"] == "BUY"
    assert proof["assessment"]["status"] == expected, proof["assessment"]
    scheduler = StrategyScheduler(worker, context_provider=captured["provider"])
    outcome = scheduler.run_once(limit=1)
    if expected == "WAIT":
        assert outcome == {signal_id: "IGNORED"}
        assert_consumed_rejection(database, worker, signal_id)
    else:
        assert proof["assessment"]["path"] == "REVERSAL_BREAK_CONFIRM"
        assert outcome == {signal_id: "PREPARED"}
        prepared = worker.consume(signal_id, context=context)
        result, sent, readiness = existing.fake_dispatch(
            database, worker, signal_id, prepared["order_id"]
        )
        assert result == "ACKNOWLEDGED" and len(sent) == 1
        assert sent[0]["side"] == "SELL"
        assert readiness["blockers"] == []


def test_every_source_factory_wraps_cycle_gate_including_quant():
    """Static wiring check avoids importing the credentials-bearing entrypoint."""
    source = (
        Path(__file__).resolve().parents[2] / "services/v2_testnet_daemon_entry.py"
    ).read_text()
    tree = ast.parse(source)
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_testnet_process"
    )
    loop = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Tuple)
        and [getattr(item, "id", None) for item in node.target.elts]
        == ["strategy", "source"]
    )
    # Top-level assignments in the source loop must execute for s3, TV AND quant.
    calls = [
        node.value
        for node in loop.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
    ]
    names = [getattr(call.func, "id", None) for call in calls]
    assert names.count("EntryStructureContext") == 1
    assert names.index("StructuralReentryContext") < names.index(
        "EntryStructureContext"
    )
    assert names.index("EntryStructureContext") < names.index("LiquidityContext")
    call = calls[names.index("EntryStructureContext")]
    strategy = next(kw.value for kw in call.keywords if kw.arg == "strategy")
    assert isinstance(strategy, ast.Name) and strategy.id == "strategy"


def test_cycle_veto_keeps_lifecycle_stages_running_each_cycle(database, monkeypatch):
    _, _, _, _, _, _, _, captured = build_cycle(database, monkeypatch)
    monkeypatch.setattr(
        pipeline_fixture, "time", SimpleNamespace(time_ns=lambda: NOW * 1000000)
    )
    pipeline, calls, _, _, _ = pipeline_fixture.case.__wrapped__(database)
    pipeline.schedulers[0].context_provider = captured["provider"]
    for _ in range(2):
        calls.clear()
        result = pipeline.run_once()
        assert result["status"] == "CYCLE_COMPLETE", result
        assert calls[:4] == ["protection", "exits", "settlement", "followups"]
        with database() as conn:
            signals = {
                row[0]
                for row in conn.execute(
                    "SELECT signal_id::text FROM v2_inbound_signals WHERE source='s3'"
                ).fetchall()
            }
        phase = result["phases"].get("s6", {})
        # Pipeline appends progress diagnostics to the per-signal outcomes.
        assert all(
            value == "IGNORED" for key, value in phase.items() if key in signals
        ), phase
        if "progress" in phase:
            assert phase["progress"]["incomplete"] == 0
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
def test_full_cycle_entry_controls_cannot_intercept_reduceonly_exit(
    database, monkeypatch, strategy
):
    import v2_core.entry_controls as controls_module
    import v2_core.entry_opportunities as opportunity_module

    runtime, opened, venue, stage = setup_exit(
        database,
        strategy=strategy,
        allow_writes=True,
        now=121 * 60000 + 3 if strategy == "S6" else 600003,
        **(
            {"mark_price": "99"}
            if strategy == "S6"
            else {"mark_price": "100", "funding_rate": "-.006"}
        ),
    )
    policy = PolicyStore(database, runtime.scope)
    previous = policy.read()
    policy.patch(
        rehearsal_profile("1000"),
        expected_version=previous.version,
        reason="QA capital guards",
    )
    configure(
        database,
        runtime.scope,
        {**CYCLE, "reentry.mode": "ENFORCE"},
        expected_version=0,
        reason="QA full trade cycle enabled",
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("an entry-only control must never block a reduce-only exit")

    monkeypatch.setattr(controls_module, "read", forbidden)
    monkeypatch.setattr(controls_module, "validate_submission", forbidden)
    monkeypatch.setattr(opportunity_module, "validate", forbidden)
    outcome = stage.run_once()["exits"][opened["intent_id"]]
    assert outcome["status"] == "DISPATCHED", outcome
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == ("SELL" if strategy == "S6" else "BUY")
