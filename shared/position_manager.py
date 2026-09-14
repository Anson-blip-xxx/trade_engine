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
from shared.redis_store import lock_acquire as _lock_acquire, lock_release as _lock_release
from shared.binance_api import FAPI as _FAPI, TG_TOKEN as _TG_TOKEN, TG_CHAT_ID as _TG_CHAT_ID
from shared.postgres_client import record_trade_event as _pg_record_event
from shared.exit_factors import (
    should_exit_on_1h_reversal, early_loss_momentum_weak, is_stagnant_profit,
)
from execution import core as _exec_core
from execution import service as _exec_service
from execution.adapters import binance as _exec_binance
from execution.adapters import position_state as _exec_pos_state
from position_state import service as ps_service
from position_state.service import parse_position_risk as _ext_pos_parse

from position_protection import service as pp_service

from position_monitoring import service as _mon_svc

from position_reconcile import service as _rc_service
from position_reconcile import deps as _rc_deps

from position_lifecycle import service as _lc_service
from position_lifecycle import deps as _lc_deps
from position_market import funding as _funding_helper
from position_market import auth as _auth_helper
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
    """Lazy/env 读取（P8-04：parse 机械迁 position_market.auth；
    `is None` call-time 懒加载判别 + globals 缓存语义 == HEAD）。"""
    global _API_KEY, _API_SECRET
    if _API_KEY is None:
        api_key, secret = _auth_helper.load_api_keys(_BASE / 'config/binance.env')
        if api_key is not None:
            _API_KEY = api_key
        if secret is not None:
            _API_SECRET = secret

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
_last_algo_update = {}  # {symbol: timestamp} AlgoSL 更新节流

# Algo Order API 限速：队列消费，后台线程以 11s 间隔依次处理
_ALGO_QUEUE = []           # [(symbol, side, trigger_price, qty), ...]
_ALGO_QUEUE_LOCK = threading.Lock()
_ALGO_WORKER_STARTED = False

def _protection_service() -> 'pp_service.ProtectionService':
    """P7-04B 晚绑定 factory：每次 algo SL 调用解析当前模块态
    （`_algo_enqueue/_algo_start_worker/_algo_place_sl_inner/_algo_cancel/
    _cancel_all_algo` 均可在调用时被 monkeypatch 替换——测试 seam 保留）。"""
    from position_protection import service as _pps
    return _pps.ProtectionService(
        enqueue_fn=_algo_enqueue,
        start_worker_fn=_algo_start_worker,
        place_algo_sl_fn=_algo_place_sl_inner,
        cancel_id_fn=_algo_cancel,
        cancel_all_fn=_cancel_all_algo,
    )


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
# P8-01：_WS_LEASE_KEY/_WS_LEASE_TTL 静态值迁 position_config
# （见 L411 import；原位 alias 由 import 供给，seam 不变）
_WS_INSTANCE = f'{os.getpid()}-{uuid.uuid4().hex[:8]}'

def _ws_am_leader() -> bool:
    """（P7-05B：交换到 PositionMonitoringService — thin delegation，
    fail-open 语义经 P7-05A golden 冻结。）"""
    return _monitoring_service().am_leader()

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
    """（P7-05B：交换到 PositionMonitoringService — thin delegation，
    schema/平仓顺序经 P7-05A golden 冻结。）"""
    _monitoring_service().ws_on_message(ws, message)

def _ws_on_open(ws):
    _pmlog('[WS已连接] 开始接收实时仓位')

def _ws_on_error(ws, error):
    _pmlog(f'[WS错误] {error}')

def _ws_on_close(ws, close_status_code, close_msg):
    _pmlog(f'[WS断开] code={close_status_code} msg={close_msg} 5s后重连')

def _ws_connect_loop():
    """（P7-05B：指定交换到 PositionMonitoringService — thin delegation，
    cadence/leader 语义经 P7-05A golden 冻结；线程 spawn 仍由本入口触发
    （L562 `_WS_THREAD`），避免双 owner。）"""
    _monitoring_service().ws_connect_loop()

