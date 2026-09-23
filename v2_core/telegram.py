"""Explicit Telegram operational sink; bounded I/O, no credential-bearing errors.

Telegram has no idempotency key for sendMessage: delivery is at least once.
Event IDs are visible so a replay can be identified, never used as trade approval.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection


class TelegramDeliveryError(RuntimeError):
    """Fixed diagnostic, never raw URL, token, message or response."""


def _number(value, places=8):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION") from None
    if not parsed.is_finite():
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    quantum = Decimal(1).scaleb(-places)
    rendered = format(parsed.quantize(quantum).normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _time(value):
    if type(value) is not int or value < 0:
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    utc_plus_8 = timezone(timedelta(hours=8), name="UTC+8")
    return datetime.fromtimestamp(value / 1000, tz=utc_plus_8).strftime(
        "%Y-%m-%d %H:%M:%S UTC+8"
    )


def _trade_text(scope, kind, _event_id, payload):
    common = {
        "symbol",
        "direction",
        "strategy",
        "system_tag",
        "event_type",
        "score",
        "leverage",
        "margin_type",
        "quantity",
    }
    required = common | (
        {
            "entry_price",
            "opening_notional",
            "planned_notional",
            "planned_loss",
            "stop_price",
            "stop_status",
            "fees",
            "opened_at_ms",
            "analysis_reason",
            "required_margin",
        }
        if kind == "TRADE_OPENED"
        else {
            "entry_price",
            "closing_price",
            "opening_notional",
            "gross_pnl",
            "fees",
            "funding",
            "net_pnl",
            "return_pct",
            "risk_multiple",
            "exit_reason",
            "held_ms",
            "opened_at_ms",
            "closed_at_ms",
            "stop_status",
        }
    )
    if set(payload) != required or any(
        not isinstance(payload[key], (str, int)) for key in required
    ):
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    side = "做多 LONG" if payload["direction"] == "LONG" else "做空 SHORT"
    title = (
        "🟢 V2 TESTNET 开仓成功"
        if kind == "TRADE_OPENED"
        else (
            "✅ V2 TESTNET 平仓盈利"
            if Decimal(str(payload["net_pnl"])) > 0
            else "🔴 V2 TESTNET 平仓完成"
        )
    )
    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"标的：{payload['symbol']}  |  {side}",
        f"策略：{payload['strategy']} / {payload['system_tag']}  |  {payload['event_type']}",
        f"评分：{payload['score']}  |  杠杆：{payload['leverage']}x {payload['margin_type']}",
    ]
    if kind == "TRADE_OPENED":
        lines += [
            "",
            "📌 成交",
            f"数量：{_number(payload['quantity'])}",
            f"均价：{_number(payload['entry_price'])}",
            f"实际名义价值：{_number(payload['opening_notional'])} USDT",
            f"计划名义价值：{_number(payload['planned_notional'])} USDT",
            f"预估保证金：{_number(payload['required_margin'])} USDT",
            f"开仓手续费：{payload['fees']}",
            "",
            "🛡 风控与保护",
            f"计划最大损失：{_number(payload['planned_loss'])} USDT",
            f"止损：{_number(payload['stop_price'])}  |  {payload['stop_status']}",
            f"历史调整：{payload['analysis_reason']}",
            "",
            f"时间：{_time(payload['opened_at_ms'])}",
        ]
    else:
        held = int(payload["held_ms"])
        lines += [
            "",
            "📌 成交与结果",
            f"数量：{_number(payload['quantity'])}",
            f"开仓均价：{_number(payload['entry_price'])}",
            f"平仓均价：{_number(payload['closing_price'])}",
            f"开仓名义价值：{_number(payload['opening_notional'])} USDT",
            f"毛收益：{_number(payload['gross_pnl'])} USDT",
            f"手续费：{_number(payload['fees'])} USDT",
            f"资金费/现金调整：{_number(payload['funding'])} USDT",
            f"净收益：{_number(payload['net_pnl'])} USDT",
            f"收益率：{_number(payload['return_pct'], 4)}%",
            f"风险倍数：{_number(payload['risk_multiple'], 4)}R",
            "",
            "🧭 退出说明",
            f"原因：{payload['exit_reason']}",
            f"持仓：{held // 3600000}h {(held % 3600000) // 60000}m {(held % 60000) // 1000}s",
            f"保护单终态：{payload['stop_status']}",
            f"开仓时间：{_time(payload['opened_at_ms'])}",
            f"平仓时间：{_time(payload['closed_at_ms'])}",
        ]
    return "\n".join(lines)


class TelegramOperationalSink:
    def __init__(
        self, *, token, chat_id, environment, connection_factory=HTTPSConnection
    ):
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{15,}", token)
            or not isinstance(chat_id, str)
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", chat_id)
            or environment not in {"SANDBOX", "LIVE"}
            or not callable(connection_factory)
        ):
            raise ValueError("explicit Telegram credentials and environment required")
        self._token, self._chat_id = token, chat_id
        self.environment, self._connection = environment, connection_factory

    def __call__(self, event_id, scope, kind, payload):
        if scope != self.environment:
            return False
        if kind in {"TRADE_OPENED", "TRADE_CLOSED"}:
            text = _trade_text(scope, kind, event_id, payload)
        elif kind not in {
            "ACCOUNT_INVENTORY",
            "MARKET_FAILURE",
            "CANDLE_QUARANTINED",
            "PROTECTION_RECOVERY",
            "TRADING_DAEMON",
        }:
            return False
        else:
            # Never forward arbitrary fields, API responses, balances or exception text.
            allowed = (
                "status",
                "blockers",
                "position_count",
                "ordinary_order_count",
                "conditional_order_count",
                "stage",
                "error_code",
                "outcome",
                "state_id",
                "parent_status",
                "account_id",
                "observed_at_ms",
            )
            clean = {key: payload[key] for key in allowed if key in payload}
            text = f"[V2 {scope}] {kind}\nevent_id={event_id}\n" + json.dumps(
                clean, ensure_ascii=False, sort_keys=True
            )
        if len(text) > 3500:
            raise TelegramDeliveryError("NOTIFICATION_TOO_LARGE")
        body = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        conn = None
        try:
            conn = self._connection("api.telegram.org", timeout=10)
            conn.request(
                "POST",
                "/bot" + self._token + "/sendMessage",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED")
            value = json.loads(raw)
            if (
                value.get("ok") is not True
                or type(value["result"]["message_id"]) is not int
                or value["result"]["message_id"] <= 0
                or str(value["result"]["chat"]["id"]) != self._chat_id
            ):
                raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED")
            return True
        except Exception:  # noqa: BLE001 - suppress credential-bearing URL/response errors
            raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - never replace delivery outcome
                    pass
