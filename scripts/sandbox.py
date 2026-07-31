#!/usr/bin/env python3
"""
sandbox.py — 纸交易沙盘模式（仓库内统一版）

由两套旧实现合并：
  - 旧版（部署在仓库父目录）：文件持久化状态 + 真实行情自动止盈止损 + CLI
  - 仓库版：SANDBOX=1 环境变量激活（测试/验证用）+ 喂价 seed_price + 内存兼容字段

启用方式（任一）：
  1. 环境变量 SANDBOX=1
  2. 标记文件 strategies/config/SANDBOX_MODE 存在（旧用法）

行为：
  - 所有下单 API 调用被拦截，写入本地沙盘状态文件 strategies/config/sandbox_state.json
  - 行情数据（k线、ticker）照常从币安读取不拦截
  - 止盈止损根据真实价格计算，check_positions() 自动平仓

CLI:
  python scripts/sandbox.py --enable    # 启用并重置
  python scripts/sandbox.py --disable   # 关闭
  python scripts/sandbox.py --status    # 查看概要
"""
import json, time, os, logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger('sandbox')

_BASE = Path(__file__).resolve().parent.parent          # trading_engine
CONFIG_DIR = _BASE / 'strategies/config'
SANDBOX_FILE = CONFIG_DIR / 'SANDBOX_MODE'
STATE_FILE = CONFIG_DIR / 'sandbox_state.json'

DEFAULT_BALANCE = 500.0


def is_active() -> bool:
    """沙盘是否启用：环境变量优先，其次标记文件。"""
    if os.environ.get('SANDBOX', '').strip() in ('1', 'true', 'TRUE'):
        return True
    try:
        return SANDBOX_FILE.exists()
    except Exception:
        return False


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {'positions': [], 'algo_orders': [],
            'balance': DEFAULT_BALANCE, 'total_pnl': 0.0, 'trade_count': 0}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  价格（真实行情 + 测试喂价）
# ════════════════════════════════════════════════════════════

_PRICE_CACHE: dict = {}   # symbol -> {'price': float, 'time': timestamp}
_PRICE_CACHE_EXPIRY = 10  # seconds


def seed_price(symbol: str, price: float):
    """喂价：测试/验证用，覆盖沙盘持仓与成交价格，避免请求真实行情。"""
    _PRICE_CACHE[symbol] = {'price': float(price), 'time': time.time() + _PRICE_CACHE_EXPIRY}


def _get_current_price(symbol: str) -> Optional[float]:
    """获取当前价格（优先缓存，减少 API 调用）"""
    cached = _PRICE_CACHE.get(symbol)
    if cached and time.time() - cached['time'] < _PRICE_CACHE_EXPIRY:
        return cached['price']
    try:
        import urllib.request
        url = f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}'
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            price = float(data.get('price', 0))
            _PRICE_CACHE[symbol] = {'price': price, 'time': time.time()}
            return price
    except Exception:
        return cached['price'] if cached else None


# ════════════════════════════════════════════════════════════
#  Mock API Responses（模拟币安下单接口）
# ════════════════════════════════════════════════════════════

_mock_order_id = 10000000


def mock_post_order(params: dict) -> dict:
    """模拟 fapi/v1/order POST"""
    global _mock_order_id
    if not is_active():
        return None  # 不拦截
    _mock_order_id += 1
    state = _load_state()
    symbol = params.get('symbol', '')
    side = params.get('side', 'BUY')
    order_type = params.get('type', 'MARKET')
    quantity = float(params.get('quantity', 0))
    price = float(params.get('stopPrice', 0))
    reduce = params.get('reduceOnly', 'false')
    leverage = int(params.get('leverage', 0)) or 3

    now_ms = int(time.time() * 1000)
    order_id = _mock_order_id

    # 市价开仓
    if order_type == 'MARKET' and reduce == 'false':
        # 获取当前价格模拟成交
        cur_price = _get_current_price(symbol)
        if not cur_price or cur_price <= 0:
            cur_price = float(price) if price > 0 else 1.0
        pos_side = 'LONG' if side == 'BUY' else 'SHORT'

        pos = {
            'symbol': symbol, 'side': pos_side, 'entryPrice': cur_price,
            'positionAmt': quantity if side == 'BUY' else -quantity,
            'leverage': leverage,
            'unRealizedProfit': 0, 'markPrice': cur_price,
            'open_time': time.time(),
            'update_time': time.time(),
            'mock': True, 'sandbox_order_id': order_id,
        }
        state['positions'].append(pos)
        _log.info(f'[沙盘] 开仓 {symbol} {pos_side} qty={quantity} @{cur_price}')
        _save_state(state)
        return {
            'orderId': order_id, 'symbol': symbol, 'status': 'FILLED',
            'executedQty': str(quantity), 'cumQuote': str(quantity * cur_price),
            'avgPrice': str(cur_price),
        }

    # 止损单（支持 PM algoOrder 格式）
    if order_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'CONDITIONAL'):
        # PM 用 triggerPrice，直接下单用 stopPrice
        trigger = float(params.get('triggerPrice', params.get('stopPrice', 0)))
        algo_type = params.get('algoType', '')
        _log.info(f'[沙盘] 止损单 {order_type} {symbol} trigger={trigger} reduceOnly={reduce}')
        state.setdefault('algo_orders', []).append({
            'orderId': order_id,
            'algoId': order_id,
            'symbol': symbol,
            'type': order_type,
            'algoType': algo_type,
            'triggerPrice': trigger,
            'stopPrice': trigger,
            'reduceOnly': reduce == 'true',
        })
        _save_state(state)
        # PM 期待的返回格式
        if algo_type == 'CONDITIONAL':
            return {
                'algoId': order_id, 'symbol': symbol, 'status': 'NEW',
                'triggerPrice': str(trigger),
            }
        return {
            'orderId': order_id, 'symbol': symbol, 'status': 'NEW',
            'stopPrice': str(trigger),
        }

    # 止损限价单
    if order_type == 'STOP':
        _log.info(f'[沙盘] 挂单 {order_type} {symbol} stopPrice={price}')
        return {
            'orderId': order_id, 'symbol': symbol, 'status': 'NEW',
            'stopPrice': str(price),
        }

    return {'orderId': order_id, 'symbol': symbol, 'status': 'NEW'}


