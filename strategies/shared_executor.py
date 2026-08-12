"""
Shared executor base — 供 S6 (Long) / S8 (Short) 复用
职责: S3 事件读取、去重、状态管理、PM 集成、告警
"""
import json, time, threading, sys, requests, hashlib, hmac, os
from urllib.parse import urlencode
from pathlib import Path
from typing import Optional

TRADE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = TRADE_DIR / 'strategies/config'
LOG_DIR = TRADE_DIR.parent / 'logs'

sys.path.insert(0, str(TRADE_DIR))
from shared.binance_api import FAPI
from shared.position_manager import monitor_all, close_position, _algo_enqueue, _algo_start_worker, _load as _pm_load
from shared.postgres_client import record_trade_event as _pg_record_event
from strategies.position_models import AtrRiskPositionSizer

# 周期内持仓缓存，避免 pm_monitor + get_position_count 重复调用 _pm_load
_POS_CACHE: dict[str, dict] | None = None
from shared.redis_store import get as _rget, set as _rset, subscribe as _rsubscribe
from shared.redis_store import lock_acquire as _lock_acquire, lock_release as _lock_release

# ── Telegram ──
# 从 binance.env 加载 TG 配置 + API 密钥
_CONFIG_ENV = TRADE_DIR / 'config/binance.env'
_TG_TOKEN = ''
_TG_CHAT_ID = 0
_API_KEY = ''
_API_SECRET = ''


if _CONFIG_ENV.exists():
    _is_testnet = False
    for line in _CONFIG_ENV.read_text().splitlines():
        if '=' in line:
            k, v = line.strip().split('=', 1)
            if k == 'TG_NOTIFY_TOKEN': _TG_TOKEN = v
            elif k == 'TG_NOTIFY_CHAT_ID': _TG_CHAT_ID = int(v)
            elif k == 'BINANCE_TESTNET': _is_testnet = v.strip().lower() == 'true'
            elif k == 'BINANCE_API_KEY' and not _is_testnet: _API_KEY = v
            elif k == 'BINANCE_API_SECRET' and not _is_testnet: _API_SECRET = v
            elif k == 'BINANCE_TESTNET_API_KEY' and _is_testnet: _API_KEY = v
            elif k == 'BINANCE_TESTNET_API_SECRET' and _is_testnet: _API_SECRET = v



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
    try:
        r = requests.get(f'{FAPI}/fapi/v1/premiumIndex?symbol={symbol}', timeout=5)
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


def bounded_stop_pct(base_stop_pct: float, atr_pct: float,
                     max_stop_pct: float = 0.08) -> float:
    """Apply ATR expansion without allowing an unbounded stop distance."""
    stop_pct = max(float(base_stop_pct), float(atr_pct) * 2 / 100) if atr_pct > 0 else float(base_stop_pct)
    return min(stop_pct, max_stop_pct)

# ── 沙盘模式 ──
_SANDBOX_ACTIVE = None

