#!/usr/bin/env python3
"""
S8 v3 — 统一做空执行器 (S3 Market Brain 驱动)
=============================================
消费 s3_events.json 中的 SHORT 信号:
  PULSE_DOWN    → 逐仓做空 (爆发力强, 紧止损)
  TREND_DOWN    → 全仓做空 (趋势性回落)
  PANIC_SELL    → 逐仓做空 (恐慌抛售, 紧止损)

不再自算任何指标，全部从 s3_market_data.json 读取。
不再自扫标的，全部由 S3 事件驱动。

架构: S3 Event → Filter → Position → PM
"""

import sys, time
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE / 'trading_engine'))

from shared_executor import (
    _log, read_s3_events, read_s3_market_data,
    is_event_fresh, release_event_fresh, load_state, save_state,
    reconcile_positions, pm_monitor,
    open_position, calc_position_qty, fapi_get, tg_send, has_any_position,
    market_allows_trading, get_position_count, has_position,
    _event_expected_move, subscribe_s3_notify, wait_scan,
    maybe_log_analysis_panel, bounded_stop_pct, drawdown_mode,
    maybe_replace_recovery_position, event_is_stale, price_is_overextended,
    contract_score, leverage_for_score, classify_entry_mode, event_age_sec,
)

NAME = 'S8'
SCAN_INTERVAL = 10

# ── 开仓参数 ──
POSITION_SIZE_USDT = 20
STOP_LOSS_PCT = {
    'PULSE_DOWN':  0.04,    # 逐仓: 4% 紧止损
    'PANIC_SELL':  0.035,   # 逐仓: 3.5% 更紧止损
    'TREND_DOWN':  0.08,    # 全仓: 8% 宽松止损
    'VIOLENT_BEARISH': 0.08, # 极端波动: 8% 止损上限
    'PUMP_DOWN':  0.08,     # 泵空: 8% 止损
}
MAX_POSITIONS = 2
MAX_ATR_PCT = 6.0
MAX_STOP_LOSS_PCT = 0.08
LEVERAGE = {
    'PULSE_DOWN':  5,
    'PANIC_SELL':  5,
    'TREND_DOWN':  3,
    'VIOLENT_BEARISH': 3,
    'PUMP_DOWN':  2,
}
MARGIN_MODE = {
    'PULSE_DOWN':  'ISOLATED',
    'PANIC_SELL':  'ISOLATED',
    'TREND_DOWN':  'CROSSED',
    'VIOLENT_BEARISH': 'CROSSED',
    'PUMP_DOWN':  'ISOLATED',
}

SHORT_SIGNALS = ('PULSE_DOWN', 'TREND_DOWN', 'PANIC_SELL', 'VIOLENT_BEARISH', 'PUMP_DOWN')

def _tg(msg: str) -> Optional[int]:
    return tg_send(f'<b>{NAME}</b> {msg}')