def mock_cancel_order(symbol: str, order_id: int = None) -> dict:
    """模拟取消订单"""
    if not is_active():
        return None
    _log.info(f'[沙盘] 撤单 {symbol} orderId={order_id}')
    state = _load_state()
    state['algo_orders'] = [o for o in state.get('algo_orders', [])
                            if o.get('orderId') != order_id]
    _save_state(state)
    return {'orderId': order_id, 'status': 'CANCELED', 'code': 0, 'symbol': symbol}


def mock_get_position_risk(symbol: str = None) -> list:
    """模拟 /fapi/v2/positionRisk — 返回沙盘持仓"""
    if not is_active():
        return []
    state = _load_state()
    results = []
    syms = [symbol] if symbol else list(set(
        p.get('symbol', '') for p in state['positions']))
    for sym in syms:
        matching = [p for p in state['positions']
                    if p.get('symbol') == sym and
                    abs(float(p.get('positionAmt', 0))) > 0.0001]
        if not matching:
            continue
        # 合并同 symbol 持仓
        total_amt = sum(float(p.get('positionAmt', 0)) for p in matching)
        if abs(total_amt) < 0.0001:
            continue
        side = 'LONG' if total_amt > 0 else 'SHORT'
        # 加权平均入场价
        weighted_entry = sum(float(p.get('entryPrice', 0)) *
                             abs(float(p.get('positionAmt', 0)))
                             for p in matching) / abs(total_amt)
        leverage = int(matching[0].get('leverage', 3)) or 3
        results.append({
            'symbol': sym, 'positionAmt': str(total_amt),
            'entryPrice': str(weighted_entry), 'markPrice': str(_get_current_price(sym)),
            'leverage': str(leverage), 'unRealizedProfit': '0',
            'positionInitialMargin': str(round(abs(total_amt) * weighted_entry / leverage, 2)),
            'initialMargin': str(round(abs(total_amt) * weighted_entry / leverage, 2)),
            'isolated': 'false', 'positionSide': side,
        })
    return results


def mock_get_account() -> dict:
    """模拟 /fapi/v2/account"""
    if not is_active():
        return {}
    state = _load_state()
    positions = mock_get_position_risk()
    return {
        'totalWalletBalance': str(state.get('balance', DEFAULT_BALANCE)),
        'totalUnrealizedProfit': '0',
        'totalPositionInitialMargin': '0',
        'assets': [{'asset': 'USDT',
                    'walletBalance': str(state.get('balance', DEFAULT_BALANCE)),
                    'unrealizedProfit': '0',
                    'availableBalance': str(state.get('balance', DEFAULT_BALANCE))}],
        'positions': positions,
        'canTrade': True,
    }


def mock_get_open_orders(symbol: str = None) -> list:
    """模拟 /fapi/v1/openOrders — 返回沙盘挂单（含未触发的条件单）"""
    if not is_active():
        return []
    state = _load_state()
    orders = []
    for o in state.get('algo_orders', []):
        if symbol and o.get('symbol') != symbol:
            continue
        orders.append({
            'orderId': o['orderId'], 'symbol': o['symbol'],
            'status': 'NEW', 'type': o['type'],
            'stopPrice': str(o['stopPrice']),
            'reduceOnly': 'true' if o.get('reduceOnly') else 'false',
        })
    return orders


