"""Offline typed opening inputs and account controls; no database or venue I/O."""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_capital import SCOPE, material

from v2_core import entry_controls, liquidity_controls, liquidity_submission
from v2_core.evidence import canonical, digest
from v2_core.runtime_policy import PolicyStore
from v2_core.strategy_execution_context import (
    execution_context,
    read_execution_context,
    validate_execution_context,
)


def frozen(module, changes=None, *, version=None):
    values = module.resolve(changes or {})
    version = int(bool(changes)) if version is None else version
    return {
        "version": version,
        "values": values,
        "digest": digest(
            canonical({"scope": asdict(SCOPE), "version": version, "values": values})
        ),
    }


def scene(*, entry=None, liquidity=None, policy=None, market=False, side="BUY"):
    plan, record = material(
        plan_changes={
            "observed_at_ms": 1000,
            "decided_at_ms": 1001,
            "expires_at_ms": 10000,
            "side": side,
            "stop_price": "95" if side == "BUY" else "105",
        }
    )
    inputs = {
        "entry_controls": frozen(entry_controls, entry),
        "liquidity_controls": frozen(liquidity_controls, liquidity),
        "policy": {
            "entry.reentry_enabled": False,
            "entry.price_guard_enabled": False,
            "entry.max_adverse_price_fraction": "0.01",
            "entry.max_stop_risk_increase": "1.10",
            **(policy or {}),
        },
    }
    if market:
        inputs.update(
            liquidity_evidence={
                "version": "binance-depth-v1",
                "symbol": plan.symbol,
                "source_environment": "LIVE",
                "source_host": "fapi.binance.com",
                "observed_at_ms": 1000,
                "received_at_ms": 1001,
                "last_update_id": 1,
                "bids": [["99.99", "10"]],
                "asks": [["100.01", "10"]],
            },
            execution_quote={
                "symbol": plan.symbol,
                "environment": "SANDBOX",
                "source_host": "demo-fapi.binance.com",
                "book_at_ms": 1000,
                "spread_fraction": "0.0002",
            },
        )
    context = execution_context(plan, **inputs)
    record["snapshot"]["features"] = {"execution": context}
    return plan, record, context


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, query, parameters):
        self.calls.append((query, parameters))
        return SimpleNamespace(fetchone=lambda: self.row)


def connection(record, *, leg="OPEN", quantity=None):
    return Connection(
        (
            *record["scope"].values(),
            record["producer"],
            record["strategy_version"],
            record["config_digest"],
            record["payload"],
            record["evidence_strategy_version"],
            record["evidence_config_digest"],
            record["config"],
            record["snapshot"],
            leg,
            quantity or record["payload"]["quantity"],
        )
    )


def typed_controls(monkeypatch, plan, context):
    monkeypatch.setattr(
        "v2_core.strategy_execution_context.read_execution_context",
        lambda *_: (plan, deepcopy(context)),
    )
    for name, module in (
        ("entry_controls", entry_controls),
        ("liquidity_controls", liquidity_controls),
    ):
        current = module.from_snapshot(context[name], SCOPE)
        monkeypatch.setattr(module, "read", lambda *_, current=current: current)
    monkeypatch.setattr(
        PolicyStore, "read", lambda _: SimpleNamespace(values=context["policy"])
    )


def order(plan):
    return {
        "order_id": str(uuid4()),
        "symbol": plan.symbol,
        "side": plan.side,
        "quantity": plan.quantity,
    }


def test_committed_reader_binds_plan_order_and_scope_and_detaches_context():
    plan, record, original = scene(market=True)
    conn = connection(record)
    identity = str(uuid4())
    loaded, context = read_execution_context(conn, SCOPE, identity)
    assert loaded == plan
    assert context == original and "directional" not in context
    assert conn.calls[0][1] == (identity, *asdict(SCOPE).values())
    context["entry_controls"]["values"]["reentry.mode"] = "ENFORCE"
    context["liquidity_evidence"]["asks"][0][1] = "0"
    assert original["entry_controls"]["values"]["reentry.mode"] == "OFF"
    assert original["liquidity_evidence"]["asks"][0][1] == "10"


@pytest.mark.parametrize("marker", [None, {}, {"schema_version": "future-v2"}])
def test_present_bad_plan_marker_never_falls_back_to_legacy(marker):
    _, record, _ = scene()
    record["snapshot"]["strategy_plan"] = marker
    with pytest.raises(ValueError, match="STRATEGY_PLAN"):
        read_execution_context(connection(record), SCOPE, str(uuid4()))


