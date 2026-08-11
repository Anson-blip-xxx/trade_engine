#!/usr/bin/env python3
"""
S6 v3 — 统一做多执行器 (S3 Market Brain 驱动)
=============================================
消费 s3_events.json 中的 LONG 信号:
  PULSE_UP  → 逐仓做多 (短平快, 紧止损)
  TREND_UP  → 全仓做多 (持仓周期长)

不再自算任何指标，全部从 s3_market_data.json 读取。
不再自扫标的，全部由 S3 事件驱动。

架构: S3 Event → Filter → Position → PM
"""

import sys, time, json, os
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE / 'trading_engine'))

from shared_executor import (
    _log, read_s3_events, read_s3_market_data,
    is_event_fresh, release_event_fresh, load_state, save_state,
    reconcile_positions, pm_monitor,
    open_position, calc_position_qty, fapi_get, tg_send,
    market_allows_trading, get_position_count, has_position,
    _event_expected_move, subscribe_s3_notify, wait_scan,
    maybe_log_analysis_panel, bounded_stop_pct, drawdown_mode,
    maybe_replace_recovery_position, event_is_stale, price_is_overextended,
    contract_score, leverage_for_score, classify_entry_mode, event_age_sec,
)

NAME = 'S6'
SCAN_INTERVAL = 10   # 每 10s 检查一次事件

# ── 开仓参数 ──
POSITION_SIZE_USDT = 20     # 每单固定 $20
STOP_LOSS_PCT = {
    'PULSE_UP': 0.04,       # 逐仓: 4% 紧止损
    'TREND_UP': 0.08,       # 全仓: 8% 宽松止损
    'VIOLENT_BULLISH': 0.08, # 极端波动: 8% 止损上限
    'PUMP_UP': 0.08,        # 泵多: 8% 止损
}
MAX_POSITIONS = 2           # 最多同时持有 2 个做多仓位
MAX_ATR_PCT = 6.0           # 极端波动过滤，避免追入容易被止损扫出的行情
MAX_STOP_LOSS_PCT = 0.08    # 单笔止损距离上限，限制尾部亏损
LEVERAGE = {
    'PULSE_UP': 5,
    'TREND_UP': 3,
    'VIOLENT_BULLISH': 3,
    'PUMP_UP': 2,
}
MARGIN_MODE = {
    'PULSE_UP': 'ISOLATED',
    'TREND_UP': 'CROSSED',
    'VIOLENT_BULLISH': 'CROSSED',
    'PUMP_UP': 'ISOLATED',
}

# ── 系统标签映射 ──
SYSTEM_TAG = {
    'PULSE_UP': 'S6A',
    'TREND_UP': 'S6A',
    'VIOLENT_BULLISH': 'S6A',
    'PUMP_UP': 'S6B',
}

# ── 事件类型映射 ──
LONG_SIGNALS = ('PULSE_UP', 'TREND_UP', 'VIOLENT_BULLISH', 'PUMP_UP')

# ── TG 通知 ──
def _tg(msg: str) -> Optional[int]:
    return tg_send(f'<b>{NAME}</b> {msg}')

