"""
position_manager.py — 统一持仓生命周期管理
职责：开仓、监控（硬止损/移成本/追踪锁利/时间止损）、平仓
各系统（S6/S8A/S8B）只负责「过滤信号→设置开仓参数」，后续全由 PM 管理。

日志： logs/position_manager/YYYYMMDD.log
状态： config/pm_state.json

同步：平仓/状态变更时自动回写各系统 legacy state（过渡兼容）

✅ 2026-07-18: 接入 Algo Order API（/fapi/v1/algoOrder）下止损单，
   轮询止损保留作为兜底。
✅ 2026-07-20: Algo 限速改为队列消费模式，后台线程以 11s 间隔依次处理。
"""

import time, json, threading
from pathlib import Path
from shared.redis_store import get as _rget, set as _rset

_BASE       = Path(__file__).parent.parent
_LOG_DIR    = _BASE / 'logs/position_manager'

# ── 沙盘模式 ──
try:
    from scripts.sandbox import is_active as _sandbox_active
    from scripts.sandbox import mock_post_order, mock_cancel_order
    from scripts.sandbox import mock_get_position_risk, mock_get_open_orders, mock_get_account
    _HAS_SANDBOX = True
except ImportError:
    _HAS_SANDBOX = False
    def _sandbox_active(): return False
    def mock_post_order(*a, **kw): return None
    def mock_cancel_order(*a, **kw): return None
    def mock_get_position_risk(*a, **kw): return []
    def mock_get_open_orders(*a, **kw): return []
    def mock_get_account(*a, **kw): return {}


# ── 轻量 API 工具（避免依赖 s6_auto_trader 的深模块链） ──
_API_KEY = None
_API_SECRET = None
def _ensure_apikey():
    global _API_KEY, _API_SECRET
    if _API_KEY is None:
        env_path = _BASE / 'config/binance.env'
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    if k.strip() == 'BINANCE_API_KEY':
                        _API_KEY = v.strip()
                    elif k.strip() in ('BINANCE_SECRET_KEY', 'BINANCE_API_SECRET'):
                        _API_SECRET = v.strip()

