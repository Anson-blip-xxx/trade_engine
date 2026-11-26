"""Typed trade messages from real isolated journals, never real Telegram."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_account_inventory import TelegramConnection, sink
from tests.v2_core.test_v2_strategy_settlement import SCOPE, closed_plan
from v2_core.external_close_recovery import ExternalCloseRecovery
from v2_core.ledger import Ledger
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_lifecycle import STRATEGY_OUTCOME_SCHEMA
from v2_core.trade_notifications import (
    TradeLifecycleNotifications,
    _require_typed_opening_protection,
)

database = database_fixture


def ready(
    database, *, side="BUY", outcome=True, partial=False, protection_status="EXPIRED"
):
    case = closed_plan(database, side=side, partial=partial)
    result = case.stage.settle(case.episode, record_outcome=outcome)
    assert result["status"] == "SETTLED", result
    trace = case.data.trace(case.episode)
    opening = next(row for row in trace["orders"] if row["leg"] == "OPEN")
    store = BusinessState(database)
    key = StateKey(
        **asdict(SCOPE), namespace="opening-readiness-v1", key=opening["order_id"]
    )
    saved = store.read(key)
    payload = json.loads(saved.payload_json)
    payload.update(
        account_scope=asdict(SCOPE),
        symbol=case.plan.symbol,
        quantity=case.plan.quantity,
        leverage=case.plan.leverage,
        margin_type=case.plan.margin_mode,
        required_margin=".55",
    )
    assert (
        store.change(
            key,
            expected_version=saved.version,
            request_key="notification-proof",
            payload=payload,
            reason="QA_NOTIFICATION_PROOF",
        ).code
        == "APPLIED"
    )
    protected = StateKey(
        **asdict(SCOPE),
        namespace="testnet-protection-v1",
        key="typed-notification-proof",
    )
    assert (
        store.change(
            protected,
            expected_version=0,
            request_key="fixture",
            payload={
                "spec": {
                    "episode": case.episode,
                    "symbol": case.plan.symbol,
                    "side": case.plan.exit_side,
                    "kind": "STOP_MARKET",
                    "trigger_price": case.plan.stop_price,
                },
                "status": protection_status,
                "observation": {"algoId": 77},
            },
            reason="QA_CONFIRMED_PROTECTION",
        ).code
        == "APPLIED"
    )
    case.readiness, case.protected = key, protected
    return case


def texts(connection):
    return [
        json.loads(call[1]["body"])["text"]
        for call in connection.calls
        if call[0][1].endswith("/sendMessage")
    ]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("outcome", [False, True])
def test_typed_open_and_close_are_verified_without_legacy_scores_or_outcomes(
    database, side, outcome
):
    case = ready(database, side=side, outcome=outcome)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    closing = notify._closing(case.episode)
    assert Decimal(closing["net_pnl"]) == case.net
    assert Decimal(closing["return_pct"]) == case.net * 100
    assert closing["direction"] == ("LONG" if side == "BUY" else "SHORT")
    assert notify.run_once() == {
        "claimed": 2,
        "delivered": 2,
        "failed": 0,
        "superseded": 0,
    }
    messages = texts(connection)
    assert len(messages) == 2
    assert any("开仓成功" in text for text in messages)
    assert any("平仓盈利" in text for text in messages)
    assert "历史开仓补发：该订单已平仓结算" in messages[0]
    for text in messages:
        assert "无传统评分" in text and "评分：99" not in text
        assert "structure / v1" in text and "UTC+8" in text
        assert case.episode not in text and case.plan.plan_digest not in text
        assert "S6" not in text and "S8" not in text
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_directional_outcomes"
        ).fetchone() == (0,)
    state = notify.store.read(
        StateKey(**asdict(SCOPE), namespace=STRATEGY_OUTCOME_SCHEMA, key=case.episode)
    )
    assert (
        state is not None
    ) is outcome  # Message projection never creates research rows.
    assert notify.run_once()["claimed"] == 0
    assert len(connection.calls) == 2


def test_typed_notifications_preserve_send_receipt_and_pin_retry_idempotency(database):
    case = ready(database)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(
        database, sink(connection), scope=SCOPE, pin_messages=True
    )
    event = str(uuid4())
    assert notify._deliver(event, case.episode, "ORDER_STATE:4", {}) is True
    assert len(texts(connection)) == 1
    # Simulate a confirmed Telegram send followed by a lost projector ACK.
    restarted = TradeLifecycleNotifications(
        database, sink(connection), scope=SCOPE, pin_messages=True
    )
    assert restarted._deliver(event, case.episode, "ORDER_STATE:4", {}) is True
    assert len(texts(connection)) == 1
    connection.raw = b'{"ok":true,"result":true}'
    restarted._retry_pins(1)
    saved = restarted.store.read(restarted._pin_key(event))
    assert json.loads(saved.payload_json)["status"] == "PINNED"
    assert len(connection.calls) == 2
    restarted._retry_pins(1)
    assert len(connection.calls) == 2


def test_partially_filled_cancelled_opening_reports_actual_settled_quantity(database):
    case = ready(database, partial=True)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    closing = notify._closing(case.episode)
    assert Decimal(closing["quantity"]) == Decimal(".005")
    assert Decimal(closing["opening_notional"]) == Decimal(".5")
    assert Decimal(closing["net_pnl"]) == case.net
    assert Decimal(closing["return_pct"]) == case.net / Decimal(".5") * 100
    assert notify.run_once() == {
        "claimed": 1,
        "delivered": 1,
        "failed": 0,
        "superseded": 0,
    }
    assert len(texts(connection)) == 1
    assert "平仓盈利" in texts(connection)[0]


@pytest.mark.parametrize("defect", ["quantity", "marker", "scope"])
def test_invalid_typed_trace_cannot_fall_back_to_directional_message(
    database, monkeypatch, defect
):
    case = ready(database)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    original = notify.data.trace

    def corrupt(episode):
        trace = deepcopy(original(episode))
        if defect == "quantity":
            trace["decision"]["strategy_plan"]["quantity"] = "999"
        elif defect == "marker":
            trace["decision"]["strategy_plan"] = None
        else:
            trace["scope"]["account_id"] = "foreign-account"
        return trace

    monkeypatch.setattr(notify.data, "trace", corrupt)
    with pytest.raises(ValueError):
        notify._deliver(str(uuid4()), case.episode, "ORDER_STATE:4", {})
    assert connection.calls == []


def test_foreign_account_cannot_project_typed_episode(database):
    case = ready(database)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(
        database, sink(connection), scope=replace(SCOPE, account_id="foreign")
    )
    assert notify.run_once()["claimed"] == 0
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        notify._closing(case.episode)
    assert not connection.calls


def test_optional_outcome_corruption_is_not_ignored_or_used_as_pnl(database):
    case = ready(database)
    store = BusinessState(database)
    key = StateKey(**asdict(SCOPE), namespace=STRATEGY_OUTCOME_SCHEMA, key=case.episode)
    saved = store.read(key)
    payload = json.loads(saved.payload_json)
    payload["net_pnl"] = "999999"
    assert (
        store.change(
            key,
            expected_version=saved.version,
            request_key="corrupt-outcome",
            payload=payload,
            reason="QA_CORRUPT_OUTCOME",
        ).code
        == "APPLIED"
    )
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    with pytest.raises(ValueError, match="OUTCOME_MISMATCH"):
        notify._deliver(str(uuid4()), case.episode, "SETTLED:1", {})
    assert not connection.calls


def test_settlement_and_current_ledger_net_must_agree(database, monkeypatch):
    case = ready(database, outcome=False)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    original = Ledger.report

    def changed(self, *args, **kwargs):
        return {**original(self, *args, **kwargs), "net_pnl": "999999"}

    monkeypatch.setattr(Ledger, "report", changed)
    with pytest.raises(ValueError, match="SETTLEMENT_MISMATCH"):
        notify._closing(case.episode)
    assert not connection.calls


@pytest.mark.parametrize(
    "field,value", [("leverage", 8), ("account_scope", {}), ("status", "UNKNOWN")]
)
def test_readiness_and_protection_are_explicitly_bound(database, field, value):
    case = ready(database)
    store = BusinessState(database)
    key = case.protected if field == "status" else case.readiness
    saved = store.read(key)
    payload = json.loads(saved.payload_json)
    payload[field] = value
    store.change(
        key,
        expected_version=saved.version,
        request_key="bad-bound-proof",
        payload=payload,
        reason="QA_NOTIFICATION_PROOF_MISMATCH",
    )
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    with pytest.raises(ValueError, match="READINESS_MISMATCH|EVIDENCE_PENDING"):
        notify._opening(case.episode)
    assert not connection.calls


@pytest.mark.parametrize(
    "status", ["CANCELED", "EXPIRED", "FINISHED", "TRIGGERED", "TRIGGERING", "UNKNOWN"]
)
def test_pure_protection_gate_never_calls_terminal_parent_live_protection(status):
    trace = {
        "orders": [{"order_id": "o", "leg": "OPEN"}],
        "fills": [{"order_id": "o", "quantity": ".01"}],
        "episode": {"status": "ACTIVE"},
    }
    with pytest.raises(ValueError, match="PROTECTION_PENDING"):
        _require_typed_opening_protection(trace, {"status": status})
    _require_typed_opening_protection(trace, {"status": "NEW"})


def test_pure_protection_gate_flat_unsettled_is_not_historical_success():
    trace = {
        "orders": [{"order_id": "o", "leg": "OPEN"}, {"order_id": "c", "leg": "CLOSE"}],
        "fills": [
            {"order_id": "o", "quantity": ".01"},
            {"order_id": "c", "quantity": ".01"},
        ],
        "episode": {"status": "ACTIVE"},
    }
    with pytest.raises(ValueError, match="PROTECTION_PENDING"):
        _require_typed_opening_protection(trace, {"status": "EXPIRED"})
    trace["episode"]["status"] = "SETTLED"
    _require_typed_opening_protection(trace, {"status": "EXPIRED"})


@pytest.mark.parametrize(
    "status", ["NEW", "CANCELED", "EXPIRED", "FINISHED", "TRIGGERED", "TRIGGERING"]
)
def test_live_typed_opening_requires_confirmed_active_parent(database, status):
    from tests.v2_core.test_v2_strategy_protection import Venue, new_opening, position

    _, plan, episode, opening, data = new_opening(database)
    store = BusinessState(database)
    # Explicit isolated baseline fixture; no real private account is consulted.
    store.change(
        StateKey(**asdict(plan.scope), namespace="opening-readiness-v1", key=opening),
        expected_version=0,
        request_key="qa-ready",
        payload={
            "status": "READY_FOR_GUARDED_SUBMISSION",
            "account_scope": asdict(plan.scope),
            "symbol": plan.symbol,
            "quantity": plan.quantity,
            "leverage": plan.leverage,
            "margin_type": plan.margin_mode,
            "required_margin": ".55",
        },
        reason="QA_NOTIFICATION_READY",
    )
    venue = Venue()
    position(venue)
    spec = ProtectionSpec(
        episode, plan.symbol, plan.exit_side, "STOP_MARKET", plan.stop_price
    )
    protocol = TestnetProtection(store, venue, scope=plan.scope)
    assert protocol.submit_once(spec)["status"] == "NEW"
    # Observe a synthetic exchange response through the real protocol validator;
    # never fabricate a local cancellation or external-close resolution.
    venue.parents[protocol.params(spec)["clientAlgoId"]]["algoStatus"] = status
    assert protocol.query(spec)["status"] == status
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=plan.scope)
    before = data.trace(episode)
    result = notify.run_once()
    assert result["claimed"] == 1
    assert result["delivered"] == (1 if status == "NEW" else 0)
    assert result["failed"] == (0 if status == "NEW" else 1)
    assert len(texts(connection)) == (1 if status == "NEW" else 0)
    if status == "NEW":
        assert "历史开仓补发" not in texts(connection)[0]
    assert data.trace(episode) == before


def external_flat_case(database):
    case = ready(database, protection_status="UNKNOWN")
    before = case.data.trace(case.episode)
    # Reuse the actual importer: existing exact flat ledger + fresh synthetic
    # account inventory retire the ambiguous parent without claiming a cancel.
    recovery = ExternalCloseRecovery(
        database, case.venue, scope=SCOPE, clock_ms=lambda: case.tick + 71000
    )
    assert recovery.reconcile(case.episode)["status"] == "RECONCILED"
    saved = BusinessState(database).read(case.protected)
    assert json.loads(saved.payload_json)["status"] == "EXTERNAL_FLAT_RECONCILED"
    after = case.data.trace(case.episode)
    for field in ("orders", "fills", "settlements", "cash"):
        assert after[field] == before[field]
    return case, recovery


def test_verified_external_flat_can_notify_original_settlement_once(database):
    case, _ = external_flat_case(database)
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    assert Decimal(notify._closing(case.episode)["net_pnl"]) == case.net
    assert notify.run_once()["delivered"] == 2
    assert "EXTERNAL_FLAT_RECONCILED" in texts(connection)[-1]
    assert notify.run_once()["claimed"] == 0
    assert len(texts(connection)) == 2


@pytest.mark.parametrize("defect", ["scope", "residual", "deleted"])
def test_external_flat_marker_without_matching_proof_cannot_notify(database, defect):
    case, recovery = external_flat_case(database)
    store = BusinessState(database)
    key = recovery._key(case.episode)
    saved = store.read(key)
    payload = json.loads(saved.payload_json)
    if defect == "scope":
        payload["account_scope"]["account_id"] = "foreign"
    elif defect == "residual":
        payload["remaining_quantity"] = ".01"
    store.change(
        key,
        expected_version=saved.version,
        request_key="qa-corrupt-resolution",
        payload={} if defect == "deleted" else payload,
        reason="QA_INVALID_EXTERNAL_RESOLUTION",
        deleted=defect == "deleted",
    )
    connection = TelegramConnection()
    notify = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    with pytest.raises(ValueError, match="EXTERNAL_FLAT_RESOLUTION"):
        notify._closing(case.episode)
    assert not connection.calls
