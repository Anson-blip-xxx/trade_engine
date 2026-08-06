#!/usr/bin/env python3
"""
s3 Market Brain v3.5 — Event-Driven Market Intelligence
=========================================================
职责: 只输出市场事实（Event），不输出多空，不耦合策略。

核心变更（2026-07-22 code review）:
  - ATR 方向修正
  - EMA 增量计算 O(1)
  - 锁范围缩小（copy → release → compute）
  - Event 生命周期（ACTIVE/UPDATE/END）
  - Event 去重增加 strength delta 检查
  - Redis 写优先，JSON 只做调试快照
"""
import json, time, threading, hmac, hashlib, requests, os, sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from collections import deque

# ── 目录 ────────────────────────────────────────────────────
TRADE_DIR  = Path(__file__).resolve().parent.parent
CONFIG_DIR = TRADE_DIR / 'strategies/config'
_LOG_DIR   = TRADE_DIR.parent / 'logs/s3'

# Redis
sys.path.insert(0, str(TRADE_DIR))
from shared.redis_store import set as _rset, get as _rget, publish as _rpublish

# ── 参数 ────────────────────────────────────────────────────
TOP_N        = 60
MIN_VOL_24H  = 50_000_000
FETCH_INTERVAL = 60
PERSIST_INTERVAL = 300   # 5min 持久化一次缓存
WINDOWS = [('15m', 15), ('1h', 60), ('4h', 240), ('24h', 1440)]
FAPI_URL = 'https://fapi.binance.com'
WS_BIGORDER = 'wss://fstream.binance.com/stream?streams='
_MAX_KLINES = 1500   # 最大保留 K 线数

# ── WS 大单流参数（不变） ──────────────────────────────────
BIG_ORDER_MIN_USDT = 50_000
WS_SYMBOLS = ['btcusdt']

# ── 事件阈值 ────────────────────────────────────────────────
THRESHOLDS = {
    'pulse_up':      {'15m': 5.0, '1h': 8.0, 'vol_ratio': 1.5},
    'pulse_down':    {'15m': -5.0, '1h': -8.0, 'vol_ratio': 1.5},
    'panic_sell':    {'15m': -4.0, 'vol_ratio': 2.0},
    'trend_up':      {'1h': 1.0, '4h': 2.0, '24h': 5.0},
    'trend_down':    {'1h': -1.0, '4h': -2.0, '24h': -5.0},
    'high_vol':      {'vol_ratio': 2.0},
    'low_vol':       {'vol_ratio': 0.3},
    'pump_up':       {'15m': 8.0, '1h': 12.0, 'vol_ratio': 2.0},
    'pump_down':     {'15m': -8.0, '1h': -12.0, 'vol_ratio': 2.0},
}

# ── Event 冷却 ──────────────────────────────────────────────
EVENT_COOLDOWN = 30       # 同(sym+type)最短间隔
STRENGTH_RESEND_DELTA = 20  # strength 变化超过此值重新推送
EVENT_MAX_AGE = 300       # 5min 后事件自动 END

