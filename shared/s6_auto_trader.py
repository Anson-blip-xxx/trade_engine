#!/usr/bin/env python3
"""
s6_auto_trader.py — 自动交易引擎
触发条件: s2信号(主) + s3加仓(副)
策略: 动态杠杆 + ATR止损 + 追踪止盈
"""
import hmac, hashlib, time, requests, json, os
from urllib.parse import urlencode
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from collections import defaultdict
from indicators import calc_ema, calc_ema_slope, calc_atr, calc_avg_atr, calc_rsi, calc_macd_hist, calc_vol_ratio
from models import PositionState, WatchlistEntry, RoundSnapshot
import health
import importlib

# === 缓存层 ===
_cache = {}
def cached(ttl):
    def deco(fn):
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            now = time.time()
            if key in _cache and _cache[key][1] > now:
                return _cache[key][0]
            val = fn(*args, **kwargs)
            if val is not None:
                _cache[key] = (val, now + ttl)
            return val
        return wrapper
    return deco
from pathlib import Path
from datetime import datetime
import sys as _sys; _sys.path.insert(0, str(Path(__file__).parent.parent))
_sys.path.insert(0, str(Path(__file__).parent.parent / 's8-system'))
from trading_engine.shared.position_score import calc_position_live_score
from shared.redis_store import get as _rget, set as _rset
# 共享模块（预算常量 + 持仓互斥）
from shared_positions import (
    SHARED_BUDGET_PCT, S6_POSITION_PCT, S6_MIN_POSITION,
    score_to_fraction, is_open as s8_is_open,
    get_live_price,
)
# 泵检测已合并到s2扫描器 >> shared_scanner不再需要

# === 配置 ===
CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config/binance.env'
CIRCUIT_LOSS_COUNT = 3
CIRCUIT_COOLDOWN = 3600
S2P_REJECT_LOG = Path(__file__).resolve().parent.parent / 'config/s2p_rejection_log.jsonl'

def _ch_query(sql):
    """执行ClickHouse查询，返回结果行列表"""
    import subprocess
    r = subprocess.run(['clickhouse-client', '--query', sql], capture_output=True, text=True)
    return r.stdout.strip().split('\n') if r.stdout.strip() else []

def get_cycle_pnl():
    """从 Binance income history 计算新周期净盈亏（不受转账影响）"""
    try:
        checkpoint = _rget('checkpoint:pnl')
        if not checkpoint:
            checkpoint = {'start_ms': int(time.time() * 1000)}
            _rset('checkpoint:pnl', checkpoint)
        start_ms = checkpoint['start_ms']
        total = 0
        for itype in ['REALIZED_PNL', 'FUNDING_FEE', 'COMMISSION']:
            data = fapi_get('/fapi/v1/income', {'incomeType': itype, 'startTime': start_ms, 'limit': 1000})
            if isinstance(data, list):
                total += sum(float(x['income']) for x in data)
        return round(total, 4)
    except Exception as e:
        log(f'[WARN] get_cycle_pnl: {e}')
        return None

LOSS_COOLDOWN_SEC = 7200  # 亏损后同币种冷却2小时

def _write_loss_cooldown(symbol):
    try:
        cd = _rget('cd:loss') or {}
        cd[symbol] = time.time()
        _rset('cd:loss', cd)
    except Exception:
        pass

def _check_loss_cooldown(symbol):
    """返回 (in_cooldown, remaining_min)。先查 Redis，没有则查 CH 兜底。"""
    try:
        cd = _rget('cd:loss')
        if cd and symbol in cd:
            elapsed = time.time() - cd[symbol]
            if elapsed < LOSS_COOLDOWN_SEC:
                return True, int((LOSS_COOLDOWN_SEC - elapsed) / 60)
    except Exception:
        pass

    # CH 兜底：查最近一笔该标的的亏损时间
    try:
        sql = ("SELECT toUnixTimestamp(max(trade_time)) FROM default.trade_history "
               "WHERE symbol={symbol:String} AND result='loss' AND trade_time >= now() - INTERVAL 3 HOUR")
        r = subprocess.run(
            ['clickhouse-client', '-q', sql, f'--param_symbol={symbol}'],
            capture_output=True, text=True, timeout=3
        )
        ts = r.stdout.strip()
        if ts and ts != '0':
            elapsed = time.time() - float(ts)
            if elapsed < LOSS_COOLDOWN_SEC:
                remaining = int((LOSS_COOLDOWN_SEC - elapsed) / 60)
                # 同步回文件，避免下次再查 CH
                _write_loss_cooldown(symbol)
                return True, remaining
    except Exception:
        pass

    return False, 0

_last_snap = [0]

def _write_equity_snapshot():
    """每5分钟写入equity_snapshot到ClickHouse"""
    try:
        state = load_state()
        positions = state.get('positions', {})
        realized = get_cycle_pnl() or 0.0
        unrealized = 0.0
        if positions:
            real_syms = {k.replace('_SHORT', '') for k in positions}
            price_map = batch_get_prices(list(real_syms))
            for sym, pos in positions.items():
                real_sym = sym.replace('_SHORT', '')
                current_price = price_map.get(real_sym, pos['entry'])
                pnl = (current_price - pos['entry']) * pos['qty']
                if pos.get('side') == 'SHORT':
                    pnl = (pos['entry'] - current_price) * pos['qty']
                unrealized += pnl
        import subprocess
        row = json.dumps({
            'snap_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'system': 's6',
            'total_equity': round(realized + unrealized, 4),
            'realized_pnl': round(realized, 4),
            'unrealized_pnl': round(unrealized, 4),
            'open_positions': len(positions),
        })
        subprocess.run(
            ['clickhouse-client', '-q', 'INSERT INTO default.equity_snapshot FORMAT JSONEachRow'],
            input=row, text=True, timeout=5, capture_output=True
        )
    except Exception as e:
        log(f'[equity_snapshot] write error: {e}')

def record_trade(symbol, entry, exit_price, qty, leverage, source, open_time, exit_reason='',
                 signal_type='', market_state_entry='', btc_trend_entry='', breadth_entry='',
                 side='LONG', score=0,
                 atr_entry=0.0, rsi_entry=0.0, funding_entry=0.0,
                 oi_change_entry=0.0, btc_1h_pct=0.0, sl_price=0.0, tp1_price=0.0,
                 # 新增综合字段
                 margin_mode='', position_alloc_usdt=0.0, account_balance=0.0,
                 pool_remaining=0.0, be_done=0, trail_active=0,
                 algo_sl_id=0, ghost_cleanup=0):
    """记录交易结果（尽力而为，绝不抛异常） v3 — 从 Binance 拉实际数据，不自己算"""
    try:
        # 以 Binance 实际数据为准，初始值用作 API 失败时的兜底
        if side == 'SHORT':
            _pct = (entry - exit_price) / entry * 100
            _pnl = (entry - exit_price) * qty
        else:
            _pct = (exit_price - entry) / entry * 100
            _pnl = (exit_price - entry) * qty
        pct, pnl_usdt = _pct, _pnl
        duration_min = int((time.time() - open_time) / 60)
        result = 'win' if pct > 0 else 'loss'

        # 从 Binance 拉实际已实现盈亏（含手续费），覆盖估算值
        try:
            since = int(open_time * 1000) - 1000
            income_data = []
            page_since = since
            while True:
                batch = fapi_get('/fapi/v1/income', {
                    'symbol': symbol, 'incomeType': 'REALIZED_PNL',
                    'startTime': page_since, 'limit': 100
                })
                if not isinstance(batch, list) or not batch:
                    break
                income_data.extend(batch)
                if len(batch) < 100:
                    break
                page_since = batch[-1]['time'] + 1
            if income_data:
                pnl_usdt = sum(float(x['income']) for x in income_data)
                result = 'win' if pnl_usdt > 0 else 'loss'
                # 从实际盈亏反推百分比（基于名义本金 entry*qty）
                notional = entry * qty
                if notional > 0:
                    pct = pnl_usdt / notional * 100
        except Exception as e:
            log(f'[Binance盈亏拉取失败，用估算值] {symbol}: {e}')

        duration_min = int((time.time() - open_time) / 60)

        # 获取市场状态（尽力获取，失败用空值）
        _market_state = market_state_entry or ''
        _btc_trend = btc_trend_entry or ''
        _breadth = breadth_entry or ''
        _btc_price = 0.0
        if not _market_state:
            try:
                _md = _rget('market:s3_data')
                if _md:
                    _md_sym = _md.get('symbols', {}).get('BTCUSDT', {})
                    _btc_price = float(_md_sym.get('15m', {}).get('close', _btc_price))
                _ms = _rget('market:s0')
                if _ms:
                    _market_state = str(_ms.get('regime', ''))
                    _btc_trend = str(_ms.get('btc_trend', ''))
                    _breadth = str(_ms.get('breadth', ''))
            except Exception:
                pass
        if result == 'loss':
            _write_loss_cooldown(symbol)
    except Exception as e:
        log(f'[记账计算失败] {symbol}: {e}')
        return

    # 写入ClickHouse（失败不影响后续）
    try:
        import subprocess

        sl_pct_v = round((sl_price - entry) / entry * 100, 2) if entry > 0 and sl_price > 0 else 0.0

        row = json.dumps({
            'symbol': symbol,
            'system_name': source,
            'side': side,
            'entry': entry,
            'exit_price': exit_price,
            'qty': qty,
            'leverage': leverage,
            'pct': round(pct, 2),
            'pnl_usdt': round(pnl_usdt, 2),
            'duration_min': duration_min,
            'result': result,
            'exit_reason': exit_reason,
            'event_type': signal_type,
            'strength': score,
            'margin_mode': margin_mode,
            'position_alloc_usdt': round(float(position_alloc_usdt), 2),
            'account_balance_at_open': round(float(account_balance), 2),
            'pool_remaining_after': round(float(pool_remaining), 2),
            'sl_price': round(float(sl_price), 8),
            'sl_pct': sl_pct_v,
            'atr_entry': round(float(atr_entry), 8),
            'be_done': 1 if be_done else 0,
            'trail_active': 1 if trail_active else 0,
            'market_state': _market_state,
            'btc_trend': _btc_trend,
            'btc_price_close': round(_btc_price, 2),
            'market_breadth': round(float(_breadth), 4) if _breadth and _breadth.replace('.','',1).replace('-','',1).isdigit() else 0,
            'algo_sl_id': int(algo_sl_id) if algo_sl_id else 0,
            'ghost_cleanup': 1 if ghost_cleanup else 0,
        })
        subprocess.run(
            ['clickhouse-client', '--query',
             'INSERT INTO default.trade_history FORMAT JSONEachRow'],
            input=row, text=True, timeout=5
        )
    except Exception as e:
        log(f'[CH写入失败] {symbol}: {e}')

    # 读取统计（失败用默认值）
    wins, losses, total_pnl, avg_win, avg_loss = 0, 0, 0.0, 0.0, 0.0
    try:
        rows = _ch_query("SELECT countIf(result='win'), countIf(result='loss'), sum(pnl_usdt), "
                         "avgIf(pct, result='win'), avgIf(pct, result='loss') FROM default.trade_history")
        if rows and rows[0]:
            parts = rows[0].split('\t')
            if len(parts) == 5:
                wins, losses = int(parts[0]), int(parts[1])
                total_pnl, avg_win, avg_loss = float(parts[2]), float(parts[3]), float(parts[4])
    except Exception as e:
        log(f'[CH统计失败] {symbol}: {e}')

    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0

    # 新周期准确盈亏（从 Binance income 计算，不受转账影响）
    cycle_pnl = get_cycle_pnl()
    cycle_str = f"{cycle_pnl:+.2f} USDT" if cycle_pnl is not None else "计算中..."

    # 发送TG消息（失败不影响）
    try:
        emoji = '✅' if pct > 0 else '❌'
        msg = (f"{emoji} *平仓* {symbol}\n"
               f"入场: {entry} → 出场: {exit_price:.4f}\n"
               f"盈亏: {pct:+.1f}% | {pnl_usdt:+.2f} USDT\n"
               f"持仓: {duration_min}分钟 | 杠杆: {leverage}x\n\n"
               f"📊 *累计战绩* ({total}单)\n"
               f"胜率: {win_rate:.0f}% ({wins}胜{losses}负)\n"
               f"本周期净盈亏: {cycle_str}\n"
               f"均盈: {avg_win:+.1f}% | 均亏: {avg_loss:+.1f}%")
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=10)
        mid = r.json().get('result', {}).get('message_id')
        if mid:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/pinChatMessage',
                json={'chat_id': TG_CHAT_ID, 'message_id': mid, 'disable_notification': True}, timeout=5)
    except Exception as e:
        log(f'[TG推送失败] {symbol}: {e}')

    log(f'[交易记录] {symbol} {pct:+.1f}% {pnl_usdt:+.2f}U 胜率{win_rate:.0f}%')
    try:
        check_circuit_breaker()
    except Exception as e:
        log(f'[熔断检查失败] {e}')

