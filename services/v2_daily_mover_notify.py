"""Bounded daily-research DM delivery; no group fallback or implicit retries."""

import json
import os
import re
from http.client import HTTPSConnection

from services.v2_managed_health import _notification_credentials


class MoverNotificationError(RuntimeError):
    """Fixed, non-sensitive diagnostic; outcome may be unknown after timeout."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_RESPONSE_FIELD")
        result[key] = value
    return result


class PrivateMoverNotifier:
    """Only sendMessage to one explicitly configured private destination."""

    def __init__(self, *, token, destination, connection_factory=HTTPSConnection):
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{15,}", token)
            or not isinstance(destination, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", destination)
            or not callable(connection_factory)
        ):
            raise ValueError("MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID")
        self._token = token
        self._destination = destination
        self._connection_factory = connection_factory

    def __call__(self, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 3500:
            raise ValueError("MOVER_PRIVATE_NOTIFICATION_TEXT_INVALID")
        try:
            # Telegram counts UTF-16 code units; bound astral symbols as well.
            if len(text.encode("utf-16-le")) // 2 > 3500:
                raise ValueError("MOVER_PRIVATE_NOTIFICATION_TEXT_INVALID")
            body = json.dumps(
                {"chat_id": self._destination, "text": text},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        except (UnicodeError, TypeError):
            raise ValueError("MOVER_PRIVATE_NOTIFICATION_TEXT_INVALID") from None
        if len(body) > 16_000:
            raise ValueError("MOVER_PRIVATE_NOTIFICATION_TEXT_INVALID")
        connection = None
        try:
            connection = self._connection_factory("api.telegram.org", timeout=10)
            connection.request(
                "POST",
                "/bot" + self._token + "/sendMessage",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(65_537)
            if response.status != 200 or len(raw) > 65_536:
                raise MoverNotificationError("MOVER_PRIVATE_NOTIFICATION_UNCONFIRMED")
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(value, dict)
                or value.get("ok") is not True
                or not isinstance(value.get("result"), dict)
                or type(value["result"].get("message_id")) is not int
                or value["result"]["message_id"] <= 0
                or not isinstance(value["result"].get("chat"), dict)
                or str(value["result"]["chat"].get("id")) != self._destination
            ):
                raise MoverNotificationError("MOVER_PRIVATE_NOTIFICATION_UNCONFIRMED")
            return value["result"]["message_id"]
        except Exception:  # noqa: BLE001 - never expose token URL, response or text
            raise MoverNotificationError(
                "MOVER_PRIVATE_NOTIFICATION_UNCONFIRMED"
            ) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001, S110 - cannot replace outcome
                    pass


def make_notifier(
    env=None,
    *,
    credential_loader=_notification_credentials,
    connection_factory=HTTPSConnection,
):
    """Missing explicit config returns None; invalid private config is rejected.

    The protected loader may contain the existing group's chat ID; that value
    is deliberately never used here. Only V2_TG_ALERT_CHAT_ID chooses delivery.
    """
    values = os.environ if env is None else env
    destination = values.get("V2_TG_ALERT_CHAT_ID")
    directory = values.get("CREDENTIALS_DIRECTORY")
    if not destination or not directory:
        return None
    if (
        not isinstance(destination, str)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", destination)
        or not isinstance(directory, str)
        or not directory.startswith("/")
    ):
        raise ValueError("MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID")
    try:
        credentials = credential_loader(directory)
        token = credentials["TG_NOTIFY_TOKEN"]
        return PrivateMoverNotifier(
            token=token,
            destination=destination,
            connection_factory=connection_factory,
        )
    except Exception:  # noqa: BLE001 - suppress paths/ACL/material in diagnostics
        raise ValueError("MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID") from None
