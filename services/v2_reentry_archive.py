"""Lazy, dedicated read-only archive port; never reuse the 10s trading client."""

import json
import threading
from dataclasses import dataclass

from services.v2_shared_market import IncrementalArchive


@dataclass(frozen=True)
class ReadLimits:
    connect_seconds: int = 1
    receive_seconds: int = 2
    execution_seconds: int = 1
    result_rows: int = 2500
    result_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        for field, low, high in (
            ("connect_seconds", 1, 2),
            ("receive_seconds", 1, 5),
            ("execution_seconds", 1, 3),
            ("result_rows", 1500, 10000),
            ("result_bytes", 262144, 8 * 1024 * 1024),
        ):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("REENTRY_READ_LIMIT_INVALID")
        if self.execution_seconds > self.receive_seconds:
            raise ValueError("REENTRY_READ_LIMIT_INVALID")


def _client(**kwargs):
    import clickhouse_connect

    return clickhouse_connect.get_client(**kwargs)


class ReentryArchive:
    def __init__(self, *, limits=None, factory=None):
        self.limits = limits or ReadLimits()
        self.factory = factory or _client
        self.client = None
        self.lock = threading.Lock()

    @classmethod
    def from_mapping(cls, values):
        return cls(
            limits=ReadLimits(
                **json.loads(values.get("V2_REENTRY_READ_LIMITS_JSON", "{}"))
            )
        )

    def read(self, target, start, end):
        if not self.lock.acquire(blocking=False):
            raise ValueError("REENTRY_ARCHIVE_BUSY")
        try:
            if self.client is None:
                p = self.limits
                self.client = self.factory(
                    host="127.0.0.1",
                    port=18123,
                    database="trade_v2_testnet",
                    connect_timeout=p.connect_seconds,
                    send_receive_timeout=p.receive_seconds,
                    query_retries=0,
                    settings={
                        "readonly": 1,
                        "max_threads": 1,
                        "max_execution_time": p.execution_seconds,
                        "timeout_overflow_mode": "throw",
                        "max_result_rows": p.result_rows,
                        "max_result_bytes": p.result_bytes,
                        "result_overflow_mode": "throw",
                    },
                )
            return IncrementalArchive(self.client, "SANDBOX").read(target, start, end)
        except Exception:
            self.close()
            raise
        finally:
            self.lock.release()

    def close(self):
        if self.client is not None:
            client, self.client = self.client, None
            client.close()