def load_config():
    env = {}
    with open(CONFIG_FILE) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

CFG = load_config()
API_KEY = CFG['BINANCE_API_KEY']
SECRET = CFG['BINANCE_API_SECRET']
TG_TOKEN = CFG['TG_NOTIFY_TOKEN']
TG_CHAT_ID = int(CFG['TG_NOTIFY_CHAT_ID'])

FAPI = 'https://fapi.binance.com'
MAX_POSITIONS = 5       # 最多同时持仓
MAX_POSITION_PCT  = 0.10  # 单仓最多10%余额（凯利上限）
MAX_LEVERAGE = 10       # calc_leverage 最大杠杆上限

# ── 全局资金管理：80%硬上限 ──────────────────────────────────────────
GLOBAL_MARGIN_CAP = 0.80   # 所有仓位总占用保证金 ≤ 钱包余额 × 80%

def get_wallet_balance() -> float:
    """返回钱包余额（不含浮动盈亏），用于资金管理计算"""
    try:
        data = fapi_get('/fapi/v2/balance')
        for item in data:
            if item['asset'] == 'USDT':
                return float(item['balance'])
    except Exception:
        pass
    return 0.0

def get_total_margin_used() -> float:
    """实时查询 Binance 所有持仓的已用保证金总和"""
    try:
        pos_risk = fapi_get('/fapi/v2/positionRisk')
        return sum(
            abs(float(p.get('positionAmt', 0))) * float(p.get('entryPrice', 0)) / max(int(p.get('leverage', 1)), 1)
            for p in pos_risk
            if isinstance(p, dict) and float(p.get('positionAmt', 0)) != 0
        )
    except Exception:
        return 0.0

def check_global_budget(requested_margin: float = 0.0, wallet_balance: float = None) -> tuple:
    """
    全局资金熔断检查：
    所有仓位总保证金不能超过钱包余额的 80%（剩下2成封存不动）
    """
    if wallet_balance is None:
        wallet_balance = get_wallet_balance()
    if wallet_balance <= 0:
        return False, '无法获取钱包余额'
    used = get_total_margin_used()
    hard_cap = wallet_balance * GLOBAL_MARGIN_CAP
    if used + requested_margin > hard_cap:
        return False, (
            f'资金熔断: 已用{used:.1f}U + 请求{requested_margin:.1f}U = {used+requested_margin:.1f}U > '
            f'钱包{wallet_balance:.1f}U × {GLOBAL_MARGIN_CAP*100:.0f}% = {hard_cap:.1f}U'
        )
    return True, f'预算OK (总上限{hard_cap:.1f}U, 已用{used:.1f}U, 剩余{hard_cap-used:.1f}U)'


_budget_cache = {}

def get_position_risk_cache(*, _force_refresh=False) -> list:
    """获取 Binance 全部持仓（含缓存）"""
    now = time.time()
    if not _force_refresh and 'pos_risk' in _budget_cache:
        val, ts = _budget_cache['pos_risk']
        if now - ts < 3:
            return val
    try:
        data = fapi_get('/fapi/v2/positionRisk')
        if isinstance(data, list):
            _budget_cache['pos_risk'] = (data, now)
            return data
        return []
    except Exception:
        return []

def calc_atr_size_mult(atr: float, price: float, aggressive: bool = True) -> float:
    """ATR 杠杆加权：波动大减仓"""
    atr_pct = atr / price * 100
    if atr_pct > 8:  return 0.25
    elif atr_pct > 5: return 0.25 if aggressive else 0.5
    elif atr_pct > 3: return 0.75
    return 1.0


def get_shared_remaining(wallet_balance: float = None) -> float:
    """返回当前可用的总预算（钱包×0.8 - 已用保证金）"""
    if wallet_balance is None:
        wallet_balance = get_wallet_balance()
    try:
        used = get_total_margin_used()
        return max(0.0, round(wallet_balance * GLOBAL_MARGIN_CAP - used, 2))
    except Exception:
        return round(wallet_balance * GLOBAL_MARGIN_CAP, 2)

_REJECT_MATRIX = defaultdict(int)  # {filter_name: count}
_FUNNEL_STATS = defaultdict(int)   # {layer_name: pass_count}
_REJECT_MATRIX_LAST_FLUSH = [0]
_S2P_LOG_CTX: dict = {}

def _write_s2p_rejection(ctx: dict):
    """追加一行 s2p 拒绝日志到 JSONL 文件"""
    try:
        with open(S2P_REJECT_LOG, 'a') as f:
            f.write(json.dumps(ctx, ensure_ascii=False) + '\n')
    except Exception:
        pass

def set_s2p_ctx(ctx: dict):
    global _S2P_LOG_CTX
    _S2P_LOG_CTX = ctx

def fapi_get_public(path, params=None):
    """Binance 公共接口 GET（无需签名）"""
    if params is None: params = {}
    try:
        r = requests.get('https://fapi.binance.com' + path, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        return None
    except:
        return None

def _sandbox_intercept(path, params):
    try:
        sb = importlib.import_module('scripts.sandbox')
        if not sb.is_active():
            return None
        low = path.lower().replace('-', '').replace('_', '')
        if 'positionrisk' in low:
            return sb.mock_get_position_risk((params or {}).get('symbol'))
        if 'account' in low and 'trade' not in low:
            return sb.mock_get_account()
        if 'order' in low or 'algo' in low:
            if 'cancel' in low or 'delete' in low:
                return sb.mock_cancel_order((params or {}).get('symbol', ''), (params or {}).get('orderId'))
            return sb.mock_post_order(params or {})
        if 'openorders' in low:
            return sb.mock_get_open_orders((params or {}).get('symbol'))
    except Exception:
        pass
    return None

def fapi_get(path, params=None):
    sb = _sandbox_intercept(path, params or {})
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params or {})
        r = requests.get(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_get', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_get', success=False, latency_ms=(time.time()-t0)*1000)
        raise

def fapi_post(path, params):
    sb = _sandbox_intercept(path, params)
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params)
        r = requests.post(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_post', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_post', success=False, latency_ms=(time.time()-t0)*1000)
        raise

def fapi_delete(path, params):
    sb = _sandbox_intercept(path, params)
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params)
        r = requests.delete(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_delete', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_delete', success=False, latency_ms=(time.time()-t0)*1000)
        raise

def sign(params):
    params['timestamp'] = int(time.time() * 1000)
    qs = urlencode(params)
    params['signature'] = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return params


def tg(text):
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}, timeout=10)
    except:
        pass

_LOG_DIR = Path(__file__).parent.parent.parent / 'logs/s6'

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        _LOG_DIR.mkdir(exist_ok=True)
        (_LOG_DIR / f'{datetime.now().strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass

# === 状态管理 ===
def load_state():
    state = _rget('state:trader')
    if state:
        return state
    return {'positions': {}}

def save_state(state):
    _rset('state:trader', state)

def is_circuit_open():
    """检查熔断是否激活"""
    data = _rget('breaker:circuit')
    if not data:
        return False
    if time.time() - data.get('ts', 0) < CIRCUIT_COOLDOWN:
        return True
    _rset('breaker:circuit', {})
    return False

def is_market_volatile():
    """检测市场是否剧烈震荡（BTC/ETH 15分钟波动率）"""
    try:
        for sym in ['BTCUSDT', 'ETHUSDT']:
            klines = get_klines(sym, '15m', 4)
            if not klines:
                continue
            highs = [float(k[2]) for k in klines]
            lows  = [float(k[3]) for k in klines]
            swing = (max(highs) - min(lows)) / min(lows) * 100
            if swing > 3:  # 1小时内波动超3%视为剧烈震荡
                log(f'[市场波动] {sym} 1h波幅{swing:.1f}%，市场剧烈震荡')
                return True
    except:
        pass
    return False

def check_circuit_breaker():
    """连续亏损 + 市场剧烈震荡 → 触发熔断"""
    history = _rget('log:trade')
    if not history or len(history) < CIRCUIT_LOSS_COUNT:
        return
    recent = [t['result'] for t in history[-CIRCUIT_LOSS_COUNT:]]
    if all(r == 'loss' for r in recent) and is_market_volatile():
        _rset('breaker:circuit', {'ts': time.time()})
        msg = f"🔴 *熔断触发*\n连续{CIRCUIT_LOSS_COUNT}单亏损 + 市场剧烈震荡\n暂停开仓{CIRCUIT_COOLDOWN//60}分钟"
        log(msg.replace('*', ''))
        tg(msg)

def load_candidates():
    """备选池，保留24小时"""
    pool = _rget('pool:candidate')
    if not pool:
        return {}
    now = time.time()
    return {s: v for s, v in pool.items() if now - v['ts'] < 86400}

def save_candidates(pool):
    _rset('pool:candidate', pool)

def add_candidate(symbol, score):
    # 过滤TradFi传统资产合约
    if is_tradfi(symbol):
        log(f'[备选池] {symbol} 是TradFi传统资产，不加入备选池')
        return
    pool = load_candidates()
    pool[symbol] = {'score': score, 'ts': time.time()}
    save_candidates(pool)
    log(f'[备选池] 加入 {symbol} 评分{score}')

# === 市场数据 ===
@cached(20)  # 20s TTL — 余额变化不频繁
def get_balance():
    data = fapi_get('/fapi/v2/balance')
    for item in data:
        if item['asset'] == 'USDT':
            return float(item['balance']) + float(item['crossUnPnl'])
    return 0

# === 价格内存缓存（REST版，替代WebSocket）===
_price_cache: dict = {}
_price_cache_ts: float = 0
_PRICE_CACHE_TTL = 5   # 5秒刷一次（全量拉取weight=2，24/min，完全安全）

def _refresh_price_cache():
    global _price_cache, _price_cache_ts
    if time.time() - _price_cache_ts < _PRICE_CACHE_TTL:
        return
    try:
        r = requests.get(f'{FAPI}/fapi/v1/ticker/price', timeout=5)
        data = r.json()
        if isinstance(data, list):
            _price_cache = {d['symbol']: float(d['price']) for d in data}
            _price_cache_ts = time.time()
    except Exception as e:
        log(f'[WARN] price cache refresh: {e}')

def batch_get_prices(symbols: list) -> dict:
    if not symbols:
        return {}
    _refresh_price_cache()
    return {s: _price_cache[s] for s in symbols if s in _price_cache}

def get_price(symbol):
    # 优先使用s9实时价格（1秒级刷新，无额外API）
    try:
        p = get_live_price(symbol)
        if p > 0:
            return p
    except Exception:
        pass
    # 兜底：5秒全量缓存
    _refresh_price_cache()
    if symbol in _price_cache:
        return _price_cache[symbol]
    # 最后兜底：单独请求
    r = requests.get(f'{FAPI}/fapi/v1/ticker/price', params={'symbol': symbol}, timeout=5)
    data = r.json()
    if 'price' not in data:
        raise ValueError(f'get_price failed: {data}')
    return float(data['price'])

@cached(480)
def get_oi_and_funding(symbol):
    """获取当前OI变化、资金费率、价格变化率"""
    try:
        # 当前资金费率
        fr = requests.get(f'{FAPI}/fapi/v1/premiumIndex', params={'symbol': symbol}, timeout=5).json()
        funding = float(fr.get('lastFundingRate', 0))
        # OI变化（当前 vs 30分钟前）
        oi_hist = requests.get(f'{FAPI}/futures/data/openInterestHist',
            params={'symbol': symbol, 'period': '30m', 'limit': 2}, timeout=5).json()
        if len(oi_hist) >= 2:
            oi_chg = (float(oi_hist[-1]['sumOpenInterest']) - float(oi_hist[-2]['sumOpenInterest'])) / float(oi_hist[-2]['sumOpenInterest']) * 100
        else:
            oi_chg = 0
        # 价格变化率（30分钟）
        klines = get_klines(symbol, '30m', 2)
        if len(klines) >= 2:
            price_chg = (float(klines[-1][4]) - float(klines[-2][4])) / float(klines[-2][4]) * 100
        else:
            price_chg = 0
        return funding, oi_chg, price_chg
    except:
        return 0, 0, 0

@cached(55)
def get_current_oi(symbol) -> float:
    """获取当前绝对OI值（用于与 entry_oi 比较）"""
    try:
        r = requests.get(f'{FAPI}/futures/data/openInterestHist',
            params={'symbol': symbol, 'period': '30m', 'limit': 1}, timeout=5).json()
        return float(r[-1]['sumOpenInterest']) if r else 0.0
    except:
        return 0.0

def get_atr(symbol, period=14):
    """计算ATR"""
    r = requests.get(f'{FAPI}/fapi/v1/klines',
        params={'symbol': symbol, 'interval': '1h', 'limit': period + 1}, timeout=10)
    klines = r.json()
    if not isinstance(klines, list):
        return 0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0

def is_atr_compressed(symbol):
    """
    ATR压缩检测：当前ATR < 过去30周期均值×70%
    返回: (is_compressed, compression_ratio)
    """
    try:
        klines = get_klines(symbol, '1h', 45)
        if len(klines) < 45:
            return False, 1.0
        
        # 计算过去30周期的ATR
        atrs = []
        for i in range(15, len(klines)):
            trs = []
            for j in range(max(0, i-14), i):
                high = float(klines[j][2])
                low = float(klines[j][3])
                prev_close = float(klines[j-1][4]) if j > 0 else float(klines[j][1])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            atrs.append(sum(trs) / len(trs) if trs else 0)
        
        current_atr = atrs[-1]
        avg_atr = sum(atrs[-30:]) / 30
        
        if avg_atr == 0:
            return False, 1.0
        
        ratio = current_atr / avg_atr
        is_compressed = ratio < 0.7
        
        return is_compressed, ratio
    except:
        return False, 1.0

def get_symbol_info(symbol):
    """获取合约精度信息"""
    r = requests.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10)
    for s in r.json()['symbols']:
        if s['symbol'] == symbol:
            qty_precision = s['quantityPrecision']
            price_precision = s['pricePrecision']
            return qty_precision, price_precision
    return 3, 2