def _light_fapi_post(path: str, params: dict) -> dict | None:
    """简易 fapi_post（仅用于 AlgoSL 下单，不依赖 s6_auto_trader）"""
    # ═══ 沙盘拦截 ═══
    if _sandbox_active():
        if 'order' in path.lower() or 'algo' in path.lower():
            mock = mock_post_order(params)
            if mock:
                return mock
    import requests, hmac, hashlib
    from urllib.parse import urlencode
    _ensure_apikey()
    if not _API_KEY or not _API_SECRET:
        return None
    params['timestamp'] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    params['signature'] = sig
    try:
        r = requests.post(f'https://fapi.binance.com{path}', params=params,
                          headers={'X-MBX-APIKEY': _API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else r.json()
    except Exception as e:
        _pmlog(f'[_light_fapi_post 异常] {path}: {e}')
        return None

def _light_fapi_get(path: str, params: dict = None) -> dict | list | None:
    """简易 fapi_get（不依赖 s6_auto_trader）"""
    # ═══ 沙盘拦截 ═══
    if _sandbox_active():
        if 'positionrisk' in path.lower() or 'position_risk' in path.lower():
            sym = (params or {}).get('symbol', None)
            return mock_get_position_risk(sym)
        if 'account' in path.lower() and not 'trade' in path.lower():
            return mock_get_account()
        if 'openorders' in path.lower().replace(' ', ''):
            sym = (params or {}).get('symbol', None)
            return mock_get_open_orders(sym)
    import requests, hmac, hashlib
    from urllib.parse import urlencode
    _ensure_apikey()
    if not _API_KEY or not _API_SECRET:
        return None
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    params['signature'] = sig
    try:
        r = requests.get(f'https://fapi.binance.com{path}', params=params,
                         headers={'X-MBX-APIKEY': _API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else r.json()
    except Exception as e:
        _pmlog(f'[_light_fapi_get 异常] {path}: {e}')
        return None


def _light_fapi_delete(path: str, params: dict = None) -> dict | None:
    """简易 fapi_delete（不依赖 s6_auto_trader）"""
    # ═══ 沙盘拦截 ═══
    if _sandbox_active():
        if 'order' in path.lower() or 'algo' in path.lower():
            sym = (params or {}).get('symbol', '')
            oid = (params or {}).get('orderId', (params or {}).get('algoId', 0))
            mock = mock_cancel_order(sym, oid)
            if mock:
                return mock
    import requests, hmac, hashlib
    from urllib.parse import urlencode
    _ensure_apikey()
    if not _API_KEY or not _API_SECRET:
        return None
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    params['signature'] = sig
    try:
        r = requests.delete(f'https://fapi.binance.com{path}', params=params,
                            headers={'X-MBX-APIKEY': _API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else r.json()
    except Exception as e:
        _pmlog(f'[_light_fapi_delete 异常] {path}: {e}')
        return None


def _light_get_price(symbol: str) -> float:
    """轻量获取最新价（公开 ticker API，无需签名）"""
    import requests as _req
    try:
        r = _req.get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}', timeout=5)
        return float(r.json()['price']) if r.status_code == 200 else 0.0
    except Exception:
        return 0.0


# 懒加载 data_cache（在追踪锁利趋势感知中使用，避免影响 PM 启动）
_data_cache_mod = None
def _get_data_cache():
    global _data_cache_mod
    if _data_cache_mod is None:
        import importlib
        _data_cache_mod = importlib.import_module('shared.data_cache')
    return _data_cache_mod

_S6_API = None

# API 限速保护：每个标的最近一次修改止损的时间戳
_last_api_call = {}  # {symbol: timestamp}
_API_COOLDOWN = 3    # 同标的API调用最小间隔（秒）

# Algo Order API 限速：队列消费，后台线程以 11s 间隔依次处理
_ALGO_QUEUE = []           # [(symbol, side, trigger_price, qty), ...]
_ALGO_QUEUE_LOCK = threading.Lock()
_ALGO_WORKER_STARTED = False

def _algo_start_worker():
    """启动后台队列消费线程（只启动一次）"""
    global _ALGO_WORKER_STARTED
    if _ALGO_WORKER_STARTED:
        return
    _ALGO_WORKER_STARTED = True
    t = threading.Thread(target=_algo_worker_loop, daemon=True, name='algo-worker')
    t.start()
    _pmlog('[AlgoWorker] 后台队列消费线程已启动')

def _algo_worker_loop():
    """后台循环：每 11s 从队列取一个任务执行"""
    while True:
        task = None
        with _ALGO_QUEUE_LOCK:
            if _ALGO_QUEUE:
                task = _ALGO_QUEUE.pop(0)
        if task:
            symbol, side, trigger_price, qty = task
            try:
                _algo_place_sl_inner(symbol, side, trigger_price, qty)
            except Exception as e:
                _pmlog(f'[AlgoWorker异常] {symbol}: {e}')
            time.sleep(11)  # 限速间隔
        else:
            time.sleep(1)  # 队列空，1s 后再检查

def _algo_enqueue(symbol: str, side: str, trigger_price: float, qty: float):
    """将 Algo 止损任务加入队列"""
    with _ALGO_QUEUE_LOCK:
        _ALGO_QUEUE.append((symbol, side, trigger_price, qty))
    _pmlog(f'[AlgoEnqueue] {symbol} side={side} trigger={trigger_price} qty={qty} 已入队')

def _algo_place_sl_inner(symbol: str, side: str,
                          trigger_price: float, qty: float) -> dict:
    """
    实际调用 Binance API 下 Algo 条件止损单（无限速检查，由调用者保证）。
    使用轻量 API 调用，不依赖 s6_auto_trader。
    """
    try:
        # 按交易所精度舍入数量和价格（exchangeInfo 是公开 API）
        import requests as _req
        try:
            _ei = _req.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=10)
            if _ei.status_code == 200:
                _ei_data = _ei.json()
                for s in _ei_data.get('symbols', []):
                    if s['symbol'] == symbol:
                        for f in s['filters']:
                            if f['filterType'] == 'LOT_SIZE':
                                step = float(f['stepSize'])
                                qty = qty - (qty % step)
                                qty = round(qty, 8)
                            if f['filterType'] == 'PRICE_FILTER':
                                tick = float(f['tickSize'])
                                trigger_price = trigger_price - (trigger_price % tick)
                                trigger_price = round(trigger_price, 8)
                        break
        except Exception:
            pass

        # 下单前先取消该币所有活跃条件单（防止重启积累重复单）
        _cancel_all_algo(symbol)

        result = _light_fapi_post('/fapi/v1/algoOrder', {
            'symbol': symbol,
            'side': side,
            'positionSide': 'BOTH',
            'algoType': 'CONDITIONAL',
            'type': 'STOP_MARKET',
            'triggerPrice': trigger_price,
            'quantity': qty,
            'workingType': 'MARK_PRICE',
            'timeInForce': 'GTC',
            'reduceOnly': 'true',
        })
        if isinstance(result, dict) and 'algoId' in result:
            _pmlog(f'[AlgoSL成功] {symbol} 止损{trigger_price} id={result["algoId"]}')
            # 更新 PM 状态的 algo_sl_id（写入 JSON）
            try:
                positions = _load()
                if symbol in positions:
                    positions[symbol]['algo_sl_id'] = result['algoId']
                    _save(positions)
            except Exception:
                pass
            except Exception:
                pass
        else:
            _pmlog(f'[AlgoSL失败] {symbol}: {result}')
        return result
    except Exception as e:
        _pmlog(f'[AlgoSL异常] {symbol}: {e}')
        return {'error': str(e)}


def _algo_cancel(algo_id: int) -> dict:
    """取消一个条件单（DELETE）"""
    try:
        _, _, fapi_delete, _, _, _, _, _ = _s6api()
        result = fapi_delete('/fapi/v1/algoOrder', {'algoId': algo_id})
        return result
    except Exception as e:
        return {'error': str(e)}

def _cancel_all_algo(symbol: str):
    """取消一个币种的所有活跃条件单"""
    try:
        existing = _light_fapi_get('/fapi/v1/allAlgoOrders', {'symbol': symbol})
        if existing and isinstance(existing, list):
            for o in existing:
                if o.get('algoStatus') in ('NEW', 'WORKING', 'TRIGGERED'):
                    _aid = o['algoId']
                    _light_fapi_delete('/fapi/v1/algoOrder', {'symbol': symbol, 'algoId': _aid})
                    _pmlog(f'[批量取消] {symbol} algoId={_aid} trigger={o.get("triggerPrice")}')
    except Exception as e:
        _pmlog(f'[批量取消异常] {symbol}: {e}')

def _s6api():
    """懒加载 s6_auto_trader API（轻量兜底，避免 models/indicators 缺失时崩）"""
    global _S6_API
    if _S6_API is None:
        try:
            from trading_engine.shared.s6_auto_trader import (
                fapi_get, fapi_post, fapi_delete,
                get_price, get_symbol_info,
                get_oi_and_funding, get_rsi,
                record_trade,
            )
            _S6_API = (fapi_get, fapi_post, fapi_delete, get_price, get_symbol_info,
                        get_oi_and_funding, get_rsi, record_trade)
        except Exception as e:
            # s6_auto_trader 依赖缺失时用轻量兜底
            def _price(sym):
                return _light_get_price(sym)
            def _info(sym):
                return None
            def _oif(sym):
                return (None, None, None)
            def _rsi(sym, p=14):
                return 50.0
            def _rec(*a, **kw):
                pass
            _S6_API = (_light_fapi_get, _light_fapi_post, _light_fapi_get, _price, _info,
                        _oif, _rsi, _rec)
            _pmlog(f'[_s6api 兜底] s6_auto_trader 不可用: {e}，使用轻量 API')
    return _S6_API

# ── 系统级参数 ──────────────────────────────────────────────────────────
SYSTEM_CFG = {
    'S8A': {
        'be_done_threshold': 2.0,      # 浮盈≥% 触发止损移到成本
        'trail_mult': 0.3,             # 追踪间距 (×ATR)
        'time_stop_min': 240,          # 时间止损（分钟）
        'time_extend_min': 60,         # 可延期（分钟）
        'extend_rsi_min': 60,          # 延期条件：RSI > 此值
        'extend_funding_min': 0.0005,  # 延期条件：资金费 > 此值
        'sl_breach_max': -8.0,         # 紧急止损（%）
    },
    'S8B': {
        'be_done_threshold': 3.0,
        'trail_mult': 0.3,
        'time_stop_min': 300,
        'time_stop_fast_min': 150,     # 资金费<-0.015%时快速结束
        'funding_fast_threshold': -0.00015,
    },
    'S6': {
        'be_done_threshold': 2.5,
        'trail_mult': 0.3,
        'time_stop_min': 120,
    },
}


def _get_funding_rate(symbol: str) -> float:
    """获取当前资金费率，失败返回 0"""
    try:
        r = requests.get(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}',
                         timeout=5)
        return float(r.json().get('lastFundingRate', 0))
    except Exception:
        return 0.0


def _pmlog(msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    date = time.strftime('%Y%m%d')
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG_DIR / f'{date}.log', 'a') as f:
            f.write(f'[{ts}] {msg}\n')
    except Exception:
        pass


def _try_record_ghost_trade(sym: str, meta: dict):
    """幽灵仓数据落库（不抛异常）。通过文件标记去重，防止双进程重复写"""
    try:
        # 去重：该 symbol 近期已被 _close 处理过则跳过
        if _was_closed_recently(sym):
            _pmlog(f'[幽灵跳过] {sym} 已由 _close 记录，跳过')
            return

        _, _, _, _, _, _, _, record_trade = _s6api()
        entry = meta.get('entry', 0)
        side = meta.get('side', 'LONG')
        qty = meta.get('original_qty', meta.get('qty', 0))
        leverage = meta.get('leverage', 3)
        system_name = meta.get('system', '')
        open_time = meta.get('open_time', time.time())
        ghost_price = _light_get_price(sym) or entry
        record_trade(sym, entry, ghost_price, qty, leverage, system_name, open_time,
                     exit_reason='幽灵仓关闭', side=side,
                     score=meta.get('score', 0), atr_entry=meta.get('atr', 0),
                     sl_price=meta.get('sl', 0), ghost_cleanup=True)
    except Exception as e:
        _pmlog(f'[幽灵记录失败] {sym}: {e}')

_SYSTEM_KEYS = {
    'S6': 'state:s6',
    'S8': 'state:s8',
}

def _load() -> dict:
    """
    合并加载：Binance 实际持仓 + 本地策略元数据。
    Binance 是 existence/entry/qty 的绝对来源；本地文件只存策略上下文（event_type, strength, be_done 等）。
    返回 {symbol: pos_dict}，其中 pos_dict 含 system 标签。
    """
    now = time.time()

    # 1. 从 Binance 拉实际持仓
    real_positions: dict[str, dict] = {}
    try:
        real_r = _light_fapi_get('/fapi/v2/positionRisk')
        if isinstance(real_r, list):
            for p in real_r:
                amt = abs(float(p.get('positionAmt', 0)))
                if amt < 0.001:
                    continue
                sym = p['symbol']
                entry = float(p['entryPrice'])
                upnl = float(p.get('unRealizedProfit', 0))
                side = 'SHORT' if float(p.get('positionAmt', 0)) < 0 else 'LONG'
                real_positions[sym] = {
                    'entry': entry, 'side': side, 'qty': amt,
                    'upnl': upnl, 'leverage': int(p.get('leverage', 3)),
                    'margin': p.get('marginType', 'CROSSED').upper(),
                }
    except Exception:
        pass  # 拉不到 Binance 时回退到本地文件

    # 2. 从 Redis 读策略元数据
    meta: dict[str, dict] = {}
    for name, key in _SYSTEM_KEYS.items():
        try:
            state = _rget(key)
            if not state:
                continue
            for sym, pos in state.get('positions', {}).items():
                pos['system'] = name
                if 'open_time' not in pos:
                    pos['open_time'] = pos.get('ts', now)
                if 'sl' not in pos:
                    pos['sl'] = pos.get('stop', 0)
                if 'stop' not in pos:
                    pos['stop'] = pos.get('sl', 0)
                if 'qty' not in pos:
                    pos['qty'] = pos.get('original_qty', 0)
                meta[sym] = pos
        except Exception:
            pass

    # 3. 合并：Binance 为来源，补充本地元数据
    merged: dict[str, dict] = {}
    for sym, bp in real_positions.items():
        mp = meta.pop(sym, {})
        merged[sym] = {
            # Binance 绝对来源
            'entry': bp['entry'],
            'side': bp['side'],
            'qty': bp['qty'],
            'leverage': bp['leverage'],
            'margin': bp.get('margin', mp.get('margin', 'CROSSED')),
            # 本地元数据（不存在则用猜的系统标签）
            'system': mp.get('system', 'S6' if bp['side'] == 'LONG' else 'S8'),
            'open_time': mp.get('open_time', now),
            'event_type': mp.get('event_type', ''),
            'strength': mp.get('strength', 50),
            'score': mp.get('score', mp.get('strength', 50)),
            'sl': mp.get('sl', 0),  # 不要 fallback 到 entry，entry=sl 导致硬止损立刻触发
            'be_done': mp.get('be_done', False),
            'trail': mp.get('trail', False),
            'atr': mp.get('atr', 0),
            'algo_sl_id': mp.get('algo_sl_id', 0),
            'ts': mp.get('ts', now),
            'stop': mp.get('stop', bp['entry']),
        }

    # 4. 清理本地有但 Binance 无的幽灵（meta 中剩下的就是幽灵）
    #    仅当日首次出现时 log，避免每周期刷屏
    if hasattr(_load, '_ghost_seen'):
        _ghost_seen = _load._ghost_seen
    else:
        _ghost_seen = set()
        _load._ghost_seen = _ghost_seen
    for sym in list(meta.keys()):
        if sym not in _ghost_seen:
            _ghost_seen.add(sym)
            _pmlog(f'[幽灵仓] {sym} 交易所已无持仓，忽略 (入场={meta[sym].get("entry", 0)})')
            # 首次出现幽灵：调用 record_trade 写入 ClickHouse
            _try_record_ghost_trade(sym, meta[sym])

    # 5. 如果 Binance 返回了真实的持仓数据，则写回 state 文件修复不一致
    #    如果 Binance 不可用（real_positions 为空），则回退到本地数据
    if real_positions:
        _save(merged)
        return merged

    # 回退：使用本地文件数据
    _pmlog('[_load] Binance 不可用，回退到本地文件')
    for sym, mp in meta.items():
        merged[sym] = mp
    return merged


def _save(positions: dict):
    """
    写入回各系统 Redis（按 system 标签分组），保持单一数据源。
    """
    by_system: dict[str, dict] = {}
    for sym, pos in positions.items():
        sys_name = pos.get('system', '')
        by_system.setdefault(sys_name, {})[sym] = pos
    for name, key in _SYSTEM_KEYS.items():
        try:
            existing = _rget(key) or {}
            if name in by_system:
                existing['positions'] = by_system[name]
            _rset(key, existing)
        except Exception:
            pass


def _round_qty(symbol: str, qty: float) -> float:
    try:
        _, _, _, _, get_symbol_info, _, _, _ = _s6api()
        info = get_symbol_info(symbol)
        if isinstance(info, (list, tuple)):
            _, prec = info
        elif isinstance(info, dict):
            prec = info.get('price_precision', info.get('quantity_precision', 6))
        else:
            prec = 6
        return round(qty, prec)
    except Exception:
        return round(qty, 6)


def _get_cfg(pos: dict) -> dict:
    """从持仓获取系统配置，兜底用 S8A"""
    system = pos.get('system', '') or pos.get('signal_type', '')
    for key in ('S8A', 'S8B', 'S6'):
        if key in system:
            return SYSTEM_CFG.get(key, {})
    return SYSTEM_CFG.get('S8A', {})


# ═══════════════════════════════════════════════════════════════════════
#  开仓
# ═══════════════════════════════════════════════════════════════════════

def open_position(
    symbol: str,
    side: str,          # 'SHORT' | 'LONG'
    entry: float,
    qty: float,
    leverage: int,
    sl: float,          # 硬止损价
    tp1: float = 0,     # 保留接口但不再使用（由动态止盈替代）
    tp2: float = 0,
    *,
    atr: float = 0,
    score: int = 0,
    reasons: dict = None,
    signal_type: str = '',
    system: str = '',
    margin_type: str = 'CROSSED',
    metadata: dict = None,
) -> bool:
    """
    统一开仓。
    - 设杠杆/保证金
    - 下市价单
    - 挂条件止损单 Algo Order API（/fapi/v1/algoOrder）
    - 写入 pm_state + 同步原系统
    """
    _, fapi_post, _, _, _, _, _, _ = _s6api()

    # 1. 杠杆
    try:
        fapi_post('/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    except Exception as e:
        _pmlog(f'[开仓] {symbol} 杠杆设置: {e}')

    # 2. 保证金模式
    try:
        fapi_post('/fapi/v1/marginType', {'symbol': symbol, 'marginType': margin_type})
    except Exception as e:
        _pmlog(f'[开仓] {symbol} 保证金({margin_type}): {e}')

    # 3. 市价开仓
    order_side = 'SELL' if side == 'SHORT' else 'BUY'
    try:
        order = fapi_post('/fapi/v1/order', {
            'symbol': symbol, 'side': order_side, 'type': 'MARKET',
            'quantity': qty, 'positionSide': 'BOTH',
        })
    except Exception as e:
        _pmlog(f'[开仓失败] {symbol}: {e}')
        return False

    # 4. 下条件止损单（Algo Order API）→ 入队异步消费
    sl_side = 'BUY' if side == 'SHORT' else 'SELL'
    _algo_start_worker()
    _algo_enqueue(symbol, sl_side, sl, qty)
    _pmlog(f'[开仓AlgoSL] {symbol} 止损{sl} 已入队')
    algo_sl_id = None  # worker 完成后会更新 Redis

    # 5. 记录
    now = int(time.time())
    position = {
        'entry': entry, 'qty': qty, 'original_qty': qty,
        'leverage': leverage, 'sl': sl,
        'open_time': now, 'atr': atr,
        'side': side, 'system': system,
        'signal_type': signal_type, 'score': score,
        'margin_type': margin_type,
        'be_done': False,
        'algo_sl_id': algo_sl_id,
        **(metadata or {}),
        **(reasons or {}),
    }
    positions = _load()
    if symbol in positions:
        _pmlog(f'[开仓] {symbol} 已在持仓中，跳过')
        return True
    positions[symbol] = position
    _save(positions)
    _pmlog(f'[开仓] {system} {symbol} {side} 入场{entry} 止损{sl} {leverage}x score={score}')
    _sync_to_legacy(symbol, position)
    return True


# ═══════════════════════════════════════════════════════════════════════
#  监控
# ═══════════════════════════════════════════════════════════════════════

def _ghost_cleanup(positions: dict) -> list:
    """幽灵仓清理：对比 Binance positionRisk，从 state 文件和内存中清除并记录 trade"""
    closed = []
    try:
        _, _, _, _, _, _, _, record_trade = _s6api()
    except Exception:
        record_trade = lambda *a, **kw: None
    try:
        real_r = _light_fapi_get('/fapi/v2/positionRisk')
        if not isinstance(real_r, list):
            return closed
        real_syms = set()
        for p in real_r:
            if isinstance(p, dict) and abs(float(p.get('positionAmt', 0))) >= 0.001:
                real_syms.add(p['symbol'])
        for sym in list(positions.keys()):
            if sym in real_syms:
                continue
            pos = positions.pop(sym, None)
            if not pos:
                continue
            entry = pos.get('entry', 0)
            side = pos.get('side', 'LONG')
            qty = pos.get('original_qty', pos.get('qty', 0))
            ghost_price = _light_get_price(sym) or entry
            _pmlog(f'[幽灵仓] {sym} 交易所已无持仓，清理 (入场={entry} 现价={ghost_price})')
            record_trade(sym, entry, entry, qty,
                         pos.get('leverage', 1), pos.get('system', ''),
                         pos.get('open_time', time.time()),
                         exit_reason='手动平仓', side=side,
                         score=pos.get('score', 0),
                         atr_entry=pos.get('atr', 0),
                         sl_price=pos.get('sl', 0),
                         margin_mode=pos.get('margin', ''),
                         be_done=pos.get('be_done', False),
                         trail_active=pos.get('trail', False),
                         algo_sl_id=pos.get('algo_sl_id', 0),
                         ghost_cleanup=True)
            _sync_remove_from_legacy(sym, pos.get('system', ''))
            closed.append((sym, '手动平仓', ghost_price))
        if closed:
            for name, key in _SYSTEM_KEYS.items():
                try:
                    existing = _rget(key) or {}
                    updated = existing.get('positions', {})
                    updated = {s: p for s, p in updated.items() if p.get('system') != name}
                    sys_positions = {s: p for s, p in positions.items() if p.get('system') == name}
                    updated.update(sys_positions)
                    existing['positions'] = updated
                    _rset(key, existing)
                except Exception:
                    pass
            _pmlog(f'[幽灵清理完毕] 共清除 {len(closed)} 个幽灵仓')
    except Exception as e:
        _pmlog(f'[幽灵检测异常] {e}')
    return closed


def monitor_all(system_filter: str = '') -> list:
    """
    统一监控所有持仓。
    返回 [(symbol, reason, close_price), ...]

    Step 0: Ghost 检测 — 比对 Binance 实际持仓，清理幽灵仓
    Step 1-N: 硬止损 → be_done → 追踪锁利 → 时间止损

    system_filter: 如 'S6' 则只处理该系统的持仓（防止双进程重复推送）
    """
    positions = _load()
    if not positions:
        return []
    # 过滤系统
    if system_filter:
        positions = {s: p for s, p in positions.items() if p.get('system') == system_filter}
    if not positions:
        return []

    closed = []
    for symbol in list(positions.keys()):
        try:
            r = _monitor_one(symbol, positions[symbol], positions)
            if r:
                closed.append((symbol, *r))
        except Exception as e:
            _pmlog(f'[监控异常] {symbol}: {e}')
    _save(positions)

    # 持仓快照日志
    if closed:
        log_position_summary()

    return closed


def _monitor_one(symbol: str, pos: dict, positions: dict):
    """单币种：硬止损 → be_done → 追踪锁利 → 时间止损"""
    fapi_get, fapi_post, fapi_delete, get_price, _, _, _, _ = _s6api()
    price  = get_price(symbol)
    entry  = pos['entry']
    atr    = pos.get('atr', 0)
    hold   = (time.time() - pos['open_time']) / 60
    cfg    = _get_cfg(pos)

    # 资金费率检查（费率高时主动平仓，避免持续烧钱）
    fund_rate = _get_funding_rate(symbol)
    if pos['side'] == 'SHORT' and fund_rate < -0.005:
        _pmlog(f'[费率警告] {symbol} SHORT 资金费率 {fund_rate:.4%} <-0.5% 强制平仓')
        reason = f'资金费率过高 {fund_rate:.4%}'
        _close(symbol, pos, price, reason, positions)
        return (reason, price)
    if pos['side'] == 'LONG' and fund_rate > 0.005:
        _pmlog(f'[费率警告] {symbol} LONG 资金费率 {fund_rate:.4%} >0.5% 强制平仓')
        reason = f'资金费率过高 {fund_rate:.4%}'
        _close(symbol, pos, price, reason, positions)
        return (reason, price)
    # 警告级别（仅通知一次）
    warn_tag = 'fund_warned'
    if not pos.get(warn_tag):
        if pos['side'] == 'SHORT' and fund_rate < -0.002:
            pos[warn_tag] = True
            _pmlog(f'[费率警告] {symbol} SHORT 资金费率 {fund_rate:.4%} (>=0.2%，注意费率成本)')
        elif pos['side'] == 'LONG' and fund_rate > 0.002:
            pos[warn_tag] = True
            _pmlog(f'[费率警告] {symbol} LONG 资金费率 {fund_rate:.4%} (>=0.2%，注意费率成本)')

    if pos['side'] == 'SHORT':
        pnl = (entry - price) / entry * 100
        sl_breached = bool(pos.get('sl')) and pos['sl'] != entry and price >= pos['sl']
    else:
        pnl = (price - entry) / entry * 100
        sl_breached = bool(pos.get('sl')) and pos['sl'] != entry and price <= pos['sl']

    # 1. 硬止损
    if sl_breached:
        _close(symbol, pos, price, '硬止损', positions)
        return ('硬止损', price)

    # 2. 紧急止损（主止损 — Binance 已废弃 STOP_MARKET，全靠轮询）
    max_loss = cfg.get('sl_breach_max', -8.0)
    if pnl < max_loss:
        _close(symbol, pos, price, f'紧急止损 pnl={pnl:.1f}%', positions)
        return (f'紧急止损 pnl={pnl:.1f}%', price)

    # 3. be_done：盈利达标 → 止损移到成本
    be_pct = cfg.get('be_done_threshold', 2.0)
    if not pos.get('be_done') and pnl >= be_pct:
        _update_stop_loss(symbol, pos, price, entry)

    # 4. 追踪锁利（专业量化版）
    #    - 多周期 ATR 基价（防泵后 ATR 虚低）
    #    - 15m 收盘确认（不收市不追踪）
    #    - 收 > EMA20 进入等待区（下根确认是否真反转）
    #    - 1h EMA 趋势安全阀
    if pos.get('be_done') and pnl >= be_pct:
        trail_mult = cfg.get('trail_mult', 0.3)
        trail_result = _calc_trail_sl(symbol, pos, price, trail_mult, positions)
        if trail_result == 'exit':
            # 等待区确认：2根连续收>EMA20 → 趋势反转离场
            _close(symbol, pos, price, '趋势反转（2次收>EMA20）', positions)
            return ('趋势反转', price)
        elif trail_result is not None:
            # 新追踪价位
            _place_trail_sl(symbol, pos, trail_result, positions)

    # 4.5 1h EMA 安全阀：大周期趋势转向 → 强制离场（但免开仓后前60分钟）
    #      浮盈 >=150% 时豁免，完全交给移动止盈
    if hold < 60 or pnl >= 40:
        pass  # 新开仓60分钟内不介入 / 大盈利仓只靠移动止盈
    else:
        try:
            k1h = _get_data_cache().get_klines(symbol, '1h', 22)
            if k1h and len(k1h) >= 21:
                c1h = [float(x[4]) for x in k1h[-21:]]
                ema9_1h = sum(c1h[-9:]) / 9
                ema20_1h = sum(c1h[-20:]) / 20
                if pos['side'] == 'SHORT' and ema9_1h > ema20_1h * 1.02:
                    _close(symbol, pos, price, '1h趋势反转', positions)
                    return ('1h趋势反转', price)
                elif pos['side'] != 'SHORT' and ema9_1h < ema20_1h * 0.98:
                    _close(symbol, pos, price, '1h趋势反转', positions)
                    return ('1h趋势反转', price)
        except Exception:
            pass

    # 5. 时间止损
    ts_min = cfg.get('time_stop_min', 240)
    if hold > ts_min:
        if pnl < 0:
            # 浮亏 — 检查是否可延期
            if not pos.get('time_extended'):
                _, _, _, _, _, get_oi_and_funding, get_rsi, _ = _s6api()
                try:
                    rsi = get_rsi(symbol)
                    _, _, funding = get_oi_and_funding(symbol)
                    ext_r = cfg.get('extend_rsi_min', 60)
                    ext_f = cfg.get('extend_funding_min', 0.0005)
                    if rsi > ext_r and (funding or 0) > ext_f:
                        pos['time_extended'] = True
                        pos['extend_deadline'] = time.time() + cfg.get('time_extend_min', 60) * 60
                        _pmlog(f'[时间延期] {symbol} RSI={rsi:.0f} 再观察1h')
                        return None
                except Exception:
                    pass
                if time.time() < pos.get('extend_deadline', 0):
                    return None
            _close(symbol, pos, price, '时间止损', positions)
            return ('时间止损', price)
        elif pnl < be_pct:
            # 微盈/不亏 — 提前释放
            _close(symbol, pos, price, '时间止损', positions)
            return ('时间止损', price)

    return None


# ═══════════════════════════════════════════════════════════════════════
#  对账
# ═══════════════════════════════════════════════════════════════════════

def reconcile_all():
    """
    对账：对比 PM state vs Binance 实际持仓。
    - PM有但Binance无 → 清理幽灵仓
    - Binance有但PM无 → 告警（可能丢失跟踪）
    返回 (ghost_cleaned, missing_tracked)
    """
    fapi_get, _, _, _, _, _, _, _ = _s6api()
    positions = _load()
    ghost = []
    missing = []

    # Binance 实际持仓
    try:
        real_r = fapi_get('/fapi/v2/positionRisk')
        # API错误（限速/banned）时不执行对账——宁漏不错
        if not isinstance(real_r, list):
            _pmlog(f'[对账跳过] Binance API返回异常: {type(real_r).__name__}')
            return [], []
        real_positions = {}
        for p in real_r:
            if isinstance(p, dict):
                amt = float(p.get('positionAmt', 0))
                if abs(amt) >= 0.001:  # 忽略极微量
                    real_positions[p['symbol']] = {
                        'amt': amt,
                        'side': 'SHORT' if amt < 0 else 'LONG',
                    }
    except Exception as e:
        _pmlog(f'[对账失败] Binance API: {e}')
        return [], []

    # PM有但Binance无
    for sym in list(positions.keys()):
        if sym not in real_positions:
            _pmlog(f'[对账] 幽灵仓清除: {sym} entry={positions[sym].get("entry")}（state有但交易所无）')
            positions.pop(sym, None)
            ghost.append(sym)
            _sync_remove_from_legacy(sym, positions.get(sym, {}).get('system', ''))

    # Binance有但PM无
    for sym, info in real_positions.items():
        if sym not in positions:
            _pmlog(f'[对账] 漏记仓: {sym} {info["side"]} 持仓{info["amt"]}（交易所已有但PM未跟踪）')
            missing.append(sym)

    _save(positions)
    return ghost, missing


# ═══════════════════════════════════════════════════════════════════════
#  持仓快照日志
# ═══════════════════════════════════════════════════════════════════════

def log_position_summary():
    """输出当前持仓快照到 PM 日志"""
    positions = _load()
    if not positions:
        _pmlog('[持仓] (空)')
        return
    for sym, pos in positions.items():
        try:
            _, _, _, get_price, _, _, _, _ = _s6api()
            price = get_price(sym)
            if pos['side'] == 'SHORT':
                pnl = (pos['entry'] - price) / pos['entry'] * 100
            else:
                pnl = (price - pos['entry']) / pos['entry'] * 100
            hold = (time.time() - pos['open_time']) / 60
            _pmlog(f'[持仓] {sym} [{pos.get("system","?")}] {pos["side"]} '
                   f'入场={pos["entry"]} 现价={price} pnl={pnl:+.1f}% '
                   f'持仓{hold:.0f}min be_done={pos.get("be_done")} '
                   f'止损={pos.get("sl","?")}')
        except Exception as e:
            _pmlog(f'[持仓] {sym} 日志失败: {e}')


# ═══════════════════════════════════════════════════════════════════════
#  止损管理
# ═══════════════════════════════════════════════════════════════════════

def _update_stop_loss(symbol: str, pos: dict, price: float, entry: float):
    """be_done：止损移到成本价"""
    global _last_api_call
    now = time.time()
    if now - _last_api_call.get(symbol, 0) < _API_COOLDOWN:
        return
    _, fapi_post, _, _, get_symbol_info, _, _, _ = _s6api()
    try:
        _, prec = get_symbol_info(symbol)
    except Exception:
        prec = 6

    be_sl = round(entry * 1.001, prec) if pos['side'] == 'SHORT' else round(entry * 0.999, prec)

    if (pos['side'] == 'SHORT' and be_sl >= pos['sl']) or \
       (pos['side'] != 'SHORT' and be_sl <= pos['sl']):
        return

    try:
        sl_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
        # 取消旧Algo止损，入队新单
        old_algo = pos.get('algo_sl_id')
        if old_algo:
            _algo_cancel(old_algo)
        _algo_start_worker()
        _algo_enqueue(symbol, sl_side, be_sl, pos['qty'])
        pos['sl'] = be_sl
        pos['be_done'] = True
        _last_api_call[symbol] = now
        _pmlog(f'[be_done] {symbol} 止损移至成本 {be_sl}')
    except Exception as e:
        _pmlog(f'[be_done失败] {symbol}: {e}')


def _calc_atr(klines: list, period: int = 14) -> float:
    """从 kline 列表计算 ATR"""
    if not klines or len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); l = float(klines[i][3]); pc = float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def _calc_trail_base(symbol: str, price: float) -> float:
    """多周期 ATR 基价：max(15m_ATR, 1h_ATR/4, 价格×1.5%)
    避免泵后磨跌期 15m ATR 虚低导致止损过紧"""
    dc = _get_data_cache()
    try:
        k15 = dc.get_klines(symbol, '15m', 15)
        k1h = dc.get_klines(symbol, '1h', 15)
    except Exception:
        return price * 0.015

    atr_15m = _calc_atr(k15, 14) if k15 else 0
    atr_1h = _calc_atr(k1h, 14) if k1h else 0
    pct_base = price * 0.015

    return max(atr_15m, atr_1h / 4, pct_base)


def _calc_trail_sl(symbol: str, pos: dict, price: float, mult: float, positions: dict):
    """
    专业量化追踪止损。

    策略：
    1. 多周期 ATR 基价（而非单一 15m ATR）
    2. 泵后币自动 ×3 间距（24h 振幅 > 20%）
    3. 15m 收盘确认（不收市不追踪，再紧的针也打不掉）
    4. 收 > EMA20 → 进入等待区（不下单不收紧）
       下一根也收 > EMA20 → 返回 'exit' 信号离场
       下一根收 < EMA20 → 清理等待区，恢复追踪

    返回：
      None     → 不需要更新（同根K线，或等待区第1根）
      数值     → 新止损价
      'exit'   → 等待区确认趋势反转，需要离场
    """
    dc = _get_data_cache()
    pump_mult = 1.0

    # ── 0. 泵后检测（24h 内任一 15m 蜡烛振幅 > 4%）──
    try:
        _tmp_k = dc.get_klines(symbol, '15m', 96)
        if _tmp_k:
            for _kk in _tmp_k:
                _o, _h, _l = float(_kk[1]), float(_kk[2]), float(_kk[3])
                if _o > 0 and (_h - _l) / _o * 100 > 4:
                    pump_mult = 1.5
                    break
    except Exception:
        pass

    # ── 1a. 大浮盈自动收紧间距 ──
    # 当浮盈 > 8% 时，逐步收紧追踪（保护利润）
    if pos['side'] == 'SHORT':
        pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
    else:
        pnl_pct = (price - pos['entry']) / pos['entry'] * 100
    if pnl_pct > 8:
        # 浮盈每多 2%，间距收紧一档（最少 0.5x base）
        tighten = max(0.5, 1.0 - (pnl_pct - 8) / 20)
        mult *= tighten
        pump_mult = min(pump_mult, 1.5)  # 泵后间距也同时收紧

    # ── 1. 多周期 ATR 基价 ──
    base_atr = _calc_trail_base(symbol, price)
    effective_atr = base_atr * mult * pump_mult
    if effective_atr <= 0:
        return None

    # ── 2. 15m 收盘确认 ──
    try:
        k15 = dc.get_klines(symbol, '15m', 22)
        if not k15 or len(k15) < 3:
            return None
    except Exception:
        return None

    # kl[-1] = 未完成蜡烛, kl[-2] = 最新收盘
    last_closed_ts = k15[-2][0]
    if pos.get('last_15m_candle', 0) >= last_closed_ts:
        return None  # 同根K线，不收市不追踪
    pos['last_15m_candle'] = last_closed_ts

    # 15m EMA20 计算
    if len(k15) < 21:
        return None
    c15 = [float(x[4]) for x in k15[-21:]]
    ema20_15 = sum(c15[-20:]) / 20
    last_close = float(k15[-2][4])

    # ── 3. 等待区逻辑（收 > EMA20）──
    wait_key = 'trail_confirm_until'
    if pos['side'] == 'SHORT' and last_close > ema20_15:
        if pos.get(wait_key):
            # 已经等了一根，第二根也收在 EMA20 之上 → 趋势可能反转
            pos.pop(wait_key, None)
            return 'exit'
        else:
            # 第一根收 > EMA20 → 进入等待区，不更新止损
            pos[wait_key] = last_closed_ts
            return None
    elif pos['side'] != 'SHORT' and last_close < ema20_15:
        if pos.get(wait_key):
            pos.pop(wait_key, None)
            return 'exit'
        else:
            pos[wait_key] = last_closed_ts
            return None

    # 收盘回归 EMA20 之下 → 清理等待区
    pos.pop(wait_key, None)

    # ── 4. 计算新止损价 ──
    if pos['side'] == 'SHORT':
        sl = round(price + effective_atr, 6)
        sl = max(sl, round(ema20_15, 6))  # EMA20 兜底
        if sl < pos['sl'] and sl > price:
            return sl
    else:
        sl = round(price - effective_atr, 6)
        sl = min(sl, round(ema20_15, 6))  # EMA20 兜底
        if sl > pos['sl'] and sl < price:
            return sl
    return None


def _place_trail_sl(symbol: str, pos: dict, trail_sl: float, positions: dict):
    """更新追踪止损（轮询用） + 尝试同步到Algo Order API"""
    global _last_api_call
    now = time.time()
    if now - _last_api_call.get(symbol, 0) < _API_COOLDOWN:
        return
    _, _, _, _, _, _, _, _ = _s6api()
    old = pos['sl']
    pos['sl'] = trail_sl
    _last_api_call[symbol] = now
    _save(positions)
    locked = (pos['entry'] - trail_sl) / pos['entry'] * 100 if pos['side'] == 'SHORT' \
        else (trail_sl - pos['entry']) / pos['entry'] * 100
    _pmlog(f'[追踪锁利] {symbol} 止损 {old}→{trail_sl} (锁{locked:.1f}%利润)')

    # 入队更新 Algo 条件单（后台队列消费，不影响轮询）
    _cancel_all_algo(symbol)
    try:
        sl_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
        _algo_start_worker()
        _algo_enqueue(symbol, sl_side, trail_sl, pos['qty'])
        _pmlog(f'[追踪锁利Algo] {symbol} 止损更新已入队')
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  平仓
# ═══════════════════════════════════════════════════════════════════════

def close_position(symbol: str, reason: str) -> bool:
    """外部调用平仓"""
    positions = _load()
    pos = positions.get(symbol)
    if not pos:
        _pmlog(f'[平仓] {symbol} PM无此持仓')
        return False
    _, _, _, get_price, _, _, _, _ = _s6api()
    price = get_price(symbol)
    _close(symbol, pos, price, reason, positions)
    return True


def _mark_closed(symbol: str):
    """跨进程标记：该 symbol 已被 _close 处理过，幽灵忽略"""
    try:
        d = _LOG_DIR / 'closed_markers'
        d.mkdir(parents=True, exist_ok=True)
        (d / symbol).write_text(str(time.time()))
    except Exception:
        pass


def _was_closed_recently(symbol: str, within_hours: int = 4) -> bool:
    """检查 symbol 近期是否被 _close 处理过"""
    try:
        d = _LOG_DIR / 'closed_markers'
        marker = d / symbol
        if marker.exists():
            age = time.time() - float(marker.read_text().strip())
            return age < within_hours * 3600
    except Exception:
        pass
    return False


def _close(symbol: str, pos: dict, price: float, reason: str, positions: dict):
    """内部平仓：取消条件单 → 确认实盘 → 市价平 → 落库 → 删记录 → 同步原系统"""
    _mark_closed(symbol)
    fapi_get, fapi_post, _, _, _, _, _, record_trade = _s6api()

    # 0. 取消该币所有条件单（防止残留重复单）
    try:
        _cancel_all_algo(symbol)
        _pmlog(f'[平仓Algo取消] {symbol} 已清理全部条件单')
    except Exception as e:
        _pmlog(f'[平仓Algo取消异常] {symbol}: {e}')
    try:
        real_r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
        if pos['side'] == 'SHORT':
            real_pos = next((x for x in real_r if isinstance(x, dict)
                           and float(x.get('positionAmt', 0)) < 0), None)
        else:
            real_pos = next((x for x in real_r if isinstance(x, dict)
                           and float(x.get('positionAmt', 0)) > 0), None)
        if not real_pos:
            # 止损单已在交易所触发平仓，记录本次平仓
            close_qty = pos.get('original_qty', pos.get('qty', 0))
            if pos['side'] == 'SHORT':
                pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
                pnl_u   = round((pos['entry'] - price) * close_qty, 2)
            else:
                pnl_pct = (price - pos['entry']) / pos['entry'] * 100
                pnl_u   = round((price - pos['entry']) * close_qty, 2)
            _pmlog(f'[平仓] {symbol} 交易所已平仓 pnl={pnl_pct:+.1f}% ({pnl_u:+.2f}U) 原因={reason}')
            record_trade(symbol, pos['entry'], price, close_qty, pos.get("leverage", 3),
                         pos.get('system', ''), pos['open_time'],
                         exit_reason=reason, side=pos['side'],
                         score=pos.get('score', 0),
                         atr_entry=pos.get('atr', 0),
                         sl_price=pos.get('sl', 0),
                         margin_mode=pos.get('margin', ''),
                         be_done=pos.get('be_done', False),
                         trail_active=pos.get('trail', False),
                         algo_sl_id=pos.get('algo_sl_id', 0))
            positions.pop(symbol, None)
            _save(positions)
            _sync_remove_from_legacy(symbol, pos.get('system', ''))
            return

        close_qty = _round_qty(symbol, abs(float(real_pos['positionAmt'])))
        close_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
        result = fapi_post('/fapi/v1/order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': close_qty, 'positionSide': 'BOTH', 'reduceOnly': 'true',
        })
        if isinstance(result, dict) and result.get('code'):
            _pmlog(f'[平仓失败] {symbol}: {result.get("msg")}')
            return
    except Exception as e:
        _pmlog(f'[平仓异常] {symbol}: {e}')
        return

    # 盈亏
    if pos['side'] == 'SHORT':
        pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
        pnl_u   = round((pos['entry'] - price) * close_qty, 2)
    else:
        pnl_pct = (price - pos['entry']) / pos['entry'] * 100
        pnl_u   = round((price - pos['entry']) * close_qty, 2)

    _pmlog(f'[平仓] {symbol} {reason} pnl={pnl_pct:+.1f}% ({pnl_u:+.2f}U)')

    # 落库
    record_trade(symbol, pos['entry'], price, close_qty, pos.get("leverage", 3),
                 pos.get('system', ''), pos['open_time'],
                 exit_reason=reason, side=pos['side'],
                 score=pos.get('score', 0),
                 atr_entry=pos.get('atr', 0),
                 sl_price=pos.get('sl', 0),
                 margin_mode=pos.get('margin', ''),
                 be_done=pos.get('be_done', False),
                 trail_active=pos.get('trail', False),
                 algo_sl_id=pos.get('algo_sl_id', 0))

    # 删记录 + 同步
    positions.pop(symbol, None)
    _save(positions)
    _sync_remove_from_legacy(symbol, pos.get('system', ''))


def _set_cooldown(symbol: str, system: str, pnl_pct: float):
    """平仓后写入对应系统的冷却期（由策略主循环负责，PM 写入会导致并发覆盖）"""
    pass



def _sync_to_legacy(symbol: str, pos: dict):
    """写回原系统 state"""
    key = _SYSTEM_KEYS.get(pos.get('system', ''))
    if not key:
        return
    try:
        state = _rget(key)
        if state and 'positions' in state:
            state['positions'][symbol] = pos
            _rset(key, state)
    except Exception:
        pass


def _sync_remove_from_legacy(symbol: str, system: str):
    """平仓后从原系统 state 删除"""
    key = _SYSTEM_KEYS.get(system)
    if not key:
        return
    try:
        state = _rget(key)
        if state:
            state.get('positions', {}).pop(symbol, None)
            _rset(key, state)
    except Exception:
        pass


def migrate_existing_positions():
    """迁移各系统现存持仓到 PM（启动时调用一次）"""
    positions = _load()
    changed = False
    for system, key in _SYSTEM_KEYS.items():
        try:
            state = _rget(key)
            if not state:
                continue
            for sym, pos in state.get('positions', {}).items():
                if sym not in positions:
                    pos['system'] = pos.get('system', system)
                    if 'side' not in pos:
                        pos['side'] = pos.get('side', 'SHORT')
                    if 'original_qty' not in pos:
                        pos['original_qty'] = pos.get('qty', 0)
                    positions[sym] = pos
                    changed = True
                    _pmlog(f'[迁移] {system} {sym} 入场{pos.get("entry")} 已纳入PM管理')
        except Exception as e:
            _pmlog(f'[迁移失败] {system}: {e}')
    if changed:
        _save(positions)
    return positions
