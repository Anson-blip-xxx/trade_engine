"""Binance Execution Adapter — 复用现有实现，不复制 HTTP 代码。

Inventory 决策（EXECUTION_SERVICE_INVENTORY.md §1.3/§4/§5.1）：
- open 路径的底层实现 = shared_executor 的 fapi_post（异常→None + se 沙盘拦截语义，
  E-OBS-11a），因此默认 Adapter 以函数引用注入该 callable。
- **不 import strategies**：callables 在构造时注入（依赖方向 strategies → execution，
  反向禁止）；实际 wiring（把 se.fapi_post 接进来）属 P4-03-01-B 的调用方迁移。
- 不统一四套 Binance 实现（se 自带 / binance_api / PM _light_fapi_* / S7）——
  binance_api 与 PM 实现保持现址不动（E-OBS-10：各路径 sandbox 语义不同）。
- 不新增 retry / 错误归一化 / 异常包装：异常语义 = 注入 callable 的原样语义。
"""
from __future__ import annotations

from typing import Callable, Optional

from execution.core import OrderIntent

#: 与 shared_executor.fapi_post 同签名的 callable（path, params) -> dict | None
FapiPost = Callable[[str, dict], Optional[dict]]


class SharedExecutorBinanceAdapter:
    """shared_executor.fapi_post 语义的 Binance 下单适配器（open 路径默认实现）。

    异常/None 语义 = 注入的 fapi_post 原样：
    - 注入 se.fapi_post   → 异常内部吞掉返回 None（当前 SE 开仓路径语义）
    - 注入 binance_api.fapi_post → 异常上抛（若未来某路径需要，wiring 时显式选择）
    本类自身不做任何 try/except。
    """

    ORDER_PATH = '/fapi/v1/order'

    def __init__(self, fapi_post: FapiPost) -> None:
        self._fapi_post = fapi_post

    def place_order(self, intent: OrderIntent) -> Optional[dict]:
        """intent → POST {ORDER_PATH}，params 逐字 = OrderIntent.to_params()。

        不修改 intent、不增删参数（四套 intent 的 Golden 差异由 core 冻结：
        SE Open 无 positionSide/reduceOnly；PM Open +positionSide；
        Full Close +reduceOnly；Partial Close +positionSide 无 reduceOnly）。
        """
        return self._fapi_post(self.ORDER_PATH, intent.to_params())
