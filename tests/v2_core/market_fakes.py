"""Explicit QA doubles: never network clients or business-file fallbacks."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit


def candle_batch(offset=0):
    observed = 86400000 + offset
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": observed,
        "candles": {
            "BTCUSDT": [
                {
                    "t": observed - (index + 1) * 60000,
                    "o": "100",
                    "h": "102",
                    "l": "99",
                    "c": "101",
                    "v": "10",
                    "tbv": "6",
                }
                for index in range(1440)
            ]
        },
    }


class ArchiveClient:
    def __init__(self):
        self.tables = {"v2_candle_archive": {}, "v2_candle_manifests": {}}
        self.inserts = []

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))
        for row in rows:
            self.tables[table][row[0]] = tuple(row[1:])

    def query(self, sql, parameters):
        if "keys" in parameters:
            rows = [
                (key, self.tables["v2_candle_archive"][key][0])
                for key in parameters["keys"]
                if key in self.tables["v2_candle_archive"]
            ]
        else:
            table = (
                "v2_candle_manifests"
                if "v2_candle_manifests" in sql
                else "v2_candle_archive"
            )
            row = self.tables[table].get(parameters["digest"])
            rows = [row] if row is not None else []
        return SimpleNamespace(result_rows=rows)


class Budget:
    def __init__(self, allowed=True):
        self.allowed, self.weights, self.penalties = allowed, [], []

    def permit(self, weight):
        self.weights.append(weight)
        return self.allowed

    def penalize(self, seconds):
        self.penalties.append(seconds)


class MarketHTTP:
    def __init__(
        self, *, server_time=86402001, status=200, raw=None, retry="", fail=None
    ):
        self.server_time, self.status, self.raw, self.retry, self.fail = (
            server_time,
            status,
            raw,
            retry,
            fail,
        )
        self.calls, self.closed = [], 0
        self.mutate = lambda rows: rows

    def __call__(self, host, timeout):
        owner = self

        class Connection:
            def request(self, method, target, headers):
                owner.calls.append((host, timeout, method, target, headers))
                self.target = target
                if owner.fail:
                    raise owner.fail

            def getresponse(self):
                if owner.raw is not None:
                    data = owner.raw
                elif self.target == "/fapi/v1/time":
                    data = json.dumps({"serverTime": owner.server_time}).encode()
                else:
                    params = parse_qs(urlsplit(self.target).query)
                    start = int(params["startTime"][0])
                    rows = [
                        [
                            start + i * 60000,
                            "100",
                            "102",
                            "99",
                            "101",
                            "100" if i == 1439 else "10",
                            start + (i + 1) * 60000 - 1,
                            "1000",
                            10,
                            "60" if i == 1439 else "6",
                            "600",
                            "0",
                        ]
                        for i in range(1440)
                    ]
                    data = json.dumps(owner.mutate(rows)).encode()
                return SimpleNamespace(
                    status=owner.status,
                    read=lambda limit: data[:limit],
                    getheader=lambda key, default="": owner.retry,
                )

            def close(self):
                owner.closed += 1

        return Connection()
