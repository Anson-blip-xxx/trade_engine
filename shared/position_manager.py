"""
position_manager.py — 统一持仓生命周期管理
职责：开仓、监控（硬止损/移成本/追踪锁利/时间止损）、平仓
各系统（S6/S8A/S8B）只负责「过滤信号→设置开仓参数」，后续全由 PM 管理。

日志： logs/position_manager/YYYYMMDD.log
状态： config/pm_state.json

数据源：pm:positions 是唯一持仓状态来源。
平仓/开仓由 PM 全权管理，策略直接查询 PM 获取持仓计数。

✅ 2026-07-18: 接入 Algo Order API（/fapi/v1/algoOrder）下止损单，
    轮询止损保留作为兜底。
✅ 2026-07-20: Algo 限速改为队列消费模式，后台线程以 11s 间隔依次处理。
"""

import time, json, threading, hmac, hashlib, os, uuid, requests
from pathlib import Path
from urllib.parse import urlencode
from shared.redis_store import get as _rget, set as _rset
from shared.binance_api import FAPI as _FAPI
from shared.postgres_client import record_trade_event as _pg_record_event

_BASE       = Path(__file__).parent.parent
_LOG_DIR    = _BASE.parent / 'logs/position_manager'

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
            is_testnet = False
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    k = k.strip()
                    if k == 'BINANCE_TESTNET':
                        is_testnet = v.strip().lower() == 'true'
                    elif k == 'BINANCE_API_KEY' and not is_testnet:
                        _API_KEY = v.strip()
                    elif k in ('BINANCE_SECRET_KEY', 'BINANCE_API_SECRET') and not is_testnet:
                        _API_SECRET = v.strip()
                    elif k == 'BINANCE_TESTNET_API_KEY' and is_testnet:
                        _API_KEY = v.strip()
                    elif k == 'BINANCE_TESTNET_API_SECRET' and is_testnet:
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
        r = requests.post(f'{_FAPI}{path}', params=params,
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
        r = requests.get(f'{_FAPI}{path}', params=params,
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
        r = requests.delete(f'{_FAPI}{path}', params=params,
                            headers={'X-MBX-APIKEY': _API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else r.json()
    except Exception as e:
        _pmlog(f'[_light_fapi_delete 异常] {path}: {e}')
        return None


def _light_get_price(symbol: str) -> float:
    """轻量获取最新价（公开 ticker API，无需签名）"""
    import requests as _req
    try:
        r = _req.get(f'{_FAPI}/fapi/v1/ticker/price?symbol={symbol}', timeout=5)
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
_last_algo_update = {}  # {symbol: timestamp} AlgoSL 更新节流
_ALGO_UPDATE_INTERVAL = 60  # AlgoSL 最少间隔（秒）
_ALGO_MIN_CHANGE_PCT = 0.2  # SL 变化 < 此值（%）不更新 AlgoSL

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
            _ei = _req.get(f'{_FAPI}/fapi/v1/exchangeInfo', timeout=10)
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
            from shared.binance_api import fapi_get, fapi_post, fapi_delete
            from shared.market_data import get_price, get_symbol_info, get_oi_and_funding, get_rsi
            from shared.trade_recorder import record_trade
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

# ═══════════════════════════════════════════════════════════════════════
#  WS 实时持仓（一层数据源）
# ═══════════════════════════════════════════════════════════════════════

_WS_POSITIONS: dict[str, dict] = {}
_WS_LAST_UPDATE: float = 0
_WS_LOCK = threading.Lock()
_WS_STOP = False

# WS 领导选举：Redis 原子锁，只允许一个进程持有用户数据流连接，
# 避免 s6/s8 双进程共用同一 listenKey 互相踢线。
_WS_LEASE_KEY = 'ws:leader'
_WS_LEASE_TTL = 45
_WS_INSTANCE = f'{os.getpid()}-{uuid.uuid4().hex[:8]}'

def _ws_am_leader() -> bool:
    """尝试成为 WS 领导者：抢锁成功或仍持有锁则返回 True。"""
    try:
        from shared.redis_store import lock_owner, lock_acquire, lock_renew
        owner = lock_owner(_WS_LEASE_KEY)
        if owner == _WS_INSTANCE:
            lock_renew(_WS_LEASE_KEY, _WS_INSTANCE, _WS_LEASE_TTL)
            return True
        if owner is None:
            return lock_acquire(_WS_LEASE_KEY, _WS_INSTANCE, _WS_LEASE_TTL)
        return False
    except Exception:
        return True  # 兜底：锁服务异常时允许连接，避免完全失去实时监控

def _ws_url() -> str:
    u = _FAPI.replace('https://', 'wss://')
    return u.replace('fapi', 'fstream')

def _ws_listen_key() -> str:
    _ensure_apikey()
    import requests as _req
    r = _req.post(f'{_FAPI}/fapi/v1/listenKey',
                   headers={'X-MBX-APIKEY': _API_KEY}, timeout=10)
    return r.json().get('listenKey', '')

def _ws_on_message(ws, message):
    global _WS_LAST_UPDATE
    try:
        data = json.loads(message)
        if data.get('e') != 'ACCOUNT_UPDATE':
            return
        with _WS_LOCK:
            for p in data['a']['P']:
                sym = p['s']
                amt = float(p['pa'])
                if abs(amt) < 0.001:
                    prev = _WS_POSITIONS.pop(sym, None)
                    if prev and not _was_closed_recently(sym):
                        side = prev.get('side', 'LONG')
                        _pmlog(f'[WS平仓] {sym} {side} AlgoSL(入场={prev.get("entry", 0)})')
                        # 先落库（此时 closed 标记未设，不会被 _try_record_ghost_trade 自跳）
                        # 再标记，供 _load/ghost_cleanup 跨进程去重
                        _try_record_ghost_trade(sym, prev)
                        _mark_closed(sym)
                else:
                    side = 'LONG' if amt > 0 else 'SHORT'
                    _WS_POSITIONS[sym] = {
                        'entry': float(p['ep']), 'side': side, 'qty': abs(amt),
                        'leverage': int(p.get('lev', 3)),
                        'margin': p.get('mt', 'cross').upper(),
                        'system': '?', 'open_time': time.time(),
                        'sl': 0, 'be_done': False,
                    }
            _WS_LAST_UPDATE = time.time()

    except Exception as e:
        _pmlog(f'[WS消息异常] {e}')

def _ws_on_open(ws):
    _pmlog('[WS已连接] 开始接收实时仓位')

def _ws_on_error(ws, error):
    _pmlog(f'[WS错误] {error}')

def _ws_on_close(ws, close_status_code, close_msg):
    _pmlog(f'[WS断开] code={close_status_code} msg={close_msg} 5s后重连')

def _ws_connect_loop():
    while not _WS_STOP:
        if not _ws_am_leader():
            time.sleep(5)
            continue
        try:
            import websocket
            import requests as _req
            key = _ws_listen_key()
            if not key:
                time.sleep(5)
                continue
            url = f'{_ws_url()}/ws/{key}'
            _pmlog(f'[WS连接] 用户数据流 {url}')
            ws = websocket.WebSocketApp(
                url,
                on_open=_ws_on_open,
                on_message=_ws_on_message,
                on_error=_ws_on_error,
                on_close=_ws_on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            _pmlog(f'[WS重连] {e}')
        time.sleep(5)

# ── 系统级参数 ──────────────────────────────────────────────────────────
SYSTEM_CFG = {
    'S8A': {
        'be_done_threshold': 2.0,      # 浮盈≥% 触发止损移到成本
        'trail': {
            'base_mult': 0.3,          # 基础追踪间距 (×ATR)
            'tighten_pct': 8.0,        # 浮盈≥此值开始收紧
            'tighten_min': 0.5,        # 最紧间距倍率
            'breakeven_atr': 1.5,      # 浮盈≥此值×ATR → 保本加固
        },
        'time_stop_min': 240,          # 时间止损（分钟）
        'time_extend_min': 60,         # 可延期（分钟）
        'extend_rsi_min': 60,          # 延期条件：RSI > 此值
        'extend_funding_min': 0.0005,  # 延期条件：资金费 > 此值
        'sl_breach_max': -8.0,         # 紧急止损（%）
        'partial_tp': {5: 0.3},        # 浮盈≥% → 平仓比例
    },
    'S8B': {
        'be_done_threshold': 3.0,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 8.0,
            'tighten_min': 0.5,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 300,
        'time_stop_fast_min': 150,
        'funding_fast_threshold': -0.00015,
        'partial_tp': {8: 0.3},
    },
    'S6A': {
        'be_done_threshold': 2.5,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 5.0,        # 普通做多浮盈保护更积极
            'tighten_min': 0.3,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 120,
        'partial_tp': {5: 0.5},
    },
    'S6B': {
        'be_done_threshold': 3.0,      # 泵多需要更大空间
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 8.0,        # 泵多收紧更晚
            'tighten_min': 0.5,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 120,
        'partial_tp': {8: 0.3},
    },
    # 旧 S6 兜底（存量仓位可能还是 system='S6'）
    'S6': {
        'be_done_threshold': 2.5,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 5.0,
            'tighten_min': 0.3,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 120,
        'partial_tp': {5: 0.3},
    },
}


def _get_funding_rate(symbol: str) -> float:
    """获取当前资金费率，失败返回 0"""
    try:
        r = requests.get(f'{_FAPI}/fapi/v1/premiumIndex?symbol={symbol}',
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

if os.environ.get('PM_NO_WS') != '1':
    _WS_THREAD = threading.Thread(target=_ws_connect_loop, daemon=True)
    _WS_THREAD.start()
    _pmlog('[WS] 后台线程已启动')


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
                     sl_price=meta.get('sl', 0),
                     position_id=_position_id(sym, meta), final_close=True,
                     ghost_cleanup=True)
    except Exception as e:
        _pmlog(f'[幽灵记录失败] {sym}: {e}')

_SYSTEM_KEYS = {
    'S6': 'state:s6',
    'S8': 'state:s8',
}

def _load_meta() -> dict:
    """从 pm:positions 读取本地元数据。"""
    try:
        local_pos = _rget('pm:positions')
        if isinstance(local_pos, dict):
            return {s: p for s, p in local_pos.items() if isinstance(p, dict)}
    except Exception:
        pass
    return {}

def _merge_meta(raw: dict[str, dict], meta: dict, now: float) -> dict:
    """用本地元数据 enrich 原始持仓数据。"""
    merged = {}
    for sym, bp in raw.items():
        if _was_closed_recently(sym):
            _pmlog(f'[闭标记修复] {sym} 实际仍有持仓，清除关闭标记')
            _clear_closed_marker(sym)
        mp = meta.pop(sym, {})
        merged[sym] = {
            'entry': bp['entry'], 'side': bp['side'], 'qty': min(bp['qty'], mp.get('qty', bp['qty'])),
            'leverage': bp.get('leverage', 3),
            'margin': bp.get('margin', mp.get('margin', 'CROSSED')),
            'system': mp.get('system', 'S6' if bp['side'] == 'LONG' else 'S8'),
            'open_time': mp.get('open_time', now),
            'event_type': mp.get('event_type', ''),
            'strength': mp.get('strength', 50),
            'score': mp.get('score', mp.get('strength', 50)),
            'sl': mp.get('sl') or round(bp['entry'] * (1.08 if bp['side'] == 'SHORT' else 0.92), 8),
            'be_done': mp.get('be_done', False),
            'trail': mp.get('trail', False),
            'atr': mp.get('atr', 0),
            'algo_sl_id': mp.get('algo_sl_id', 0),
            'ts': mp.get('ts', now), 'stop': mp.get('stop', bp['entry']),
            'tp_done': mp.get('tp_done', []),
            'highest': mp.get('highest', bp['entry']),
            'lowest': mp.get('lowest', bp['entry']),
            'position_id': mp.get(
                'position_id',
                f"{mp.get('system', 'S6')}:{sym}:{mp.get('open_time', now):.6f}",
            ),
            'trend_reversal_warned': mp.get('trend_reversal_warned', False),
        }
    return merged


def _merge_meta_preserving_missing(raw: dict[str, dict], meta: dict,
                                   now: float) -> dict:
    """保留交易所快照中暂时缺失的本地仓位，交给幽灵仓流程核验。

    WS/REST 快照可能短暂不完整，不能在这里直接删除本地元数据；否则
    ``_ghost_cleanup`` 看不到该仓位，也就无法记录平仓或发送通知。
    """
    merged = _merge_meta(raw, dict(meta), now)
    for symbol, position in meta.items():
        if symbol not in merged and not _was_closed_recently(symbol):
            merged[symbol] = position
    return merged

def _load() -> dict:
    """
    三层加载持仓：
      一层 WS 实时流 → 二层 REST 轮询 → 三层本地元数据
    沙盘模式：跳过 WS/REST，仅用本地。
    """
    now = time.time()

    if _sandbox_active():
        meta = _load_meta()
        return {s: p for s, p in meta.items() if not _was_closed_recently(s)}

    # ── 一层：WS 实时流 ──
    ws_fresh = _WS_LAST_UPDATE > 0 and now - _WS_LAST_UPDATE < 30
    if ws_fresh:
        with _WS_LOCK:
            ws_positions = dict(_WS_POSITIONS)
        if ws_positions:
            meta = _load_meta()
            merged = _merge_meta_preserving_missing(ws_positions, meta, now)
            _save(merged)
            return merged

    # ── 二层：REST 轮询 ──
    rest_positions: dict[str, dict] = {}
    try:
        real_r = _light_fapi_get('/fapi/v2/positionRisk')
        if isinstance(real_r, list):
            for p in real_r:
                amt = abs(float(p.get('positionAmt', 0)))
                if amt < 0.001:
                    continue
                side = 'SHORT' if float(p.get('positionAmt', 0)) < 0 else 'LONG'
                rest_positions[p['symbol']] = {
                    'entry': float(p['entryPrice']), 'side': side, 'qty': amt,
                    'leverage': int(p.get('leverage', 3)),
                    'margin': p.get('marginType', 'CROSSED').upper(),
                }
    except Exception:
        pass

    if rest_positions:
        meta = _load_meta()
        merged = _merge_meta_preserving_missing(rest_positions, meta, now)
        _save(merged)
        return merged

    # ── 三层：本地元数据 ──
    meta = _load_meta()
    merged = {s: p for s, p in meta.items() if not _was_closed_recently(s)}
    if merged:
        _save(merged)
    return merged


def _save(positions: dict):
    """写入 pm:positions 单一数据源。"""
    try:
        _rset('pm:positions', positions)
    except Exception:
        pass


def _round_qty(symbol: str, qty: float) -> float:
    try:
        _, _, _, _, get_symbol_info, _, _, _ = _s6api()
        info = get_symbol_info(symbol)
        if isinstance(info, (list, tuple)):
            prec = info[0]  # (qty_precision, price_precision)
        elif isinstance(info, dict):
            prec = info.get('quantity_precision', info.get('price_precision', 6))
        else:
            prec = 6
        return round(qty, prec)
    except Exception:
        return round(qty, 6)


def _get_cfg(pos: dict) -> dict:
    """从持仓获取系统配置，兜底用 S8A"""
    system = pos.get('system', '') or pos.get('signal_type', '')
    for key in ('S8A', 'S8B', 'S6A', 'S6B', 'S6'):
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
    if _was_closed_recently(symbol):
        _pmlog(f'[开仓拒绝] {symbol} 4h 内被平仓过，跳过')
        return False
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
        'tp_done': [],
        'highest': entry if side != 'SHORT' else entry,
        'lowest': entry if side == 'SHORT' else entry,
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
    return True


# ═══════════════════════════════════════════════════════════════════════
#  监控
# ═══════════════════════════════════════════════════════════════════════

def _ghost_cleanup(positions: dict, system_filter: str = '') -> list:
    """幽灵仓清理：对比 Binance positionRisk，清除并记录 trade。沙盘模式跳过。"""
    if _sandbox_active():
        return []
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
            pos = positions.get(sym)
            if not pos:
                continue
            # WS 领导者已通过 closed 标记记录过，避免双进程重复记账
            if _was_closed_recently(sym):
                positions.pop(sym, None)
                continue
            # 只清理属于自己系统的幽灵仓，不碰对方进程的仓位
            if system_filter and not pos.get('system', '').startswith(system_filter):
                continue
            positions.pop(sym, None)
            entry = pos.get('entry', 0)
            side = pos.get('side', 'LONG')
            qty = pos.get('original_qty', pos.get('qty', 0))
            ghost_price = _light_get_price(sym) or entry
            _pmlog(f'[幽灵仓] {sym} 交易所已无持仓，清理 (入场={entry} 现价={ghost_price})')
            record_trade(sym, entry, ghost_price, qty,
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
                          position_id=_position_id(sym, pos), final_close=True,
                          ghost_cleanup=True)
            # 标记已清理，防止下一轮 _load 从 meta 重新读到后再次清理/重复记账
            _mark_closed(sym)
            closed.append((sym, '手动平仓', ghost_price, entry, qty, side))
        if closed:
            _pmlog(f'[幽灵清理完毕] 共清除 {len(closed)} 个幽灵仓')
    except Exception as e:
        _pmlog(f'[幽灵检测异常] {e}')
    return closed


_monitor_heartbeat_ts: float = 0
_RECENTLY_GHOSTED: list = []  # 本轮检测到的幽灵仓，monitor_all 消费后清空
_CLOSE_ERROR_LOG_TS: dict[str, float] = {}


def _log_close_error(symbol: str, message: str, interval: int = 60):
    """限频重复平仓错误；实际重试仍由监控循环继续执行。"""
    now = time.time()
    if now - _CLOSE_ERROR_LOG_TS.get(symbol, 0) >= interval:
        _CLOSE_ERROR_LOG_TS[symbol] = now
        _pmlog(f'[平仓失败] {symbol}: {message}')


def _position_id(symbol: str, pos: dict) -> str:
    """Return a stable ID for one aggregate exchange position."""
    return str(pos.get('position_id') or ':'.join([
        str(pos.get('system', '')), symbol,
        f"{float(pos.get('entry', 0)):.12g}",
        f"{float(pos.get('open_time', 0)):.6f}",
    ]))


def _should_exit_1h_reversal(pnl: float) -> bool:
    """Only exit on reversal once the position is at least breakeven."""
    return pnl >= 0


def _early_loss_momentum_weak(klines: list, side: str) -> bool:
    """Check whether the last 15m candles still move against the position."""
    if not klines or len(klines) < 4:
        return False
    closes = [float(row[4]) for row in klines[-4:]]
    baseline = sum(closes[:3]) / 3
    if side == 'SHORT':
        return closes[-1] >= closes[-2] and closes[-1] > baseline
    return closes[-1] <= closes[-2] and closes[-1] < baseline

def monitor_all(system_filter: str = '') -> list:
    """
    统一监控所有持仓。
    返回 [(symbol, reason, close_price), ...]

    Step 0: Ghost 检测 — 比对 Binance 实际持仓，清理幽灵仓
    Step 1-N: 硬止损 → be_done → 追踪锁利 → 时间止损

    system_filter: 如 'S6' 则只处理该系统的持仓（防止双进程重复推送）
    """
    global _monitor_heartbeat_ts
    positions = _load()
    if not positions:
        now = time.time()
        if now - _monitor_heartbeat_ts > 60:
            _monitor_heartbeat_ts = now
            _pmlog('[监控心跳] 无持仓')
        return []
    now = time.time()
    if now - _monitor_heartbeat_ts > 60:
        _monitor_heartbeat_ts = now
        _, _, _, get_price, _, _, _, _ = _s6api()
        parts = []
        for s, p in list(positions.items())[:8]:
            entry = p.get('entry')
            side_mark = p.get('side', '?')[:1]
            if entry:
                try:
                    cur = get_price(s)
                    if cur:
                        pnl = (entry - cur) / entry * 100 if p.get('side') == 'SHORT' else (cur - entry) / entry * 100
                        parts.append(f'{s}({side_mark} {pnl:+.1f}%)')
                    else:
                        parts.append(f'{s}({side_mark})')
                except Exception:
                    parts.append(f'{s}({side_mark})')
            else:
                parts.append(f'{s}({side_mark})')
        _pmlog(f'[监控心跳] {", ".join(parts)}' if parts else '[监控心跳] 无持仓')
    # Step 0: Ghost 清理 — 对比交易所实盘，已平但 PM 未知的仓位
    ghost_closed = _ghost_cleanup(positions, system_filter)
    # 过滤系统
    if system_filter:
        all_positions = positions
        positions = {s: p for s, p in positions.items() if p.get('system', '').startswith(system_filter)}
    else:
        all_positions = positions
    closed = list(ghost_closed)
    if positions:
        for symbol in list(positions.keys()):
            try:
                r = _monitor_one(symbol, positions[symbol], all_positions)
                if r:
                    closed.append((symbol, *r))
            except Exception as e:
                _pmlog(f'[监控异常] {symbol}: {e}')
    # 消费本轮幽灵仓（AlgoSL 平仓），只消费属于本系统的
    closed_syms = {c[0] for c in closed}
    remaining = []
    while _RECENTLY_GHOSTED:
        g = _RECENTLY_GHOSTED.pop(0)
        g_sym = g[0]
        g_side = g[5] if len(g) >= 6 else None
        if g_sym in closed_syms:
            continue  # ghost_cleanup 已经处理过了，跳过重复
        if not system_filter or not g_side:
            closed.append(g)
            closed_syms.add(g_sym)
        elif system_filter == 'S6' and g_side == 'LONG':
            closed.append(g)
            closed_syms.add(g_sym)
        elif system_filter == 'S8' and g_side == 'SHORT':
            closed.append(g)
            closed_syms.add(g_sym)
        else:
            remaining.append(g)
    _RECENTLY_GHOSTED.extend(remaining)
    _save(all_positions)

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
        if _close(symbol, pos, price, reason, positions):
            return (reason, price, entry, pos['qty'], pos['side'])
        return None
    if pos['side'] == 'LONG' and fund_rate > 0.005:
        _pmlog(f'[费率警告] {symbol} LONG 资金费率 {fund_rate:.4%} >0.5% 强制平仓')
        reason = f'资金费率过高 {fund_rate:.4%}'
        if _close(symbol, pos, price, reason, positions):
            return (reason, price, entry, pos['qty'], pos['side'])
        return None
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
        if _close(symbol, pos, price, '硬止损', positions):
            return ('硬止损', price, entry, pos['qty'], pos['side'])
        return None

    # 2. 紧急止损（主止损 — Binance 已废弃 STOP_MARKET，全靠轮询）
    max_loss = cfg.get('sl_breach_max', -8.0)
    if pnl < max_loss:
        reason = f'紧急止损 pnl={pnl:.1f}%'
        if _close(symbol, pos, price, reason, positions):
            return (reason, price, entry, pos['qty'], pos['side'])
        return None

    # Early-loss protection: cut a losing trade after 30 minutes only when
    # 15m momentum still confirms the adverse direction.
    if hold >= 30 and pnl <= -2.0:
        try:
            k15 = _get_data_cache().get_klines(symbol, '15m', 4)
            if _early_loss_momentum_weak(k15, pos['side']):
                reason = f'早期亏损保护 pnl={pnl:.1f}%'
                if _close(symbol, pos, price, reason, positions):
                    return (reason, price, entry, pos['qty'], pos['side'])
                return None
        except Exception as e:
            _pmlog(f'[早期亏损保护异常] {symbol}: {e}')

    # 3. be_done：盈利达标 → 止损移到成本
    be_pct = cfg.get('be_done_threshold', 2.0)
    if not pos.get('be_done') and pnl >= be_pct:
        _update_stop_loss(symbol, pos, price, entry)

    # 4. 分层止盈：浮盈达到阈值时平掉部分仓位
    partial_tp = cfg.get('partial_tp', {})
    if partial_tp and pnl > 0:
        # 按阈值升序检查（低→高），避免低阈值被高阈值覆盖
        for tp_pct in sorted(partial_tp.keys()):
            if tp_pct <= pnl and tp_pct not in pos.get('tp_done', []):
                close_ratio = partial_tp[tp_pct]
                close_qty = _round_qty(symbol, pos['qty'] * close_ratio)
                if close_qty > 0 and pos['qty'] > close_qty:
                    pos['tp_done'] = pos.get('tp_done', []) + [tp_pct]
                    _partial_close(symbol, pos, price, close_qty, tp_pct, positions)
                break  # 每次只触发一层

    # 5. 追踪锁利
    if pos.get('be_done') and pnl >= be_pct:
        trail_cfg = cfg.get('trail', {'base_mult': 0.3})
        trail_result = _calc_trail_sl(symbol, pos, price, trail_cfg, positions)
        if trail_result == 'exit':
            # 等待区确认：2根连续收>EMA20 → 趋势反转离场
            if _close(symbol, pos, price, '趋势反转（2次收>EMA20）', positions):
                return ('趋势反转', price, entry, pos['qty'], pos['side'])
            return None
        elif trail_result is not None:
            # 新追踪价位
            _place_trail_sl(symbol, pos, trail_result, positions)

    # 6. 1h EMA 安全阀：大周期趋势转向 → 强制离场（但免开仓后前60分钟）
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
                    if _should_exit_1h_reversal(pnl):
                        if _close(symbol, pos, price, '1h趋势反转', positions):
                            return ('1h趋势反转', price, entry, pos['qty'], pos['side'])
                    elif not pos.get('trend_reversal_warned'):
                        pos['trend_reversal_warned'] = True
                        _pmlog(f'[1h反转观察] {symbol} 当前亏损 {pnl:+.1f}%，暂不平仓，交给止损/时间止损处理')
                    return None
                elif pos['side'] != 'SHORT' and ema9_1h < ema20_1h * 0.98:
                    if _should_exit_1h_reversal(pnl):
                        if _close(symbol, pos, price, '1h趋势反转', positions):
                            return ('1h趋势反转', price, entry, pos['qty'], pos['side'])
                    elif not pos.get('trend_reversal_warned'):
                        pos['trend_reversal_warned'] = True
                        _pmlog(f'[1h反转观察] {symbol} 当前亏损 {pnl:+.1f}%，暂不平仓，交给止损/时间止损处理')
                    return None
        except Exception:
            pass

    # 7. 时间止损
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
            if _close(symbol, pos, price, '时间止损', positions):
                return ('时间止损', price, entry, pos['qty'], pos['side'])
            return None
        elif pnl < be_pct:
            # 微盈/不亏 — 提前释放
            if _close(symbol, pos, price, '时间止损', positions):
                return ('时间止损', price, entry, pos['qty'], pos['side'])
            return None

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


def _calc_trail_sl(symbol: str, pos: dict, price: float, trail_cfg: dict, positions: dict):
    """
    统一追踪止损。

    trail_cfg:
      base_mult      — 基础间距 (×ATR)
      tighten_pct    — 浮盈≥此值开始收紧
      tighten_min    — 最紧间距倍率
      breakeven_atr  — 浮盈≥此值×ATR → 保本加固

    策略：
    1. 多周期 ATR 基价（max(15m_ATR, 1h_ATR/4, 价格×1.5%)）
    2. 泵后币自动 ×1.5 间距（24h 振幅 > 4%）
    3. 15m 收盘确认（不收市不追踪）
    4. 收 > EMA20 → 等待区（下一根验证后 exit）
    5. 保本加固（breakeven_atr）

    返回：
      None     → 不需要更新
      数值     → 新止损价
      'exit'   → 趋势反转离场
    """
    dc = _get_data_cache()
    mult = float(trail_cfg.get('base_mult', 0.3))
    tighten_pct = float(trail_cfg.get('tighten_pct', 8.0))
    tighten_min = float(trail_cfg.get('tighten_min', 0.5))
    be_atr = trail_cfg.get('breakeven_atr')  # None 表示不做

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
    if pos['side'] == 'SHORT':
        pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
    else:
        pnl_pct = (price - pos['entry']) / pos['entry'] * 100
    if pnl_pct > tighten_pct:
        tighten = max(tighten_min, 1.0 - (pnl_pct - tighten_pct) / 20)
        mult *= tighten
        pump_mult = min(pump_mult, 1.5)

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

    last_closed_ts = k15[-2][0]
    if pos.get('last_15m_candle', 0) >= last_closed_ts:
        return None
    pos['last_15m_candle'] = last_closed_ts

    if len(k15) < 21:
        return None
    c15 = [float(x[4]) for x in k15[-21:]]
    ema20_15 = sum(c15[-20:]) / 20
    last_close = float(k15[-2][4])

    # ── 3. 等待区逻辑（收 > EMA20）──
    wait_key = 'trail_confirm_until'
    if pos['side'] == 'SHORT' and last_close > ema20_15:
        if pos.get(wait_key):
            pos.pop(wait_key, None)
            return 'exit'
        else:
            pos[wait_key] = last_closed_ts
            return None
    elif pos['side'] != 'SHORT' and last_close < ema20_15:
        if pos.get(wait_key):
            pos.pop(wait_key, None)
            return 'exit'
        else:
            pos[wait_key] = last_closed_ts
            return None

    pos.pop(wait_key, None)

    # ── 4. 计算新止损价（吊灯式 Chandelier Exit：锚定持仓期极值，天然只升不降）──
    new_sl = None
    if pos['side'] == 'SHORT':
        lowest = min(pos.get('lowest', pos['entry']), price)
        pos['lowest'] = lowest
        sl = round(lowest + effective_atr, 6)
        sl = max(sl, round(ema20_15, 6))
        if sl < pos['sl'] and sl > price:
            new_sl = sl
    else:
        highest = max(pos.get('highest', pos['entry']), price)
        pos['highest'] = highest
        sl = round(highest - effective_atr, 6)
        sl = min(sl, round(ema20_15, 6))
        if sl > pos['sl'] and sl < price:
            new_sl = sl

    # ── 5. 保本加固：浮盈≥breakeven_atr×ATR 时止损拉到成本 ──
    #    注意：只允许向有利方向调整（LONG 只升不降, SHORT 只降不升），防止震荡
    if be_atr is not None and base_atr > 0:
        if pos['side'] == 'SHORT':
            profit_atr = (pos['entry'] - price) / base_atr
            if profit_atr >= be_atr:
                entry_be = round(pos['entry'] * 1.001, 6)
                if entry_be < pos['sl'] and (new_sl is None or (entry_be < new_sl and entry_be > price)):
                    new_sl = entry_be
        else:
            profit_atr = (price - pos['entry']) / base_atr
            if profit_atr >= be_atr:
                entry_be = round(pos['entry'] * 0.999, 6)
                if entry_be > pos['sl'] and (new_sl is None or (entry_be > new_sl and entry_be < price)):
                    new_sl = entry_be

    if new_sl is not None and new_sl == pos.get('sl'):
        return None
    return new_sl


def _place_trail_sl(symbol: str, pos: dict, trail_sl: float, positions: dict):
    """更新追踪止损（轮询用） + 尝试同步到Algo Order API"""
    global _last_algo_update
    now = time.time()
    _, _, _, _, _, _, _, _ = _s6api()
    old = pos['sl']
    if trail_sl == old:
        return

    # ── 节流：AlgoSL 更新间隔 < 60s 或 SL 变化 < 0.2% 时只存本地，不更新交易所 ──
    change_pct = abs(trail_sl - old) / old * 100 if old else 0
    if now - _last_algo_update.get(symbol, 0) < _ALGO_UPDATE_INTERVAL and change_pct < _ALGO_MIN_CHANGE_PCT:
        pos['sl'] = trail_sl
        _save(positions)
        return

    pos['sl'] = trail_sl
    _last_algo_update[symbol] = now
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
    return _close(symbol, pos, price, reason, positions, force=True)


def _mark_closed(symbol: str):
    """跨进程标记：该 symbol 已被 _close 处理过（Redis + 4h TTL），幽灵忽略"""
    try:
        _rset(f'closed:{symbol}', {'ts': time.time()})
    except Exception:
        pass


def _was_closed_recently(symbol: str, within_hours: int = 4) -> bool:
    """检查 symbol 近期是否被 _close 处理过（Redis 原子性，跨进程共享）"""
    try:
        data = _rget(f'closed:{symbol}')
        if data and 'ts' in data:
            return time.time() - data['ts'] < within_hours * 3600
    except Exception:
        pass
    return False


def _clear_closed_marker(symbol: str):
    """清除关闭标记（仓位重新打开后调用）"""
    try:
        from shared.redis_store import delete as _rdelete
        _rdelete(f'closed:{symbol}')
    except Exception:
        pass


def _partial_close(symbol: str, pos: dict, price: float, close_qty: float,
                   tp_pct: float, positions: dict):
    """分层止盈：市价平掉 close_qty 数量，保留剩余仓位"""
    _, fapi_post, _, _, _, _, _, _ = _s6api()
    close_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
    try:
        r = fapi_post('/fapi/v1/order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': close_qty, 'positionSide': 'BOTH',
        })
        if not isinstance(r, dict) or r.get('code') is not None:
            _pmlog(f'[分层止盈失败] {symbol} qty={close_qty}: 交易所拒绝 {r}')
            return
    except Exception as e:
        _pmlog(f'[分层止盈失败] {symbol} qty={close_qty}: {e}')
        return
    pos['qty'] = round(pos['qty'] - close_qty, 4)
    pnl_u = round((price - pos['entry']) * close_qty, 2) if pos['side'] == 'LONG' \
        else round((pos['entry'] - price) * close_qty, 2)
    _save(positions)
    _pmlog(f'[分层止盈] {symbol} +{pnl_u:.2f}USDT qty={close_qty} 剩余={pos["qty"]} ({tp_pct}%)')


def _close(symbol: str, pos: dict, price: float, reason: str, positions: dict, *,
           force: bool = False) -> bool:
    """内部平仓：取消条件单 → 确认实盘 → 市价平 → 落库 → 删记录
    force=False 时防重入：该币 4h 内已被处理过则直接跳过，避免双进程重复平仓/重复记账。"""
    if not force and _was_closed_recently(symbol):
        _pmlog(f'[平仓跳过] {symbol} 近期已处理，防止重复平仓 ({reason})')
        return False
    _mark_closed(symbol)
    _, fapi_post, _, _, _, _, _, record_trade = _s6api()

    # ═══ 沙盘模式：跳过 Binance API 检查，直接记录 ═══
    if _sandbox_active():
        try:
            from scripts.sandbox import _close_position as _sb_close
            _sb_close(symbol)
        except Exception:
            pass
        _pmlog(f'[平仓·沙盘] {symbol} {reason} 入场={pos.get("entry")} 现价={price}')
        close_qty = pos.get('original_qty', pos.get('qty', 0))
        if pos['side'] == 'SHORT':
            pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
            pnl_u   = round((pos['entry'] - price) * close_qty, 2)
        else:
            pnl_pct = (price - pos['entry']) / pos['entry'] * 100
            pnl_u   = round((price - pos['entry']) * close_qty, 2)
        record_trade(symbol, pos['entry'], price, close_qty, pos.get("leverage", 3),
                     pos.get('system', ''), pos['open_time'],
                     exit_reason=reason, side=pos['side'],
                     score=pos.get('score', 0),
                     atr_entry=pos.get('atr', 0),
                     sl_price=pos.get('sl', 0),
                      margin_mode=pos.get('margin', ''),
                      be_done=pos.get('be_done', False),
                      trail_active=pos.get('trail', False),
                      algo_sl_id=pos.get('algo_sl_id', 0),
                      position_id=_position_id(symbol, pos), final_close=True)
        positions.pop(symbol, None)
        _save(positions)
        return True

    # ═══ 实盘模式 ═══
    fapi_get, fapi_post, _, _, _, _, _, record_trade = _s6api()

    try:
        real_r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
        if pos['side'] == 'SHORT':
            real_pos = next((x for x in real_r if isinstance(x, dict)
                           and x.get('symbol') == symbol
                           and float(x.get('positionAmt', 0)) < 0), None)
        else:
            real_pos = next((x for x in real_r if isinstance(x, dict)
                           and x.get('symbol') == symbol
                           and float(x.get('positionAmt', 0)) > 0), None)
        if not real_pos:
            # 止损单已在交易所触发平仓，记录本次平仓
            try:
                _cancel_all_algo(symbol)
                _pmlog(f'[平仓Algo取消] {symbol} 已清理全部条件单')
            except Exception as e:
                _pmlog(f'[平仓Algo取消异常] {symbol}: {e}')
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
                         algo_sl_id=pos.get('algo_sl_id', 0),
                         position_id=_position_id(symbol, pos), final_close=True)
            _pg_record_event({
                'event_id': f"position:{_position_id(symbol, pos)}:flat",
                'position_id': _position_id(symbol, pos),
                'event_type': 'EXCHANGE_POSITION_FLAT',
                'order_id': '', 'fill_id': '', 'price': price, 'qty': close_qty,
                'realized_pnl': pnl_u, 'payload': {'reason': reason},
            })
            positions.pop(symbol, None)
            _save(positions)
            return True

        requested_close_qty = _round_qty(symbol, abs(float(real_pos['positionAmt'])))
        close_qty = requested_close_qty
        close_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
        result = fapi_post('/fapi/v1/order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': close_qty, 'positionSide': 'BOTH', 'reduceOnly': 'true',
        })
        if isinstance(result, dict) and result.get('code'):
            _log_close_error(symbol, result.get('msg', result), interval=60)
            # 不要在市价单失败前删除原止损单，避免仓位裸奔。
            _clear_closed_marker(symbol)
            return False

        # Market orders can be partially filled. Keep the position and
        # accumulate this slice until positionRisk confirms it is flat.
        reported_filled_qty = abs(float(result.get('executedQty', 0))) if isinstance(result, dict) else 0.0
        remaining_r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
        remaining_pos = next((x for x in (remaining_r or []) if isinstance(x, dict)
                              and x.get('symbol') == symbol
                              and ((pos['side'] == 'SHORT' and float(x.get('positionAmt', 0)) < 0)
                                   or (pos['side'] != 'SHORT' and float(x.get('positionAmt', 0)) > 0))), None)
        remaining_qty = abs(float(remaining_pos.get('positionAmt', 0))) if remaining_pos else 0.0
        if remaining_qty >= 0.001:
            if reported_filled_qty < 0.001:
                pos['qty'] = remaining_qty
                positions[symbol] = pos
                _save(positions)
                _log_close_error(symbol, '平仓响应无成交数量，保留仓位等待重试')
                return False
            filled_qty = reported_filled_qty
            pos['qty'] = remaining_qty
            positions[symbol] = pos
            _save(positions)
            record_trade(symbol, pos['entry'], price, filled_qty, pos.get("leverage", 3),
                         pos.get('system', ''), pos['open_time'],
                         exit_reason=reason, side=pos['side'],
                         score=pos.get('score', 0), atr_entry=pos.get('atr', 0),
                         sl_price=pos.get('sl', 0), margin_mode=pos.get('margin', ''),
                         be_done=pos.get('be_done', False),
                         trail_active=pos.get('trail', False),
                         algo_sl_id=pos.get('algo_sl_id', 0),
                         position_id=_position_id(symbol, pos), final_close=False)
            _pg_record_event({
                'event_id': f"order:{result.get('orderId', '')}:close:{filled_qty}",
                'position_id': _position_id(symbol, pos),
                'event_type': 'CLOSE_ORDER_PARTIAL',
                'order_id': str(result.get('orderId', '')), 'fill_id': '',
                'price': price, 'qty': filled_qty, 'realized_pnl': 0.0,
                'payload': result,
            })
            _pmlog(f'[平仓部分成交] {symbol} qty={filled_qty} 剩余={remaining_qty}')
            return False
        # Some Binance-compatible responses omit executedQty on a filled
        # market order. If positionRisk is flat, the requested quantity is
        # the only safe fallback for accounting.
        close_qty = reported_filled_qty if reported_filled_qty >= 0.001 else requested_close_qty
    except Exception as e:
        _pmlog(f'[平仓异常] {symbol}: {e}')
        _clear_closed_marker(symbol)
        return False

    # 市价平仓成功后再清理剩余条件单。
    try:
        _cancel_all_algo(symbol)
        _pmlog(f'[平仓Algo取消] {symbol} 已清理全部条件单')
    except Exception as e:
        _pmlog(f'[平仓Algo取消异常] {symbol}: {e}')

    # 盈亏
    if pos['side'] == 'SHORT':
        pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
        pnl_u   = round((pos['entry'] - price) * close_qty, 2)
    else:
        pnl_pct = (price - pos['entry']) / pos['entry'] * 100
        pnl_u   = round((price - pos['entry']) * close_qty, 2)

    _pg_record_event({
        'event_id': f"order:{result.get('orderId', '')}:close:final",
        'position_id': _position_id(symbol, pos),
        'event_type': 'CLOSE_ORDER_FILLED',
        'order_id': str(result.get('orderId', '')), 'fill_id': '',
        'price': price, 'qty': close_qty, 'realized_pnl': pnl_u,
        'payload': result,
    })

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
                  algo_sl_id=pos.get('algo_sl_id', 0),
                  position_id=_position_id(symbol, pos), final_close=True)

    # 删记录
    positions.pop(symbol, None)
    _save(positions)
    return True


def _set_cooldown(symbol: str, system: str, pnl_pct: float):
    """平仓后写入对应系统的冷却期（由策略主循环负责，PM 写入会导致并发覆盖）"""
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
