import json
from dataclasses import asdict

from test_v2_account_inventory import TelegramConnection, sink
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from v2_core.state import BusinessState, StateKey
from v2_core.trade_notifications import TradeLifecycleNotifications

database = database_fixture
closed = closed_fixture


def save_protection(database, episode):
    key = StateKey(
        **asdict(SCOPE), namespace="testnet-protection-v1", key="qa-protection"
    )
    assert (
        BusinessState(database)
        .change(
            key,
            expected_version=0,
            request_key="confirmed",
            payload={
                "spec": {
                    "episode": episode,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "kind": "STOP_MARKET",
                    "trigger_price": "90",
                },
                "status": "EXPIRED",
                "observation": {"algoId": 77},
            },
            reason="QA_PROTECTION",
        )
        .code
        == "APPLIED"
    )


def add_required_margin(database, runtime, episode):
    opening = next(
        order
        for order in runtime.data.trace(episode)["orders"]
        if order["leg"] == "OPEN"
    )
    store = BusinessState(database)
    key = StateKey(
        **asdict(SCOPE), namespace="opening-readiness-v1", key=opening["order_id"]
    )
    current = store.read(key)
    payload = json.loads(current.payload_json)
    payload["required_margin"] = "68.75"
    assert (
        store.change(
            key,
            expected_version=current.version,
            request_key="notification-evidence",
            payload=payload,
            reason="QA_NOTIFICATION_EVIDENCE",
        ).code
        == "APPLIED"
    )


def messages(connection):
    return [json.loads(call[1]["body"])["text"] for call in connection.calls]


def test_confirmed_open_and_settlement_render_complete_durable_messages(
    database, closed
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    add_required_margin(database, runtime, episode)
    settled = service(database, income).settle(episode)
    assert settled["status"] == "SETTLED", settled
    save_protection(database, episode)

    connection = TelegramConnection()
    notifications = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    first = notifications.run_once()
    assert first == {
        "claimed": 2,
        "delivered": 2,
        "failed": 0,
        "superseded": 0,
    }, messages(connection)
    rendered = messages(connection)
    assert len(rendered) == 2
    opening = next(text for text in rendered if "开仓成功" in text)
    closing = next(text for text in rendered if "平仓完成" in text)
    for expected in (
        "BTCUSDT",
        "S6 / S6A",
        "杠杆：3x CROSSED",
        "计划最大损失：10 USDT",
        "1970-01-01 08:00:00 UTC+8",
    ):
        assert expected in opening
    for expected in (
        "开仓均价：100",
        "平仓均价：92",
        "毛收益：-10 USDT",
        "手续费：0.096 USDT",
        "资金费/现金调整：-0.01 USDT",
        "净收益：-10.106 USDT",
        "收益率：-8.0848%",
        "原因：RECONCILED_EXTERNAL_CLOSE",
    ):
        assert expected in closing
    for hidden in (
        "追溯",
        "Episode",
        "Signal",
        "Event",
        "本地订单",
        "交易所订单",
        "Client ID",
        "保护单 ID",
        episode,
    ):
        assert hidden not in opening and hidden not in closing
    assert notifications.run_once() == {
        "claimed": 0,
        "delivered": 0,
        "failed": 0,
        "superseded": 0,
    }
    assert len(connection.calls) == 2


def test_open_notification_waits_for_confirmed_protection(database, closed):
    runtime, episode, _ = closed
    baseline(database, runtime, episode)
    add_required_margin(database, runtime, episode)
    connection = TelegramConnection()
    notifications = TradeLifecycleNotifications(database, sink(connection), scope=SCOPE)
    result = notifications.run_once()
    assert result["claimed"] == result["failed"] == 1
    assert result["delivered"] == 0 and connection.calls == []
