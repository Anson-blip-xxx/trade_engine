"""
Shared executor base — 供 S6 (Long) / S8 (Short) 复用
职责: S3 事件读取、去重、状态管理、PM 集成、告警
"""
import json, time, threading, sys, requests, hashlib, hmac, os
from urllib.parse import urlencode
from pathlib import Path
from typing import Optional

TRADE_DIR = Path('/root/.openclaw/trade')
CONFIG_DIR = TRADE_DIR / 'trading_engine/strategies/config'
LOG_DIR = TRADE_DIR / 'trading_engine/logs'

sys.path.insert(0, str(TRADE_DIR / 'trading_engine'))
sys.path.insert(0, str(TRADE_DIR))
from shared.position_manager import monitor_all, close_position, _algo_enqueue, _algo_start_worker
from shared.redis_store import get as _rget, set as _rset

# ── Telegram ──
# 从 binance.env 加载 TG 配置 + API 密钥
_CONFIG_ENV = Path('/root/.openclaw/trade/trading_engine/config/binance.env')
_TG_TOKEN = ''
_TG_CHAT_ID = 0
_API_KEY = ''
_API_SECRET = ''


if _CONFIG_ENV.exists():
    for line in _CONFIG_ENV.read_text().splitlines():
        if '=' in line:
            k, v = line.strip().split('=', 1)
            if k == 'TG_NOTIFY_TOKEN': _TG_TOKEN = v
            elif k == 'TG_NOTIFY_CHAT_ID': _TG_CHAT_ID = int(v)
            elif k == 'BINANCE_API_KEY': _API_KEY = v
            elif k == 'BINANCE_API_SECRET': _API_SECRET = v



def tg_send(text: str) -> Optional[int]:
    """发送 Telegram 通知（HTML 模式），返回 message_id 或 None"""
    if not _TG_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code == 200:
            return r.json().get('result', {}).get('message_id')
    except Exception:
        pass
    return None


def tg_pin(message_id: int):
    """置顶一条消息"""
    if not _TG_TOKEN or not message_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{_TG_TOKEN}/pinChatMessage",
            json={"chat_id": _TG_CHAT_ID, "message_id": message_id, "disable_notification": True}, timeout=5)
    except Exception:
        pass

# ── 最小名义价值缓存 ──
_MIN_NOTIONAL_CACHE: dict = {}
def _get_funding_rate(symbol: str) -> float:
    """获取当前资金费率，失败返回 0。极值：-0.001 ~ 0.001"""
    try:
        import requests
        r = requests.get(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}', timeout=5)
        return float(r.json().get('lastFundingRate', 0))
    except Exception:
        return 0.0


def _get_min_notional(symbol: str) -> float:
    """获取最小名义价值"""
    if symbol in _MIN_NOTIONAL_CACHE:
        return _MIN_NOTIONAL_CACHE[symbol]
    try:
        info = fapi_get('/fapi/v1/exchangeInfo')
        if isinstance(info, dict):
            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    for f in s['filters']:
                        if f['filterType'] == 'MIN_NOTIONAL':
                            val = float(f.get('notional', f.get('minNotional', 5)))
                            _MIN_NOTIONAL_CACHE[symbol] = val
                            return val
    except Exception:
        pass
    return 5.0  # 默认最小 5 USDT

# ── 沙盘模式 ──
_SANDBOX_ACTIVE = None

def _sandbox_check():
    global _SANDBOX_ACTIVE
    if _SANDBOX_ACTIVE is not None:
        return _SANDBOX_ACTIVE
    p = Path('/root/.openclaw/trade/trading_engine/strategies/config/SANDBOX_MODE')
    _SANDBOX_ACTIVE = p.exists()
    return _SANDBOX_ACTIVE

def _sandbox_post(path: str, params: dict) -> Optional[dict]:
    """拦截 fapi_post，对 ORDER 操作转向沙盘"""
    if not _sandbox_check():
        return None
    if 'order' not in path.lower():
        return None
    try:
        from scripts.sandbox import mock_post_order
        return mock_post_order(params)
    except ImportError:
        return None

