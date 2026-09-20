"""S0 market projection publisher: Redis and ClickHouse, no business files.

The legacy file callback argument is retained for call compatibility but never
invoked. This advisory market publisher is not a trading lifecycle authority.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

#: 与 shared.redis_store.set 同签名 (key, data)
RedisSet = Callable[..., None]
#: 原子文件写入（path 由 state_file 回调提供）
FileWrite = Callable[[dict], None]
#: 与 shared.clickhouse_client.insert 同签名 (table, row)
ChInsert = Callable[..., bool]
#: CH 错误回调（legacy 语义 = log.warning，不抛）
ChErrorHandler = Callable[[Exception], None]


class S0PublisherAdapter:
    """S0PublisherPort 的注入式缓存/分析实现。"""

    REDIS_KEY = 'market:s0'

    def __init__(self, redis_set: RedisSet, file_write: FileWrite,
                 ch_insert: ChInsert, on_ch_error: ChErrorHandler) -> None:
        self._redis_set = redis_set
        self._file_write = file_write
        self._ch_insert = ch_insert
        self._on_ch_error = on_ch_error

    def publish_state(self, state: dict) -> None:
        """Publish advisory projections without local file persistence."""
        # Redis projection failure does not prevent the analytics attempt.
        try:
            self._redis_set(self.REDIS_KEY, state)
        except Exception:
            pass
        # V2: local files are not a business-state sink.
        # 3. ClickHouse（失败 → on_ch_error 后效错误，不抛）
        row = json.dumps({
            'market_state':  state['market_state'],
            'btc_trend':     state['btc_trend'],
            'breadth':       state['breadth'],
            'breadth_ratio': state['breadth_ratio'],
            'volatility':    state['volatility'],
            'risk_off':      1 if state['risk_off'] else 0,
        })
        try:
            self._ch_insert('default.market_state_log', row)
        except Exception as e:
            self._on_ch_error(e)

    # ── canonical 原子文件内核（供 orchestration 注入 file_write 用） ──
    @staticmethod
    def atomic_file_write(json_writer=None, *, state_file_path: str,
                          state: dict) -> None:
        """Retired entry point: explicitly reject any business file writer."""
        raise RuntimeError('V2 business file storage is disabled')


class S0RedisMarketAdapter:
    """S0MarketDataPort 的 Redis/FAPI 注入式实现（晚绑定）。"""

    S3_KEY = 'market:s3_data'
    SENTIMENT_KEY = 'market:sentiment'
    TICKER_PATH = '/fapi/v1/ticker/24hr'

    def __init__(self, redis_get: Callable[..., dict],
                 fapi_fetch: Callable[..., list]) -> None:
        self._rget = redis_get
        self._fapi_get = fapi_fetch

    def read_s3_market(self) -> dict:
        try:
            data = self._rget(self.S3_KEY)
            if data and 'symbols' in data:
                return data
        except Exception:
            pass
        return {}

    def read_s3_window(self, symbol: str, tf: str) -> dict:
        """镜像 legacy `_s3_window`（每次独立 _rget；异常→{}）。"""
        try:
            data = self._rget(self.S3_KEY)
            if data and 'symbols' in data:
                sym_data = data['symbols'].get(symbol, {})
                return sym_data.get(tf, {})
        except Exception:
            pass
        return {}

    def fetch_ticker_24h(self) -> list:
        """委托注入的 fapi_get（raise/non-200 语义由 legacy 实现保留）。"""
        return self._fapi_get(self.TICKER_PATH)

    def read_sentiment(self) -> dict:
        try:
            data = self._rget(self.SENTIMENT_KEY)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}


class BreadthPoolMemoryState:
    """process-local 6h 池的 backing 桥接（get/put 经注入回调）。

    backing_get 返回 (symbols, ts)；backing_put 写回 legacy 模块全局
    （orchestration 用 global 语句组装回调）——保持可 monkeypatch。
    """

    def __init__(self, backing_get: Callable[[], tuple],
                 backing_put: Callable[[list, float], None]) -> None:
        self._get = backing_get
        self._put = backing_put

    def get(self) -> tuple[list, float]:
        return self._get()

    def put(self, symbols: list, ts: float) -> None:
        self._put(symbols, ts)
