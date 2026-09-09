"""Execution Core — 纯执行逻辑（P4-02 提取）。

从 strategies/shared_executor.py 与 shared/position_manager.py 的 Execution 路径
提取的无 IO、无副作用纯逻辑。字段与算法均为当前真实代码的忠实冻结，
不是重新设计。

依赖规则：stdlib only（dataclasses / enum / typing）。
禁止：redis / requests / shared_executor / position_manager / strategies / time 等。

IO（Binance 调用、exchangeInfo 抓取、Redis、PG、TG、sandbox 状态）留在
shared_executor / PM / 后续 P4-03 Execution Service。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

# ═══════════════════════════════════════════════════════════════
#  Side 映射（se L943 / PM L794, L1685, L1798 同一映射）
# ═══════════════════════════════════════════════════════════════

def order_side_for(side: str) -> str:
    """持仓/信号方向 → 开仓订单方向。严格匹配 'SHORT'，其余一律 BUY。

    真实代码（开仓）：'SELL' if side == 'SHORT' else 'BUY'
    （se L943 与 pm.open_position L794 同一映射）。
    """
    return 'SELL' if side == 'SHORT' else 'BUY'


def close_order_side_for(side: str) -> str:
    """持仓方向 → 平仓订单方向。与开仓相反：SHORT→BUY（买回），其余→SELL。

    真实代码（平仓）：'BUY' if pos['side'] == 'SHORT' else 'SELL'
    （pm._close L1798 与 pm._partial_close L1685 同一映射）。
    """
    return 'BUY' if side == 'SHORT' else 'SELL'


# ═══════════════════════════════════════════════════════════════
#  Order Intent（字段全部来自真实代码，无新增参数）
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class OrderIntent:
    """一张订单的纯数据意图。None 字段不会出现在 to_params() 输出中。"""
    symbol: str
    side: str                      # BUY | SELL（交易所订单方向）
    type: str = 'MARKET'
    quantity: float = 0.0
    newOrderRespType: Optional[str] = None   # 仅 se.open_position 传 'RESULT'
    positionSide: Optional[str] = None       # 仅 PM 系（BOTH）
    reduceOnly: Optional[str] = None         # 仅 PM._close 传 'true'（E-OBS-5 非对称）

    def to_params(self) -> dict:
        """还原为真实代码发出的 params dict（键序与原字面量一致）。"""
        params: dict[str, Any] = {
            'symbol': self.symbol,
            'side': self.side,
            'type': self.type,
            'quantity': self.quantity,
        }
        if self.newOrderRespType is not None:
            params['newOrderRespType'] = self.newOrderRespType
        if self.positionSide is not None:
            params['positionSide'] = self.positionSide
        if self.reduceOnly is not None:
            params['reduceOnly'] = self.reduceOnly
        return params


def se_open_intent(symbol: str, side: str, qty: float) -> OrderIntent:
    """se.open_position 的市价开仓意图（se L944-950：无 positionSide / reduceOnly）。"""
    return OrderIntent(symbol=symbol, side=order_side_for(side), type='MARKET',
                       quantity=qty, newOrderRespType='RESULT')


def pm_open_intent(symbol: str, side: str, qty: float) -> OrderIntent:
    """pm.open_position 的市价开仓意图（PM L796-799：positionSide=BOTH）。"""
    return OrderIntent(symbol=symbol, side=order_side_for(side), type='MARKET',
                       quantity=qty, positionSide='BOTH')


def close_intent(symbol: str, side: str, qty: float) -> OrderIntent:
    """pm._close 的市价平仓意图（PM L1798-1801：reduceOnly='true'）。

    side 为持仓方向；SHORT → BUY（买回），LONG → SELL。
    """
    return OrderIntent(symbol=symbol, side=close_order_side_for(side), type='MARKET',
                       quantity=qty, positionSide='BOTH', reduceOnly='true')


def partial_close_intent(symbol: str, side: str, qty: float) -> OrderIntent:
    """pm._partial_close 的分层止盈意图（PM L1685-1690：无 reduceOnly）。

    side 为持仓方向；SHORT → BUY，LONG → SELL。
    """
    return OrderIntent(symbol=symbol, side=close_order_side_for(side), type='MARKET',
                       quantity=qty, positionSide='BOTH')


# ═══════════════════════════════════════════════════════════════
#  Response parsing（se L951-960 冻结）
# ═══════════════════════════════════════════════════════════════

def is_rejected(result: Any) -> bool:
    """se L951 判定：`not result or result.get('code')`。None/空 → 拒绝。"""
    return not result or result.get('code')


@dataclass(frozen=True)
class ExecutionResult:
    """se.open_position 对 Binance RESULT 响应的解析产物（字段名照搬）。"""
    order_id: Any
    status: str
    filled_qty: float
    cum_qty: float
    avg_price: float
    rejected: bool


def parse_execution_result(result: dict, entry_price: float) -> ExecutionResult:
    """解析成交结果（se L956-960 逐行冻结，含 fallback 与异常语义）。

    - status 缺失 → 'NEW'
    - executedQty 缺失 → 0；取 abs(float(...))（负数翻转、非数字抛 ValueError/TypeError）
    - cumQty 缺失 → 回退 executedQty
    - avgPrice 非法（缺失/空串/'0'/≤0）→ 回退 entry_price
    不捕获任何异常：float() 的 ValueError/TypeError 原样上抛
    （真实代码由 open_position 外层 try 捕获 → return False）。
    """
    status = result.get('status', 'NEW')
    filled_qty = abs(float(result.get('executedQty', 0)))
    cum_qty = abs(float(result.get('cumQty', filled_qty)))
    avg_price_str = result.get('avgPrice', '0')
    avg_price = float(avg_price_str) if avg_price_str and float(avg_price_str) > 0 \
        else entry_price
    return ExecutionResult(order_id=result.get('orderId'), status=status,
                           filled_qty=filled_qty, cum_qty=cum_qty,
                           avg_price=avg_price, rejected=is_rejected(result))


# ═══════════════════════════════════════════════════════════════
#  Fill classification（se L963-991 分支顺序与阈值冻结）
# ═══════════════════════════════════════════════════════════════

class OpenFillOutcome(Enum):
    """真实 if 链的分支名（非新状态机；顺序即真实判定顺序）。"""
    UNFILLED_NEW = 'unfilled_new'          # status NEW 且 filled==0 → 取消并失败
    ZERO_FILL = 'zero_fill'                # filled<0.01 且 cum<0.01 → 失败
    PARTIAL_BELOW_HALF = 'partial_below_half'  # filled < qty*0.5 → 取消剩余并接受
    ACCEPTED = 'accepted'                  # 其余 → 接受


def classify_open_fill(status: str, filled_qty: float, cum_qty: float,
                       requested_qty: float) -> OpenFillOutcome:
    """按真实代码顺序分类（rejection 在 parse/is_rejected 层处理）。

    真实阈值：0.01（zero-fill）、0.5（partial 分界，严格小于）。
    """
    if status == 'NEW' and filled_qty == 0:
        return OpenFillOutcome.UNFILLED_NEW
    if filled_qty < 0.01 and cum_qty < 0.01:
        return OpenFillOutcome.ZERO_FILL
    if filled_qty < requested_qty * 0.5:
        return OpenFillOutcome.PARTIAL_BELOW_HALF
    return OpenFillOutcome.ACCEPTED


# ═══════════════════════════════════════════════════════════════
#  Quantity normalization 纯核（IO 留在调用方）
# ═══════════════════════════════════════════════════════════════

def round_qty_by_lot_step(qty: float, step: float) -> float:
    """se._round_qty 的纯核（se L1066-1070 逐行冻结）。

    qty - (qty % step) 为向下截断（非四舍五入）；
    decimals 来自 stepStr 去尾零后的小数位（'1.0'→'1.'→0 位）。
    """
    step_str = str(step).rstrip('0')
    decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
    return round(qty - (qty % step), decimals)


def round_qty_from_exchange_info(info: Any, symbol: str, qty: float) -> Optional[float]:
    """在 exchangeInfo 中查 symbol 的 LOT_SIZE stepSize 并舍入。

    返回 None 表示"未找到"（info 非 dict / symbol 缺失 / 无 LOT_SIZE），
    调用方（se._round_qty）回退原始 qty；float() 异常原样上抛（调用方 except）。
    """
    if not isinstance(info, dict):
        return None
    for s in info.get('symbols', []):
        if s['symbol'] == symbol:
            for f in s['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    return round_qty_by_lot_step(qty, float(f['stepSize']))
    return None


def round_qty_by_precision(qty: float, prec: int) -> float:
    """pm._round_qty 的纯核（PM L723-735）：round(qty, prec)，失败层回退 6 位。"""
    return round(qty, prec)


# ═══════════════════════════════════════════════════════════════
#  Close 纯数学（PM._close / _partial_close 冻结）
# ═══════════════════════════════════════════════════════════════

def remaining_after_partial(qty: float, close_qty: float) -> float:
    """pm._partial_close L1697：round(qty - close_qty, 4)。

    冻结 OBS（E-OBS-7）：close_qty 为负时 qty 反向放大（10-(-100)=110）。
    """
    return round(qty - close_qty, 4)


def partial_pnl_u(side: str, entry: float, price: float, close_qty: float) -> float:
    """pm._partial_close L1698-1699 的 pnl_u。"""
    return round((price - entry) * close_qty, 2) if side == 'LONG' \
        else round((entry - price) * close_qty, 2)


def position_pnl(side: str, entry: float, price: float,
                 close_qty: float) -> tuple[float, float]:
    """pm._close 的 (pnl_pct, pnl_u)（L1868-1873；沙盘/flat 分支同一公式）。

    pnl_pct 不做 round（真实代码仅在日志 f-string 中格式化）。
    """
    if side == 'SHORT':
        pnl_pct = (entry - price) / entry * 100
        pnl_u = round((entry - price) * close_qty, 2)
    else:
        pnl_pct = (price - entry) / entry * 100
        pnl_u = round((price - entry) * close_qty, 2)
    return pnl_pct, pnl_u


REMAINING_EPS = 0.001   # PM._close 的剩余/成交判定阈值（L1818/L1854）

def has_remaining_position(remaining_qty: float) -> bool:
    """PM._close L1818：remaining_qty >= 0.001 → 走部分成交保留分支。"""
    return remaining_qty >= REMAINING_EPS

def accounted_close_qty(reported_filled_qty: float,
                        requested_close_qty: float) -> float:
    """PM._close L1854 fallback：executedQty 可信（≥0.001）用它，否则用请求量。"""
    return reported_filled_qty if reported_filled_qty >= REMAINING_EPS \
        else requested_close_qty


# ═══════════════════════════════════════════════════════════════
#  Sandbox 拦截谓词（se._sandbox_post L144 冻结）
# ═══════════════════════════════════════════════════════════════

def is_order_path(path: str) -> bool:
    """path 小写含 'order' 即拦截（/fapi/v1/order 与 /fapi/v1/algoOrder 均命中）。"""
    return 'order' in path.lower()