# ── 系统级参数 ──────────────────────────────────────────────────────────
# P8-01：SYSTEM_CFG 迁 position_config/constants.py（literal relocation）
from position_config.constants import (SYSTEM_CFG,
    _SYSTEM_KEYS, _WS_LEASE_KEY, _WS_LEASE_TTL,
    _API_COOLDOWN, _ALGO_UPDATE_INTERVAL,
    _ALGO_MIN_CHANGE_PCT)

def _funding_fetch_fn(symbol):
    """P8-03：transport 留 PM（C1 不迁）——注入式 fetch callable。"""
    return requests.get(f'{_FAPI}/fapi/v1/premiumIndex?symbol={symbol}',
                        timeout=5)


def _get_funding_rate(symbol: str) -> float:
    """获取当前资金费率，失败返回 0（P8-03：thin helper delegation；
    endpoint/params/parsing/failure→0 逐字；调用次数不变）。"""
    return _funding_helper.read_funding_rate(symbol, _funding_fetch_fn)


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
    """幽灵仓数据落库（P7-06B：thin delegation → ReconcileService，
    lock 内二次去重序经 P7-06A golden 冻结）。"""
    return _reconcile_service().try_record_ghost_trade(sym, meta)


def _load_meta() -> dict:
    """从 pm:positions 读取本地元数据（P7-02：经 StateService）。"""
    return _state_service().load_meta()

def _is_tradable_symbol(symbol: str) -> bool:
    """Engine trades *USDT futures only. Stale legacy testnet contracts
    (*USD_PERP) must never enter PM monitoring or alerts."""
    return bool(symbol) and symbol.endswith('USDT') and '_PERP' not in symbol


