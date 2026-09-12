"""S3 Event Detector — 事件判断主体（P5-04 提取）。

把 "为什么会产生这个事件" 的候选判断从 God Module 拿出来：
feature windows → 候选事件（level dict，与 legacy 输出逐字同构）。

逐字冻结：
- THRESHOLDS 常量（事件阈值字典 **唯一原型**）
- if 链/append 顺序（PULSE_UP → … → ATR_EXPAND → FAILED_BREAKOUT）
- 比较算符（>= <= > <）
- strength 公式（floor/min/max/int()/abs）
- 超买/超卖 guard（4h EMA20 ± 3×ATR%）与 strong_breakout（close_pos 语义，
  S3-3：真实 producer 不写 → 恒 False）
- VIOLENT 方向 = 类型后缀；FAILED_BREAKOUT 携带 direction 字段

状态/时间依赖：FAILED_BREAKOUT 不在本模块实现（经 breakout_runner 注入
调用 legacy `_detect_failed_breakout`，state_store/time_fn 由 legacy 侧
提供——P5-03 seam）。`log_fn` 注入（默认无日志，legacy 传 s3._log 保持
skip 提示行）。

禁止 redis/requests/websocket/shared/execution/risk/time/IO；
无全局可变状态（THRESHOLDS 为只读使用）。
"""
from __future__ import annotations

from typing import Callable, Optional

# ── 事件阈值（唯一原型，值/键逐字自 strategies/s3_orderflow.py 迁移） ────
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


def _noop(*args, **kwargs):
    return None


def detect_candidate_events(symbol: str, windows: dict, windows_raw: dict,
                            log_fn: Optional[Callable] = None,
                            breakout_runner: Optional[Callable] = None) -> list:
    """特征窗口 → 候选事件（detect_events 主体逐字镜像，除 FAILED_BREAKOUT
    经 breakout_runner 注入）。

    - windows: {'15m': {...}, '1h': {...}, '4h': {...}, '24h': {...}}
    - windows_raw: {'15m': [kline...], '4h': [kline...]}（最新→最旧）
    - log_fn: skip 提示（legacy 传 s3._log 以保持日志门行为）
    - breakout_runner: 注入 FAILED_BREAKOUT 状态机调用
      （签名 (symbol, raw_4h, raw_15m, events)），事件 append 位置不变
    """
    _log_fn = log_fn if log_fn is not None else _noop
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

    def _strong_breakout(side: str) -> bool:
        """Distinguish a supported breakout from a thin late spike."""
        close_pos = float(w15m.get('close_pos', 50) or 50)
        flow = w15m.get('taker_buy_ratio')
        flow_ok = flow is None or (float(flow) >= 0.55 if side == 'LONG' else float(flow) <= 0.45)
        volume_ok = float(w15m.get('vol_ratio', 0) or 0) >= 1.5
        trend_change = float(w1h.get('chg', 0) or 0)
        trend4h = float(w4h.get('chg', 0) or 0)
        if side == 'LONG':
            return close_pos >= 65 and flow_ok and volume_ok and trend_change > 0 and trend4h > 0
        return close_pos <= 35 and flow_ok and volume_ok and trend_change < 0 and trend4h < 0

    # ── PULSE_UP ──
    if w15m.get('chg', 0) >= THRESHOLDS['pulse_up']['15m'] or \
       w1h.get('chg', 0) >= THRESHOLDS['pulse_up']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pulse_up']['vol_ratio']:
            breakout = _strong_breakout('LONG')
            if not _is_overbought() or breakout:
                strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
                event = {
                    'type': 'PULSE_UP', 'symbol': symbol,
                    'strength': max(20, strength),
                    'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                }
                if breakout:
                    event['breakout_confirmed'] = True
                events.append(event)
            else:
                _log_fn(f'[S3] {symbol} PULSE_UP 跳过：已超买')

    # ── PULSE_DOWN ──
    if w15m.get('chg', 0) <= THRESHOLDS['pulse_down']['15m'] or \
       w1h.get('chg', 0) <= THRESHOLDS['pulse_down']['1h']:
        if w15m.get('vol_ratio', 0) >= THRESHOLDS['pulse_down']['vol_ratio']:
            breakout = _strong_breakout('SHORT')
            if not _is_oversold() or breakout:
                strength = min(99, int(abs(w15m.get('chg', 0)) * 8 + abs(w1h.get('chg', 0)) * 4))
                event = {
                    'type': 'PULSE_DOWN', 'symbol': symbol,
                    'strength': max(20, strength),
                    'chg_15m': w15m.get('chg'), 'chg_1h': w1h.get('chg'),
                }
                if breakout:
                    event['breakout_confirmed'] = True
                events.append(event)
            else:
                _log_fn(f'[S3] {symbol} PULSE_DOWN 跳过：已超卖')

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
            _log_fn(f'[S3] {symbol} PANIC_SELL 跳过：已超卖')

    # ── VIOLENT_MOVE（极端波动检测） ──
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
            _log_fn(f'[S3] {symbol} VIOLENT_{_dir} 波动 {_1h_vol:.0f}%/4h={_4h_vol:.0f}%')

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
                    (not _is_overbought() or _strong_breakout('LONG'))
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
                    (not _is_oversold() or _strong_breakout('SHORT'))
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
    raw_15m = windows_raw.get('15m', [])
    raw_4h  = windows_raw.get('4h', [])
    if breakout_runner is not None and len(raw_4h) >= 2 and len(raw_15m) >= 3:
        breakout_runner(symbol, raw_4h, raw_15m, events)

    return events