def test_only_absent_plan_marker_is_legacy():
    _, record, _ = scene()
    del record["snapshot"]["strategy_plan"]
    conn = connection(record)
    assert read_execution_context(conn, SCOPE, str(uuid4())) is None
    query = conn.calls[0][0]
    for field in ("config", "snapshot"):
        assert (
            f"CASE WHEN e.snapshot ? 'strategy_plan' THEN e.{field} "
            "ELSE '{}'::jsonb END"
        ) in query


@pytest.mark.parametrize("features", [{}, {"execution": None}, {"execution": {}}])
def test_typed_context_mandatory_even_without_configured_controls(features):
    _, record, _ = scene()
    record["snapshot"]["features"] = features
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_CONTEXT"):
        read_execution_context(connection(record), SCOPE, str(uuid4()))


@pytest.mark.parametrize("leg,quantity", [("CLOSE", "2.5"), ("OPEN", "1")])
def test_reader_rejects_close_and_suballocation(leg, quantity):
    _, record, _ = scene()
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_ORDER_MISMATCH"):
        read_execution_context(
            connection(record, leg=leg, quantity=quantity), SCOPE, str(uuid4())
        )


def test_missing_order_is_not_legacy():
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_ORDER_MISSING"):
        read_execution_context(Connection(None), SCOPE, str(uuid4()))


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema", "other-schema"),
        ("symbol", "ETHUSDT"),
        ("account_scope", asdict(replace(SCOPE, account_id="other"))),
        ("plan_digest", "f" * 64),
        ("execution_authorized", True),
        ("directional", {"price": "100"}),
        ("entry_controls", None),
        ("liquidity_controls", {}),
        ("execution_quote", None),
    ],
)
def test_context_rejects_identity_and_schema_tampering(key, value):
    plan, _, context = scene()
    context[key] = value
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_CONTEXT_INVALID"):
        validate_execution_context(plan, context)


@pytest.mark.parametrize(
    "change",
    [
        {"entry.reentry_enabled": 1},
        {"entry.price_guard_enabled": "false"},
        {"entry.max_adverse_price_fraction": "0"},
        {"entry.max_adverse_price_fraction": "NaN"},
        {"entry.max_stop_risk_increase": "1.5001"},
        {"entry.max_stop_risk_increase": 1.1},
        {"capital.enabled": False},
    ],
)
def test_policy_accepts_only_exact_shared_opening_subset(change):
    plan, _, context = scene()
    context["policy"].update(change)
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_CONTEXT_INVALID"):
        validate_execution_context(plan, context)


def test_context_policy_cannot_omit_disabled_setting():
    plan, _, context = scene()
    del context["policy"]["entry.price_guard_enabled"]
    with pytest.raises(ValueError, match="STRATEGY_EXECUTION_CONTEXT_INVALID"):
        validate_execution_context(plan, context)


@pytest.mark.parametrize("name", ["entry_controls", "liquidity_controls"])
def test_nonzero_changed_control_version_always_rejects(monkeypatch, name):
    plan, _, context = scene()
    typed_controls(monkeypatch, plan, context)
    module = entry_controls if name == "entry_controls" else liquidity_controls
    changed = module.from_snapshot(frozen(module, version=1), SCOPE)
    monkeypatch.setattr(module, "read", lambda *_: changed)
    with pytest.raises(ValueError, match="CONTROLS_CHANGED_REPROPOSE"):
        module.validate_submission(None, SCOPE, str(uuid4()))


@pytest.mark.parametrize("module", [entry_controls, liquidity_controls])
def test_missing_order_keeps_existing_control_error(monkeypatch, module):
    current = module.from_snapshot(frozen(module), SCOPE)
    monkeypatch.setattr(module, "read", lambda *_: current)
    with pytest.raises(ValueError, match="CONTROLS_ORDER_MISSING"):
        module.validate_submission(Connection(None), SCOPE, str(uuid4()))


@pytest.mark.parametrize(
    "changes",
    [
        {"sizing.remaining_budget_enabled": True},
        {"history.mode": "SOFT"},
        {"reentry.mode": "ENFORCE"},
        {
            "reentry.mode": "ENFORCE",
            "reentry.gate_scope": "TRADE_CYCLE",
            "reentry.enforce_sides": ["BUY", "SELL"],
        },
    ],
)
def test_equal_snapshots_do_not_bypass_unsupported_active_controls(
    monkeypatch, changes
):
    plan, _, context = scene(entry=changes)
    typed_controls(monkeypatch, plan, context)
    with pytest.raises(ValueError, match="STRATEGY_ENTRY_CONTROLS_UNSUPPORTED"):
        entry_controls.validate_submission(None, SCOPE, str(uuid4()), now_ms=1002)


