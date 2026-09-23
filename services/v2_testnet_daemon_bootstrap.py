"""Fail-closed service configuration and outer-loop notification adapters.

This module does not read files, inspect process environment, create clients or
start a daemon.  The process entrypoint must pass an explicit mapping and ports.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.telegram import TelegramOperationalSink


def _boolean(value, name):
    if value not in {"true", "false"}:
        raise ValueError(name + " must be explicitly true or false")
    return value == "true"


@dataclass(frozen=True)
class TestnetDaemonConfig:
    __test__ = False

    account_id: str
    interval_seconds: int
    enable_entries: bool
    enable_protection_writes: bool
    enable_reduce_only_exits: bool

    @classmethod
    def from_mapping(cls, values):
        required = {
            "V2_ACCOUNT_ID",
            "V2_ENVIRONMENT",
            "V2_INTERVAL_SECONDS",
            "V2_ENABLE_ENTRIES",
            "V2_ENABLE_PROTECTION_WRITES",
            "V2_ENABLE_REDUCE_ONLY_EXITS",
            "V2_TESTNET_WRITE_ACK",
        }
        if not isinstance(values, Mapping) or any(
            key not in values or not isinstance(values[key], str) for key in required
        ):
            raise ValueError("explicit Testnet daemon configuration required")
        account = values["V2_ACCOUNT_ID"]
        if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", account):
            raise ValueError("invalid Testnet account identity")
        if values["V2_ENVIRONMENT"] != "SANDBOX":
            raise ValueError("Testnet daemon refuses non-SANDBOX environment")
        raw_interval = values["V2_INTERVAL_SECONDS"]
        if not raw_interval.isascii() or not raw_interval.isdigit():
            raise ValueError("bounded daemon interval required")
        interval = int(raw_interval)
        if not 1 <= interval <= 60:
            raise ValueError("bounded daemon interval required")
        entries = _boolean(values["V2_ENABLE_ENTRIES"], "V2_ENABLE_ENTRIES")
        protection = _boolean(
            values["V2_ENABLE_PROTECTION_WRITES"],
            "V2_ENABLE_PROTECTION_WRITES",
        )
        exits = _boolean(
            values["V2_ENABLE_REDUCE_ONLY_EXITS"],
            "V2_ENABLE_REDUCE_ONLY_EXITS",
        )
        if entries and not protection:
            raise ValueError("entry permission requires protection permission")
        expected_ack = "SANDBOX:" + account
        if any((entries, protection, exits)):
            if values["V2_TESTNET_WRITE_ACK"] != expected_ack:
                raise ValueError("account-bound Testnet write acknowledgement required")
        elif values["V2_TESTNET_WRITE_ACK"] not in {"", expected_ack}:
            raise ValueError("invalid Testnet write acknowledgement")
        return cls(account, interval, entries, protection, exits)

    @property
    def write_enabled(self):
        return any(
            (
                self.enable_entries,
                self.enable_protection_writes,
                self.enable_reduce_only_exits,
            )
        )

    def public_summary(self):
        return {
            "account_id": self.account_id,
            "environment": "SANDBOX",
            "interval_seconds": self.interval_seconds,
            "enable_entries": self.enable_entries,
            "enable_protection_writes": self.enable_protection_writes,
            "enable_reduce_only_exits": self.enable_reduce_only_exits,
        }


class DaemonTelegramNotifier:
    """Best-effort outer-loop alert; never a trade acknowledgement.

    This deliberately does not use PG: it must remain useful when the daemon
    checkpoint itself fails because PostgreSQL is unavailable.
    """

    def __init__(self, sink, *, account_id, clock_ms):
        if (
            not isinstance(sink, TelegramOperationalSink)
            or sink.environment != "SANDBOX"
            or not isinstance(account_id, str)
            or not account_id
            or not callable(clock_ms)
        ):
            raise ValueError("explicit Testnet daemon notification ports required")
        self.sink, self.account_id, self.clock_ms = sink, account_id, clock_ms

    def __call__(self, event):
        if (
            not isinstance(event, dict)
            or set(event) != {"status", "error_code", "account_scope"}
            or event["status"] != "UNAVAILABLE"
            or not isinstance(event["error_code"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", event["error_code"])
            or not isinstance(event["account_scope"], dict)
            or event["account_scope"]
            != {
                "exchange": "BINANCE",
                "account_id": self.account_id,
                "environment": "SANDBOX",
                "product": "FUTURES",
            }
        ):
            return False
        observed = milliseconds(self.clock_ms())
        payload = {
            "status": "UNAVAILABLE",
            "error_code": event["error_code"],
            "account_id": self.account_id,
            "observed_at_ms": observed,
        }
        identity = "daemon:" + digest(canonical(payload))
        return self.sink(identity, "SANDBOX", "TRADING_DAEMON", payload)
