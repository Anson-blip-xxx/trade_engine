"""Funding-rate helper（P8-03，C5；pure parse — transport 不迁）。

Contract 冻结（behavior 0 change）：
- 解析冻结：`response.json().get('lastFundingRate', 0)` → `float`（无 Decimal）
- 失败拓扑冻结：fetch/transport/parse **任何**异常 → 0.0（HEAD 首路径：
  API exception / .json() 失败 / 字段缺省 None / invalid numeric 全部被
  caller 的 try/except 吞成 0——本 helper 镜像同一拓扑）
- 无缓存；无 TTL；无 memoization；每次调用 fresh 一次 fetch
- 调用次数不变（caller 高度控制 fetch_fn 调用次数）
"""
from __future__ import annotations

from typing import Callable


def read_funding_rate(symbol: str, fetch_fn: Callable[[str], object]) -> float:
    """read funding via injected legacy transport。

    fetch_fn(symbol) 返回 Binance response 对象（含 .json()）——
    transport 仍由 caller 拥有；本函数只承载：调用 + 解析 + 失败→0 拓扑。
    """
    try:
        r = fetch_fn(symbol)
        return float(r.json().get('lastFundingRate', 0))
    except Exception:
        return 0.0