def _open_long(state: dict, evt: dict, market: dict) -> dict:
    """根据 S3 事件开做多仓位"""
    symbol = evt['symbol']
    event_type = evt['type']
    system_tag = SYSTEM_TAG.get(event_type, NAME)

    if event_is_stale(evt):
        _log(NAME, f'{symbol} {event_type} 事件已过期，拒绝追入')
        return state

    # ── 信号冷却检查 ──
    if not is_event_fresh(symbol, event_type, cooldown_s=180):
        _log(NAME, f'{symbol} {event_type} 冷却中，跳过')
        return state

    # ── 仓位上限 ──
    pos_count = get_position_count(NAME)
    if drawdown_mode() == 'recovery' and pos_count >= 1:
        if not maybe_replace_recovery_position(NAME, 'LONG', symbol, evt.get('strength', 50)):
            _log(NAME, f'回撤恢复模式最多持有 1 个仓位，跳过 {symbol}')
            return state
    if pos_count >= MAX_POSITIONS:
        _log(NAME, f'已达仓位上限 {MAX_POSITIONS}/{MAX_POSITIONS}，跳过 {symbol}')
        return state

    # ── 冷却期检查 ──
    cooldowns = state.get('cooldowns', {})
    if symbol in cooldowns and time.time() < cooldowns[symbol]:
        _log(NAME, f'{symbol} 在冷却期中，跳过')
        return state

    # ── 市场状态 ──
    if not market_allows_trading(NAME, 'LONG'):
        return state

    # ── 已有仓位检查 ──
    if has_position(NAME, symbol):
        _log(NAME, f'{symbol} 已有持仓，跳过')
        return state

    # ── 获取价格 ──
    ticker = fapi_get(f'/fapi/v1/ticker/price?symbol={symbol}')
    if not ticker or 'price' not in ticker:
        release_event_fresh(symbol, event_type)
        _log(NAME, f'获取价格失败 {symbol}')
        return state
    price = float(ticker['price'])

    # ── 获取窗口数据 ──
    win_data = market.get(symbol, {})

    # ── 趋势过滤：多头只在价格 > 1h EMA20 时开仓 ──
    _1h = win_data.get('1h', {})
    _ema20 = _1h.get('ema20', 0)
    _atr_abs = float(_1h.get('atr', 0))
    _flow = win_data.get('15m', {}).get('taker_buy_ratio')
    _rsi15 = float(win_data.get('15m', {}).get('rsi', 50))
    _entry_mode = classify_entry_mode(price, float(_ema20), _rsi15, _flow, 'LONG')
    if _entry_mode == 'UNCONFIRMED':
        _log(NAME, f'{symbol} 左右侧入场均未确认，跳过')
        return state
    if _entry_mode == 'RIGHT_MOMENTUM' and _ema20 > 0 and price < _ema20:
        _log(NAME, f'{symbol} 价格 {price:.4f} < 1h EMA20 {_ema20:.4f}，不做多')
        return state

    max_extension = 1.25 if event_type == 'VIOLENT_BULLISH' else 2.0
    if _entry_mode == 'RIGHT_MOMENTUM' and price_is_overextended(price, float(_ema20), _atr_abs, 'LONG', max_extension):
        _log(NAME, f'{symbol} 价格距离1h EMA20超过 {max_extension:.2f} ATR，拒绝追多')
        return state

    # ── 波动率过滤：1h ATR% > 6% 跳过（波动太大止损易被扫） ──
    _atr_pct = float(_1h.get('atr_pct', 0))
    if _atr_pct > MAX_ATR_PCT:
        _log(NAME, f'{symbol} 1h ATR={_atr_pct:.1f}% > {MAX_ATR_PCT:.0f}%，跳过')
        return state

    # ── 计算仓位 ──
    _atr_pct_val = float(_1h.get('atr_pct', 0))
    _extension = ((price - float(_ema20)) / _atr_abs
                  if _ema20 and _atr_abs else 0)
    _event_age = event_age_sec(evt)
    _score = contract_score(evt.get('strength', 50), event_type, _atr_pct_val,
                             _extension, _flow, _event_age, 'LONG')
    leverage = leverage_for_score(event_type, _score, _atr_pct_val)
    margin = MARGIN_MODE.get(event_type, 'CROSSED')
    stop_pct = STOP_LOSS_PCT.get(event_type, 0.06)

    # ATR 自适应止损（ATR × 2，但受止损上限约束）
    _atr_pct_val = float(_1h.get('atr_pct', 0))
    stop_pct = bounded_stop_pct(stop_pct, _atr_pct_val, MAX_STOP_LOSS_PCT)

    qty = calc_position_qty(NAME, state, symbol, price, event_type, _score, leverage,
                            atr_pct=_atr_pct_val, stop_pct=stop_pct)
    stop_price = round(price * (1 - stop_pct), 8)

    # ── 开仓 ──
    ok = open_position(system_tag, symbol, 'LONG', price, stop_price,
                       qty, margin, leverage, event_type, _score,
                       tg_fn=_tg, expected_move_pct=_event_expected_move(evt),
                       decision_context={
                           'signal_type': event_type,
                           'strength': _score,
                           'raw_strength': evt.get('strength', 50),
                           'entry_mode': _entry_mode,
                           'price': price,
                           'ema20_1h': _ema20,
                           'atr_pct_1h': _atr_pct_val,
                           'taker_buy_ratio_15m': win_data.get('15m', {}).get('taker_buy_ratio'),
                           'orderflow_bias_15m': win_data.get('15m', {}).get('orderflow_bias'),
                       })
    if ok:
        _log(NAME, f'✅ 开多 {symbol} {margin} {event_type} str={evt.get("strength")}')
    return state

def main():
    _log(NAME, 'S6 v3 启动 (S3 Market Brain 驱动 | 做多执行器)')
    _log(NAME, 'S6A: PULSE_UP(逐仓) TREND_UP(全仓) VIOLENT_BULLISH(全仓)')
    _log(NAME, 'S6B: PUMP_UP(逐仓)')

    state = load_state(NAME)
    state = reconcile_positions(NAME, state)

    _ps = subscribe_s3_notify()

    while True:
        try:
            # 1. 加载 S3 数据
            events = read_s3_events()
            market = read_s3_market_data()

            # 2. PM 监控（持仓全由 PM 的 pm:positions 管理）
            state, closed = pm_monitor(NAME, state, tg_fn=_tg)

            # 3. 处理做多事件
            for evt in events:
                if evt.get('type') in LONG_SIGNALS:
                    state = _open_long(state, evt, market)

            # 4. 心跳（从 PM 查询持仓数）
            pos_count = get_position_count(NAME)
            if pos_count > 0:
                _log(NAME, f'[心跳] 当前持仓: {pos_count}')
            maybe_log_analysis_panel(NAME, interval_sec=300, window_sec=3600)

        except Exception as e:
            _log(NAME, f'主循环异常: {e}')

        # s3 事件通知唤醒（即时响应），无通知则 10s 轮询兜底
        wait_scan(_ps, SCAN_INTERVAL)

if __name__ == '__main__':
    main()