@cached(86400)
def _fetch_tradfi_blacklist():
    """获取TradFi（传统资产）合约黑名单，底层资产非COIN（如INDEX）的合约禁止开仓"""
    blacklist = set()
    try:
        r = requests.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10)
        data = r.json()
        for s in data.get('symbols', []):
            if s.get('underlyingType') != 'COIN' and s.get('contractType') == 'PERPETUAL':
                blacklist.add(s['symbol'])
        if blacklist:
            log(f'[TradFi黑名单] 过滤{len(blacklist)}个传统资产合约: {", ".join(sorted(blacklist))}')
        else:
            log('[TradFi黑名单] 未发现传统资产合约')
    except Exception as e:
        log(f'[TradFi黑名单] 获取失败: {e}')
    return blacklist

def is_tradfi(symbol):
    """检查合约是否属于TradFi（传统资产）"""
    return symbol.replace('_SHORT', '') in _fetch_tradfi_blacklist()

@cached(55)
def score_signal(symbol, funding_rate=None, oi_chg_pct=None, signal_source=''):
    """
    综合评分 0-14分（P2 Signal Score v2）
    维度：Funding(2) + OI(2) + EMA位置(2) + ATR(2) + 止损空间(2) + 宽度(2) + BTC趋势(1) + 信号源(1)
    """
    score = 0
    try:
        price = get_price(symbol)
        atr = get_atr(symbol)
        ma20 = get_ma(symbol)
        support = get_support_level(symbol)
        sl_pct = (price - support * 0.995) / price * 100

        # 自动取 funding/OI（如未传入）
        if funding_rate is None or oi_chg_pct is None:
            try:
                _f, _oi, _ = get_oi_and_funding(symbol)
                if funding_rate is None:  funding_rate = _f
                if oi_chg_pct is None:    oi_chg_pct = _oi
            except Exception:
                funding_rate = funding_rate or 0
                oi_chg_pct   = oi_chg_pct or 0

        # 1. Funding负值深度（越负越好）
        if funding_rate < -0.05:   score += 2
        elif funding_rate < -0.02: score += 1

        # 2. OI涨幅
        if oi_chg_pct > 20:   score += 2
        elif oi_chg_pct > 10: score += 1

        # 3. 价格刚突破MA20（距离<1%最佳）
        if ma20 > 0:
            dist = (price - ma20) / ma20 * 100
            if 0 < dist < 1:   score += 2
            elif 0 < dist < 3: score += 1

        # 4. ATR波动适中
        vol_pct = atr / price * 100 if price > 0 else 0
        if 1 <= vol_pct <= 2: score += 2
        elif vol_pct < 3:     score += 1

        # 5. 止损空间（越小越好）
        if sl_pct < 1:   score += 2
        elif sl_pct < 2: score += 1

        # 6. 市场宽度（s0）
        try:
            import sys as _sys
            _sys.path.insert(0, '/root/.openclaw/trade/s0-market-guard')
            from s0_reader import load_market_state as _load_s0
            _s0 = _load_s0()
            if _s0:
                if _s0['breadth'] == 'strong':   score += 2
                elif _s0['breadth'] == 'normal': score += 1
                # BTC趋势
                if _s0['btc_trend'] == 'bull':   score += 1
        except Exception:
            pass

        # 7. 信号源加成（强信号源）
        if signal_source in ('s2a', 's2e', 's2j'):  score += 1

    except Exception:
        pass
    return score

# === 动态杠杆 ===
def calc_leverage(atr, price):
    vol_pct = atr / price * 100
    if vol_pct < 1:   lev = 10
    elif vol_pct < 2: lev = 7
    elif vol_pct < 3: lev = 5
    else:             lev = 3
    return min(lev, MAX_LEVERAGE)

_TOP_SYMBOLS_CACHE: list = []
_TOP_SYMBOLS_TS: float = 0
_EXCLUDE_STABLE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDTUSDT", "FDUSDUSDT"}

def get_top_symbols(n=50) -> list:
    """按24h成交量取前N个合约，缓存6小时（与s0保持一致）"""
    global _TOP_SYMBOLS_CACHE, _TOP_SYMBOLS_TS
    if _TOP_SYMBOLS_CACHE and time.time() - _TOP_SYMBOLS_TS < 6 * 3600:
        return _TOP_SYMBOLS_CACHE
    try:
        tickers = requests.get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=10).json()
        ranked = sorted(
            [t for t in tickers if t['symbol'].endswith('USDT') and t['symbol'] not in _EXCLUDE_STABLE],
            key=lambda t: float(t['quoteVolume']), reverse=True
        )[:n]
        _TOP_SYMBOLS_CACHE = [t['symbol'] for t in ranked]
        _TOP_SYMBOLS_TS = time.time()
        log(f'[top_symbols] 已更新 top{n}: {_TOP_SYMBOLS_CACHE[:5]}...')
    except Exception as e:
        log(f'[WARN] get_top_symbols: {e}')
    return _TOP_SYMBOLS_CACHE

_klines_cache: dict = {}
_KLINES_TTL = 300  # 5分钟 TTL，1h K线不需要实时

def get_klines(symbol, interval='1h', limit=20):
    key = (symbol, interval, limit)
    cached_entry = _klines_cache.get(key)
    if cached_entry and time.time() - cached_entry[1] < _KLINES_TTL:
        return cached_entry[0]
    r = requests.get(f'{FAPI}/fapi/v1/klines',
        params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
    data = r.json()
    result = data if isinstance(data, list) else []
    if result:
        _klines_cache[key] = (result, time.time())
    return result

def get_ma(symbol, period=20):
    """获取均线"""
    klines = get_klines(symbol, '1d', period + 1)
    closes = [float(k[4]) for k in klines]
    return sum(closes[-period:]) / period if len(closes) >= period else 0

def get_ema(symbol, period=20, interval='1h'):
    klines = get_klines(symbol, interval, period * 2)
    closes = [float(k[4]) for k in klines]
    return calc_ema(closes, period)

def get_rsi(symbol, period=14, interval='1h'):
    klines = get_klines(symbol, interval, period + 2)
    closes = [float(k[4]) for k in klines]
    return calc_rsi(closes, period)

def get_macd(symbol, interval='1h'):
    klines = get_klines(symbol, interval, 60)
    closes = [float(k[4]) for k in klines]
    return calc_macd_hist(closes)

def is_strict_hour():
    """非欧美主力时段（04:00-20:00 UTC+8）启用严格过滤"""
    hour = datetime.now().hour
    return 4 <= hour < 20

def is_high_risk_hour():
    """美盘开盘高风险时段（21:30-02:00 UTC+8）"""
    hour = datetime.now().hour
    minute = datetime.now().minute
    # 21:30-23:59 或 00:00-02:00
    return (hour == 21 and minute >= 30) or (22 <= hour <= 23) or (0 <= hour < 2)

def get_market_state():
    """
    市场状态机 — 统一从 s0 读取
    返回: ('trend'|'range'|'risk-off', score)
    """
    try:
        import sys as _sys
        _sys.path.insert(0, '/root/.openclaw/trade/s0-market-guard')
        from s0_reader import load_market_state as _load_s0
        _s0 = _load_s0()
        if _s0 and 'market_state' in _s0:
            _ms = _s0['market_state']
            _score = _s0.get('trend_strength', 50)
            return _ms, _score
    except Exception:
        pass
    return 'range', 50
def get_market_breadth():
    """
    市场宽度检测：统计前100币中有多少在日线EMA20上方
    返回: (breadth_pct, mode)
    - breadth > 60%: 'strong' (强趋势市场)
    - breadth 30-60%: 'normal' (正常市场)
    - breadth < 30%: 'weak' (弱市场，Defensive Mode)
    """
    try:
        # 获取前100交易量的币种
        tickers = requests.get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=10).json()
        top_symbols = sorted([t for t in tickers if t['symbol'].endswith('USDT')], 
                            key=lambda x: float(x['quoteVolume']), reverse=True)[:100]
        
        above_ema20 = 0
        total_checked = 0
        
        for ticker in top_symbols[:50]:  # 只检查前50个，避免API限流
            try:
                symbol = ticker['symbol']
                price = float(ticker['lastPrice'])
                ema20 = get_ema(symbol, 20, '1d')
                if ema20 > 0:
                    total_checked += 1
                    if price > ema20:
                        above_ema20 += 1
            except:
                continue
        
        if total_checked == 0:
            return 50, 'normal'
        
        breadth_pct = above_ema20 / total_checked * 100
        
        if breadth_pct > 60:
            mode = 'strong'
        elif breadth_pct > 30:
            mode = 'normal'
        else:
            mode = 'weak'
        
        return breadth_pct, mode
    except:
        return 50, 'normal'

def get_signal_freshness(symbol):
    """
    信号生命周期检测：返回信号新鲜度评分(0-100)
    新信号(形成<5根K线)得分高，老信号(>30根K线)得分低
    """
    try:
        # 检查EMA20金叉持续时间
        klines = get_klines(symbol, '1h', 35)
        if len(klines) < 35:
            return 50
        
        closes = [float(k[4]) for k in klines]
        # 简化EMA计算
        ema20_values = []
        ema60_values = []
        
        # 计算最近35根的EMA20和EMA60
        for i in range(20, len(closes)):
            ema20 = sum(closes[i-20:i]) / 20
            ema20_values.append(ema20)
        for i in range(60, len(closes)):
            ema60 = sum(closes[i-60:i]) / 60
            ema60_values.append(ema60)
        
        # 找到最近一次金叉位置
        golden_cross_bars = 0
        for i in range(min(len(ema20_values), len(ema60_values)) - 1, 0, -1):
            if ema20_values[i] > ema60_values[i]:
                golden_cross_bars += 1
            else:
                break
        
        # 评分：<5根=100分，5-15根=80分，15-30根=50分，>30根=20分
        if golden_cross_bars < 5:
            return 100
        elif golden_cross_bars < 15:
            return 80
        elif golden_cross_bars < 30:
            return 50
        else:
            return 20
    except:
        return 50

