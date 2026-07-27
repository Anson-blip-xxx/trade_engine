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
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE / 'trading_engine'))

from shared_executor import (
    _log, read_s3_events, read_s3_market_data,
    is_event_fresh, load_state, save_state,
    reconcile_positions, pm_monitor,
    open_position, calc_position_qty, fapi_get, tg_send,
    market_allows_trading,
)

NAME = 'S8'
SCAN_INTERVAL = 10

# ── 开仓参数 ──
POSITION_SIZE_USDT = 20
STOP_LOSS_PCT = {
    'PULSE_DOWN':  0.04,    # 逐仓: 4% 紧止损
    'PANIC_SELL':  0.035,   # 逐仓: 3.5% 更紧止损
    'TREND_DOWN':  0.08,    # 全仓: 8% 宽松止损
    'VIOLENT_BEARISH': 0.10, # 极端波动: 10% 宽止损
}
MAX_POSITIONS = 2
LEVERAGE = {
    'PULSE_DOWN':  5,
    'PANIC_SELL':  5,
    'TREND_DOWN':  3,
    'VIOLENT_BEARISH': 3,
}
MARGIN_MODE = {
    'PULSE_DOWN':  'ISOLATED',
    'PANIC_SELL':  'ISOLATED',
    'TREND_DOWN':  'CROSSED',
    'VIOLENT_BEARISH': 'CROSSED',
}

SHORT_SIGNALS = ('PULSE_DOWN', 'TREND_DOWN', 'PANIC_SELL', 'VIOLENT_BEARISH')

def _tg(msg: str) -> Optional[int]:
    return tg_send(f'<b>{NAME}</b> {msg}')

def _open_short(state: dict, evt: dict, market: dict) -> dict:
    symbol = evt['symbol']
    event_type = evt['type']

    # 冷却检查
    if not is_event_fresh(symbol, event_type, cooldown_s=180):
        _log(NAME, f'{symbol} {event_type} 冷却中，跳过')
        return state

    # 仓位上限
    pos_count = len(state.get('positions', {}))
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
    if symbol in state.get('positions', {}):
        _log(NAME, f'{symbol} 已有持仓')
        return state

    # 价格
    ticker = fapi_get(f'/fapi/v1/ticker/price?symbol={symbol}')
    if not ticker or 'price' not in ticker:
        _log(NAME, f'获取价格失败 {symbol}')
        return state
    price = float(ticker['price'])

    # 获取市场数据（做空趋势过滤）
    market = read_s3_market_data()
    win_data = market.get(symbol, {})

    # 趋势过滤：做空只在价格 < 1h EMA20 时开仓
    _1h = win_data.get('1h', {})
    _ema20 = _1h.get('ema20', 0)
    if _ema20 > 0 and price > _ema20:
        _log(NAME, f'{symbol} 价格 {price:.4f} > 1h EMA20 {_ema20:.4f}，不做空')
        return state

    # 波动率过滤：1h ATR% > 8% 跳过
    _atr_pct = float(_1h.get('atr_pct', 0))
    if _atr_pct > 8:
        _log(NAME, f'{symbol} 1h ATR={_atr_pct:.1f}% > 8%，跳过')
        return state

    # 参数
    leverage = LEVERAGE.get(event_type, 3)
    margin = MARGIN_MODE.get(event_type, 'CROSSED')
    stop_pct = STOP_LOSS_PCT.get(event_type, 0.05)

    # ATR 自适应止损（ATR × 2，不低于固定止损）
    if _atr_pct > 0:
        stop_pct = max(stop_pct, _atr_pct * 2 / 100)

    qty = calc_position_qty(NAME, state, symbol, price, event_type, evt.get('strength', 50), leverage)
    stop_price = round(price * (1 + stop_pct), 8)

    # 开仓
    ok = open_position(NAME, symbol, 'SHORT', price, stop_price,
                       qty, margin, leverage, event_type, evt.get('strength', 50),
                       tg_fn=_tg)
    if ok:
        state.setdefault('positions', {})[symbol] = {
            'entry': price, 'stop': stop_price, 'side': 'SHORT',
            'qty': qty, 'margin': margin,
            'event_type': event_type, 'strength': evt.get('strength', 50),
            'ts': time.time(), 'open_time': time.time(),
        }
        save_state(NAME, state)
        _log(NAME, f'✅ 开空 {symbol} {margin} {event_type} str={evt.get("strength")}')
    return state

def main():
    _log(NAME, 'S8 v3 启动 (S3 Market Brain 驱动 | 做空执行器)')
    _log(NAME, '消费事件: PULSE_DOWN(逐仓) TREND_DOWN(全仓) PANIC_SELL(逐仓)')

    state = load_state(NAME)
    state = reconcile_positions(NAME, state)

    while True:
        try:
            events = read_s3_events()
            market = read_s3_market_data()

            state, closed = pm_monitor(NAME, state, tg_fn=_tg)
            # 平仓后设置冷却期（在策略主循环内写入，防止 PM 与策略并发写覆盖）
            now = time.time()
            for sym, reason, pnl_pct in closed:
                cd = 14400 if pnl_pct < 0 else 7200
                state.setdefault('cooldowns', {})[sym] = now + cd
            if closed:
                save_state(NAME, state)

            for evt in events:
                if evt.get('type') in SHORT_SIGNALS:
                    state = _open_short(state, evt, market)

            pos_count = len(state.get('positions', {}))
            if pos_count > 0:
                _log(NAME, f'[心跳] 当前持仓: {pos_count}')

        except Exception as e:
            _log(NAME, f'主循环异常: {e}')

        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()