def _merge_meta(raw: dict[str, dict], meta: dict, now: float,
                alert_external: bool = False) -> dict:
    """用本地元数据 enrich 原始持仓数据。"""
    merged = {}
    for sym, bp in raw.items():
        if not _is_tradable_symbol(sym):
            continue
        if _was_closed_recently(sym):
            _pmlog(f'[闭标记修复] {sym} 实际仍有持仓，清除关闭标记')
            _clear_closed_marker(sym)
        mp = meta.pop(sym, {})
        if not mp and alert_external:
            _notify_external_position(sym, bp, 'S6' if bp['side'] == 'LONG' else 'S8')
        elif mp:
            _rset(f'alert:external_position:pending:{sym}', {})
        system_name = mp.get('system', 'S6' if bp['side'] == 'LONG' else 'S8')
        merged[sym] = {
            'entry': bp['entry'], 'side': bp['side'], 'qty': min(bp['qty'], mp.get('qty', bp['qty'])),
            'leverage': bp.get('leverage', 3),
            'margin': bp.get('margin', mp.get('margin', 'CROSSED')),
            'system': system_name,
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
                f"{system_name}:{sym}:{mp.get('open_time', now):.6f}",
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
    merged = _merge_meta(raw, dict(meta), now, alert_external=True)
    for symbol, position in meta.items():
        if symbol not in merged and not _was_closed_recently(symbol) and _is_tradable_symbol(symbol):
            merged[symbol] = position
    return merged

def _load() -> dict:
    """
    三层加载持仓：
      一层 WS 实时流 → 二层 REST 轮询 → 三层本地元数据
    沙盘模式：跳过 WS/REST，仅用本地。
    （P7-02：状态 assembly 委托 PositionStateService；P8-02：closure 外提
    成模块级 `_ws_snapshot/_merge_and_save/_meta_filtered`，链序/回退序/
    异常吞错逐字保持。）
    """
    return _state_service().load_assembly(
        ws_snapshot_fn=_ws_snapshot,
        rest_positions_fn=_rest_positions_snapshot,
        merge_and_save_fn=_merge_and_save,
        meta_filtered_fn=_meta_filtered,
    )


def _state_service() -> 'ps_service.PositionStateService':
    """P7-02 晚绑定 factory：每次 `_load`/marker 调用解析当前模块态
    （`_rget/_rset/_WS_*/shared.redis_store.delete` 均可在调用时被替换——
    monkeypatch seam 保留；OBS-5 经 direct delete callable 保持）。"""
    from shared.redis_store import delete as _direct_delete

    def _direct_marker_delete(key):
        _direct_delete(key)

    return ps_service.PositionStateService(
        state_port=_position_state(),
        marker_set=_rset,
        marker_get=_rget,
        marker_delete=_direct_marker_delete,
        sandbox_check=_sandbox_active,
    )


def _ws_snapshot() -> tuple:
    """P8-02：从 `_load` 内联 closure 外提的 WS 快照 glue（语义逐字：
    锁内 copy — fresh dict；backing 仍是 PM 单 owner）。"""
    with _WS_LOCK:
        return (_WS_LAST_UPDATE, dict(_WS_POSITIONS))


def _merge_and_save(raw, now) -> dict:
    """三层链第 2/3 层 spare merge 胶水（P7-02 冻结语义；
    依赖均经模块全局晚绑定——monkeypatch seam 保留）。"""
    meta = _load_meta()
    merged = _merge_meta_preserving_missing(raw, meta, now)
    _save(merged)
    return merged


def _meta_filtered() -> dict:
    """第 3 层 meta 兜底（recently closed 过滤；merged 非空才 save
    ——PMB-19 双 save 的第一笔）。"""
    meta = _load_meta()
    merged = {s: p for s, p in meta.items() if not _was_closed_recently(s)}
    if merged:
        _save(merged)
    return merged


def _rest_positions_snapshot() -> dict:
    """REST 轮询解析（P8-02：解析机械迁 StateService `parse_position_risk`；
    fetch/异常吞错留本 owner——语义逐字）。"""
    rest_positions: dict[str, dict] = {}
    try:
        real_r = _light_fapi_get('/fapi/v2/positionRisk')
        rest_positions = _ext_pos_parse(real_r, rest_positions)  # 增量语义逐字
    except Exception:
        pass
    return rest_positions


def _position_state() -> '_exec_pos_state.RedisPositionStateAdapter':
    """Position State factory (P4-03-01-D2 pilot wiring, only _save path goes through boundary).

    Injects the current module-level _rget/_rset each time via late binding (preserving monkeypatch
    test semantics); the rest of the key/value/TTL/exception semantics are mirrored inside the
    adapter (passed through verbatim from PM._save / _load_meta). Redis access for other keys
    (closed marker/locks/alerts) does not go through this boundary.
    """
    return _exec_pos_state.RedisPositionStateAdapter(_rget, _rset)


def _save(positions: dict):
    """Writes the pm:positions single source of truth (via PositionStatePort boundary,
    P4-03-01-D2 pilot: provable mechanical equivalence)."""
    _position_state().save_positions(positions)


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

def _lifecycle_service():
    """P7-07B 晚绑定 factory（P8-05B1：factory-time 构建新鲜 5 bundle
    → 新 LifecycleService；monkeypatch seam 保留；无 cached/singleton deps）。"""
    return _lc_service.PositionLifecycleService(
        runtime=_lc_deps.LifecycleRuntimeDeps(log=_pmlog, now=time.time),
        execution=_lc_deps.LifecycleExecutionDeps(
            exec_fn=_execution_service, ci=_exec_core.close_intent,
            pi=_exec_core.partial_close_intent),
        state=_lc_deps.LifecycleStateDeps(
            load=_load, save=_save,
            wcr=_was_closed_recently, mc=_mark_closed,
            clr=_clear_closed_marker, posid=_position_id, rq=_round_qty),
        protection=_lc_deps.LifecycleProtectionDeps(
            wkr=_algo_start_worker, enq=_algo_enqueue,
            acx=_algo_cancel, cxa=_cancel_all_algo),
        action=_lc_deps.LifecycleActionDeps(
            close_fn=_close, s6=_s6api, sandbox=_sandbox_active,
            pg=_pg_record_event,
            lce=_log_close_error),
    )


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
    """统一开仓（P7-07B：thin delegation → PositionLifecycleService；
    OBS-3 / PMB-30 序经 P7-07A golden 冻结）。"""
    return _lifecycle_service().open_position(
        symbol, side, entry, qty, leverage, sl, tp1, tp2,
        atr=atr, score=score, reasons=reasons, signal_type=signal_type,
        system=system, margin_type=margin_type, metadata=metadata)


# ═══════════════════════════════════════════════════════════════════════
#  监控
# ═══════════════════════════════════════════════════════════════════════

def _reconcile_service():
    """P7-06B 晚绑定 factory（P8-05B2：factory-time 构建新鲜 5 bundle
    → 新 ReconcileService；monkeypatch seam 保留；无 runtime backing
    迁移——`_RECENTLY_GHOSTED` 等仅消费仍经 monitoring）。"""
    return _rc_service.PositionReconcileService(
        runtime=_rc_deps.ReconcileRuntimeDeps(lgt=_pmlog, now=time.time),
        state=_rc_deps.ReconcileStateDeps(
            load=_load, save=_save,
            wcr=_was_closed_recently, mc=_mark_closed,
            posid=_position_id, rq=_round_qty),
        coordination=_rc_deps.ReconcileCoordinationDeps(
            lacq=_lock_acquire, lrel=_lock_release,
            rdget=_rget, rdset=_rset,
            pid=os.getpid, uid=lambda: uuid.uuid4().hex[:8]),
        notification=_rc_deps.ReconcileNotificationDeps(
            pg=_pg_record_event, tgt=_TG_TOKEN, tgc=_TG_CHAT_ID,
            rqst=requests),
        action=_rc_deps.ReconcileActionDeps(
            exf=_light_fapi_get, gpx=_light_get_price,
            s6=_s6api, sandbox=_sandbox_active,
            sk=_SYSTEM_KEYS),
    )


def _ghost_cleanup(positions: dict, system_filter: str = '') -> list:
    """幽灵仓清理（P7-06B：thin delegation → PositionReconcileService，
    行为经 P7-06A golden 冻结）。"""
    return _reconcile_service().ghost_cleanup(positions, system_filter)


def _ghost_cleanup_one(sym: str, pos: dict, positions: dict, record_trade, closed: list):
    """（P7-06B：thin delegation——pop→record 序经 PMB-23 冻结。）"""
    _reconcile_service().ghost_cleanup_one(sym, pos, positions,
                                           record_trade, closed)


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


def _notify_external_position(symbol: str, raw: dict, system: str):
    """（P7-06B：thin delegation → ReconcileService；30s/24h 语义与
    TG/PG 吞错不对称经 P7-06A golden 冻结。）"""
    _reconcile_service().notify_external_position(symbol, raw, system)


def _should_exit_1h_reversal(pnl: float) -> bool:
    """Only exit on reversal once the position is at least breakeven."""
    return should_exit_on_1h_reversal(pnl)


def _early_loss_momentum_weak(klines: list, side: str) -> bool:
    """Check whether the last 15m candles still move against the position."""
    return early_loss_momentum_weak(klines, side)


def _is_stagnant_profit(pnl_usdt: float, hold_min: float,
                        min_hold_min: float = 90,
                        max_profit_usdt: float = 1.0) -> bool:
    """Identify profitable positions that no longer justify capital use."""
    return is_stagnant_profit(pnl_usdt, hold_min, min_hold_min, max_profit_usdt)

def _monitoring_service():
    """P7-05B 晚绑定 factory：每次调用解析当前模块态，注入 Monitoring
    Service（monkeypatch seam 保留；runtime backing 单一 = 模块全局。"""
    return _mon_svc.PositionMonitoringService(
        now=time.time, log=_pmlog, load=_load, save=_save,
        m1=_monitor_one, gcl=_ghost_cleanup, gq=_RECENTLY_GHOSTED,
        summ=log_position_summary,
        s6=_s6api,
        ghb=lambda: globals()['_monitor_heartbeat_ts'],
        shb=lambda v: globals().__setitem__('_monitor_heartbeat_ts', v),
        cfg=_get_cfg, fund=_get_funding_rate,
        cls=_close,
        dc=_get_data_cache, elm=_early_loss_momentum_weak,
        stag=_is_stagnant_profit, g1h=_should_exit_1h_reversal,
        us=_update_stop_loss, pc=_partial_close, rq=_round_qty,
        pp=_peak_pullback_check, cts=_calc_trail_sl, pt=_place_trail_sl,
        wsl=_WS_LOCK, wsp=_WS_POSITIONS,
        swlu=lambda v: globals().__setitem__('_WS_LAST_UPDATE', v),
        wst=lambda: globals()['_WS_STOP'],
        ldr=lambda: _ws_am_leader(),
        wcr=_was_closed_recently,
        trgt=_try_record_ghost_trade,
        mc=_mark_closed,
        lkey=_WS_LEASE_KEY, lttl=_WS_LEASE_TTL, inst=_WS_INSTANCE,
        lkfn=_ws_listen_key, wsf=_ws_url,
        oofn=lambda: _ws_on_open, oe=lambda: _ws_on_error,
        oc=lambda: _ws_on_close,
    )


def monitor_all(system_filter: str = '') -> list:
    """
    统一监控所有持仓（P7-05B：指定交换到 PositionMonitoringService — thin
    delegation；11 步出场链/节流/ghost 序/ref 语义经 P7-05A golden 冻结）。

    system_filter: 如 'S6' 则只处理该系统的持仓（防止双进程重复推送）
    """
    return _monitoring_service().monitor_all(system_filter)



def _monitor_one(symbol: str, pos: dict, positions: dict):
    """单币种：硬止损 → be_done → 追踪锁利 → 时间止损
    （P7-05B：指定交换到 PositionMonitoringService — thin delegation，
    11 步出场链经 P7-05A golden 冻结）。"""
    return _monitoring_service().monitor_one(symbol, pos, positions)


# ═══════════════════════════════════════════════════════════════════════
#  对账
# ═══════════════════════════════════════════════════════════════════════

def reconcile_all():
    """
    对账（P7-06B：thin delegation → PositionReconcileService；silent
    第二通道语义经 P7-06A golden 冻结——无 record/mark/lock/adoption）。
    """
    return _reconcile_service().reconcile_all()


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


def _peak_pullback_check(pos: dict, price: float, cfg: dict) -> str | float | None:
    """实时峰值回撤保护：浮盈达标后跟踪极值，实时把锁利止损推到交易所。

    与追踪锁利的区别：逐监控周期实时判断，不等 15m 收盘确认，
    专门防"浮盈到顶后回踩拉升、利润全部吐回"的快速反转。

    返回语义：
      None   → 未武装，不动作
      float  → 本次应上移的锁利止损价（交给 _place_trail_sl 推到交易所）
      str    → 回撤已超阈值，直接平仓

    一旦武装（浮盈≥trigger_pct）即保持武装，回踩导致 pnl 跌破触发值时
    保护仍生效（否则从峰值回踩的一瞬间 pnl 会先跌破阈值，保护被解除）。
    """
    guard = cfg.get('peak_guard', {}) or {}
    if not guard:
        return None
    trigger_pct = float(guard.get('trigger_pct', 3.0))
    dd_pct = float(guard.get('drawdown_pct', 2.0))
    entry = float(pos.get('entry', 0) or 0)
    if entry <= 0:
        return None
    if pos['side'] == 'SHORT':
        pnl_pct = (entry - price) / entry * 100
    else:
        pnl_pct = (price - entry) / entry * 100

    if not pos.get('peak_guard_armed'):
        if pnl_pct < trigger_pct:
            return None
        pos['peak_guard_armed'] = True
        pos['lowest'] = min(float(pos.get('lowest', price) or price), price)
        pos['highest'] = max(float(pos.get('highest', price) or price), price)

    if pos['side'] == 'SHORT':
        pos['lowest'] = min(float(pos.get('lowest', price) or price), price)
        extreme = pos['lowest']
        pullback = (price - extreme) / extreme * 100 if extreme > 0 else 0
        locked_sl = round(extreme * (1 + dd_pct / 100), 6)
    else:
        pos['highest'] = max(float(pos.get('highest', price) or price), price)
        extreme = pos['highest']
        pullback = (extreme - price) / extreme * 100 if extreme > 0 else 0
        locked_sl = round(extreme * (1 - dd_pct / 100), 6)

    if pullback >= dd_pct:
        return f'峰值回撤保护 pnl={pnl_pct:.1f}% 回撤{pullback:.1f}%'
    return locked_sl


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
        max_dd = float(trail_cfg.get('max_drawdown_pct', 0) or 0)
        if max_dd > 0:
            sl = min(sl, round(lowest * (1 + max_dd / 100), 6))
        if pnl_pct >= tighten_pct:
            min_lock = float(trail_cfg.get('min_profit_lock_pct', 0))
            if min_lock > 0:
                sl = min(sl, round(pos['entry'] * (1 - min_lock / 100), 6))
        if sl < pos['sl'] and sl > price:
            new_sl = sl
    else:
        highest = max(pos.get('highest', pos['entry']), price)
        pos['highest'] = highest
        sl = round(highest - effective_atr, 6)
        sl = min(sl, round(ema20_15, 6))
        max_dd = float(trail_cfg.get('max_drawdown_pct', 0) or 0)
        if max_dd > 0:
            sl = max(sl, round(highest * (1 - max_dd / 100), 6))
        if pnl_pct >= tighten_pct:
            min_lock = float(trail_cfg.get('min_profit_lock_pct', 0))
            if min_lock > 0:
                sl = max(sl, round(pos['entry'] * (1 + min_lock / 100), 6))
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

def _execution_service() -> '_exec_service.ExecutionService':
    """close/partial 路径 ExecutionService 工厂（P4-03-01-C 接线）。

    每次调用以**当前 _s6api() 返回的 fapi_post** 构建 adapter（晚绑定）：
    - 保留 _s6api 双实现错误语义（binance_api 上抛 / _light 返回 None，N2）
    - 沙盘仍由 _close 的 _sandbox_active() 前置判断（不在 service 内，不统一）
    - Adapter 无 try/except：异常语义 = 注入的 fapi_post 原样（无 retry）
    不持全局实例，无新增可变状态。
    """
    _, fapi_post, _, _, _, _, _, _ = _s6api()
    return _exec_service.ExecutionService(
        binance=_exec_binance.SharedExecutorBinanceAdapter(fapi_post))


def close_position(symbol: str, reason: str) -> bool:
    """外部调用平仓（P7-07B：thin delegation → LifecycleService）。"""
    return _lifecycle_service().close_position(symbol, reason)


def _mark_closed(symbol: str):
    """跨进程标记：该 symbol 已被 _close 处理过（Redis；4h 由 ts 比较实现，
    非 TTL——PMB-4 冻结）（P7-02：经 StateService，失败吞错语义不变）"""
    try:
        _state_service().mark_closed(symbol)
    except Exception:
        pass


def _was_closed_recently(symbol: str, within_hours: int = 4) -> bool:
    """检查 symbol 近期是否被 _close 处理过（Redis 原子性，跨进程共享）
    （P7-02：经 StateService；ts 比较窗口语义不变）"""
    try:
        return _state_service().was_closed_recently(symbol, within_hours)
    except Exception:
        return False


def _clear_closed_marker(symbol: str):
    """清除关闭标记（仓位重新打开后调用）
    （P7-02：OBS-5 直连 delete seam 经注入 callable 保留；失败吞错不变）"""
    try:
        _state_service().clear_closed(symbol)
    except Exception:
        pass


def _partial_close(symbol: str, pos: dict, price: float, close_qty: float,
                   tp_pct: float, positions: dict):
    """分层止盈（P7-07B：thin delegation → LifecycleService；无 reduceOnly/
    负数增仓/0 ledger 语义经 E-OBS-5/7、PMB-6 冻结）。"""
    _lifecycle_service().partial_close(symbol, pos, price, close_qty,
                                       tp_pct, positions)


def _close(symbol: str, pos: dict, price: float, reason: str, positions: dict, *,
           force: bool = False) -> bool:
    """内部平仓（P7-07B：thin delegation → LifecycleService；marker-first
    /save 序不对称经 P7-07A golden 冻结）。"""
    return _lifecycle_service().close(symbol, pos, price, reason, positions,
                                      force=force)

def _set_cooldown(symbol: str, system: str, pnl_pct: float):
    """平仓后写入对应系统的冷却期（由策略主循环负责，PM 写入会导致并发覆盖）"""
    pass



def migrate_existing_positions():
    """迁移各系统现存持仓到 PM（P7-06B：thin delegation →
    ReconcileService；启动 once 语义经 P7-06A golden 冻结）。"""
    return _reconcile_service().migrate_existing_positions()