@pytest.mark.parametrize(
    "frozen_enabled,current_enabled", [(True, False), (False, True)]
)
def test_either_shared_reentry_policy_blocks_typed_open(
    monkeypatch, frozen_enabled, current_enabled
):
    plan, _, context = scene(policy={"entry.reentry_enabled": frozen_enabled})
    typed_controls(monkeypatch, plan, context)
    monkeypatch.setattr(
        PolicyStore,
        "read",
        lambda _: SimpleNamespace(values={"entry.reentry_enabled": current_enabled}),
    )
    with pytest.raises(ValueError, match="STRATEGY_REENTRY_POLICY_UNSUPPORTED"):
        entry_controls.validate_submission(None, SCOPE, str(uuid4()))


def test_shadow_entry_controls_preserve_no_veto_semantics(monkeypatch):
    plan, _, context = scene(entry={"history.mode": "SHADOW", "reentry.mode": "SHADOW"})
    typed_controls(monkeypatch, plan, context)
    assert entry_controls.validate_submission(None, SCOPE, str(uuid4())) is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_typed_liquidity_uses_plan_price_and_normalized_quote(monkeypatch, side):
    plan, _, context = scene(liquidity={"mode": "ENFORCE"}, market=True, side=side)
    typed_controls(monkeypatch, plan, context)
    assert (
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, order(plan), now_ms=1002
        )
        == 6000
    )


@pytest.mark.parametrize(
    "section,key,value,reason",
    [
        (
            "liquidity_evidence",
            "source_environment",
            "SANDBOX",
            "SOURCE_ENVIRONMENT_INVALID",
        ),
        ("liquidity_evidence", "source_host", "other.test", "SOURCE_HOST_INVALID"),
        ("liquidity_evidence", "observed_at_ms", 1002, "POST_DECISION_EVIDENCE"),
        ("liquidity_evidence", "received_at_ms", 1002, "POST_DECISION_EVIDENCE"),
        ("liquidity_evidence", "symbol", "ETHUSDT", "SYMBOL_MISMATCH"),
        ("execution_quote", "environment", "LIVE", "SOURCE_ENVIRONMENT_INVALID"),
        ("execution_quote", "source_host", "fapi.binance.com", "SOURCE_HOST_INVALID"),
        ("execution_quote", "book_at_ms", 1002, "POST_DECISION_EVIDENCE"),
    ],
)
def test_typed_liquidity_rejects_wrong_venue_and_post_decision_evidence(
    monkeypatch, section, key, value, reason
):
    plan, _, context = scene(liquidity={"mode": "ENFORCE"}, market=True)
    context[section][key] = value
    typed_controls(monkeypatch, plan, context)
    with pytest.raises(ValueError, match=reason):
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, order(plan), now_ms=1003
        )


@pytest.mark.parametrize(
    "missing,reason",
    [
        ("liquidity_evidence", "LIQUIDITY_EVIDENCE_MISSING"),
        ("execution_quote", "LIQUIDITY_EXECUTION_EVIDENCE_MISSING"),
    ],
)
def test_typed_liquidity_cannot_use_missing_normalized_evidence(
    monkeypatch, missing, reason
):
    plan, _, context = scene(liquidity={"mode": "ENFORCE"}, market=True)
    del context[missing]
    typed_controls(monkeypatch, plan, context)
    with pytest.raises(ValueError, match=reason):
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, order(plan), now_ms=1002
        )


@pytest.mark.parametrize(
    "quote_age,now,reason",
    [(30000, 6000, "LIQUIDITY_STALE"), (100, 1100, "LIQUIDITY_EXECUTION_STALE")],
)
def test_frozen_liquidity_expiry_is_strict_for_both_sources(
    monkeypatch, quote_age, now, reason
):
    plan, _, context = scene(
        liquidity={"mode": "ENFORCE", "max_execution_quote_age_ms": quote_age},
        market=True,
    )
    typed_controls(monkeypatch, plan, context)
    with pytest.raises(ValueError, match=reason):
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, order(plan), now_ms=now
        )


def test_typed_liquidity_recomputes_for_submitted_quantity(monkeypatch):
    plan, _, context = scene(liquidity={"mode": "ENFORCE"}, market=True)
    typed_controls(monkeypatch, plan, context)
    with pytest.raises(ValueError, match="LIQUIDITY_INSUFFICIENT_DEPTH"):
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, {**order(plan), "quantity": "20"}, now_ms=1002
        )


def test_shadow_liquidity_never_adds_a_live_veto(monkeypatch):
    plan, _, context = scene(liquidity={"mode": "SHADOW"})
    typed_controls(monkeypatch, plan, context)
    assert (
        liquidity_submission.validate_opening_liquidity(
            None, SCOPE, order(plan), now_ms=1002
        )
        is None
    )
