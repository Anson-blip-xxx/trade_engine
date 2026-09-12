"""S0 Publisher Port — 分类结果三写链的最小契约（P6-03）。

真实 seam：`services/s0/s0_market_guard.write_state`（Redis → 原子文件 →
ClickHouse，S0-9 三种不同失败语义）。契约保留**单一方法**——当前 production
contract 是一条有顺序、有异常语义的链，不拆成三个方法。

不得 import：Redis client / clickhouse / services.s0 / shared / strategies /
execution / classifier core（s0.core）；无时钟/无网络/无文件 IO。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class S0PublisherPort(Protocol):
    """S0 状态发布能力端口（structural typing；adapter 无需显式继承）。"""

    def publish_state(self, state: dict) -> None:
        """按真实三写链发布一帧市场状态。

        语义（S0-9 逐字镜像，由 adapter 实现）：
        - Redis 失败 → 吞错继续（file/CH 照写）
        - 原子文件失败 → 异常上抛（CH 跳过）
        - CH 失败 → 不上抛（调用方视为成功，error 由 on_ch_error 回调处理）
        - 不 mutate state；不返回值
        """
        ...


class S0MarketDataPort(Protocol):
    """S0 输入侧最小契约（P6-04；结构性类型（structural typing））。

    三个 seam = 现有 IO 助手（`_rget('market:s3_data')` / `_rget('market:sentiment')`
    / `fapi_get('/fapi/v1/ticker/24hr')`）的镜像能力；不做 regime 分类、
    不含 breadth 池 6h 缓存（BreadthPoolState 单独）。
    """

    def read_s3_market(self) -> dict:
        """读 market:s3_data 整帧。

        语义 = S3 消费者镜像（缺 key/畸形/异常 → {}，无 stale 检查——
        producer/采样聚类 consumer 均无 stale gate，S0-3 家族保持）。
        """
        ...

    def read_s3_window(self, symbol: str, tf: str) -> dict:
        """读单币单窗（镜像 `_s3_window`：独立 _rget 重读，异常→{}）。"""
        ...

    def fetch_ticker_24h(self) -> list:
        """Binance /fapi/v1/ticker/24hr（fapi_get 的 raise/non-200 语义原样）。"""
        ...

    def read_sentiment(self) -> dict:
        """读 market:sentiment（非 dict/缺 key → {}，S0 情绪缺省）。"""
        ...


class BreadthPoolState(Protocol):
    """广度采样池（6h 缓存）的显式 state 边界。

    语义（S0-4/S0-5 家族保持）：
    - process-local；重启=清空
    - 6h = 严格 `< 6*3600`（== 6h 即过期）——由 caller 以 fake now 校验
    - refresh 失败 → caller 回退老池（本 store 不做回填/不抛错）
    """

    def get(self) -> tuple[list, float]:
        """(symbols, ts)——返回 backing 引用（list 可变引用，不复制）。"""
        ...

    def put(self, symbols: list, ts: float) -> None:
        """写入新池与时间戳（原地替换 backing 引用语义与模块全局一致）。"""
        ...