def _open_short(state: dict, evt: dict, market: dict) -> dict:
    symbol = evt['symbol']
    event_type = evt['type']

    if event_is_stale(evt):
        _log(NAME, f'{symbol} {event_type} 事件已过期，拒绝追入')
        return state

    # 冷却检查
    if not is_event_fresh(symbol, event_type, cooldown_s=180):
        _log(NAME, f'{symbol} {event_type} 冷却中，跳过')
        return state

    # 仓位上限
    pos_count = get_position_count(NAME)
    if drawdown_mode() == 'recovery' and pos_count >= 1:
        if not maybe_replace_recovery_position(NAME, 'SHORT', symbol, evt.get('strength', 50)):
            _log(NAME, '回撤恢复模式最多持有 1 个仓位，跳过')
            return state
    if pos_count >= MAX_POSITIONS:
        _log(NAME, f'已达仓位上限 {MAX_POSITIONS}/{MAX_POSITIONS}')
        return state

    # 冷却期
    cooldowns = state.get('cooldowns', {})
    if symbol in cooldowns and time.time() < cooldowns[symbol]:
        _log(NAME, f'{symbol} 冷却中')
        return state

    # 市场状态
    if not market_allows_trading(NAME, 'SHORT'):
        return state

    # 已有仓位
    if has_any_position(symbol):
        _log(NAME, f'{symbol} 已有持仓')
        return state

    # 价格
    ticker = fapi_get(f'/fapi/v1/ticker/price?symbol={symbol}')
    if not ticker or 'price' not in ticker:
        release_event_fresh(symbol, event_type)
        _log(NAME, f'获取价格失败 {symbol}')
        return state
    price = float(ticker['price'])

    # 获取市场数据（做空趋势过滤）
    market = read_s3_market_data()
    win_data = market.get(symbol, {})

    # 趋势过滤：做空只在价格 < 1h EMA20 时开仓
    _1h = win_data.get('1h', {})
    _ema20 = _1h.get('ema20', 0)
    _atr_abs = float(_1h.get('atr', 0))
    _flow = win_data.get('15m', {}).get('taker_buy_ratio')
    _rsi15 = float(win_data.get('15m', {}).get('rsi', 50))
    _entry_mode = classify_entry_mode(price, float(_ema20), _rsi15, _flow, 'SHORT')
    if _entry_mode == 'UNCONFIRMED':
        _log(NAME, f'{symbol} 左右侧入场均未确认，跳过')
        return state
    if _entry_mode == 'RIGHT_MOMENTUM' and _ema20 > 0 and price > _ema20:
        _log(NAME, f'{symbol} 价格 {price:.4f} > 1h EMA20 {_ema20:.4f}，不做空')
        return state

    max_extension = 1.25 if event_type == 'VIOLENT_BEARISH' else 2.0
    if _entry_mode == 'RIGHT_MOMENTUM' and price_is_overextended(price, float(_ema20), _atr_abs, 'SHORT', max_extension):
        _log(NAME, f'{symbol} 价格距离1h EMA20超过 {max_extension:.2f} ATR，拒绝追空')
        return state

    # 波动率过滤：1h ATR% > 6% 跳过
    _atr_pct = float(_1h.get('atr_pct', 0))
    if _atr_pct > MAX_ATR_PCT:
        _log(NAME, f'{symbol} 1h ATR={_atr_pct:.1f}% > {MAX_ATR_PCT:.0f}%，跳过')
        return state

    # 参数
    _atr_pct_val = _atr_pct
    _extension = ((float(_ema20) - price) / _atr_abs
                  if _ema20 and _atr_abs else 0)
    _event_age = event_age_sec(evt)
    _score = contract_score(evt.get('strength', 50), event_type, _atr_pct_val,
                             _extension, _flow, _event_age, 'SHORT')
    leverage = leverage_for_score(event_type, _score, _atr_pct_val)
    margin = MARGIN_MODE.get(event_type, 'CROSSED')
    stop_pct = STOP_LOSS_PCT.get(event_type, 0.05)

    # ATR 自适应止损（ATR × 2，不低于固定止损）
    stop_pct = bounded_stop_pct(stop_pct, _atr_pct_val, MAX_STOP_LOSS_PCT)

    qty = calc_position_qty(NAME, state, symbol, price, event_type, _score, leverage,
                             atr_pct=_atr_pct_val, stop_pct=stop_pct)
    stop_price = round(price * (1 + stop_pct), 8)

    # 开仓
    ok = open_position(NAME, symbol, 'SHORT', price, stop_price,
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
        _log(NAME, f'✅ 开空 {symbol} {margin} {event_type} str={evt.get("strength")}')
    return state

def main():
    _log(NAME, 'S8 v3 启动 (S3 Market Brain 驱动 | 做空执行器)')
    _log(NAME, '消费事件: PULSE_DOWN(逐仓) TREND_DOWN(全仓) PANIC_SELL(逐仓) PUMP_DOWN(逐仓)')

    state = load_state(NAME)
    state = reconcile_positions(NAME, state)

    _ps = subscribe_s3_notify()

    while True:
        try:
            events = read_s3_events()
            market = read_s3_market_data()

            state, closed = pm_monitor(NAME, state, tg_fn=_tg)

            for evt in events:
                if evt.get('type') in SHORT_SIGNALS:
                    state = _open_short(state, evt, market)

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