@cached(60)
def get_btc_volatility():
    """BTC 15分钟振幅，返回百分比"""
    try:
        klines = get_klines('BTCUSDT', '15m', 4)
        if len(klines) < 4:
            return 0
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        max_high = max(highs)
        min_low = min(lows)
        return (max_high - min_low) / min_low * 100
    except:
        return 0

@cached(300)
def get_recent_high(symbol, hours=4):
    """获取最近N小时最高价（突破确认用）"""
    klines = get_klines(symbol, '1h', hours)
    return max(float(k[2]) for k in klines) if klines else 0

def get_support_level(symbol):
    """获取最近支撑位（最近20根1小时K线的最低点）"""
    klines = get_klines(symbol, '1h', 20)
    lows = [float(k[3]) for k in klines]
    return min(lows) if lows else 0

def calc_position_size(balance, history):
    """凯利公式动态仓位"""
    # 连续亏损保护（优先检查，不依赖数据量）
    if len(history) >= 3:
        recent_results = [t['result'] for t in history[-3:]]
        if recent_results.count('loss') >= 3:
            return 0.05

    if len(history) < 5:
        return MAX_POSITION_PCT  # 数据不足用默认值
    recent = history[-10:]
    wins = [t for t in recent if t['result'] == 'win']
    losses = [t for t in recent if t['result'] == 'loss']
    win_rate = len(wins) / len(recent)
    avg_win = sum(t['pct'] for t in wins) / len(wins) if wins else 1
    avg_loss = abs(sum(t['pct'] for t in losses) / len(losses)) if losses else 1
    # 凯利公式: f = W/L - (1-W)/W_avg
    kelly = win_rate / avg_loss - (1 - win_rate) / avg_win if avg_win > 0 else 0
    kelly = max(0.03, min(0.15, kelly * 0.5))  # 半凯利，限制3%-15%
    # 连续亏损保护
    recent_results = [t['result'] for t in history[-3:]]
    if recent_results.count('loss') >= 3:
        kelly = min(kelly, 0.05)
    return kelly

