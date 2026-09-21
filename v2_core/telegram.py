"""Explicit Telegram operational sink; bounded I/O, no credential-bearing errors.

Telegram has no idempotency key for sendMessage: delivery is at least once.
Event IDs are visible so a replay can be identified, never used as trade approval.
"""

import json
import re
from http.client import HTTPSConnection


class TelegramDeliveryError(RuntimeError):
    """Fixed diagnostic, never raw URL, token, message or response."""


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
        if kind not in {"ACCOUNT_INVENTORY", "MARKET_FAILURE", "CANDLE_QUARANTINED"}:
            return False
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