# ════════════════════════════════════════════════════════════
#  沙盘引擎：价格检查 + 自动平仓
# ════════════════════════════════════════════════════════════

def check_positions() -> list:
    """沙盘主循环：检查每个持仓的止盈止损，返回需要关闭的持仓"""
    if not is_active():
        return []
    state = _load_state()
    to_close = []

    for pos in state['positions'][:]:
        sym = pos.get('symbol', '')
        cur_price = _get_current_price(sym)
        if not cur_price:
            continue

        entry = float(pos.get('entryPrice', 0))
        amt = float(pos.get('positionAmt', 0))
        if abs(amt) < 0.0001 or entry <= 0:
            continue
        side = pos.get('side', 'LONG' if amt > 0 else 'SHORT')
        pnl_pct = (cur_price - entry) / entry * 100
        if side == 'SHORT':
            pnl_pct = -pnl_pct

        # 检查该持仓挂出的条件单是否触发
        for o in [x for x in state.get('algo_orders', []) if x.get('symbol') == sym]:
            otype = o.get('type', '')
            stop_price = float(o.get('stopPrice', 0))
            if side == 'LONG' and otype in ('STOP_MARKET', 'STOP'):
                if cur_price <= stop_price:
                    to_close.append({'symbol': sym, 'reason': '止损', 'pnl_pct': pnl_pct,
                                     'entry': entry, 'exit': cur_price, 'qty': amt})
                    break
            elif side == 'SHORT' and otype in ('STOP_MARKET', 'STOP'):
                if cur_price >= stop_price:
                    to_close.append({'symbol': sym, 'reason': '止损', 'pnl_pct': pnl_pct,
                                     'entry': entry, 'exit': cur_price, 'qty': amt})
                    break

    # 执行平仓
    for close in to_close:
        _close_position(close['symbol'])
        _log.info(f'[沙盘] 平仓 {close["symbol"]} pnl={close["pnl_pct"]:.2f}%')

    return to_close


def _close_position(symbol: str):
    """沙盘平仓：从状态中移除持仓，累加 PnL"""
    state = _load_state()
    new_positions = []
    for pos in state['positions']:
        if pos.get('symbol') != symbol:
            new_positions.append(pos)
            continue
        # 计算 PnL
        entry = float(pos.get('entryPrice', 0))
        amt = float(pos.get('positionAmt', 0))
        cur_price = _get_current_price(symbol) or entry
        side = pos.get('side', 'LONG' if amt > 0 else 'SHORT')
        if side == 'LONG':
            pnl = (cur_price - entry) * amt
        else:
            pnl = (entry - cur_price) * abs(amt)
        state['total_pnl'] = round(state.get('total_pnl', 0) + pnl, 2)
        state['balance'] = round(state.get('balance', 0) + pnl, 2)
        state['trade_count'] = state.get('trade_count', 0) + 1
    state['positions'] = new_positions
    state['algo_orders'] = [o for o in state.get('algo_orders', [])
                            if o.get('symbol') != symbol]
    _save_state(state)


def reset(balance: float = DEFAULT_BALANCE):
    """重置沙盘状态（可选指定初始余额）。"""
    _PRICE_CACHE.clear()
    _save_state({'positions': [], 'algo_orders': [],
                 'balance': balance, 'total_pnl': 0.0, 'trade_count': 0})
    if is_active():
        _log.info('[沙盘] 重置状态')


def summary() -> str:
    """沙盘概要"""
    if not is_active():
        return '❌ 沙盘模式未启用'
    state = _load_state()
    positions = state.get('positions', [])
    lines = [
        f'📊 **沙盘状态**',
        f'余额: ${state.get("balance", 0):.2f}',
        f'总 PnL: ${state.get("total_pnl", 0):.2f}',
        f'开仓数: {len(positions)}',
        f'总交易次数: {state.get("trade_count", 0)}',
    ]
    for pos in positions:
        sym = pos.get('symbol', '')
        side = pos.get('side', '')
        entry = float(pos.get('entryPrice', 0))
        amt = float(pos.get('positionAmt', 0))
        cur = _get_current_price(sym) or entry
        pnl_pct = (cur - entry) / entry * 100
        if side == 'SHORT':
            pnl_pct = -pnl_pct
        lines.append(f'  {sym} {side} qty={abs(amt):.4f} entry={entry:.4f} pnl={pnl_pct:.1f}%')
    return '\n'.join(lines)


# ── 快速测试 ──
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    import sys
    if '--status' in sys.argv:
        print(summary())
    elif '--enable' in sys.argv:
        SANDBOX_FILE.write_text('enabled')
        reset()
        print('✅ 沙盘模式已启用，状态已重置')
    elif '--disable' in sys.argv:
        if SANDBOX_FILE.exists():
            SANDBOX_FILE.unlink()
        print('✅ 沙盘模式已关闭')
    else:
        print(summary())