# === Phase 0: 漏斗日志辅助 ===
def _rj(matrix, key):
    """记录拒绝原因并返回 False。当 s2p 上下文激活时，一并写入拒因日志。"""
    _REJECT_MATRIX[key] += 1
    matrix[key] = matrix.get(key, 0) + 1
    if _S2P_LOG_CTX:
        ctx = _S2P_LOG_CTX.copy()
        ctx['rejected_by'] = key
        ctx['reject_ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _write_s2p_rejection(ctx)
        _S2P_LOG_CTX.clear()
    return False

def _flush_reject_matrix():
    """每小时输出一次 Reject Matrix 汇总，并落库到 ClickHouse"""
    now = time.time()
    if now - _REJECT_MATRIX_LAST_FLUSH[0] < 3600:
        return
    _REJECT_MATRIX_LAST_FLUSH[0] = now
    if not _REJECT_MATRIX:
        return
    total = sum(_REJECT_MATRIX.values())
    lines = [f"  {k}: {v} ({v/total*100:.0f}%)" for k, v in sorted(_REJECT_MATRIX.items(), key=lambda x: -x[1])]
    log(f'[Reject Matrix] 累计拒绝 {total} 次\n' + '\n'.join(lines))
    
    # 落库到 ClickHouse（统一用UTC时间）
    _ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    try:
        import subprocess as _sp
        _rows = ''.join(
            f"{_ts}\ts6\t{k}\t{v}\t{round(v/total*100,1)}\n"
            for k, v in _REJECT_MATRIX.items()
        )
        _p = _sp.Popen(['clickhouse-client', '--query',
            'INSERT INTO default.s8_funnel_stats (ts,source,filter_name,count,pct) FORMAT TabSeparated'],
            stdin=_sp.PIPE, stderr=_sp.PIPE)
        _p.communicate(input=_rows.encode(), timeout=10)
    except Exception:
        pass

    # 漏斗统计
    if _FUNNEL_STATS:
        flines = [f"  {k}: {v}" for k, v in sorted(_FUNNEL_STATS.items(), key=lambda x: -x[1])]
        log(f'[Pass Funnel]\n' + '\n'.join(flines))
        try:
            import subprocess as _sp2
            _rows2 = ''.join(
                f"{_ts}\ts6_funnel\t{k}\t{v}\t0.0\n"
                for k, v in _FUNNEL_STATS.items()
            )
            _p2 = _sp2.Popen(['clickhouse-client', '--query',
                'INSERT INTO default.s8_funnel_stats (ts,source,filter_name,count,pct) FORMAT TabSeparated'],
                stdin=_sp2.PIPE, stderr=_sp2.PIPE)
            _p2.communicate(input=_rows2.encode(), timeout=10)
        except Exception as e:
            log(f'[Funnel落库失败] {e}')

    # 清空计数器（下次累计的是新数据）
    _REJECT_MATRIX.clear()
    _FUNNEL_STATS.clear()

# === 信号配置 ===
def get_signal_profile(source: str = 's2') -> dict:
    """返回信号源的过滤配置"""
    profiles = {
        's2': {
            'skip_filters': [],           # 跑满全部过滤器
            'max_hold_h': 6,
            'max_positions': 3,
        },
        's2p': {
            'skip_filters': ['s0_risk_off', 'low_liquidity'],
            'max_hold_h': 12,
            'max_positions': 2,
        },
        'candidate': {
            'skip_filters': ['s0_risk_off'],
            'max_hold_h': 4,
            'max_positions': 5,
        },
    }
    return profiles.get(source, profiles['s2'])

# === 开仓 ===
def open_position(symbol, signal_source='s2'):
    state = load_state()
    profile = get_signal_profile(signal_source)
    skip = set(profile['skip_filters'])
    _local_rj = {}  # 本次调用的拒绝原因
    _FUNNEL_STATS['01_scan'] += 1
    _flush_reject_matrix()

    # s2p 泵信号：初始化拒绝日志上下文
    if signal_source == 's2p':
        set_s2p_ctx({
            'symbol': symbol,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'price': 0,
            'atr': 0,
            'ls_val': 0,
            'funding': 0,
            'oi': 0,
            'source': 's2p',
            'skip_filters': list(skip),
            'profile_name': profile.get('name', signal_source),
        })

    # 异常行为暂停系统
    try:
        # 1. API延迟检测
        start_time = time.time()
        test_price = get_price(symbol)
        api_latency = time.time() - start_time
        if api_latency > 5:
            log(f'[异常暂停] {symbol} API延迟{api_latency:.1f}s>5s')
            return _rj(_local_rj, 'api_latency')
        
        # 记录s2p价格
        if _S2P_LOG_CTX:
            _S2P_LOG_CTX['price'] = test_price
        
        # 2. 价格跳变检测（对比1分钟前）
        klines_1m = get_klines(symbol, '1m', 2)
        if len(klines_1m) >= 2:
            prev_close = float(klines_1m[-2][4])
            price_jump = abs(test_price - prev_close) / prev_close * 100
            if price_jump > 10:
                log(f'[异常暂停] {symbol} 价格跳变{price_jump:.1f}%>10%')
                return _rj(_local_rj, 'price_jump')
        
        # 3. Funding瞬变检测
        funding, _, _ = get_oi_and_funding(symbol)
        if _S2P_LOG_CTX:
            _S2P_LOG_CTX['funding'] = funding
        if abs(funding) > 0.005:  # 0.5%
            log(f'[异常暂停] {symbol} Funding瞬变{funding:.4%}>0.5%')
            return _rj(_local_rj, 'funding_spike')
    except Exception as e:
        log(f'[异常检测失败] {symbol}: {e}')
        return _rj(_local_rj, 'exception')

    # 泵信号 L/S 多空比检查（开仓前最后一次确认）
    if signal_source == 's2p':
        try:
            import requests as _req
            _ls_r = _req.get(
                'https://fapi.binance.com/futures/data/globalLongShortAccountRatio',
                params={'symbol': symbol, 'period': '5m', 'limit': 1}, timeout=5
            ).json()
            if isinstance(_ls_r, list) and len(_ls_r) > 0:
                _ls_val = float(_ls_r[0].get('longShortRatio', 0))
                # 记录到s2p上下文
                if _S2P_LOG_CTX:
                    _S2P_LOG_CTX['ls_val'] = _ls_val
                if _ls_val > 1.5:
                    log(f'[跳过] {symbol} 泵信号L/S={_ls_val:.3f}多头FOMO过热')
                    return _rj(_local_rj, 'ls_fomo')
                elif _ls_val < 1.0:
                    log(f'[泵确认] {symbol} L/S={_ls_val:.3f} 空头占优，轧空继续 ✅')
        except Exception:
            pass  # L/S查询失败不阻塞交易

    # 市场状态机检查（泵信号豁免）
    if 's0_risk_off' not in skip:
        market_state, confidence = get_market_state()
        if market_state == 'risk-off':
            log(f'[跳过] {symbol} 市场处于Risk-Off状态（BTC日线走弱），禁止开仓')
            return _rj(_local_rj, 'risk_off')
        _FUNNEL_STATS['02_pass_risk_off'] += 1

    # 市场宽度检测
    breadth_pct, breadth_mode = get_market_breadth()
    if breadth_mode == 'weak':
        log(f'[Defensive Mode] 市场宽度{breadth_pct:.0f}%<30%，弱市场，提高过滤标准')
        # 弱市场下，只开最高质量信号（后续会提高各项过滤标准）

    # BTC波动率过滤：15分钟振幅>3%暂停开仓
    btc_vol = get_btc_volatility()
    if btc_vol > 3:
        log(f'[跳过] {symbol} BTC 15分钟振幅{btc_vol:.1f}%>3%，市场剧烈波动')
        return _rj(_local_rj, 'btc_volatility')

    # 美盘高风险时段：提高BTC波动率阈值 (已移除)

    # 熔断检查
    if is_circuit_open():
        log(f'[熔断] 暂停开仓，冷却中')
        return _rj(_local_rj, 'circuit_breaker')
    _FUNNEL_STATS['03_pass_btc_vol'] += 1

    # 持仓上限检查：满仓时尝试替换实时得分最低的仓位
    if len(state['positions']) >= MAX_POSITIONS:
        new_score = score_signal(symbol)
        # 实时得分：含趋势加分/反转惩罚/保护期
        # 泵仓位(s2p/pump)不受替换保护，不参与候选池
        live_scores = {}
        for s in state['positions']:
            pos = state['positions'][s]
            if pos.get('source') == 's2p' or pos.get('is_pump'):
                continue
            live_scores[s] = calc_position_live_score(s.replace('_SHORT',''), pos)
        
        if not live_scores:
            # 全部是泵仓位，不做替换
            return _rj(_local_rj, 'max_positions')
        
        worst_sym  = min(live_scores, key=lambda s: live_scores[s])
        worst_live = live_scores[worst_sym]
        worst_pos  = state['positions'][worst_sym]
        worst_real = worst_sym.replace('_SHORT', '')

        if new_score > worst_live:
            current_price_map = batch_get_prices([worst_real])
            worst_pct = (current_price_map.get(worst_real, worst_pos['entry']) - worst_pos['entry']) / worst_pos['entry'] * 100
            fapi_post('/fapi/v1/order', {
                'symbol': worst_real, 'side': 'SELL', 'type': 'MARKET',
                'quantity': worst_pos['qty'], 'positionSide': 'BOTH'
            })
            record_trade(worst_real, worst_pos['entry'], current_price_map[worst_real],
                        worst_pos['qty'], worst_pos['leverage'], worst_pos['source'],
                        worst_pos['open_time'], exit_reason='replaced',
                        signal_type=worst_pos.get('signal_type',''), market_state_entry=worst_pos.get('market_state_entry',''),
                        btc_trend_entry=worst_pos.get('btc_trend_entry',''), breadth_entry=worst_pos.get('breadth_entry',''),
                        score=worst_pos.get('score',0))
            del state['positions'][worst_sym]
            save_state(state)
            tg(f"🔄 *仓位替换*\n平掉 {worst_real} (持仓分={worst_live}, {worst_pct:.1f}%)\n新仓 {symbol} (信号分={new_score})")
            log(f'[替换] {worst_sym}(持仓分{worst_live}) → {symbol}(信号分{new_score})')
        else:
            # 替换失败 → 加入备选池（第4点）
            add_candidate(symbol, new_score)
            log(f'[跳过] {symbol} 满仓，加入备选池(评分{new_score})')
            return _rj(_local_rj, 'max_positions')

    # 重复开单检查
    if symbol in state['positions']:
        log(f'[跳过] {symbol} 已有持仓')
        return _rj(_local_rj, 'already_open')

    # S8共享持仓互斥：如果S8A/S8B已持有该标的空单，S6不做多
    try:
        _s8_pos = _rget('share:positions')
        if _s8_pos and symbol in _s8_pos:
            log(f'[跳过] {symbol} S8已有持仓，S6不做多')
            return _rj(_local_rj, 's8_conflict')
    except Exception:
        pass

    # 亏损冷却检查（亏损平仓后2小时内不再开同一币种）
    in_cd, remaining = _check_loss_cooldown(symbol)
    if in_cd:
        log(f'[跳过] {symbol} 亏损冷却中({remaining}min剩余)')
        return _rj(_local_rj, 'loss_cooldown')

    price = get_price(symbol)
    atr = get_atr(symbol)
    if _S2P_LOG_CTX:
        _S2P_LOG_CTX['price'] = price
        _S2P_LOG_CTX['atr'] = atr
    if atr == 0:
        log(f'[跳过] {symbol} ATR计算失败')
        return _rj(_local_rj, 'atr_fail')

    # ① 时间过滤已移除

    # ② 方向过滤：日线 → 4h → 1h 多周期联动
    # 日线：大方向过滤（ATR动态缓冲）
    if 'daily_ema20' not in skip:
        daily_ema20 = get_ema(symbol, 20, '1d')
        if daily_ema20 > 0:
            if breadth_mode == 'strong':
                buffer = 0.8 * atr
            elif breadth_mode == 'normal':
                buffer = 0.5 * atr
            else:
                buffer = 0.2 * atr
            threshold = daily_ema20 - buffer
            if price < threshold:
                log(f'[跳过] {symbol} 价格{price:.4f}<日线EMA20-{buffer/atr:.1f}ATR({threshold:.4f})，大周期空头')
                return _rj(_local_rj, 'daily_ema20')

    # 4h：中期趋势
    ma20 = get_ma(symbol)
    if ma20 > 0 and price < ma20 * 0.95:
        log(f'[跳过] {symbol} 价格低于MA20*95%，不做多')
        return _rj(_local_rj, 'ma20_95pct')

    if 'ma120' not in skip:
        ma120 = get_ma(symbol, 120)
        if ma120 > 0 and price < ma120:
            log(f'[跳过] {symbol} 价格{price:.4f}低于MA120({ma120:.4f})，中期下跌趋势')
            return _rj(_local_rj, 'ma120')

    if 'ema20_slope' not in skip:
        ema20_now = get_ema(symbol, 20, '1h')
        klines_6h = get_klines(symbol, '1h', 7)
        if len(klines_6h) >= 7:
            closes_now = [float(k[4]) for k in klines_6h[-3:]]
            closes_3h = [float(k[4]) for k in klines_6h[-6:-3]]
            ema20_3h_ago = sum(closes_3h) / 3 if closes_3h else ema20_now
            if ema20_now > 0 and ema20_3h_ago > 0:
                current_slope = (ema20_now - ema20_3h_ago) / ema20_3h_ago * 100
                closes_6h = [float(k[4]) for k in klines_6h[:3]]
                ema20_6h_ago = sum(closes_6h) / 3 if closes_6h else ema20_3h_ago
                prev_slope = (ema20_3h_ago - ema20_6h_ago) / ema20_6h_ago * 100 if ema20_6h_ago > 0 else current_slope
                if breadth_mode == 'weak':
                    if current_slope < prev_slope:
                        log(f'[跳过] {symbol} EMA20斜率恶化({prev_slope:.2f}%→{current_slope:.2f}%)，弱市场不做')
                        return _rj(_local_rj, 'ema20_slope')
                elif current_slope < -1.0 and current_slope < prev_slope:
                    log(f'[跳过] {symbol} EMA20加速下跌({prev_slope:.2f}%→{current_slope:.2f}%)')
                    return _rj(_local_rj, 'ema20_slope')
    _FUNNEL_STATS['04_pass_ema_structure'] += 1

    # BTC 4h方向（所有信号保留，BTC走弱是系统性风险）
    btc_4h = get_klines('BTCUSDT', '4h', 2)
    if len(btc_4h) >= 2:
        btc_chg = (float(btc_4h[-1][4]) - float(btc_4h[-1][1])) / float(btc_4h[-1][1]) * 100
        if btc_chg < -1.0:
            log(f'[跳过] {symbol} BTC当前4h跌幅{btc_chg:.1f}%，大盘走弱')
            return _rj(_local_rj, 'btc_4h_drop')

    if '4h_structure' not in skip:
        klines_4h = get_klines(symbol, '4h', 25)
        if len(klines_4h) >= 25:
            closes_4h = [float(k[4]) for k in klines_4h]
            k_ema = 2 / 21
            ema20_4h = sum(closes_4h[:20]) / 20
            for c in closes_4h[20:]:
                ema20_4h = c * k_ema + ema20_4h * (1 - k_ema)
            ema20_4h_prev = sum(closes_4h[:20]) / 20
            for c in closes_4h[20:-1]:
                ema20_4h_prev = c * k_ema + ema20_4h_prev * (1 - k_ema)
            slope = (ema20_4h - ema20_4h_prev) / ema20_4h_prev * 100 if ema20_4h_prev > 0 else 0
            if price < ema20_4h * 0.98:
                log(f'[跳过] {symbol} 价格低于4h EMA20×98%({ema20_4h:.4f})，结构走坏')
                return _rj(_local_rj, '4h_ema20')
            if slope < -0.3:
                log(f'[跳过] {symbol} 4h EMA20斜率{slope:.2f}%<-0.3%，趋势恶化')
                return _rj(_local_rj, '4h_ema20_slope')
            cur_4h_open = float(klines_4h[-1][1])
            if price < cur_4h_open:
                log(f'[跳过] {symbol} 当前4h K线已走弱(价格{price:.4f}<开盘{cur_4h_open:.4f})')
                return _rj(_local_rj, '4h_candle_weak')

    # 成交额过滤（所有信号保留，流动性是基础要求）
    try:
        ticker_24h = requests.get(f'{FAPI}/fapi/v1/ticker/24hr', params={'symbol': symbol}, timeout=5).json()
        volume_24h_usdt = float(ticker_24h.get('quoteVolume', 0))
        if volume_24h_usdt < 5_000_000:
            log(f'[跳过] {symbol} 24h成交额{volume_24h_usdt/1e6:.1f}M<5M，流动性不足')
            return _rj(_local_rj, 'volume_24h_low')
    except:
        pass

    if 'volume_confirm' not in skip:
        klines_vol = get_klines(symbol, '1h', 26)
        if len(klines_vol) >= 26:
            avg_vol = sum(float(k[5]) for k in klines_vol[-26:-2]) / 24
            cur_vol = float(klines_vol[-2][5])
            if avg_vol > 0 and cur_vol < avg_vol * 1.2:
                log(f'[跳过] {symbol} 成交量{cur_vol:.0f}不足均量{avg_vol:.0f}的1.2倍，无放量')
                return _rj(_local_rj, 'volume_confirm')
            prev_vol = float(klines_vol[-3][5])
            prev_close = float(klines_vol[-3][4])
            cur_close = float(klines_vol[-2][4])
            if cur_close > prev_close and cur_vol < prev_vol * 0.8:
                log(f'[跳过] {symbol} 量价背离：价格新高但成交量萎缩')
                return _rj(_local_rj, 'volume_divergence')
    _FUNNEL_STATS['05_pass_volume'] += 1

    # 上影线过长：上影线 > 实体2倍（上方抛压重）
    klines_1h = get_klines(symbol, '1h', 3)
    if len(klines_1h) >= 3:
        k = klines_1h[-2]  # 已收盘K线
        k_open, k_high, k_close = float(k[1]), float(k[2]), float(k[4])
        body = abs(k_close - k_open)
        upper_shadow = k_high - max(k_open, k_close)
        if body > 0 and upper_shadow > body * 2:
            log(f'[跳过] {symbol} 上影线过长({upper_shadow:.4f})>实体({body:.4f})×2，上方抛压重')
            return _rj(_local_rj, 'upper_wick')

    # OI+Funding二次验证（全时段）：确认信号仍然有效
    funding, oi_chg, price_chg = get_oi_and_funding(symbol)
    # 费率极度正值（多头过度拥挤）不做多
    if funding > 0.001:
        log(f'[跳过] {symbol} 资金费率{funding:.4%}过高，多头拥挤')
        return _rj(_local_rj, 'funding_high')
    # OI在下降说明信号已经衰减
    if oi_chg < -2:
        log(f'[跳过] {symbol} OI下降{oi_chg:.1f}%，信号衰减')
        return _rj(_local_rj, 'oi_decline')
    # OI异常增长：OI涨幅远超价格涨幅（杠杆堆积）
    if oi_chg > 10 and price_chg < 3 and oi_chg > price_chg * 3:
        log(f'[跳过] {symbol} OI+{oi_chg:.1f}%但价格+{price_chg:.1f}%，杠杆堆积危险')
        return _rj(_local_rj, 'oi_leverage_pile')

    # 压力位空间过滤：距离前高至少2×ATR
    recent_high = get_recent_high(symbol, 24)  # 24小时前高
    if recent_high > 0:
        space_to_high = (recent_high - price) / price * 100
        min_space = 2 * atr / price * 100
        if space_to_high < min_space:
            log(f'[跳过] {symbol} 距离前高{space_to_high:.1f}%<{min_space:.1f}%，空间不足')
            return _rj(_local_rj, 'space_to_high')

    # 信号生命周期检测
    signal_freshness = get_signal_freshness(symbol)
    if signal_freshness < 50:
        log(f'[跳过] {symbol} 信号新鲜度{signal_freshness}分<50，趋势已进入后半段')
        return _rj(_local_rj, 'signal_freshness')
    _FUNNEL_STATS['06_pass_quality'] += 1

    # 市场状态动态过滤
    if market_state == 'trend':
        rsi = get_rsi(symbol)
        if rsi > 85:
            log(f'[跳过] {symbol} RSI={rsi:.1f}>85，趋势行情仍过热')
            return _rj(_local_rj, 'rsi_overbought')
    elif market_state == 'range':
        ema20 = get_ema(symbol, 20)
        ema60 = get_ema(symbol, 60)
        ema120 = get_ema(symbol, 120)
        if ema20 > 0 and ema60 > 0 and ema120 > 0:
            if not (ema20 > ema60 > ema120):
                log(f'[跳过] {symbol} EMA未多头排列，震荡行情严格过滤')
                return _rj(_local_rj, 'ema_not_bullish')
        rsi = get_rsi(symbol)
        if rsi > 75:
            log(f'[跳过] {symbol} RSI={rsi:.1f}>75，震荡行情不追高')
            return _rj(_local_rj, 'rsi_overbought')
        hist = get_macd(symbol)
        if len(hist) >= 3 and hist[-1] > 0 and hist[-1] < hist[-2] < hist[-3]:
            log(f'[跳过] {symbol} MACD顶背离，震荡行情不入场')
            return _rj(_local_rj, 'macd_divergence')

    # ATR压缩检测：压缩状态下的突破信号质量更高
    is_compressed, compression_ratio = is_atr_compressed(symbol)
    if is_compressed:
        log(f'[ATR压缩] {symbol} 当前ATR为30周期均值的{compression_ratio:.0%}，压缩状态突破')
        # 压缩状态下放宽部分过滤条件（已经通过前面的基础过滤）

    leverage = calc_leverage(atr, price)
    # 全局预算检查：基于钱包余额（不含浮动盈亏）
    ok, budget_msg = check_global_budget(0)
    if not ok:
        log(f'[跳过] {symbol} {budget_msg}')
        return _rj(_local_rj, 'global_budget')
    remaining = get_shared_remaining()
    if remaining <= 0:
        _rj('budget_s6', symbol); return False
    # 评分驱动仓位：先算分，再定仓
    entry_score = score_signal(symbol, signal_source=signal_source)
    if entry_score < 5:
        log(f'[跳过] {symbol} 评分{entry_score}<5，信号质量不足')
        return _rj(_local_rj, 'score_too_low')
    position_usdt = remaining * (0.10 if signal_source == 's2p' else score_to_fraction(entry_score))  # 泵固定10%/单
    qty_precision, price_precision = get_symbol_info(symbol)

    # 设置杠杆
    fapi_post('/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    # 仓位模式：泵信号用逐仓隔离风险，普通信号全仓
    margin_mode = 'ISOLATED' if signal_source == 's2p' else 'CROSSED'
    fapi_post('/fapi/v1/marginType', {'symbol': symbol, 'marginType': margin_mode})

    # 计算数量
    qty = round(position_usdt * leverage / price, qty_precision)
    if qty <= 0:
        log(f'[跳过] {symbol} 数量计算为0')
        return _rj(_local_rj, 'qty_zero')

    # ④ 止损到支撑位，最小距离保护：至少1×ATR
    support = get_support_level(symbol)
    sl_from_support = support * 0.995
    sl_from_atr = price - 1.5 * atr
    sl_price = round(max(sl_from_support, sl_from_atr), price_precision)  # 取更高的（更紧的止损）
    sl_pct = (price - sl_price) / price * 100
    # 止损太紧（<1%）直接用1.5×ATR
    if sl_pct < 1.0:
        sl_price = round(price - 1.5 * atr, price_precision)
        sl_pct = (price - sl_price) / price * 100
    if sl_pct > 3.5:
        log(f'[跳过] {symbol} 止损距离{sl_pct:.1f}%>3%，风险过大')
        return _rj(_local_rj, 'stop_distance')
    tp_price = round(price + 3 * (price - sl_price), price_precision)

    # K线确认：等当前1分钟K线收盘，且收盘价>开盘价（阳线确认）
    kline = get_klines(symbol, '1m', 2)
    if kline:
        last = kline[-2] if len(kline) >= 2 else kline[-1]
        open_time = kline[-1][0]
        elapsed = (int(time.time() * 1000) - open_time) / 1000
        remaining = 60 - elapsed
        if remaining > 5:
            log(f'[K线确认] {symbol} 等待{remaining:.0f}秒收盘')
            time.sleep(remaining)
            kline = get_klines(symbol, '1m', 2)
            last = kline[-2] if len(kline) >= 2 else kline[-1]
        # 阳线确认：收盘价 > 开盘价
        k_open, k_close = float(last[1]), float(last[4])
        if k_close <= k_open:
            log(f'[跳过] {symbol} 最近K线收阴线，不入场')
            return _rj(_local_rj, 'candle_bearish')
        price = get_price(symbol)

    # 市价开多
    order = fapi_post('/fapi/v1/order', {
        'symbol': symbol, 'side': 'BUY', 'type': 'MARKET',
        'quantity': qty, 'positionSide': 'BOTH'
    })

    if 'orderId' not in order:
        log(f'[开仓失败] {symbol}: {order}')
        tg(f'❌ 开仓失败 {symbol}\n{order.get("msg", "")}')
        return _rj(_local_rj, 'order_fail')
    _FUNNEL_STATS['07_open'] += 1

    # s2p 泵信号成功开仓：记录正面样本
    if _S2P_LOG_CTX:
        _S2P_LOG_CTX['rejected_by'] = 'success'
        _S2P_LOG_CTX['reject_ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _S2P_LOG_CTX['entry_price'] = price
        _S2P_LOG_CTX['qty'] = qty
        _write_s2p_rejection(_S2P_LOG_CTX)
        _S2P_LOG_CTX.clear()

    # 挂止损单（限价止损防插针，允许0.5%滑点）
    fapi_post('/fapi/v1/order', {
        'symbol': symbol, 'side': 'SELL', 'type': 'STOP',
        'stopPrice': sl_price, 'price': round(sl_price * 0.995, price_precision),
        'quantity': qty, 'positionSide': 'BOTH', 'reduceOnly': 'true'
    })

    # 记录状态（entry_score已在仓位计算时获取）
    # 记录开仓时的OI和量，用于Thesis Failure Exit
    try:
        oi_resp = requests.get(f'{FAPI}/futures/data/openInterestHist',
            params={'symbol': symbol, 'period': '5m', 'limit': 1}, timeout=5).json()
        entry_oi = float(oi_resp[-1]['sumOpenInterest']) if oi_resp else 0
    except Exception:
        entry_oi = 0
    try:
        kl = get_klines(symbol, '1h', 3)
        entry_vol = float(kl[-2][5]) if kl and len(kl) >= 2 else 0
    except Exception:
        entry_vol = 0
    # 从 s0 读取开仓时的市场状态
    _s0_entry = {}
    try:
        import sys as _sys
        _sys.path.insert(0, '/root/.openclaw/trade/s0-market-guard')
        from s0_reader import load_market_state as _load_s0
        _s0_entry = _load_s0() or {}
    except Exception:
        pass
    pump_entry = {}
    if signal_source == 's2p':
        # 记录5m成交量用于量能衰减检测
        try:
            k5_vol = get_klines(symbol, '5m', 26)
            if k5_vol and len(k5_vol) >= 2:
                peak_vol = max(float(k[5]) for k in k5_vol[-3:])  # 最近3根的峰值量
            else:
                peak_vol = 0
        except Exception:
            peak_vol = 0
        pump_entry = {
            'is_pump': True,
            'original_qty': qty,
            'pump_peak_vol': peak_vol,
            'tp1_done': False, 'tp2_done': False, 'tp3_done': False,
            'time_stop_min': 120,
        }
    state['positions'][symbol] = {
        'entry': price, 'sl': sl_price, 'tp': tp_price,
        'qty': qty, 'leverage': leverage, 'highest': price,
        'atr': atr, 'source': signal_source, 'score': entry_score,
        'open_time': int(time.time()), 'partial_tp_done': False,
        'entry_oi': entry_oi, 'entry_vol': entry_vol,
        'signal_type': signal_source,
        'market_state_entry': _s0_entry.get('market_state', ''),
        'btc_trend_entry':    _s0_entry.get('btc_trend', ''),
        'breadth_entry':      _s0_entry.get('breadth', ''),
        **pump_entry,
    }
    save_state(state)

    msg = (f"📈 *S6 做多* {symbol}\n"
           f"方向: 多 | 评分: {entry_score}/10\n"
           f"入场: {price} | 仓位: {position_usdt:.1f}U ({leverage}x)\n"
           f"止损: {sl_price} (-{(price-sl_price)/price*100:.1f}%)\n"
           f"止盈: {tp_price} (+{(tp_price-price)/price*100:.1f}%)")
    log(msg.replace('*',''))
    tg(msg)
    return True

def open_short(symbol, signal_source='s2'):
    """做空开仓（比做多多2个验证条件）"""
    state = load_state()

    # 熔断检查
    if is_circuit_open():
        log(f'[熔断] 暂停开仓，冷却中')
        return False
    short_sym = symbol + '_SHORT'

    if len(state['positions']) >= MAX_POSITIONS:
        add_candidate(symbol + '_SHORT', score_signal(symbol))
        return False
    if short_sym in state['positions'] or symbol in state['positions']:
        log(f'[跳过] {symbol} 已有持仓')
        return False

    price = get_price(symbol)
    atr = get_atr(symbol)
    if atr == 0:
        return False

    # 时间过滤已移除

    # 做空方向过滤：价格在MA20下方（顺势做空）
    ma20 = get_ma(symbol)
    if ma20 > 0 and price > ma20 * 1.02:
        log(f'[跳过做空] {symbol} 价格高于MA20，不做空')
        return False

    # 4h跌幅确认
    klines_4h = get_klines(symbol, '4h', 2)
    if len(klines_4h) >= 2:
        chg_4h = (float(klines_4h[-1][4]) - float(klines_4h[-2][4])) / float(klines_4h[-2][4]) * 100
        if chg_4h >= 0:
            log(f'[跳过做空] {symbol} 4h涨幅{chg_4h:.1f}%，不顺势做空')
            return False

    # 跌破4h低点确认
    klines_4h_low = get_klines(symbol, '1h', 4)
    recent_low = min(float(k[3]) for k in klines_4h_low) if klines_4h_low else 0
    if price > recent_low * 1.002:
        log(f'[跳过做空] {symbol} 未跌破4h低点{recent_low:.4f}')
        return False

    # 止损：最近20h最高价上方0.5%
    klines_20h = get_klines(symbol, '1h', 20)
    resistance = max(float(k[2]) for k in klines_20h) if klines_20h else price * 1.02
    sl_price = round(resistance * 1.005, 6)
    sl_pct = (sl_price - price) / price * 100
    # 最小止损距离保护
    if sl_pct < 0.5:
        sl_price = round(price + 1.5 * atr, 6)
        sl_pct = (sl_price - price) / price * 100
    if sl_pct > 3.5:
        log(f'[跳过做空] {symbol} 止损距离{sl_pct:.1f}%>3%')
        return False

    tp_price = round(price - 3 * (sl_price - price), 6)

    leverage = calc_leverage(atr, price)
    # 全局预算检查
    ok, budget_msg = check_global_budget(0)
    if not ok:
        log(f'[跳过做空] {symbol} {budget_msg}')
        return False
    remaining = get_shared_remaining()
    if remaining <= 0:
        _rj('budget_s6', symbol); return False
    entry_score = score_signal(symbol, signal_source=signal_source)
    if entry_score < 5:
        log(f'[跳过] {symbol} 评分{entry_score}<5，信号质量不足')
        return _rj(_local_rj, 'score_too_low')
    position_usdt = remaining * score_to_fraction(entry_score)  # 剩余pool×评分%，依次递减
    qty_precision, price_precision = get_symbol_info(symbol)
    qty = round(position_usdt * leverage / price, qty_precision)
    if qty <= 0:
        return False

    fapi_post('/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    fapi_post('/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'CROSSED'})

    order = fapi_post('/fapi/v1/order', {
        'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',
        'quantity': qty, 'positionSide': 'BOTH'
    })
    if 'orderId' not in order:
        log(f'[做空失败] {symbol}: {order}')
        tg(f'❌ 做空失败 {symbol}\n{order.get("msg","")}')
        return False

    state['positions'][short_sym] = {
        'entry': price, 'sl': sl_price, 'tp': tp_price,
        'qty': qty, 'leverage': leverage, 'lowest': price,
        'atr': atr, 'source': signal_source, 'score': entry_score,
        'side': 'SHORT', 'open_time': int(time.time())
    }
    save_state(state)

    msg = (f"🔻 *S6 做空* {symbol}\n"
           f"方向: 空 | 评分: {entry_score}/10\n"
           f"入场: {price} | 仓位: {position_usdt:.1f}U ({leverage}x)\n"
           f"止损: {sl_price} (+{sl_pct:.1f}%)\n"
           f"止盈: {tp_price} (-{(price-tp_price)/price*100:.1f}%)")
    log(msg.replace('*',''))
    tg(msg)
    return True

def market_close(symbol, qty, side='SELL'):
    """市价平仓"""
    return fapi_post('/fapi/v1/order', {
        'symbol': symbol, 'side': side, 'type': 'MARKET',
        'quantity': qty, 'positionSide': 'BOTH', 'reduceOnly': 'true'
    })


def _handle_pump_exit(state, symbol, pos, price, atr, hold_min, real_sym) -> bool:
    """妖币泵仓位退出逻辑（source='s2p'）：3-TP + 120min（追踪已交PM）"""
    orig_qty = pos.get('original_qty', pos['qty'])
    profit_pct = (price - pos['entry']) / pos['entry'] * 100
    profit_atr = (price - pos['entry']) / atr if atr > 0 else 0

    # 硬止损
    if price <= pos['sl']:
        log(f'[泵止损] {symbol} 价格{price} <= 止损{pos["sl"]}')
        r = market_close(real_sym, pos['qty'])
        if 'orderId' in r:
            del state['positions'][symbol]
            record_trade(real_sym, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'],
                 exit_reason="stop_loss", signal_type="s2p", score=pos.get("score",0))
            tg(f'🛑 [泵止损] {symbol} 出场{price:.4f} {profit_pct:+.1f}%')
            return True
        return False

    # 量能衰减退出
    if pos.get('pump_peak_vol', 0) > 0:
        try:
            k5 = get_klines(real_sym, "5m", 3)
            cur_vol = float(k5[-2][5]) if k5 and len(k5) >= 2 else 0
            vol_ratio = cur_vol / pos["pump_peak_vol"]
            if vol_ratio < 0.4:
                log(f'[泵量衰减] {symbol} 当前量{cur_vol:.0f} < 峰值{pos["pump_peak_vol"]:.0f}的{vol_ratio:.0%}')
                r = market_close(real_sym, pos['qty'])
                if 'orderId' in r:
                    del state['positions'][symbol]
                    record_trade(real_sym, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'],
                         exit_reason="volume_decay", signal_type="s2p", score=pos.get("score",0))
                    tg(f'📉 [泵量衰减] {symbol} 量能萎缩至{vol_ratio:.0%} 出场{price:.4f} {profit_pct:+.1f}%')
                    return True
                return False
        except Exception:
            pass

    # TP1 (1.5xATR) -> 卖30%
    if profit_atr >= 1.5 and not pos.get("tp1_done"):
        sell_qty = min(round(orig_qty * 0.30, 6), pos["qty"])
        if sell_qty > 0:
            log(f'[泵TP1] {symbol} 减仓{sell_qty/orig_qty*100:.0f}% 浮盈{profit_atr:.1f}xATR')
            r = market_close(real_sym, sell_qty)
            if 'orderId' in r:
                state["positions"][symbol]["tp1_done"] = True
                state["positions"][symbol]["qty"] = round(pos["qty"] - sell_qty, 6)
                state["positions"][symbol]["sl"] = max(pos["sl"], round(pos["entry"] * 1.001, 8))
                tg(f'🎯 [泵TP1] {symbol} 减仓30% 价格{price:.4f} {profit_pct:+.1f}%')
                return True
        return False

    # TP2 (3.0xATR) -> 卖30%
    if profit_atr >= 3.0 and not pos.get("tp2_done"):
        sell_qty = min(round(orig_qty * 0.30, 6), pos["qty"])
        if sell_qty > 0:
            log(f'[泵TP2] {symbol} 减仓{sell_qty/orig_qty*100:.0f}% 浮盈{profit_atr:.1f}xATR')
            r = market_close(real_sym, sell_qty)
            if 'orderId' in r:
                state["positions"][symbol]["tp2_done"] = True
                state["positions"][symbol]["qty"] = round(pos["qty"] - sell_qty, 6)
                tg(f'🎯 [泵TP2] {symbol} 减仓30% 价格{price:.4f} {profit_pct:+.1f}%')
                return True
        return False

    # TP3 (4.5xATR) -> 卖25%
    if profit_atr >= 4.5 and not pos.get("tp3_done"):
        sell_qty = min(round(orig_qty * 0.25, 6), pos["qty"])
        if sell_qty > 0:
            log(f'[泵TP3] {symbol} 减仓{sell_qty/orig_qty*100:.0f}% 浮盈{profit_atr:.1f}xATR')
            r = market_close(real_sym, sell_qty)
            if 'orderId' in r:
                state["positions"][symbol]["tp3_done"] = True
                state["positions"][symbol]["qty"] = round(pos["qty"] - sell_qty, 6)
                state["positions"][symbol]["sl"] = round(price - 0.3 * atr, 8)
                tg(f'🎯 [泵TP3] {symbol} 减仓25% 价格{price:.4f} {profit_pct:+.1f}%')
                return True
        return False

    # 时间止损120min
    if hold_min >= 120:
        log(f'[泵时间止损] {symbol} 持仓{hold_min:.0f}min超过120min')
        r = market_close(real_sym, pos['qty'])
        if 'orderId' in r:
            del state['positions'][symbol]
            record_trade(real_sym, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'],
                 exit_reason="time_stop", signal_type="s2p", score=pos.get("score",0))
            tg(f'⏱ [泵时间止损] {symbol} 持仓{hold_min:.0f}min 出场{price:.4f} {profit_pct:+.1f}%')
            return True
        return False

    return False

def monitor_positions():
    """2秒高频监控：软件止损+评分检查"""
    state = load_state()
    if not state['positions']:
        return

    # 批量获取所有持仓标的价格（一次请求）
    real_syms = list({pos_key.replace('_SHORT', '') for pos_key in state['positions']})
    price_map = batch_get_prices(real_syms)

    changed = False
    for symbol, pos in list(state['positions'].items()):
        try:
            is_short = pos.get('side') == 'SHORT'
            real_sym = symbol.replace('_SHORT', '')
            price = price_map.get(real_sym) or get_price(real_sym)
            atr = pos['atr']
            hold_min = (time.time() - pos['open_time']) / 60  # 提前定义，全块复用

            if is_short:
                # 做空止损：价格涨破止损价
                if price >= pos['sl']:
                    log(f'[做空止损] {real_sym} 价格{price} >= 止损{pos["sl"]}')
                    r = market_close(real_sym, pos['qty'], side='BUY')
                    if 'orderId' in r:
                        del state['positions'][symbol]
                        record_trade(real_sym, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='stop_loss',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                        changed = True
                        continue
                # 做空止盈
                if price <= pos['tp']:
                    log(f'[做空止盈] {real_sym} 价格{price} <= 止盈{pos["tp"]}')
                    r = market_close(real_sym, pos['qty'], side='BUY')
                    if 'orderId' in r:
                        del state['positions'][symbol]
                        record_trade(real_sym, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='take_profit',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                        changed = True
            else:
                # 做多止损
                if price <= pos['sl']:
                    log(f'[止损触发] {symbol} 价格{price} <= 止损{pos["sl"]}')
                    r = market_close(symbol, pos['qty'])
                    if 'orderId' in r:
                        del state['positions'][symbol]
                        record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='stop_loss',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                        changed = True
                        continue
                    else:
                        log(f'[止损失败] {symbol}: {r}')

                # 妖币泵退出（s2p信号）：替换所有后续正常退出逻辑
                if pos.get('is_pump') or pos.get('source') == 's2p':
                    if _handle_pump_exit(state, real_sym, pos, price, atr, hold_min, real_sym):
                        continue
                    # 泵仓位不分层冻结，继续走后续通用止损

                # === s2 独立生命周期（情绪异动捕捉，快进快出）===
                # s2 本质是"资金异动捕捉"，不是趋势确认，用更短的生命周期
                if pos.get('source', '').startswith('s2'):
                    s2_hold_min = hold_min
                    s2_profit_atr = (price - pos['entry']) / atr if atr > 0 else 0

                    # 1. 前10分钟 Follow-through 验证：无方向则秒退
                    if 5 <= s2_hold_min <= 10 and s2_profit_atr < 0.1:
                        try:
                            cur_oi = get_current_oi(symbol)
                            # s2h/s2b/s2e 是非OI驱动信号，跳过OI增长检查
                            src = pos.get('source', '')
                            oi_check_required = src not in ('s2h', 's2b', 's2e')
                            oi_growing = cur_oi > pos.get('entry_oi', 0) * 1.02 if (pos.get('entry_oi', 0) > 0 and oi_check_required) else True
                            btc_kl = get_klines('BTCUSDT', '5m', 3)
                            btc_ok = True
                            if btc_kl and len(btc_kl) >= 2:
                                btc_drop = (float(btc_kl[-2][4]) - float(btc_kl[-1][4])) / float(btc_kl[-2][4]) * 100
                                btc_ok = btc_drop < 0.5
                            if not oi_growing or not btc_ok:
                                log(f'[s2快退] {symbol} {s2_hold_min:.0f}min无follow-through OI增长={oi_growing} BTC={btc_ok}')
                                r = market_close(symbol, pos['qty'])
                                if 'orderId' in r:
                                    del state['positions'][symbol]
                                    save_state(state)
                                    record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='momentum_decay',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                                    tg(f'⚡ [s2快退] {symbol} {s2_hold_min:.0f}min无延续性 出场{price:.4f} {(price-pos["entry"])/pos["entry"]*100:+.1f}%')
                                    changed = True
                                    continue
                        except Exception as e:
                            log(f'[s2快退检测异常] {symbol}: {e}')

                    # 2. s2 最大持仓 45min（趋势单用4-8h，s2只用45min）
                    if s2_hold_min >= 45:
                        log(f'[s2时间止损] {symbol} 持仓{s2_hold_min:.0f}min 超过45min限制')
                        r = market_close(symbol, pos['qty'])
                        if 'orderId' in r:
                            del state['positions'][symbol]
                            record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='time_stop',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                            tg(f'⏱ [s2时间止损] {symbol} 持仓{s2_hold_min:.0f}min 出场{price:.4f} {(price-pos["entry"])/pos["entry"]*100:+.1f}%')
                            changed = True
                            continue

                    # 3. s2 动能衰减：15min后浮盈≤0（亏损）则退出，微利单多给10分钟
                    if s2_hold_min >= 15 and not pos.get('partial_tp_done'):
                        if s2_profit_atr <= 0:  # 亏损 → 直接退出
                            log(f'[s2动能衰减] {symbol} {s2_hold_min:.0f}min浮亏{s2_profit_atr:.2f}×ATR')
                            r = market_close(symbol, pos['qty'])
                        if 'orderId' in r:
                            del state['positions'][symbol]
                            save_state(state)
                            record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='momentum_decay',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                            tg(f'📉 [s2动能衰减] {symbol} {s2_hold_min:.0f}min 出场{price:.4f} {(price-pos["entry"])/pos["entry"]*100:+.1f}%')
                            changed = True
                            continue

                # === Exit Intelligence: Dynamic Time Stop ===
                MAX_HOLD = {'s2a': 4*3600, 's2b': 6*3600, 's2c': 8*3600, 's2d': 4*3600, 's2e': 4*3600, 's2f': 4*3600, 's2g': 4*3600}
                src = pos.get('source', '')
                max_hold_sec = int(get_signal_profile(src).get('max_hold_h', MAX_HOLD.get(src, 6*3600) / 3600) * 3600)
                hold_sec = time.time() - pos['open_time']
                # 浮盈>1.5×ATR自动延长50%寿命（避免砍掉大趋势单）
                _profit_atr_ts = (price - pos['entry']) / atr if atr > 0 else 0
                if _profit_atr_ts > 1.5:
                    max_hold_sec = int(max_hold_sec * 1.5)
                if hold_sec > max_hold_sec:
                    pct = (price - pos['entry']) / pos['entry'] * 100
                    log(f'[时间止损] {symbol} 持仓{hold_sec/3600:.1f}h 超过{max_hold_sec/3600:.0f}h限制({pos.get("source","?")})')
                    r = market_close(symbol, pos['qty'])
                    if 'orderId' in r:
                        del state['positions'][symbol]
                        record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='time_stop',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                        tg(f'[时间止损] {symbol} 持仓{hold_sec/3600:.1f}h 超限({pos.get("source","?")}={max_hold_sec/3600:.0f}h) 出场价{price:.4f} 盈亏{pct:+.1f}%')
                        changed = True
                        continue

                # === Exit Intelligence: Thesis Failure Exit (评分制) ===
                try:
                    thesis_score = 0
                    thesis_reasons = []

                    # OI下降≥5%: +1分
                    if pos.get('entry_oi', 0) > 0:
                        try:
                            cur_oi = get_current_oi(symbol)
                            if cur_oi > 0:
                                oi_drop = (pos['entry_oi'] - cur_oi) / pos['entry_oi'] * 100
                                if oi_drop >= 5:
                                    thesis_score += 1
                                    thesis_reasons.append(f'OI↓{oi_drop:.0f}%')
                        except: pass

                    # 量熄火（当前1h量<开仓时60%）: +1分
                    if pos.get('entry_vol', 0) > 0:
                        try:
                            kl = get_klines(symbol, '1h', 3)
                            cur_vol = float(kl[-2][5]) if kl and len(kl) >= 2 else 0
                            if cur_vol > 0 and cur_vol < pos['entry_vol'] * 0.6:
                                thesis_score += 1
                                thesis_reasons.append('量熄火')
                        except: pass

                    # 跌破EMA20: +1分
                    try:
                        kl20 = get_klines(symbol, '1h', 22)
                        if kl20 and len(kl20) >= 21:
                            ema20 = sum(float(k[4]) for k in kl20[-21:-1]) / 20
                            if price < ema20:
                                thesis_score += 1
                                thesis_reasons.append('跌破EMA20')
                    except: pass

                    # BTC 15m跌幅>1%: +2分（宏观权重更高）
                    try:
                        btc_kl = get_klines('BTCUSDT', '15m', 2)
                        if btc_kl and len(btc_kl) >= 2:
                            btc_drop = (float(btc_kl[-2][4]) - float(btc_kl[-1][4])) / float(btc_kl[-2][4]) * 100
                            if btc_drop > 1:
                                thesis_score += 2
                                thesis_reasons.append(f'BTC↓{btc_drop:.1f}%')
                    except: pass

                    # 前30min阈值3分，30min后阈值4分（避免趋势回踩误杀）
                    threshold = 3 if hold_min < 30 else 4
                    if thesis_score >= threshold:
                        reasons_str = ' + '.join(thesis_reasons)
                        log(f'[逻辑失效] {symbol} 评分{thesis_score}/{threshold} {reasons_str}')
                        r = market_close(symbol, pos['qty'])
                        if 'orderId' in r:
                            del state['positions'][symbol]
                            record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='thesis_failure',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                            tg(f'🧠 [逻辑失效] {symbol} 评分{thesis_score} {reasons_str}\n出场{price:.4f} {(price-pos["entry"])/pos["entry"]*100:+.1f}%')
                            changed = True
                            continue
                except Exception as e:
                    log(f'[逻辑失效检测异常] {symbol}: {e}')

                # 假突破识别：开仓15分钟内，结构破坏立刻平仓
                if hold_min < 15 and price < pos['entry']:
                    klines_1h = get_klines(symbol, '1h', 3)
                    if len(klines_1h) >= 3:
                        entry_1h_open = float(klines_1h[-2][1])  # 入场时1h开盘价
                        cur_vol = float(klines_1h[-1][5])
                        avg_vol = (float(klines_1h[-2][5]) + float(klines_1h[-3][5])) / 2
                        # 跌破1h开盘价 + 放量下跌 = 假突破
                        if price < entry_1h_open and cur_vol > avg_vol * 1.3:
                            log(f'[假突破] {symbol} 开仓{hold_min:.0f}min跌破1h开盘价且放量，立刻平仓')
                            r = market_close(symbol, pos['qty'])
                            if 'orderId' in r:
                                del state['positions'][symbol]
                                save_state(state)
                                record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='thesis_failure',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                                changed = True
                                continue

                # 动能衰减早退：开仓30min后浮盈不足0.5×ATR，说明动能不足
                # 豁免：追踪止损已超过入场价（缓坡行情保本后不强退）
                stop_above_entry = pos.get('stop_loss', 0) > pos['entry']
                if 30 <= hold_min <= 60 and not pos.get('partial_tp_done') and not stop_above_entry:
                    profit_vs_atr = (price - pos['entry']) / atr if atr > 0 else 0
                    if profit_vs_atr < 0.5:
                        log(f'[动能衰减] {symbol} 开仓{hold_min:.0f}min浮盈{profit_vs_atr:.2f}×ATR<0.5，动能不足提前离场')
                        r = market_close(symbol, pos['qty'])
                        if 'orderId' in r:
                            del state['positions'][symbol]
                            save_state(state)
                            record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='momentum_decay',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                            changed = True
                            continue

                # 做多止盈
                if price >= pos['tp']:
                    log(f'[止盈触发] {symbol} 价格{price} >= 止盈{pos["tp"]}')
                    r = market_close(symbol, pos['qty'])
                    if 'orderId' in r:
                        del state['positions'][symbol]
                        record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='take_profit',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                        changed = True
                        continue

                # 做多走弱检测：浮盈≥1%才检查，量价双弱主动出场
                profit_pct = (price - pos['entry']) / pos['entry'] * 100
                if profit_pct >= 1.0:
                    klines_15m = get_klines(symbol, '15m', 7)
                    if len(klines_15m) >= 7:
                        vols = [float(k[5]) for k in klines_15m]
                        avg_vol = sum(vols[-6:-2]) / 4
                        cur_vol = vols[-2]
                        last2_bearish = (float(klines_15m[-3][4]) < float(klines_15m[-3][1]) and
                                         float(klines_15m[-4][4]) < float(klines_15m[-4][1]))
                        if cur_vol < avg_vol * 0.5 and last2_bearish:
                            log(f'[走弱出场] {symbol} 量能萎缩+连续阴线，主动平仓锁利')
                            r = market_close(symbol, pos['qty'])
                            if 'orderId' in r:
                                del state['positions'][symbol]
                                record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='momentum_decay',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                                changed = True
                                continue

                    # 放量滞涨出货检测：浮盈>2%时，连续2根1h量增但涨幅<0.1%
                    if profit_pct >= 2.0:
                        klines_1h = get_klines(symbol, '1h', 4)
                        if len(klines_1h) >= 4:
                            k1, k2 = klines_1h[-3], klines_1h[-2]
                            v1, v2 = float(k1[5]), float(k2[5])
                            chg1 = (float(k1[4]) - float(k1[1])) / float(k1[1]) * 100
                            chg2 = (float(k2[4]) - float(k2[1])) / float(k2[1]) * 100
                            if v2 > v1 * 1.3 and abs(chg2) < 0.1 and abs(chg1) < 0.1:
                                log(f'[出货信号] {symbol} 放量滞涨，主动平仓锁利')
                                r = market_close(symbol, pos['qty'])
                                if 'orderId' in r:
                                    del state['positions'][symbol]
                                    record_trade(symbol, pos['entry'], price, pos['qty'], pos['leverage'], pos['source'], pos['open_time'], exit_reason='momentum_decay',
                             signal_type=pos.get('signal_type',''), market_state_entry=pos.get('market_state_entry',''), btc_trend_entry=pos.get('btc_trend_entry',''), breadth_entry=pos.get('breadth_entry',''), score=pos.get('score',0))
                                    changed = True

        except Exception as e:
            log(f'[监控异常] {symbol}: {e}')

    if changed:
        save_state(state)

# === 持仓对账 ===
def reconcile_positions(state):
    """每5分钟对比Binance实际持仓 vs state，自动清理幽灵仓位"""
    try:
        actual = fapi_get('/fapi/v2/positionRisk', {})
        if not isinstance(actual, list):
            return state
        actual_syms = {p['symbol'] for p in actual if float(p.get('positionAmt', 0)) != 0}
        for sym in list(state['positions']):
            real_sym = sym.replace('_SHORT', '')
            if real_sym not in actual_syms:
                pos = state['positions'][sym]
                log(f'[对账] {sym} state有仓但交易所无持仓，清理幽灵仓位 entry={pos["entry"]}')
                tg(f'⚠️ [对账清理] {sym} 幽灵仓位已清除，请检查交易记录')
                del state['positions'][sym]
        save_state(state)
    except Exception as e:
        log(f'[对账异常] {e}')
    return state

# === s2信号读取 ===
def check_s2_signals():
    """读取s2最新信号"""
    try:
        data = _rget('signal:s2_latest')
        if not data:
            return []
        if time.time() - data.get('ts', 0) > 300:
            return []
        return data.get('signals', [])
    except:
        return []

# === 主循环 ===



# ── 大涨直追扫描 ──────────────────────────────────────────────────────────────
def _scan_surge(processed_signals: dict, signal_source: str = 's6_surge') -> list:
    now = time.time()
    results = []
    try:
        from shared.data_cache import get_ticker_24h, get_klines
        t24 = get_ticker_24h()
        if not t24: return results
        data = t24.get('data', {}) if isinstance(t24, dict) else {}
        if not isinstance(data, dict): return results
        gainers = [(s, d) for s, d in data.items() if s.endswith('USDT')]
        gainers.sort(key=lambda x: float(x[1].get('priceChangePercent', 0)), reverse=True)
        for sym, info in gainers[:30]:
            if sym in processed_signals and now - processed_signals[sym] < 600:
                continue
            if is_tradfi(sym):
                continue
            chg = float(info.get('priceChangePercent', 0))
            vol = float(info.get('quoteVolume', 0))
            if chg < 3 or vol < 5e6:
                continue
            k1h = get_klines(sym, '1h', 5)
            if not k1h or len(k1h) < 3: continue
            vol_window = [float(bar[5]) for bar in k1h[:-3]]
            base_vol = sum(vol_window)/len(vol_window) if vol_window else 0
            if base_vol <= 0: continue
            for bar in k1h[-3:-1]:
                o, h, c, v = float(bar[1]), float(bar[2]), float(bar[4]), float(bar[5])
                surge = (c - o) / o
                vol_ratio = v / base_vol
                if surge >= 0.04 and vol_ratio >= 2.0:
                    log(f'[大涨直追] {sym} 1h阳线+{surge*100:.1f}% vol×{vol_ratio:.1f} 24h+{chg:.1f}%')
                    from s6_auto_trader import open_position
                    result = open_position(sym, signal_source=signal_source)
                    if result:
                        results.append(sym)
                        processed_signals[sym] = now
                        log(f'[大涨直追] {sym} 开多成功')
                    break
    except Exception as e:
        log(f'[大涨直追异常] {e}')
    return results

def main():
    log('s6 自动交易引擎启动')
    # 预加载TradFi黑名单（启动时打印日志）
    import threading
    threading.Thread(target=_fetch_tradfi_blacklist, daemon=True).start()
    threading.Thread(target=tg, args=('🤖 *自动交易引擎已启动*\n最大持仓: 3单\n单仓: 10%余额\n最大杠杆: 10x',), daemon=True).start()

    last_signal_ts = [0]
    last_slow_check = [0]
    last_reconcile = [0]
    processed_signals = {}  # {symbol: timestamp} 记录已处理信号

    while True:
        try:
            monitor_positions()

            # 每5分钟对账一次
            if time.time() - last_reconcile[0] >= 300:
                last_reconcile[0] = time.time()
                state = load_state()
                reconcile_positions(state)

            # 每5分钟写一次 equity_snapshot
            if time.time() - _last_snap[0] >= 300:
                _last_snap[0] = time.time()
                _write_equity_snapshot()

            if time.time() - last_slow_check[0] >= 30:
                last_slow_check[0] = time.time()
                log('[心跳] 主循环运行中')
                _flush_reject_matrix()

                # 清理10分钟前的记录
                now = time.time()
                processed_signals = {k: v for k, v in processed_signals.items() if now - v < 600}

                signals = check_s2_signals()
                if signals:
                    last_signal_ts[0] = time.time()
                for sig in signals:
                    symbol = sig.get('symbol')
                    side = sig.get('side', 'LONG')
                    if symbol:
                        # 过滤TradFi传统资产合约（-4411错误）
                        if is_tradfi(symbol):
                            log(f'[过滤] {symbol} 是TradFi传统资产，跳过')
                            continue
                        # 去重：10分钟内同一币种只处理一次
                        if symbol in processed_signals:
                            continue
                        processed_signals[symbol] = now
                        source = sig.get('source', 's2')
                        if side == 'SHORT':
                            open_short(symbol, signal_source=source)
                        else:
                            open_position(symbol, signal_source=source)

                # 大涨直追扫描：每2分钟从S9缓存找放量大涨的币做多
                try:
                    if time.time() % 120 < 5:
                        _scan_surge(processed_signals)
                except Exception as e:
                    log(f"[大涨直追异常] {e}")
                
                # 泵信号(s2p)已由S2扫描器合并输出到s2_latest_signal.json
                if time.time() - last_signal_ts[0] > 3600:
                    pool = load_candidates()
                    for sym in sorted(pool, key=lambda s: pool[s]['score'], reverse=True):
                        state = load_state()
                        if sym in state['positions']:
                            continue
                        # 过滤TradFi传统资产合约（-4411错误）
                        if is_tradfi(sym):
                            pool.pop(sym, None)
                            log(f'[备选池过滤] {sym} 是TradFi传统资产，移除')
                            continue
                        log(f'[备选池] 检查 {sym} (评分{pool[sym]["score"]})')
                        result = open_position(sym, signal_source='candidate')
                        if result:
                            pool = load_candidates()
                            pool.pop(sym, None)
                            save_candidates(pool)
                            break

        except Exception as e:
            log(f'[主循环异常] {e}')
            health.record('main_loop', success=False)
        else:
            health.record('main_loop', success=True)

        # 每5分钟 flush 健康指标 + 检查降级
        if int(time.time()) % 300 < 2:
            health.flush_to_ch()
            alerts = health.check_degraded()
            if alerts:
                tg('⚠️ [s6健康告警]\n' + '\n'.join(alerts))

        time.sleep(2)

if __name__ == '__main__':
    main()