def _sandbox_get(path: str, params: dict = None) -> Optional[dict | list]:
    """拦截 fapi_get，对持仓/账户查询转向沙盘"""
    if not _sandbox_check():
        return None
    low_path = path.lower().replace('_', '').replace('-', '')
    if 'positionrisk' in low_path:
        from scripts.sandbox import mock_get_position_risk
        sym = (params or {}).get('symbol', None)
        return mock_get_position_risk(sym)
    if 'account' in low_path and 'trade' not in low_path:
        from scripts.sandbox import mock_get_account
        return mock_get_account()
    return None

def _fapi_sig(params: dict) -> str:
    """签名（与旧版 s6_auto_trader.py 一致）"""
    query = urlencode(params)
    return hmac.new(_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()

def fapi_get(path: str, params: dict = None) -> Optional[dict | list]:
    # ═══ 沙盘拦截 ═══
    sb = _sandbox_get(path, params)
    if sb is not None:
        return sb
    base = 'https://fapi.binance.com'
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = _fapi_sig(params)
    headers = {'X-MBX-APIKEY': _API_KEY}
    try:
        r = requests.get(f'{base}{path}', params=params, headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def fapi_post(path: str, params: dict) -> Optional[dict]:
    # ═══ 沙盘拦截 ═══
    sb = _sandbox_post(path, params)
    if sb is not None:
        return sb
    base = 'https://fapi.binance.com'
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = _fapi_sig(params)
    headers = {'X-MBX-APIKEY': _API_KEY}
    try:
        r = requests.post(f'{base}{path}', params=params, headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

# ── 日志 ──
def _log(name: str, msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [{name}] {msg}'
    print(line, flush=True)
    try:
        d = LOG_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{time.strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass

# ── S3 事件读取 ──
S3_STALE_S = 90  # 事件超过 90s 视为过期

def read_s3_events(max_age: int = S3_STALE_S) -> list:
    """读取 s3 事件数据, 返回有效事件列表"""
    try:
        data = _rget('event:s3')
        if not data:
            return []
        now = time.time()
        if now - data.get('ts', 0) > max_age:
            return []
        return data.get('events', [])
    except Exception:
        return []

def read_s3_market_data(max_age: int = 120) -> dict:
    """读取 s3 市场数据, 返回各币窗口数据"""
    try:
        data = _rget('market:s3_data')
        if not data:
            return {}
        now = time.time()
        if now - data.get('ts', 0) > max_age:
            return {}
        return data.get('symbols', {})
    except Exception:
        return {}

# ── 事件去重 ──
_event_history: dict = {}  # {(symbol, type): timestamp}

def is_event_fresh(symbol: str, event_type: str, cooldown_s: int = 120) -> bool:
    """同一标的同类型事件是否在冷却期内"""
    key = (symbol, event_type)
    now = time.time()
    last = _event_history.get(key, 0)
    if now - last < cooldown_s:
        return False
    _event_history[key] = now
    return True

# ── 仓位管理 ──
_KEY_MAP_OVERRIDE = {
    'S6': 'state:s6',
    'S8': 'state:s8',
}

def load_state(name: str) -> dict:
    key = _KEY_MAP_OVERRIDE.get(name, f'state:{name.lower()}')
    try:
        data = _rget(key)
        if data:
            return data
    except Exception:
        pass
    return {'positions': {}, 'cooldowns': {}}

def save_state(name: str, state: dict):
    key = _KEY_MAP_OVERRIDE.get(name, f'state:{name.lower()}')
    try:
        _rset(key, state)
    except Exception as e:
        _log(name, f'Save state failed: {e}')

def reconcile_positions(name: str, state: dict) -> dict:
    """对比交易所实际持仓 vs state，清鬼魂 + 补充止损单"""
    try:
        acct = fapi_get('/fapi/v2/account')
        if not isinstance(acct, dict):
            return state
        actual_syms = {p['symbol'] for p in acct.get('positions', [])
                       if abs(float(p.get('positionAmt', 0))) > 0}
        for sym in list(state.get('positions', {}).keys()):
            if sym not in actual_syms:
                _log(name, f'[对账] {sym} state有仓但交易所无持仓，清幽灵')
                del state['positions'][sym]
                save_state(name, state)
            else:
                # 已有持仓但可能缺少止损单 → 补充入队
                pos = state['positions'].get(sym, {})
                sl = pos.get('stop')
                side = pos.get('side', 'LONG')
                if sl and sl > 0:
                    close_side = 'BUY' if side == 'SHORT' else 'SELL'
                    qty = pos.get('qty', 0)
                    if qty > 0:
                        try:
                            _algo_enqueue(sym, close_side, sl, qty)
                            _log(name, f'[补止损] {sym} 止损{sl} 已入队')
                        except Exception as e:
                            _log(name, f'[补止损异常] {sym}: {e}')
    except Exception as e:
        _log(name, f'[对账异常] {e}')
    return state

# 确保 AlgoWorker 已启动（导入后只启动一次）
_algo_start_worker()

def pm_monitor(name: str, state: dict, tg_fn: callable = None) -> dict:
    """PM 监控，返回已平仓列表"""
    if not state.get('positions'):
        return state, []

    closed = monitor_all(system_filter=name)
    closed_list = []
    for symbol, reason, close_price in closed:
        pos = state['positions'].pop(symbol, None)
        if not pos:
            continue
        entry = pos['entry']
        qty = pos.get('qty', 0)
        side_mult = -1 if pos.get('side') == 'SHORT' else 1
        pnl_pct = ((close_price - entry) / entry * 100) * side_mult
        pnl_usdt = (close_price - entry) * qty * side_mult
        _log(name, f'平仓 {symbol} PnL: {pnl_pct:+.1f}% ({pnl_usdt:+.2f}U) 原因={reason}')
        # 不在这里发 TG——record_trade 已经发完整通知（含胜率/周期盈亏等统计）
        # 任何平仓后都加冷静期（不管盈亏），防止刚平又开
        state.setdefault('cooldowns', {})[symbol] = time.time() + 7200  # 2h cooldown
        if pnl_pct < 0:
            state['cooldowns'][symbol] = time.time() + 14400  # 亏损 → 4h 更长冷静
        save_state(name, state)
        closed_list.append((symbol, reason, pnl_pct))
    return state, closed_list

# ── 动态仓位计算 ────────────────────────────────────────────────────────
_POOL_BUDGET = 0.80          # 账户总余额最高使用比例
_POSITION_MIN_PCT = 0.03     # 单仓最低占可用池比例
_POSITION_MAX_PCT = 0.15     # 单仓最高占可用池比例
_POSITION_MIN_USDT = 10      # 单仓最低 USDT 名义价值


def score_to_fraction(score: float) -> float:
    """信号评分 → 资金池分配比例（3%~15%）"""
    return min(_POSITION_MAX_PCT, max(_POSITION_MIN_PCT, score / 100 * _POSITION_MAX_PCT))


def _get_balance() -> float:
    """获取 USDT 账户余额（总余额，非可用）"""
    try:
        acct = fapi_get('/fapi/v2/account')
        if acct and isinstance(acct, dict):
            for a in acct.get('assets', []):
                if a.get('asset') == 'USDT':
                    return float(a.get('walletBalance', 0))
    except Exception:
        pass
    return 0


def _calc_used_margin(state: dict) -> float:
    """从 Binance 实际持仓计算已占用保证金（不依赖 state 文件，防止 state 被清空时误算）"""
    try:
        acct = fapi_get('/fapi/v2/account')
        if not acct or not isinstance(acct, dict):
            return 0.0
        used = 0.0
        for p in acct.get('positions', []):
            amt = abs(float(p.get('positionAmt', 0)))
            if amt < 0.001:
                continue
            # 优先用实际保证金值，次选估算
            mm = float(p.get('positionInitialMargin', 0))
            if mm > 0:
                used += mm
            else:
                entry = float(p.get('entryPrice', 0))
                lev = int(p.get('leverage', 3)) or 1
                if entry > 0:
                    used += entry * amt / lev
        return used
    except Exception:
        return 0.0


def calc_position_qty(name: str, state: dict, symbol: str, price: float,
                      event_type: str, strength: int, leverage: int) -> float:
    """动态仓位计算：
       1. 取账户余额 × 80% = 资金池
       2. 减去已有持仓占用 = 可用池
       3. 信号评分 → 分配比例（3%~15%）
       4. qty = 分配额 / price * leverage
    """
    balance = _get_balance()
    pool = balance * _POOL_BUDGET
    used = _calc_used_margin(state)
    remaining = max(0, pool - used)
    alloc_pct = score_to_fraction(strength)
    position_usdt = remaining * alloc_pct

    # 最小名义价值保护
    position_usdt = max(position_usdt, _POSITION_MIN_USDT)

    qty = position_usdt / price * leverage
    _log(name, f'{symbol} 余额={balance:.0f} 池={pool:.0f} 已用={used:.0f} 可用={remaining:.0f} 分配={alloc_pct:.0%} → ${position_usdt:.0f}')
    return qty


# ── 开仓工具 ──
def open_position(name: str, symbol: str, side: str, entry_price: float,
                  stop_price: float, qty: float, margin_mode: str,
                  leverage: int, event_type: str, strength: int,
                  tg_fn: callable = None) -> bool:
    """通过 Binance API + PM 开仓"""
    # ── 全局暂停开仓（PAUSE_OPEN 文件存在时跳过所有开仓） ──
    _pause_file = Path(__file__).parent / 'config/PAUSE_OPEN'
    if _pause_file.exists():
        tg_fn and tg_fn(f'⏸ 全局暂停开仓中，{symbol} 跳过')
        return False
    try:
        # 检查最小名义价值
        min_notional = _get_min_notional(symbol)
        notional = entry_price * qty
        if min_notional and notional < min_notional:
            # 调整数量到最小名义价值
            qty = min_notional / entry_price
            _log(name, f'{symbol} 调整数量到最小名义价值 {min_notional} USDT')
            qty = _round_qty(symbol, qty)

        # 检查资金费率
        fund_rate = _get_funding_rate(symbol)
        if side == 'SHORT' and fund_rate < -0.001:  # 负费率 = 空头付钱，极值跳过
            _log(name, f'{symbol} 跳过开空：资金费率 {fund_rate:.4%} 对空头不利')
            msg = (f'⏭️ 跳过 {symbol} SHORT\n'
                   f'原因: 资金费率 {fund_rate:.4%}（空头付钱）\n'
                   f'信号: {event_type}')
            if tg_fn:
                tg_fn(msg)
            return False
        if side == 'LONG' and fund_rate > 0.001:  # 正费率 = 多头付钱，极值跳过
            _log(name, f'{symbol} 跳过开多：资金费率 {fund_rate:.4%} 对多头不利')
            msg = (f'⏭️ 跳过 {symbol} LONG\n'
                   f'原因: 资金费率 {fund_rate:.4%}（多头付钱）\n'
                   f'信号: {event_type}')
            if tg_fn:
                tg_fn(msg)
            return False

        # 设置杠杆和保证金模式
        fapi_post('/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
        if margin_mode == 'ISOLATED':
            fapi_post('/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'ISOLATED'})
        else:
            fapi_post('/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'CROSSED'})

        # 开仓（使用 RESULT 模式直接获取成交结果）
        qty = _round_qty(symbol, qty)
        order_side = 'SELL' if side == 'SHORT' else 'BUY'
        result = fapi_post('/fapi/v1/order', {
            'symbol': symbol,
            'side': order_side,
            'type': 'MARKET',
            'quantity': qty,
            'newOrderRespType': 'RESULT',
        })
        if not result or result.get('code'):
            _log(name, f'开仓失败 {symbol}: {result}')
            return False

        # 解析成交结果
        status = result.get('status', 'NEW')
        filled_qty = abs(float(result.get('executedQty', 0)))
        cum_qty = abs(float(result.get('cumQty', filled_qty)))
        avg_price_str = result.get('avgPrice', '0')
        avg_price = float(avg_price_str) if avg_price_str and float(avg_price_str) > 0 else entry_price

        # 未成交：MARKET 单可能排隊，记录但不视为失败
        if status == 'NEW' and filled_qty == 0:
            _log(name, f'{symbol} 开仓挂单中 (orderId={result["orderId"]})，PM 将自动追踪')
            # 仍然发送通知
            msg = (f'{name} 开仓 {symbol} (挂单中)\n'
                   f'方向: {side} | 类型: {margin_mode}\n'
                   f'入场: {entry_price:.4f} | 开单量: {qty}\n'
                   f'止损: {stop_price:.4f}\n'
                   f'信号: {event_type}(str={strength})\n'
                   f'杠杆: {leverage}x | 费率: {fund_rate:.4%}')
            if tg_fn:
                tg_fn(msg)
            _log(name, msg)
            return True

        # 成交量为 0 且不是挂单中 → 失败
        if filled_qty < 0.01 and cum_qty < 0.01:
            _log(name, f'{symbol} 开仓成交量为 0 (status={status}), 无法确认开仓')
            # 取消空订单
            if status == 'NEW' and result.get('orderId'):
                fapi_post('/fapi/v1/cancelOrder', {
                    'symbol': symbol,
                    'orderId': result['orderId']
                })
            return False

        # 部分成交：记录但接受
        if filled_qty < qty * 0.5:
            _log(name, f'{symbol} 部分成交 {filled_qty}/{qty} (status={status})')
            # 取消剩余部分
            if result.get('orderId'):
                fapi_post('/fapi/v1/cancelOrder', {
                    'symbol': symbol,
                    'orderId': result['orderId']
                })

        # 开仓成功 → 通知
        msg = (f'{name} 开仓 {symbol}\n'
               f'方向: {side} | 类型: {margin_mode}\n'
               f'入场: {avg_price:.4f} | 数量: {filled_qty:.2f}'
               f'{"(部分)" if filled_qty < qty * 0.95 else ""}\n'
               f'止损: {stop_price:.4f}\n'
               f'信号: {event_type}(str={strength})\n'
               f'杠杆: {leverage}x | 费率: {fund_rate:.4%}')
        if tg_fn:
            tg_fn(msg)
        _log(name, msg)

        # 挂止损单（Algo SL）
        if stop_price > 0:
            try:
                close_side = 'BUY' if side == 'SHORT' else 'SELL'
                _algo_enqueue(symbol, close_side, stop_price, filled_qty)
                _log(name, f'{symbol} 止损单已入队 ({stop_price})')
            except Exception as e:
                _log(name, f'{symbol} 止损入队失败: {e}')

        return True

    except Exception as e:
        _log(name, f'开仓异常 {symbol}: {e}')
        return False

def _round_qty(symbol: str, qty: float) -> float:
    """按交易所精度舍入数量"""
    try:
        info = fapi_get('/fapi/v1/exchangeInfo')
        if isinstance(info, dict):
            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    for f in s['filters']:
                        if f['filterType'] == 'LOT_SIZE':
                            step = float(f['stepSize'])
                            step_str = str(step).rstrip('0')
                            decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
                            return round(qty - (qty % step), decimals)
    except Exception:
        pass
    return qty

# ── 市场状态检查 ──
def get_market_state() -> dict:
    try:
        data = _rget('market:s0')
        if data:
            return data
    except Exception:
        pass
    return {}

def market_allows_trading(name: str, side: str) -> bool:
    """检查 S0 市场状态是否允许开仓"""
    ms = get_market_state()
    mode = ms.get('market_mode', 'normal')
    if mode == 'risk_off':
        _log(name, '[S0] 市场处于 risk_off 模式，跳过开仓')
        return False
    return True