# ════════════════════════════════════════════════════════════
#  Logging
# ════════════════════════════════════════════════════════════
def _log(msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [s3] {msg}'
    print(line)
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        (_LOG_DIR / f'{time.strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass

# ════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════
_API_LAST_CALL = 0

def _api_get(url: str, timeout: int = 10) -> Optional[list | dict]:
    global _API_LAST_CALL
    now = time.time()
    if now - _API_LAST_CALL < 0.1:
        time.sleep(0.1 - (now - _API_LAST_CALL))
    _API_LAST_CALL = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 429:
            _log(f"⚠️ API rate limited ({r.status_code}), backing off")
            time.sleep(5)
            return _api_get(url, timeout)
        if r.status_code != 200:
            _log(f"⚠️ API error {r.status_code} for {url[:80]}")
            return None
        return r.json()
    except Exception as e:
        _log(f"API request failed: {e}")
        return None

def get_top_symbols(n: int = TOP_N) -> list:
    """按24h成交额排序取前 n 个 USDT 永续合约"""
    data = _api_get(f'{FAPI_URL}/fapi/v1/ticker/24hr')
    if not data:
        return []
    usdt = [t for t in data if isinstance(t, dict) and t.get('symbol','').endswith('USDT')]
    return [t['symbol'] for t in sorted(
        usdt, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True
    ) if float(t.get('quoteVolume', 0)) > MIN_VOL_24H][:n]

def fetch_klines(symbol: str, interval: str = '1m', limit: int = 500) -> list:
    """获取K线，返回 [{t, o, h, l, c, v}]"""
    data = _api_get(f'{FAPI_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}')
    if not data:
        return []
    return [{
        't': k[0] // 1000,       # 转秒
        'o': float(k[1]),
        'h': float(k[2]),
        'l': float(k[3]),
        'c': float(k[4]),
        'v': float(k[5]),
        # Binance kline field 9: taker buy base-asset volume.
        'tbv': float(k[9]),
    } for k in data]

# ════════════════════════════════════════════════════════════
#  指标计算（增量优化）
# ════════════════════════════════════════════════════════════

# EMA 缓存: {symbol: {period: ema_value}}
_ema_cache: dict = {}

def compute_ema(values: list, period: int, symbol: str = None) -> float:
    """指数移动平均（支持增量更新）"""
    if not values:
        return 0
    
    # 如果有缓存且走增量路径
    if symbol and symbol in _ema_cache and period in _ema_cache[symbol]:
        prev_ema = _ema_cache[symbol][period]
        # 只需要最新一个值做增量更新
        latest = values[0]
        k = 2 / (period + 1)
        new_ema = latest * k + prev_ema * (1 - k)
        _ema_cache[symbol][period] = new_ema
        return new_ema
    
    # 首次计算（全量）
    if len(values) < period:
        result = values[-1] if values else 0
    else:
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        result = ema
    
    # 缓存
    if symbol:
        if symbol not in _ema_cache:
            _ema_cache[symbol] = {}
        _ema_cache[symbol][period] = result
    
    return result

def compute_rsi(prices: list, period: int = 14) -> float:
    """RSI 计算"""
    if len(prices) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(prices) - period, len(prices)):
        chg = prices[i] - prices[i-1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def compute_atr(candles: list, period: int = 14) -> float:
    """ATR（平均真实波幅）— candles 已经确保是最新→最旧"""
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, min(len(candles), period + 1)):
        hl = candles[i]['h'] - candles[i]['l']
        hc = abs(candles[i]['h'] - candles[i-1]['c'])
        lc = abs(candles[i]['l'] - candles[i-1]['c'])
        trs.append(max(hl, hc, lc))
    return sum(trs) / len(trs) if trs else 0

def compute_window_data(candles: list, window_min: int, symbol: str = None) -> dict:
    """
    计算单个滚动窗口数据
    candles: 最新→最旧, 取前 window_min 根
    返回 dict 包含所有预计算指标
    """
    k = candles[:window_min]
    if not k:
        return {}
    
    # 反转：从最旧→最新（指标计算需要这个方向）
    k_rev = list(reversed(k))
    
    closes  = [c['c'] for c in k_rev]
    highs   = [c['h'] for c in k_rev]
    lows    = [c['l'] for c in k_rev]
    volumes = [c['v'] for c in k_rev]
    taker_buy = [c.get('tbv', 0.0) for c in k_rev]
    
    first_close = closes[0]
    last_close  = closes[-1]
    chg_pct = ((last_close - first_close) / first_close * 100) if first_close else 0
    
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    latest_vol = volumes[-1] if volumes else 0
    total_volume = sum(volumes)
    taker_buy_volume = sum(taker_buy)
    taker_sell_volume = max(0.0, total_volume - taker_buy_volume)
    
    # 获取该币的 symbol 用于 EMA 缓存键 - 从 candles 第一根的 __symbol 属性取
    # symbol passed as param
    
    return {
        'chg':       round(chg_pct, 2),
        'atr':       round(compute_atr(k_rev), 6),
        'atr_pct':   round(compute_atr(k_rev) / last_close * 100, 4) if last_close else 0,
        'volume':    round(sum(volumes), 2),
        'vol_ratio': round(latest_vol / avg_vol, 2) if avg_vol > 0 else 1.0,
        'taker_buy_volume': round(taker_buy_volume, 2),
        'taker_sell_volume': round(taker_sell_volume, 2),
        'taker_buy_ratio': round(taker_buy_volume / total_volume, 4) if total_volume > 0 else 0.5,
        'orderflow_bias': round((taker_buy_volume - taker_sell_volume) / total_volume, 4)
        if total_volume > 0 else 0.0,
        'high':      round(max(highs), 8),
        'low':       round(min(lows), 8),
        'close':     round(last_close, 8),
        'rsi':       round(compute_rsi(closes), 2),
        'ema20':     round(compute_ema(closes, 20, symbol), 6),
        'ema60':     round(compute_ema(closes, 60, symbol), 6),
        'volatility': round((max(highs) - min(lows)) / last_close * 100, 4) if last_close else 0,
        'drawdown':  round((min(lows) - max(highs)) / max(highs) * 100, 4) if max(highs) else 0,
    }

# ════════════════════════════════════════════════════════════
#  Event 管理
# ════════════════════════════════════════════════════════════

# Event 生命周期状态
# _event_states: {f"{sym}_{type}": {state, strength, ts}}
_event_states: dict = {}

def _get_event_key(evt: dict) -> str:
    return f"{evt.get('symbol', '')}_{evt.get('type', '')}"

def _update_event_state(evt: dict, now: float) -> Optional[dict]:
    """
    管理 Event 生命周期，返回更新后的 event（或 None 表示无需发送）
    
    生命周期:
      - 首次: ACTIVE
      - 持续存在且 strength 变化大: UPDATE
      - 持续存在但 strength 变化小: 冷却跳过
      - 之前有但现在没有: END（由 _end_expired_events 处理）
    """
    key = _get_event_key(evt)
    strength = evt.get('strength', 0)
    prev = _event_states.get(key)
    
    if not prev:
        # 全新事件 → ACTIVE
        _event_states[key] = {
            'state': 'ACTIVE',
            'strength': strength,
            'ts': now,
            'sent_ts': now,
        }
        evt['state'] = 'ACTIVE'
        evt['since'] = now
        return evt
    
    # 已存在 → 检查是否需要更新
    time_since_last = now - prev.get('sent_ts', 0)
    strength_diff = abs(strength - prev['strength'])
    
    if time_since_last < EVENT_COOLDOWN and strength_diff < STRENGTH_RESEND_DELTA:
        # 冷却期内且强度变化不大 → 跳过
        return None
    
    # 需要更新
    state = 'UPDATE' if prev['state'] in ('ACTIVE', 'UPDATE') else 'ACTIVE'
    _event_states[key] = {
        'state': state,
        'strength': strength,
        'ts': now,
        'sent_ts': now,
    }
    evt['state'] = state
    evt['since'] = prev.get('ts', now)
    return evt

def _end_expired_events(all_symbols: list, now: float) -> list:
    """
    检查哪些活跃 Event 过期了（超过 EVENT_MAX_AGE 未更新），
    返回 END 事件列表
    """
    ended = []
    expired = []
    for key, state in _event_states.items():
        s = state.get('state', '')
        if s not in ('ACTIVE', 'UPDATE'):
            continue  # 非活跃事件跳过（已 END 或未知）
        age = now - state.get('ts', now)
        if age > EVENT_MAX_AGE:
            # 解析 sym 和 type（从 "BTCUSDT_TREND_UP" 这样的键中提取）
            # key format: "SYMBOL_TYPE" but TYPE may contain underscores
            # Simplest: split on first _
            parts = key.split('_', 1)
            if len(parts) == 2:
                sym, etype = parts
                ended.append({
                    'type': etype,
                    'symbol': sym,
                    'strength': state.get('strength', 0),
                    'state': 'END',
                    'since': state.get('ts', now),
                    'duration': int(age),
                })
                state['state'] = 'END'
            expired.append(key)
    for k in expired:
        del _event_states[k]
    return ended

# ════════════════════════════════════════════════════════════
#  事件检测
# ════════════════════════════════════════════════════════════

def detect_events(symbol: str, windows: dict, windows_raw: dict) -> list:
    """
    基于多窗口数据检测市场事件
    返回纯市场事实列表，不含多空倾向
    """
    events = []
    w15m = windows.get('15m', {})
    w1h  = windows.get('1h', {})
    w4h  = windows.get('4h', {})
    w24h = windows.get('24h', {})
    # ── 超买超卖检查（用于方向信号过滤：价格偏离 EMA20 太远时不产生趋势信号） ──
    _price = float(w15m.get('close', 0) or 0)
    _4h_ema20 = float(w4h.get('ema20', 0) or 0)
    _4h_atr_pct = float(w4h.get('atr_pct', 0) or 0)

    def _is_oversold() -> bool:
        """价格比 4h EMA20 低超过 3×ATR = 已超卖，不产生做空信号"""
        if _price <= 0 or _4h_ema20 <= 0 or _4h_atr_pct <= 0:
            return False
        return (_4h_ema20 - _price) / _4h_ema20 * 100 > _4h_atr_pct * 3

    def _is_overbought() -> bool:
        """价格比 4h EMA20 高超过 3×ATR = 已超买，不产生做多信号"""
        if _price <= 0 or _4h_ema20 <= 0 or _4h_atr_pct <= 0:
            return False
        return (_price - _4h_ema20) / _4h_ema20 * 100 > _4h_atr_pct * 3

    # ── PULSE_UP ──
    if w15m.get('chg', 0) >= THRESHOLDS['pulse_up']['15m'] or \
       w1h.get('chg', 0) >= THRESHOLDS['pulse_up']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pulse_up']['vol_ratio']:
            if not _is_overbought():
                strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
                events.append({
                    'type': 'PULSE_UP', 'symbol': symbol,
                    'strength': max(20, strength),
                    'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                })
            else:
                _log(f'[S3] {symbol} PULSE_UP 跳过：已超买')

    # ── PULSE_DOWN ──
    if w15m.get('chg', 0) <= THRESHOLDS['pulse_down']['15m'] or \
       w1h.get('chg', 0) <= THRESHOLDS['pulse_down']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pulse_down']['vol_ratio']:
            if not _is_oversold():
                strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
                events.append({
                    'type': 'PULSE_DOWN', 'symbol': symbol,
                    'strength': max(20, strength),
                    'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                })
            else:
                _log(f'[S3] {symbol} PULSE_DOWN 跳过：已超卖')

    # ── PANIC_SELL ──
    if w15m.get('chg', 0) <= THRESHOLDS['panic_sell']['15m'] and \
       w15m.get('vol_ratio', 0) >= THRESHOLDS['panic_sell']['vol_ratio']:
        if not _is_oversold():
            strength = min(99, int(abs(w15m.get('chg', 0)) * 12))
            events.append({
                'type': 'PANIC_SELL', 'symbol': symbol,
                'strength': max(30, strength),
                'chg_15m': w15m.get('chg'), 'vol_ratio': w15m.get('vol_ratio'),
            })
        else:
            _log(f'[S3] {symbol} PANIC_SELL 跳过：已超卖')

    # ── VIOLENT_MOVE（极端波动检测） ──
    # 捕获 pump-and-dump 等窗口内剧烈波动但收盘 chg 不反映的情况
    # 如果 1h 波动 > 15% 或 4h 波动 > 25%，总有异常
    _1h_vol = float(w1h.get('volatility', 0) or 0)
    _4h_vol = float(w4h.get('volatility', 0) or 0)
    _viol_threshold = 15  # 1h 波动 15%+ 算极端
    if _1h_vol >= _viol_threshold or _4h_vol >= _viol_threshold * 1.6:
        # 判断方向：收盘在区间上半段 = 偏多，下半段 = 偏空
        _high = float(max(w1h.get('high', 0) or 0, w4h.get('high', 0) or 0))
        _low = float(min(w1h.get('low', 0) or 0, w4h.get('low', 0) or 0))
        _mid = (_high + _low) / 2
        if _mid > 0 and _price > 0:
            _is_bull = _price > _mid  # 收盘在上半段 = 买方强势
            _dir = 'BULLISH' if _is_bull else 'BEARISH'
            _strength = min(99, int(max(_1h_vol, _4h_vol) * 3))
            events.append({
                'type': f'VIOLENT_{_dir}',
                'symbol': symbol,
                'strength': max(30, _strength),
                'vol_1h': round(_1h_vol, 1),
                'vol_4h': round(_4h_vol, 1),
                'close_pos': round((_price - _low) / (_high - _low) * 100, 1) if _high != _low else 50,
            })
            _log(f'[S3] {symbol} VIOLENT_{_dir} 波动 {_1h_vol:.0f}%/4h={_4h_vol:.0f}%')

    # ── PUMP_UP（极端拉盘：高涨幅 + 放量） ──
    if w15m.get('chg', 0) >= THRESHOLDS['pump_up']['15m'] or \
       w1h.get('chg', 0) >= THRESHOLDS['pump_up']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pump_up']['vol_ratio']:
            strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
            events.append({
                'type': 'PUMP_UP', 'symbol': symbol,
                'strength': max(30, strength),
                'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                'vol_ratio': w15m.get('vol_ratio'),
            })

    # ── PUMP_DOWN（极端砸盘：高跌幅 + 放量） ──
    if w15m.get('chg', 0) <= THRESHOLDS['pump_down']['15m'] or \
       w1h.get('chg', 0) <= THRESHOLDS['pump_down']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pump_down']['vol_ratio']:
            strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
            events.append({
                'type': 'PUMP_DOWN', 'symbol': symbol,
                'strength': max(30, strength),
                'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                'vol_ratio': w15m.get('vol_ratio'),
            })

    # ── TREND_UP ──
    trend_up_1h4h = w1h.get('chg', 0) >= THRESHOLDS['trend_up']['1h'] and \
                    w4h.get('chg', 0) >= THRESHOLDS['trend_up']['4h'] and \
                    not _is_overbought()
    trend_up_24h  = w24h.get('chg', 0) >= THRESHOLDS['trend_up']['24h'] and \
                    w24h.get('ema20', 0) > w24h.get('ema60', 0)
    if trend_up_1h4h or trend_up_24h:
        strength = int(w1h.get('chg', 0) * 10 + w4h.get('chg', 0) * 5)
        events.append({
            'type': 'TREND_UP', 'symbol': symbol,
            'strength': max(15, min(99, strength)),
            'chg_1h': w1h.get('chg'), 'chg_4h': w4h.get('chg'),
        })

    # ── TREND_DOWN ──
    trend_down_1h4h = w1h.get('chg', 0) <= THRESHOLDS['trend_down']['1h'] and \
                      w4h.get('chg', 0) <= THRESHOLDS['trend_down']['4h'] and \
                      not _is_oversold()
    trend_down_24h  = w24h.get('chg', 0) <= THRESHOLDS['trend_down']['24h'] and \
                      w24h.get('ema20', 0) < w24h.get('ema60', 0)
    if trend_down_1h4h or trend_down_24h:
        strength = int(abs(w1h.get('chg', 0)) * 10 + abs(w4h.get('chg', 0)) * 5)
        events.append({
            'type': 'TREND_DOWN', 'symbol': symbol,
            'strength': max(15, min(99, strength)),
            'chg_1h': w1h.get('chg'), 'chg_4h': w4h.get('chg'),
        })

    # ── HIGH_VOL ──
    if w15m.get('vol_ratio', 0) >= THRESHOLDS['high_vol']['vol_ratio']:
        events.append({
            'type': 'HIGH_VOL', 'symbol': symbol,
            'strength': min(99, int(w15m.get('vol_ratio', 0) * 15)),
            'vol_ratio': w15m.get('vol_ratio'),
        })

    # ── LOW_VOL ──
    if w15m.get('vol_ratio', 0) <= THRESHOLDS['low_vol']['vol_ratio'] and \
       w15m.get('vol_ratio', 0) > 0:
        events.append({
            'type': 'LOW_VOL', 'symbol': symbol,
            'strength': max(10, min(99, int((1 - w15m.get('vol_ratio', 0)) * 20))),
            'vol_ratio': w15m.get('vol_ratio'),
        })

    # ── ATR_EXPAND ──
    if windows.get('15m', {}).get('atr_pct', 0) > \
       windows.get('1h', {}).get('atr_pct', 0) * 2 and \
       w15m.get('atr_pct', 0) > 0.5:
        events.append({
            'type': 'ATR_EXPAND', 'symbol': symbol,
            'strength': min(99, int(w15m.get('atr_pct', 0) * 30)),
            'atr_15m': w15m.get('atr_pct'), 'atr_1h': windows.get('1h', {}).get('atr_pct'),
        })

    # ── FAILED_BREAKOUT (stateful peak tracking) ──
    # 用 _failed_breakout_state 追踪每个币的突破状态
    raw_15m = windows_raw.get('15m', [])
    raw_4h  = windows_raw.get('4h', [])
    if len(raw_4h) >= 2 and len(raw_15m) >= 3:
        _detect_failed_breakout(symbol, raw_4h, raw_15m, events)

    return events


# ── FAILED_BREAKOUT Stateful Detection ──

# 状态: {symbol: {state, last_high, last_low, breakout_high, breakout_low, ts}}
_fb_state: dict = {}

def _detect_failed_breakout(symbol: str, raw_4h: list, raw_15m: list, events: list):
    """基于状态追踪的失败突破检测"""
    now = time.time()
    state = _fb_state.get(symbol, {'state': 'IDLE', 'last_high': 0, 'last_low': 0})
    
    prev_4h_candles = raw_4h[1:]  # 前4h（排除最新）
    if not prev_4h_candles:
        return
    
    prev_4h_high = max(c['h'] for c in prev_4h_candles)
    prev_4h_low  = min(c['l'] for c in prev_4h_candles)
    curr_15m_high = max(c['h'] for c in raw_15m)
    curr_15m_low  = min(c['l'] for c in raw_15m)
    latest_close = raw_15m[0]['c'] if raw_15m else 0
    
    # 更新历史高/低点
    if prev_4h_high > state.get('last_high', 0):
        state['last_high'] = prev_4h_high
    if prev_4h_low < state.get('last_low', float('inf')) or not state.get('last_low'):
        state['last_low'] = prev_4h_low
    
    # 状态机
    current_state = state.get('state', 'IDLE')
    
    if current_state == 'IDLE':
        # 检测突破
        if curr_15m_high > prev_4h_high * 1.008:  # 突破前高 0.8%+
            state['state'] = 'BREAKING_HIGH'
            state['breakout_high'] = curr_15m_high
            state['breakout_low'] = curr_15m_low
            state['ts'] = now
        elif curr_15m_low < prev_4h_low * 0.992:  # 跌破前低 0.8%+
            state['state'] = 'BREAKING_LOW'
            state['breakout_high'] = curr_15m_high
            state['breakout_low'] = curr_15m_low
            state['ts'] = now
    
    elif current_state == 'BREAKING_HIGH':
        # 检查是否失败（回落）
        if latest_close < state.get('breakout_high', prev_4h_high) * 0.998:
            # 确认失败
            breakout_pct = (state['breakout_high'] - prev_4h_high) / prev_4h_high * 100
            events.append({
                'type': 'FAILED_BREAKOUT',
                'symbol': symbol,
                'strength': min(99, int(breakout_pct * 10 + 20)),
                'direction': 'HIGH',
                'breakout_high': round(prev_4h_high, 8),
                'rejected_from': round(state['breakout_high'], 8),
            })
            state['state'] = 'IDLE'
        elif curr_15m_high > state.get('breakout_high', 0) * 1.002:
            # 继续上涨 → 不是失败突破
            state['state'] = 'IDLE'
        # 超过 30 根 15m K 线（约 7.5h）还没结果 → 超时重置
        if now - state.get('ts', now) > 27000:
            state['state'] = 'IDLE'
    
    elif current_state == 'BREAKING_LOW':
        # 检查是否失败（反弹）
        if latest_close > state.get('breakout_low', prev_4h_low) * 1.002:
            breakdown_pct = (prev_4h_low - state['breakout_low']) / prev_4h_low * 100
            events.append({
                'type': 'FAILED_BREAKOUT',
                'symbol': symbol,
                'strength': min(99, int(breakdown_pct * 10 + 20)),
                'direction': 'LOW',
                'breakdown_low': round(prev_4h_low, 8),
                'rejected_from': round(state['breakout_low'], 8),
            })
            state['state'] = 'IDLE'
        elif curr_15m_low < state.get('breakout_low', float('inf')) * 0.998:
            # 继续下跌 → 不是失败突破
            state['state'] = 'IDLE'
        if now - state.get('ts', now) > 27000:
            state['state'] = 'IDLE'
    
    _fb_state[symbol] = state


# ════════════════════════════════════════════════════════════
#  全局状态
# ════════════════════════════════════════════════════════════

_symbol_klines: dict = {}    # {symbol: [{t,o,h,l,c,v}]}  最新→最旧
_symbol_windows: dict = {}   # {symbol: {15m: {...}, 1h: {...}}}
_symbol_windows_raw: dict = {}
_current_kline: dict = {}    # {symbol: {t,o,h,l,c,v}} 当前未关闭的K线
_ws_kline_connected = False  # WS kline 连接状态
_lock = threading.Lock()


# ════════════════════════════════════════════════════════════
#  WS kline_1m — 增量 K 线更新（替代 REST 轮询）
# ════════════════════════════════════════════════════════════

def _make_kline_stream_url(symbols: list) -> str:
    """生成 kline_1m 组合流 URL"""
    streams = '/'.join(f'{s.lower()}@kline_1m' for s in symbols)
    return f'wss://fstream.binance.com/stream?streams={streams}'


def _on_kline_msg(ws, message):
    """处理 kline_1m WS 消息"""
    import json as _json
    try:
        data = _json.loads(message)
        if 'data' in data:
            data = data['data']
        if data.get('e') != 'kline':
            return
        
        k = data['k']
        symbol = data['s']
        ts = k['t'] // 1000   # 毫秒→秒
        is_final = k['x']     # True = 该 K 线已关闭
        
        candle = {
            't': ts,
            'o': float(k['o']),
            'h': float(k['h']),
            'l': float(k['l']),
            'c': float(k['c']),
            'v': float(k['v']),
        }
        
        with _lock:
            if is_final:
                # K 线关闭 → 追加到历史
                if symbol not in _symbol_klines:
                    _symbol_klines[symbol] = []
                existing = _symbol_klines[symbol]
                # 去重（避免 WS 重连导致重复）
                if not existing or existing[0]['t'] != ts:
                    existing.insert(0, candle)
                    if len(existing) > _MAX_KLINES:
                        existing.pop()
                # 清除当前 K 线
                _current_kline.pop(symbol, None)
            else:
                # K 线仍在进行中 → 更新当前（高/低可能变化）
                _current_kline[symbol] = candle
    except Exception:
        pass  # WS 消息解析错误静默


def _on_kline_error(ws, error):
    _log(f"WS kline error: {error}")


def _on_kline_close(ws, close_status_code, close_msg):
    global _ws_kline_connected
    _ws_kline_connected = False
    _log(f"WS kline closed: {close_status_code} {close_msg}")


def _on_kline_open(ws):
    global _ws_kline_connected
    _ws_kline_connected = True
    _log("WS kline_1m connected")


def ws_kline_loop():
    """K线增量 WebSocket 线程"""
    import websocket as _ws
    
    reconnect_delay = 1
    
    while True:
        try:
            # 获取当前跟踪的币种列表
            with _lock:
                tracked = list(_symbol_klines.keys())
            
            if not tracked:
                _log("WS kline: no symbols yet, waiting for REST init")
                time.sleep(10)
                continue
            
            url = _make_kline_stream_url(tracked[:200])  # 最多200个
            _log(f"WS kline: connecting {len(tracked)} symbols")
            
            ws = _ws.WebSocketApp(
                url,
                on_open=_on_kline_open,
                on_message=_on_kline_msg,
                on_error=_on_kline_error,
                on_close=_on_kline_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
            
        except Exception as e:
            _log(f"WS kline loop error: {e}")
        
        time.sleep(min(reconnect_delay, 30))
        reconnect_delay = min(reconnect_delay * 2, 30)
        _log(f"WS kline reconnecting in {reconnect_delay}s...")


# ════════════════════════════════════════════════════════════
#  KlineManager
# ════════════════════════════════════════════════════════════
class KlineManager:
    """K 线数据管理器 — 磁盘缓存 + 增量更新"""

    def __init__(self):
        self.load_cache()

    def load_cache(self):
        global _symbol_klines
        try:
            data = _rget('cache:s3_rolling')
            if data and 'klines' in data:
                _symbol_klines = {k: v for k, v in data['klines'].items()}
                _log(f"Loaded cache: {len(_symbol_klines)} symbols from Redis")
            else:
                _symbol_klines = {}
        except Exception as e:
            _log(f"Cache load failed: {e}")
            _symbol_klines = {}

    def save_cache(self):
        try:
            with _lock:
                trimmed = {}
                for sym, klines in _symbol_klines.items():
                    trimmed[sym] = klines[:_MAX_KLINES]
                data = {'ts': time.time(), 'klines': trimmed}
                _rset('cache:s3_rolling', data)
        except Exception as e:
            _log(f"Cache save failed: {e}")

    def initialize_symbol(self, symbol: str):
        """首次初始化一个币的K线数据"""
        with _lock:
            if symbol in _symbol_klines and len(_symbol_klines[symbol]) >= 480:
                return
        candles = fetch_klines(symbol, '1m', 500)
        if candles:
            with _lock:
                _symbol_klines[symbol] = sorted(candles, key=lambda x: -x['t'])
            _log(f"  initialized {symbol}: {len(candles)} candles")

    def update_symbol(self, symbol: str):
        """
        增量更新 — WS 优先，REST 回退
        WS 运行时：只在新币种或数据太少时走 REST
        """
        global _ws_kline_connected
        
        with _lock:
            existing = _symbol_klines.get(symbol, [])
            latest_ts = existing[0]['t'] if existing else 0
        
        now = int(time.time())
        age = now - latest_ts if latest_ts else 999
        
        # WS 已连接且数据够新 → 跳过 REST
        if _ws_kline_connected and age < 180 and len(existing) >= 60:
            return
        
        # REST 回退
        if age > 180:
            candles = fetch_klines(symbol, '1m', 5)
        else:
            candles = fetch_klines(symbol, '1m', 2)
        
        if not candles:
            return
        
        new_sorted = sorted(candles, key=lambda x: -x['t'])
        
        with _lock:
            old = _symbol_klines.get(symbol, [])
            old_ts = {c['t'] for c in old}
            merged = old[:]
            for c in new_sorted:
                if c['t'] not in old_ts:
                    merged.append(c)
            merged.sort(key=lambda x: -x['t'])
            _symbol_klines[symbol] = merged

    def refresh_symbols(self, symbols: list):
        """批量刷新所有币的K线 — WS 运行时大部分跳过 REST"""
        ws_active = _ws_kline_connected
        rest_count = 0
        for sym in symbols:
            try:
                with _lock:
                    exists = sym in _symbol_klines and len(_symbol_klines[sym]) >= 60
                if not exists:
                    self.initialize_symbol(sym)
                    rest_count += 1
                elif not ws_active:
                    # WS 未连接时走 REST 更新
                    self.update_symbol(sym)
                    rest_count += 1
                # WS 已连接 → 直接跳过 REST（WS 线程负责更新）
            except Exception as e:
                _log(f"  Error {sym}: {e}")
        if rest_count > 0:
            _log(f"  REST refresh: {rest_count} symbols (WS={'active' if ws_active else 'inactive'})")


# ════════════════════════════════════════════════════════════
#  计算 + 检测 + 输出
# ════════════════════════════════════════════════════════════

def _merged_klines(symbol: str) -> list:
    """已收盘 1m K 线 + 进行中的 1m K 线（最新→最旧）。

    把 _current_kline 合并进窗口计算，让 15m/1h/4h/24h 指标反映「现在」
    而不是「上一根已收盘 K 线」。进行中 K 线时间戳比最后一根已收盘新才合并。
    """
    with _lock:
        klines = _symbol_klines.get(symbol, [])
        if not klines:
            return []
        merged = klines[:]
        cur = _current_kline.get(symbol)
        if cur and cur['t'] > merged[0]['t']:
            merged = [cur] + merged
        return merged


def compute_and_detect(symbols: list):
    """
    对所有币计算窗口数据 + 检测事件
    锁策略: copy → release → compute (减少锁持有时间)
    """
    global _symbol_windows, _symbol_windows_raw
    
    now = time.time()
    
    # 1. 在锁内：拷贝数据快照（合并进行中 K 线，指标反映最新价格）
    with _lock:
        klines_snapshot = {}
        for sym in symbols:
            klines = _symbol_klines.get(sym)
            if klines and len(klines) >= 15:
                merged = klines[:]
                cur = _current_kline.get(sym)
                if cur and cur['t'] > merged[0]['t']:
                    merged = [cur] + merged
                klines_snapshot[sym] = merged
    
    if not klines_snapshot:
        return
    
    # 2. 锁外：计算窗口（这个过程可能比较慢）
    all_windows = {}
    all_windows_raw = {}
    all_events = []
    
    for sym, klines in klines_snapshot.items():
        windows = {}
        windows_raw = {}
        for name, minutes in WINDOWS:
            if len(klines) >= minutes:
                window_data = compute_window_data(klines, minutes, sym)
                windows[name] = window_data
                windows_raw[name] = klines[:minutes]
            else:
                n = min(len(klines), minutes)
                window_data = compute_window_data(klines, n, sym)
                windows[name] = window_data
                windows_raw[name] = klines[:]
        
        all_windows[sym] = windows
        all_windows_raw[sym] = windows_raw
        
        # 事件检测
        events = detect_events(sym, windows, windows_raw)
        
        # Event 生命周期管理 + 去重
        for evt in events:
            managed = _update_event_state(evt, now)
            if managed:
                all_events.append(managed)
    
    # 3. 过期事件清理
    ended_events = _end_expired_events(symbols, now)
    all_events.extend(ended_events)
    
    # 4. 锁内：更新共享状态 + 输出
    with _lock:
        _symbol_windows = all_windows
        _symbol_windows_raw = all_windows_raw
    
        # ── 输出：Redis 优先，JSON 做调试快照 ──
        if all_events:
            all_events.sort(key=lambda x: -x['strength'])
            evt_data = {'ts': now, 'events': all_events}
            
            try:
                _rset('event:s3', evt_data)
                _rpublish('s3:event:notify')
            except Exception:
                pass
        else:
            try:
                _rset('event:s3', {'ts': now, 'events': []})
            except Exception:
                pass
        
        # market_data
        market_data = {'ts': now, 'symbols': all_windows}
        try:
            _rset('market:s3_data', market_data)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  WS 大单流（保持原逻辑）
# ════════════════════════════════════════════════════════════

def _futures_to_spot(futures_sym: str) -> str | None:
    if futures_sym.endswith('USDT'):
        base = futures_sym[:-4].lower()
        return f'{base}usdt' if base not in ('btc', 'eth') else base
    return None

def _get_spot_momentum(symbol: str) -> dict:
    """获取现货市场动量（简化版）"""
    try:
        spot_sym = _futures_to_spot(symbol)
        if not spot_sym:
            return {}
        r = requests.get(f'https://api.binance.com/api/v3/ticker/24hr?symbol={spot_sym.upper()}',
                         timeout=5)
        if r.status_code != 200:
            return {}
        d = r.json()
        return {'spot_chg': float(d.get('priceChangePercent', 0)),
                'spot_vol': float(d.get('quoteVolume', 0))}
    except Exception:
        return {}

def _on_trade_msg(ws, message):
    """处理 @trade 消息"""
    try:
        data = json.loads(message)
        if 'data' in data:
            data = data['data']
        qty = float(data.get('q', 0))
        price = float(data.get('p', 0))
        usdt = qty * price
        if usdt >= BIG_ORDER_MIN_USDT:
            sym = data.get('s', '')
            is_buyer = data.get('m', False) is False  # m=False → 主动买
            with _lock:
                _big_orders.append({
                    'ts': time.time(),
                    'symbol': sym.upper() if sym.islower() else sym,
                    'price': price,
                    'qty': qty,
                    'usdt': usdt,
                    'side': 'BUY' if is_buyer else 'SELL',
                })
    except Exception as e:
        pass  # WS 消息解析失败静默处理

def _on_trade_error(ws, error):
    _log(f"WS trade error: {error}")

def ws_big_order_loop():
    """大单流 WS 线程（保持独立）"""
    import websocket

    _big_orders[:] = []
    ws_url = WS_BIGORDER + '/'.join(f'{s}@trade' for s in WS_SYMBOLS)

    def analyze():
        while True:
            time.sleep(60)
            now = time.time()
            with _lock:
                recent = [o for o in _big_orders if now - o['ts'] < 120]
                buy_vol  = sum(o['usdt'] for o in recent if o['side'] == 'BUY')
                sell_vol = sum(o['usdt'] for o in recent if o['side'] == 'SELL')
                if buy_vol > 0 or sell_vol > 0:
                    signals = {'ts': now, 'signals': [{
                        'symbol': 'BTCUSDT',
                        'buy_vol': buy_vol, 'sell_vol': sell_vol,
                        'ratio': round(buy_vol / sell_vol, 2) if sell_vol > 0 else 9.99,
                    }]}
                    try:
                        _rset('signal:s3_signals', signals)
                    except Exception:
                        pass
                # 清理过期订单
                _big_orders[:] = [o for o in _big_orders if now - o['ts'] < 300]

    def spot_snapshot():
        while True:
            time.sleep(60)
            try:
                r = requests.get('https://api.binance.com/api/v3/ticker/24hr', timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                movers = sorted(
                    [t for t in data if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')],
                    key=lambda x: abs(float(x.get('priceChangePercent', 0))),
                    reverse=True
                )[:10]
                spot_data = {'ts': time.time(), 'movers': [{
                    'symbol': t['symbol'],
                    'chg': float(t['priceChangePercent']),
                    'vol': float(t['quoteVolume']),
                } for t in movers if abs(float(t.get('priceChangePercent', 0))) > 5]}
                if spot_data['movers']:
                    _rset('mover:s3_spot', spot_data)
            except Exception:
                pass

    threading.Thread(target=analyze, daemon=True).start()
    threading.Thread(target=spot_snapshot, daemon=True).start()

    while True:
        try:
            ws = websocket.WebSocketApp(ws_url, on_message=_on_trade_msg,
                                        on_error=_on_trade_error)
            ws.run_forever(ping_interval=30)
        except Exception as e:
            _log(f"WS reconnecting: {e}")
        time.sleep(5)


# ════════════════════════════════════════════════════════════
#  大单流缓冲
# ════════════════════════════════════════════════════════════
_big_orders: list = []


# ════════════════════════════════════════════════════════════
#  Heartbeat
# ════════════════════════════════════════════════════════════
_last_heartbeat = 0

def _heartbeat():
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < 60:
        return
    _last_heartbeat = now
    with _lock:
        sym_count = len(_symbol_klines)
    _log(f'[心跳] symbols={sym_count}')


# ════════════════════════════════════════════════════════════
#  Main Loop
# ════════════════════════════════════════════════════════════

def market_brain_loop():
    """Market Brain 主循环"""
    km = KlineManager()
    persist_counter = 0
    symbols_cache = []

    while True:
        try:
            # 1. 获取活跃币种
            symbols = get_top_symbols()
            if not symbols:
                _log("No symbols from API, sleeping")
                time.sleep(FETCH_INTERVAL)
                continue
            symbols_cache = symbols

            # 2. 刷新 K 线数据
            km.refresh_symbols(symbols)

            # 3. 计算窗口 + 检测事件
            compute_and_detect(symbols)

            # 4. 持久化 (每 PERSIST_INTERVAL)
            persist_counter += FETCH_INTERVAL
            if persist_counter >= PERSIST_INTERVAL:
                km.save_cache()
                persist_counter = 0

        except Exception as e:
            _log(f"Market brain loop error: {e}")
            import traceback
            _log(traceback.format_exc())

        _heartbeat()
        time.sleep(FETCH_INTERVAL)


# ════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════

def run():
    _log("=" * 50)
    _log("[s3] MARKET BRAIN v3.5 STARTING")
    _log("[s3] 变更: EMA增量/ATR修正/锁缩小/Event生命周期/Redis优先/kline WS")
    _log("=" * 50)

    # Thread 1: K线-based Market Brain
    t1 = threading.Thread(target=market_brain_loop, daemon=True)
    t1.start()
    _log("  Market Brain thread started")

    # Thread 2: WS 大单流
    t2 = threading.Thread(target=ws_big_order_loop, daemon=True)
    t2.start()
    _log("  Big order WS thread started")

    # Thread 3: WS kline_1m（增量K线，减少REST依赖）
    t3 = threading.Thread(target=ws_kline_loop, daemon=True)
    t3.start()
    _log("  Kline WS thread started")

    _log("[s3] READY")
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        _log("Shutdown")

if __name__ == '__main__':
    run()
