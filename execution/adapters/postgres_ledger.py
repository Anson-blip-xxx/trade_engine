"""Postgres Ledger Adapter — PositionLedgerPort 的注入式实现。

与同家族 adapter（Binance / PM / Redis）一致：真实实现在 wiring 时以
callable 注入（shared.postgres_client.record_trade_event /
upsert_trade_episode），execution 包不 import psycopg/redis/strategies
（无循环依赖；无-wiring 当前状态由测试冻结）。

职责仅限：Port call → existing PG helper（逐字委托）——
- 不修改 SQL / 字段 / json 序列化 / Decimal 转换（均发生在 helper 内）
- 不吞/不包装异常：helper 自身永不抛错（内部 except → False），语义原样
- transaction（commit/rollback/close）语义完全属于 helper 的 _connection，
  adapter 不触碰
"""
from __future__ import annotations

from typing import Callable

#: 与 shared.postgres_client.record_trade_event 同签名 (data: dict) -> bool
RecordTradeEvent = Callable[..., bool]
#: 与 shared.postgres_client.upsert_trade_episode 同签名 (data: dict) -> bool
UpsertTradeEpisode = Callable[..., bool]


class PostgresLedgerAdapter:
    """PositionLedgerPort 的注入式实现（惰性 wiring，零生产改动）。"""

    def __init__(self, record_trade_event: RecordTradeEvent,
                 upsert_trade_episode: UpsertTradeEpisode) -> None:
        self._record_trade_event = record_trade_event
        self._upsert_trade_episode = upsert_trade_episode

    def record_trade_event(self, data: dict) -> bool:
        """逐字委托：同一 dict 引用、同一返回值、同一异常行为。"""
        return self._record_trade_event(data)

    def upsert_trade_episode(self, data: dict) -> bool:
        """逐字委托：同一 dict 引用、同一返回值、同一异常行为。"""
        return self._upsert_trade_episode(data)