def _sandbox_check():
    global _SANDBOX_ACTIVE
    if _SANDBOX_ACTIVE is not None:
        return _SANDBOX_ACTIVE
    if os.environ.get('SANDBOX', '').strip() in ('1', 'true', 'TRUE'):
        _SANDBOX_ACTIVE = True
        return True
    p = TRADE_DIR / 'strategies/config/SANDBOX_MODE'
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
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = _fapi_sig(params)
    headers = {'X-MBX-APIKEY': _API_KEY}
    try:
        r = requests.get(f'{FAPI}{path}', params=params, headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def fapi_post(path: str, params: dict) -> Optional[dict]:
    # ═══ 沙盘拦截 ═══
    sb = _sandbox_post(path, params)
    if sb is not None:
        return sb
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = _fapi_sig(params)
    headers = {'X-MBX-APIKEY': _API_KEY}
    try:
        r = requests.post(f'{FAPI}{path}', params=params, headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

# ── 日志 ──
def _log(name: str, msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [{name}] {msg}'
    print(line, flush=True)
    try:
        d = LOG_DIR / name.lower()
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{time.strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass

# ── S3 事件读取 ──
S3_STALE_S = 90  # 事件超过 90s 视为过期

_S3_NOTIFY_CHANNEL = 's3:event:notify'

def subscribe_s3_notify():
    """订阅 s3 事件通知频道。返回 pubsub 对象或 None（Redis 不可用时降级轮询）。"""
    try:
        return _rsubscribe(_S3_NOTIFY_CHANNEL)
    except Exception:
        return None

def wait_scan(ps, timeout: float):
    """等待下一轮扫描：s3 事件通知唤醒即返回，否则超时轮询兜底。

    ps 为 None（未订阅成功）时退化为普通 sleep，行为与旧版一致。
    """
    if ps is not None:
        try:
            msg = ps.get_message(timeout=timeout)
            if msg and msg.get('type') == 'message':
                return
            if msg is not None:
                return  # subscribe 确认等非消息帧，也立即返回（无害，下一轮继续等）
            return  # 超时
        except Exception:
            pass
    time.sleep(timeout)

def read_s3_events(max_age: int = S3_STALE_S) -> list:
    """读取 s3 事件数据, 返回有效事件列表"""
    try:
        data = _rget('event:s3')
        if not data:
            return []
        now = time.time()
        if now - data.get('ts', 0) > max_age:
            return []
        snapshot_ts = data.get('ts', now)
        return [dict(event, _snapshot_ts=snapshot_ts) for event in data.get('events', [])]
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


def release_event_fresh(symbol: str, event_type: str):
    """Undo event de-duplication when the market data request failed."""
    _event_history.pop((symbol, event_type), None)

# ── 仓位管理 ──
_KEY_MAP_OVERRIDE = {
    'S6': 'state:s6',
    'S8': 'state:s8',
}

def load_state(name: str) -> dict:
    """加载策略非持仓状态（冷却期等）。持仓由 PM 的 pm:positions 统一管理。"""
    key = _KEY_MAP_OVERRIDE.get(name, f'state:{name.lower()}')
    try:
        data = _rget(key)
        if data:
            if 'positions' in data:
                del data['positions']
            return data
    except Exception:
        pass
    return {'cooldowns': {}}

def save_state(name: str, state: dict):
    """保存策略非持仓状态。positions 字段由 PM 管理，存储时剥离。"""
    key = _KEY_MAP_OVERRIDE.get(name, f'state:{name.lower()}')
    to_save = {k: v for k, v in state.items() if k != 'positions'}
    try:
        _rset(key, to_save)
    except Exception as e:
        _log(name, f'Save state failed: {e}')

def reconcile_positions(name: str, state: dict) -> dict:
    """持仓对账由 PM 的 monitor_all 全权处理，此函数不再管理 positions。"""
    return state

# 确保 AlgoWorker 已启动（导入后只启动一次）
_algo_start_worker()

def _refresh_positions() -> dict | None:
    """刷新并返回全量持仓缓存（pm:positions）。"""
    global _POS_CACHE
    try:
        _POS_CACHE = _pm_load()
    except Exception:
        _POS_CACHE = None
    return _POS_CACHE

def _update_pos_cache(name: str, symbol: str, side: str,
                      entry: float, qty: float, stop_price: float,
                      leverage: int, margin: str, event_type: str, strength: int):
    global _POS_CACHE
    if _POS_CACHE is None:
        _POS_CACHE = {}
    now = time.time()
    position_id = f'{name}:{symbol}:{entry:.12g}:{now:.6f}'
    _POS_CACHE[symbol] = {
        'entry': entry,
        'side': side.upper(),
        'qty': qty,
        'leverage': leverage,
        'margin': margin.upper(),
        'system': name,
        'open_time': now,
        'event_type': event_type,
        'strength': strength,
        'score': strength,
        'sl': stop_price,
        'be_done': False,
        'trail': False,
        'atr': 0,
        'algo_sl_id': 0,
        'ts': now,
        'stop': entry,
        'position_id': position_id,
    }
    try:
        meta = _rget('pm:positions') or {}
        meta[symbol] = dict(_POS_CACHE[symbol])
        _rset('pm:positions', meta)
    except Exception:
        pass
    return position_id

def _get_positions(name: str) -> dict:
    """从缓存获取指定系统的持仓。"""
    if _POS_CACHE is None:
        _refresh_positions()
    if _POS_CACHE:
        return {s: p for s, p in _POS_CACHE.items() if p.get('system', '').startswith(name)}
    return {}

def get_position_count(name: str) -> int:
    """获取指定系统的持仓数量（优先 PM 实时数据，防重复开仓）。"""
    try:
        positions = _pm_load()
        pm_count = sum(1 for p in positions.values() if p.get('system', '').startswith(name))
    except Exception:
        pm_count = 0
    cached = _get_positions(name)
    return max(pm_count, len(cached))

def has_position(name: str, symbol: str) -> bool:
    """判断指定系统是否持有某币。
    先查缓存，缓存没有则直接查 PM（防缓存滞后导致重复开仓）。"""
    if symbol in _get_positions(name):
        return True
    try:
        positions = _pm_load()
        return any(s == symbol and p.get('system', '').startswith(name) for s, p in positions.items())
    except Exception:
        return False


def has_any_position(symbol: str) -> bool:
    """Binance one-way mode: any side blocks a new strategy order."""
    try:
        positions = _pm_load()
        if any(s == symbol and abs(float(p.get('qty', 0))) >= 0.001
               for s, p in positions.items()):
            return True
    except Exception:
        pass
    try:
        rows = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
        return any(isinstance(p, dict) and abs(float(p.get('positionAmt', 0))) >= 0.001
                   for p in (rows or []))
    except Exception:
        return False

def pm_monitor(name: str, state: dict, tg_fn: callable = None) -> dict:
    """PM 监控，返回已平仓列表。持仓由 PM 全权管理。"""
    owner = f'{name}:{os.getpid()}'
    if not _lock_acquire('pm:monitor:writer', owner, ttl=30):
        return state, []
    try:
        closed = monitor_all(system_filter=name)
    finally:
        _lock_release('pm:monitor:writer', owner)
    _refresh_positions()
    closed_list = []
    for item in closed:
        symbol, reason, close_price = item[:3]
        if len(item) >= 6:
            entry, qty, side = item[3], item[4], item[5]
        else:
            pos = (_POS_CACHE or {}).get(symbol, {})
            entry = pos.get('entry', close_price)
            qty = pos.get('qty', 0)
            side = pos.get('side', 'LONG')
        side_mult = -1 if side == 'SHORT' else 1
        pnl_pct = ((close_price - entry) / entry * 100) * side_mult
        pnl_usdt = (close_price - entry) * qty * side_mult
        msg = f'平仓 {symbol} PnL: {pnl_pct:+.1f}% ({pnl_usdt:+.2f}U) 原因={reason}'
        _log(name, msg)
        # trade_recorder sends the single authoritative close notification
        # with position-level realized PnL. Do not send a second cache-based
        # notification here, which can disagree after partial closes.
        state.setdefault('cooldowns', {})[symbol] = time.time() + 7200
        if pnl_pct < 0:
            state['cooldowns'][symbol] = time.time() + 14400
        save_state(name, state)
        closed_list.append((symbol, reason, pnl_pct))
    return state, closed_list


def _should_notify_close(name: str, symbol: str, reason: str, entry: float, qty: float, side: str,
                         window_sec: int = 7200) -> bool:
    """TG 平仓通知去重：同一笔平仓在窗口内只推一次，避免 PM 并发/重试刷屏。"""
    try:
        key = 'notify:close'
        data = _rget(key)
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        # 顺手清理旧项；未知 Redis key 不会文件降级写本地。
        data = {k: v for k, v in data.items() if now - float(v or 0) < window_sec}
        dedup_key = '|'.join([
            str(name), str(symbol), str(reason), str(side),
            f'{float(entry):.10g}', f'{float(qty):.10g}',
        ])
        if dedup_key in data:
            try:
                _rset(key, data, double_write=False)
            except TypeError:
                _rset(key, data)
            return False
        data[dedup_key] = now
        try:
            _rset(key, data, double_write=False)
        except TypeError:
            _rset(key, data)
        return True
    except Exception:
        return True

# ── 动态仓位计算 ────────────────────────────────────────────────────────
_POOL_BUDGET = 0.80          # 账户总余额最高使用比例
_POSITION_MIN_PCT = 0.03     # 单仓最低占可用池比例
_POSITION_MAX_PCT = 0.15     # 单仓最高占可用池比例
_POSITION_MIN_USDT = 10      # 单仓最低 USDT 名义价值
_RISK_PER_TRADE = 0.01       # 单笔止损最大亏损 ≤ 账户 1%（固定风险比例法）


def score_to_fraction(score: float) -> float:
    """信号评分 → 资金池分配比例（3%~15%）"""
    return AtrRiskPositionSizer(
        min_allocation=_POSITION_MIN_PCT,
        max_allocation=_POSITION_MAX_PCT,
        min_notional=_POSITION_MIN_USDT,
    ).score_fraction(score)


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


# ── 账户级回撤熔断 ──────────────────────────────────────────────────────
_DD_HALF = 0.08    # 权益从峰值回撤 ≥8% → 仓位减半
_DD_PAUSE = 0.15   # 回撤 ≥15% → 暂停开仓
_DD_RECOVERY_DELAY = 4 * 3600  # 完全暂停后观察 4h，再进入恢复模式
_DD_RECOVERY_FACTOR = 0.25
_DD_RECOVERY_MAX_POSITIONS = 1
_DD_RECOVERY_MAX_LOSS = 0.02  # 恢复模式相对恢复起点最多再亏2%
_DD_RECOVERY_RETRY_DELAY = 6 * 3600

def _drawdown_status() -> tuple:
    """账户回撤状态：(仓位系数, 当前回撤%)。

    回撤达到 15% 后先暂停 4h，随后进入 25% 仓位的恢复模式，避免
    熔断永久阻断系统；回撤恢复到 15% 以下时退出恢复状态。
    """
    balance = _get_balance()
    if balance <= 0:
        return 1.0, 0.0
    peak = _rget('account:peak')
    if not peak or float(peak.get('bal', 0)) < balance:
        _rset('account:peak', {'bal': balance, 'ts': time.time()})
        return 1.0, 0.0
    peak_bal = float(peak.get('bal', 0))
    if peak_bal <= 0:
        return 1.0, 0.0
    dd = (peak_bal - balance) / peak_bal
    if dd >= _DD_PAUSE:
        pause = _rget('account:dd_pause') or {}
        paused_at = float(pause.get('ts', 0)) if isinstance(pause, dict) else 0.0
        if paused_at <= 0:
            paused_at = time.time()
            _rset('account:dd_pause', {
                'ts': paused_at, 'base_balance': balance, 'loss_lock': False,
            })
        base_balance = float(pause.get('base_balance', balance)) if isinstance(pause, dict) else balance
        loss_lock = bool(pause.get('loss_lock', False)) if isinstance(pause, dict) else False
        if isinstance(pause, dict) and not pause.get('base_balance'):
            pause = dict(pause)
            pause['base_balance'] = balance
            pause['loss_lock'] = loss_lock
            _rset('account:dd_pause', pause)
        if not loss_lock and balance <= base_balance * (1 - _DD_RECOVERY_MAX_LOSS):
            lock_now = time.time()
            _rset('account:dd_pause', {
                'ts': lock_now, 'base_balance': balance, 'loss_lock': True,
            })
            loss_lock = True
            paused_at = lock_now
        if loss_lock:
            lock_ts = float(pause.get('ts', paused_at)) if isinstance(pause, dict) else paused_at
            if time.time() - lock_ts < _DD_RECOVERY_RETRY_DELAY:
                return 0.0, dd * 100
            _rset('account:dd_pause', {
                'ts': time.time(), 'base_balance': balance, 'loss_lock': False,
            })
            return 0.0, dd * 100
        if time.time() - paused_at < _DD_RECOVERY_DELAY:
            return 0.0, dd * 100
        return _DD_RECOVERY_FACTOR, dd * 100
    if _rget('account:dd_pause'):
        _rset('account:dd_pause', {})
    if dd >= _DD_HALF:
        return 0.5, dd * 100
    return 1.0, dd * 100


def drawdown_mode() -> str:
    """Return normal, reduced, recovery, or halt for strategy gating."""
    factor, _ = _drawdown_status()
    if factor <= 0:
        return 'halt'
    if factor <= _DD_RECOVERY_FACTOR:
        return 'recovery'
    if factor < 1:
        return 'reduced'
    return 'normal'


def maybe_replace_recovery_position(name: str, side: str, symbol: str,
                                    candidate_score: float,
                                    margin: float = 10) -> bool:
    """Replace the weakest mature same-side position in recovery mode."""
    try:
        from shared.position_score import calc_position_live_score
        positions = _pm_load()
        candidates = [
            (sym, pos) for sym, pos in positions.items()
            if pos.get('system', '').startswith(name)
            and pos.get('side', '').upper() == side.upper()
            and sym != symbol
        ]
        if not candidates:
            return False
        scored = [(calc_position_live_score(sym, pos), sym, pos) for sym, pos in candidates]
        weakest_score, weakest_symbol, _ = min(scored, key=lambda item: item[0])
        if float(candidate_score) < weakest_score + margin:
            _log(name, f'{symbol} 候选评分 {candidate_score:.0f}，未超过 {weakest_symbol} 当前评分 {weakest_score}，跳过替换')
            return False
        if not close_position(weakest_symbol, f'恢复模式候选替换 ({symbol} score={candidate_score:.0f}>{weakest_score})'):
            _log(name, f'{symbol} 候选替换失败，保留 {weakest_symbol}')
            return False
        global _POS_CACHE
        _POS_CACHE = None
        _log(name, f'{symbol} 候选评分 {candidate_score:.0f} 替换 {weakest_symbol} score={weakest_score}')
        return True
    except Exception as exc:
        _log(name, f'{symbol} 恢复模式评分替换异常: {exc}')
        return False


def event_is_stale(event: dict, max_age_sec: int = 120) -> bool:
    """Reject persistent S3 events after their useful entry window."""
    since = float(event.get('_snapshot_ts', event.get('ts', event.get('since', time.time()))) or time.time())
    if since > 10**12:
        since /= 1000
    return time.time() - since > max_age_sec


def event_age_sec(event: dict) -> float:
    value = float(event.get('_snapshot_ts', event.get('ts', event.get('since', time.time()))) or time.time())
    if value > 10**12:
        value /= 1000
    return max(0.0, time.time() - value)


def price_is_overextended(price: float, ema20: float, atr: float,
                          side: str, max_atr: float) -> bool:
    """Reject entries that chase price too far from the 1h mean."""
    if price <= 0 or ema20 <= 0 or atr <= 0:
        return False
    extension = (price - ema20) / atr if side == 'LONG' else (ema20 - price) / atr
    return extension > max_atr


def classify_entry_mode(price: float, ema20: float, rsi: float,
                        taker_buy_ratio: float | None, side: str) -> str:
    """Classify a candidate as right-side momentum or confirmed reversal."""
    if side == 'LONG':
        if price < ema20 and rsi <= 35 and (taker_buy_ratio is None or taker_buy_ratio >= 0.52):
            return 'LEFT_REVERSAL'
        if price >= ema20:
            return 'RIGHT_MOMENTUM'
    else:
        if price > ema20 and rsi >= 65 and (taker_buy_ratio is None or taker_buy_ratio <= 0.48):
            return 'LEFT_REVERSAL'
        if price <= ema20:
            return 'RIGHT_MOMENTUM'
    return 'UNCONFIRMED'


def contract_score(strength: float, event_type: str, atr_pct: float = 0,
                   extension_atr: float = 0, taker_buy_ratio: float | None = None,
                   event_age_sec: float = 0, side: str = 'LONG') -> int:
    """Combine signal quality and entry risk into a 0-100 contract score."""
    score = float(strength)
    if taker_buy_ratio is not None:
        flow_aligned = taker_buy_ratio >= 0.52 if side == 'LONG' else taker_buy_ratio <= 0.48
        score += 5 if flow_aligned else -5
    if atr_pct > 4:
        score -= min(15, (atr_pct - 4) * 3)
    if extension_atr > 0:
        score -= min(15, extension_atr * 4)
    if event_age_sec > 0:
        score -= min(10, event_age_sec / 30)
    return max(0, min(100, int(round(score))))


def leverage_for_score(event_type: str, score: int, atr_pct: float = 0) -> int:
    """Choose leverage conservatively from quality and volatility."""
    base = {
        'PULSE_UP': 5, 'PULSE_DOWN': 5, 'PANIC_SELL': 5,
        'TREND_UP': 3, 'TREND_DOWN': 3,
        'VIOLENT_BULLISH': 3, 'VIOLENT_BEARISH': 3,
        'PUMP_UP': 2, 'PUMP_DOWN': 2,
    }.get(event_type, 3)
    if score < 60:
        return min(base, 2)
    if score < 85 or atr_pct >= 4:
        return min(base, 3)
    return base


def calc_position_qty(name: str, state: dict, symbol: str, price: float,
                      event_type: str, strength: int, leverage: int,
                      atr_pct: float = 0, stop_pct: float = 0) -> float:
    """动态仓位计算：
       1. 取账户余额 × 80% = 资金池
       2. 减去已有持仓占用 = 可用池
       3. 信号评分 → 分配比例（3%~15%）
       4. ATR 高波动衰减：ATR% 越大仓位越小（ATR 4% 为基准，ATR 8% 减半）
       5. 风险硬约束：止损亏损 ≤ 账户 1%（固定风险比例法）
       6. qty = 分配额 / price * leverage
    """
    balance = _get_balance()
    pool = balance * _POOL_BUDGET
    used = _calc_used_margin(state)
    remaining = max(0, pool - used)
    alloc_pct = score_to_fraction(strength)
    position_usdt = remaining * alloc_pct
    sizer = AtrRiskPositionSizer(
        pool_budget=_POOL_BUDGET,
        min_allocation=_POSITION_MIN_PCT,
        max_allocation=_POSITION_MAX_PCT,
        risk_per_trade=_RISK_PER_TRADE,
        min_notional=_POSITION_MIN_USDT,
    )
    modeled_budget = sizer.budget(balance, remaining, strength, leverage, atr_pct, stop_pct)
    if atr_pct > 4:
        atr_factor = max(0.2, 4.0 / atr_pct)
        _log(name, f'{symbol} ATR={atr_pct:.1f}% 衰减因子={atr_factor:.2f} → ${modeled_budget:.0f}')
    if modeled_budget < position_usdt:
        _log(name, f'{symbol} 风险模型 ${position_usdt:.0f}→${modeled_budget:.0f} (止损{stop_pct:.1%}×{leverage}x≤1%)')
    position_usdt = modeled_budget

    # 最小名义价值保护
    position_usdt = max(position_usdt, _POSITION_MIN_USDT)

    qty = position_usdt / price * leverage
    _log(name, f'{symbol} 余额={balance:.0f} 池={pool:.0f} 已用={used:.0f} 可用={remaining:.0f} 分配={alloc_pct:.0%} → ${position_usdt:.0f}')
    return qty


# ── R:R 预判 ────────────────────────────────────────────────────────────
_MIN_RR = 1.0  # 预期延续幅度 / 止损距离 最低门槛

# ── 订单分析过滤（trade_analysis） ──────────────────────────────────────
_ANALYSIS_FILTER_MIN_TRADES = 6
_ANALYSIS_FILTER_WINRATE_MIN = 35.0
_ANALYSIS_FILTER_QUALITY_MIN = 40.0
_ANALYSIS_FILTER_T60_MIN = -0.8
_ANALYSIS_FILTER_TTL = 120
_ANALYSIS_SOFT_PENALTY = 0.5
_analysis_filter_cache: dict = {}
_ANALYSIS_REJECT_KEY = 'event:analysis_reject'
_analysis_panel_last_log: dict = {}

def _event_expected_move(evt: dict) -> float:
    """估算事件预期延续幅度（%）：以事件自身触发强度为延续参考，无数据返回 0（不拦截）"""
    et = evt.get('type', '')
    try:
        if et in ('PULSE_UP', 'PULSE_DOWN', 'PUMP_UP', 'PUMP_DOWN', 'PANIC_SELL'):
            return abs(float(evt.get('chg_15m', 0) or 0))
        if et in ('TREND_UP', 'TREND_DOWN'):
            return abs(float(evt.get('chg_1h', 0) or 0))
        if et.startswith('VIOLENT_'):
            return float(evt.get('vol_1h', 0) or 0)
    except Exception:
        pass
    return 0


def _analysis_allows_open(symbol: str, event_type: str, system_name: str) -> tuple[bool, str, float]:
    """基于 trade_analysis 滚动统计做轻量过滤。历史不足时不拦截。"""
    if os.environ.get('ANALYSIS_FILTER_OFF', '').strip() in ('1', 'true', 'TRUE'):
        return True, '', 1.0

    now = time.time()
    key = (symbol, event_type, system_name)
    cached = _analysis_filter_cache.get(key)
    if cached and now - cached['ts'] < _ANALYSIS_FILTER_TTL:
        stats = cached['stats']
    else:
        try:
            from shared.trade_analyzer import get_rollup_stats
            stats = get_rollup_stats(symbol=symbol, event_type=event_type, system_name=system_name, lookback_days=14)
            _analysis_filter_cache[key] = {'ts': now, 'stats': stats}
        except Exception:
            return True, '', 1.0

    trades = int(stats.get('trades', 0) or 0)
    if trades < _ANALYSIS_FILTER_MIN_TRADES:
        return True, '', 1.0

    win_rate = float(stats.get('win_rate', 0) or 0)
    quality = float(stats.get('avg_quality_score', 0) or 0)
    t60_ret = float(stats.get('t60_avg_post_close_return_pct', 0) or 0)
    avg_pct = float(stats.get('avg_pct', 0) or 0)

    if win_rate < _ANALYSIS_FILTER_WINRATE_MIN and quality < _ANALYSIS_FILTER_QUALITY_MIN:
        return False, (f'分析过滤[low_quality]: {symbol} {event_type} 历史{trades}单 '
                       f'胜率{win_rate:.1f}% 质量{quality:.1f} 过低'), _ANALYSIS_SOFT_PENALTY
    if t60_ret < _ANALYSIS_FILTER_T60_MIN and avg_pct <= 0:
        return False, (f'分析过滤[bad_follow]: {symbol} {event_type} T60复盘均值{t60_ret:.2f}% '
                       f'且平均收益{avg_pct:.2f}%'), _ANALYSIS_SOFT_PENALTY
    return True, '', 1.0


def _record_analysis_decision(name: str, symbol: str, event_type: str, action: str, reason: str, penalty: float):
    """记录分析过滤命中（Redis，不落本地文件）。"""
    try:
        data = _rget(_ANALYSIS_REJECT_KEY)
        if not isinstance(data, dict):
            data = {}
        items = data.get('items', []) if isinstance(data.get('items', []), list) else []
        items.append({
            'ts': time.time(),
            'system': name,
            'symbol': symbol,
            'event_type': event_type,
            'action': action,
            'penalty': round(float(penalty), 4),
            'reason': reason,
        })
        data['items'] = items[-200:]
        data['ts'] = time.time()
        _rset(_ANALYSIS_REJECT_KEY, data)
    except Exception:
        pass


def get_analysis_reject_summary(window_sec: int = 3600) -> dict:
    """汇总分析过滤命中（最近 window_sec）。"""
    now = time.time()
    out = {
        'window_sec': int(window_sec),
        'total': 0,
        'by_system': {},
        'by_event': {},
        'by_action': {'block': 0, 'soft': 0},
    }
    try:
        data = _rget(_ANALYSIS_REJECT_KEY)
        items = data.get('items', []) if isinstance(data, dict) else []
        for it in items:
            ts = float(it.get('ts', 0) or 0)
            if ts <= 0 or now - ts > window_sec:
                continue
            sys_name = str(it.get('system', '') or '-')
            event_type = str(it.get('event_type', '') or '-')
            action = str(it.get('action', '') or 'block')

            out['total'] += 1
            out['by_action'][action] = out['by_action'].get(action, 0) + 1
            out['by_system'][sys_name] = out['by_system'].get(sys_name, 0) + 1
            out['by_event'][event_type] = out['by_event'].get(event_type, 0) + 1
    except Exception:
        pass
    return out


def _format_analysis_reject_summary(summary: dict) -> str:
    total = int(summary.get('total', 0) or 0)
    if total <= 0:
        return ''
    block_n = int(summary.get('by_action', {}).get('block', 0) or 0)
    soft_n = int(summary.get('by_action', {}).get('soft', 0) or 0)
    by_sys = summary.get('by_system', {})
    by_evt = summary.get('by_event', {})

    top_sys = sorted(by_sys.items(), key=lambda x: -x[1])[:3]
    top_evt = sorted(by_evt.items(), key=lambda x: -x[1])[:3]
    sys_txt = ', '.join(f'{k}:{v}' for k, v in top_sys) if top_sys else '-'
    evt_txt = ', '.join(f'{k}:{v}' for k, v in top_evt) if top_evt else '-'
    mins = max(1, int(summary.get('window_sec', 3600) // 60))
    return f'[分析过滤面板 {mins}m] total={total} block={block_n} soft={soft_n} | system[{sys_txt}] | event[{evt_txt}]'


def maybe_log_analysis_panel(name: str, interval_sec: int = 300, window_sec: int = 3600):
    """按 interval 节流输出分析过滤面板。"""
    now = time.time()
    last = float(_analysis_panel_last_log.get(name, 0) or 0)
    if now - last < interval_sec:
        return
    _analysis_panel_last_log[name] = now
    msg = _format_analysis_reject_summary(get_analysis_reject_summary(window_sec=window_sec))
    if msg:
        _log(name, msg)


def _analysis_gate(name: str, symbol: str, event_type: str, qty: float) -> tuple[bool, float, str]:
    """分析过滤门禁：hard=拒单，soft=降权。"""
    ok_hist, reason, penalty = _analysis_allows_open(symbol, event_type, name)
    if ok_hist:
        return True, qty, ''

    mode = os.environ.get('ANALYSIS_FILTER_MODE', 'hard').strip().lower()
    if mode == 'soft':
        new_qty = max(qty * max(0.1, min(1.0, penalty)), 0.0)
        msg = f'{reason} -> soft降权 qty {qty:.4f}->{new_qty:.4f}'
        _record_analysis_decision(name, symbol, event_type, 'soft', reason, penalty)
        return True, new_qty, msg

    _record_analysis_decision(name, symbol, event_type, 'block', reason, penalty)
    return False, qty, reason


# ── 近期平仓检查（Redis 原子性，与 PM 共享） ──
def _was_closed_recently(symbol: str, within_hours: int = 4) -> bool:
    """检查 symbol 近期是否被 PM _close 处理过"""
    try:
        data = _rget(f'closed:{symbol}')
        if data and 'ts' in data:
            return time.time() - data['ts'] < within_hours * 3600
    except Exception:
        pass
    return False


# ── 开仓工具 ──
def open_position(name: str, symbol: str, side: str, entry_price: float,
                  stop_price: float, qty: float, margin_mode: str,
                  leverage: int, event_type: str, strength: int,
                  tg_fn: callable = None, expected_move_pct: float = 0,
                  decision_context: dict | None = None) -> bool:
    """通过 Binance API + PM 开仓"""
    # ── 信号质量过滤 ──
    if strength < 30:
        _log(name, f'{symbol} strength={strength} < 30，信号太弱跳过')
        return False
    # ── 历史分析过滤（弱质量信号跳过） ──
    ok_hist, qty, reason = _analysis_gate(name, symbol, event_type, qty)
    if not ok_hist:
        _log(name, reason)
        return False
    if reason:
        _log(name, reason)
    # ── R:R 预判：预期延续幅度 vs 止损距离 ──
    if expected_move_pct > 0 and stop_price > 0:
        stop_dist_pct = abs(entry_price - stop_price) / entry_price * 100
        rr = expected_move_pct / stop_dist_pct if stop_dist_pct > 0 else 0
        if rr < _MIN_RR:
            _log(name, f'{symbol} R:R={rr:.1f} < {_MIN_RR} (预期{expected_move_pct:.1f}%/止损{stop_dist_pct:.1f}%)，跳过')
            return False
    # ── 账户级回撤熔断 ──
    dd_factor, dd_pct = _drawdown_status()
    if dd_factor <= 0:
        _log(name, f'{symbol} 账户回撤 {dd_pct:.1f}% ≥ {_DD_PAUSE*100:.0f}%，暂停开仓')
        return False
    if dd_factor < 1.0:
        _log(name, f'{symbol} 账户回撤 {dd_pct:.1f}%，仓位 ×{dd_factor:.1f}')
        qty *= dd_factor
    # ── 近期平仓检查（4h 内不重开同标的，给趋势充分消化时间） ──
    if _was_closed_recently(symbol):
        _log(name, f'{symbol} 4h 内刚被平仓过，跳过重开')
        return False
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
            _log(name, f'跳过 {symbol} SHORT：资金费率 {fund_rate:.4%} 对空头不利')
            return False
        if side == 'LONG' and fund_rate > 0.001:  # 正费率 = 多头付钱，极值跳过
            _log(name, f'跳过 {symbol} LONG：资金费率 {fund_rate:.4%} 对多头不利')
            return False

        # ── 实盘已有仓位检查（BOTH 单向模式禁止反向净仓） ──
        try:
            existing = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
            if isinstance(existing, list):
                for p in existing:
                    amt = float(p.get('positionAmt', 0))
                    existing_side = 'LONG' if amt > 0 else 'SHORT'
                    if abs(amt) >= 0.001:
                        _log(name, f'{symbol} 交易所已有 {existing_side} 仓位 {abs(amt)}，跳过开仓')
                        return False
        except Exception as e:
            _log(name, f'{symbol} 检查交易所仓位失败: {e}')

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

        # 未成交：MARKET 单未成交 → 取消并返回失败（低流动币种，避免虚假开仓循环）
        if status == 'NEW' and filled_qty == 0:
            _log(name, f'{symbol} MARKET 未成交 (orderId={result["orderId"]})，取消订单')
            if result.get('orderId'):
                fapi_post('/fapi/v1/cancelOrder', {
                    'symbol': symbol,
                    'orderId': result['orderId']
                })
            return False

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

        # 先更新缓存（保证原子性，失败则不开单也不发通知）
        try:
            position_id = _update_pos_cache(name, symbol, side, avg_price, filled_qty,
                                            stop_price, leverage, margin_mode, event_type, strength)
        except Exception as e:
            _log(name, f'{symbol} 缓存更新失败，取消开单: {e}')
            if result.get('orderId'):
                try:
                    fapi_post('/fapi/v1/cancelOrder', {
                        'symbol': symbol,
                        'orderId': result['orderId']
                    })
                except Exception:
                    pass
            return False

        order_payload = dict(result)
        order_payload['exchange_price'] = order_payload.get('price', '0')
        order_payload['price'] = avg_price
        order_payload['accounted_qty'] = filled_qty
        _pg_record_event({
            'event_id': f"order:{result.get('orderId', '')}:open",
            'position_id': position_id,
            'event_type': 'OPEN_ORDER_FILLED',
            'order_id': str(result.get('orderId', '')),
            'fill_id': '',
            'price': avg_price,
            'qty': filled_qty,
            'realized_pnl': 0.0,
            'payload': {
                'order': order_payload,
                'decision_context': decision_context or {},
            },
        })

        # 开仓成功 → 通知
        notional_usdt = avg_price * filled_qty
        margin_usdt = notional_usdt / leverage if leverage else notional_usdt
        msg = (f'{name} 开仓 {symbol}\n'
               f'方向: {side} | 类型: {margin_mode}\n'
               f'入场: {avg_price:.4f} | 数量: {filled_qty:.2f}'
               f'{"(部分)" if filled_qty < qty * 0.95 else ""}\n'
               f'保证金: {margin_usdt:.2f} USDT | 名义: {notional_usdt:.2f} USDT\n'
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
