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

import sys, time, os
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE / 'trading_engine'))

from shared_executor import (
    _log, read_all_signals, read_s3_market_data,
    is_event_fresh, release_event_fresh, load_state, save_state,
    reconcile_positions, pm_monitor,
    open_position, calc_position_qty, fapi_get, tg_send, has_any_position,
    market_allows_trading, get_position_count, has_position,
    _event_expected_move, subscribe_s3_notify, wait_scan,
    maybe_log_analysis_panel, bounded_stop_pct, drawdown_mode,
    maybe_replace_recovery_position, event_is_stale, price_is_overextended,
    contract_score, leverage_for_score, classify_entry_mode, event_age_sec,
    get_short_ratio,
    pump_down_uptrend_guard,
    short_signal_allows_open,
    resolve_event_flow, resolve_event_orderflow_bias,
    MIN_CONFIRMED_SHORT_STRENGTH,
)
from journal.builder import DecisionJournalBuilder, signal_source_from_event
from signals.adapters import s8_signal, to_journal_builder_kwargs
from journal.recorder import get_default_recorder, safe_record

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

def _journal_finish(jb, *, action: str, accepted: bool = False,
                    reason: str | None = None,
                    final_score: Optional[float] = None) -> None:
    """旁路记录 Decision Journal；任何异常 fail-open，绝不影响交易决策。"""
    try:
        safe_record(get_default_recorder(), jb, action=action, accepted=accepted,
                    reason=reason, final_score=final_score,
                    on_error=lambda msg: _log(NAME, msg))
    except Exception:
        pass  # 双保险：safe_record 内部已吞异常

def _journal_kwargs(evt: dict, symbol: str, event_type: str) -> dict:
    """Journal kwargs：Unified Signal 优先；Signal 异常 → fail-open 回退直连 evt。"""
    try:
        return to_journal_builder_kwargs(s8_signal(evt))
    except Exception as exc:
        _log(NAME, f'Decision Signal 构造失败(fail-open 回退直连): '
                   f'{type(exc).__name__}: {exc}')
        return {
            'signal_source': signal_source_from_event(evt),
            'signal_type': event_type,
            'symbol': symbol,
            'side': 'SHORT',
            'strength': evt.get('strength'),
            'event_id': evt.get('event_id'),
            'raw': evt,
            'strategy': NAME,
        }


def _open_short(state: dict, evt: dict, market: dict) -> dict:
    symbol = evt['symbol']
    event_type = evt['type']
    raw_strength = float(evt.get('strength', 0) or 0)

    # ── Unified Signal + Decision Journal 旁路（观察者；Signal 失败 fail-open 回退） ──
    jb = DecisionJournalBuilder(**_journal_kwargs(evt, symbol, event_type),
                                process=NAME, pid=os.getpid())

    _strength_ok = short_signal_allows_open(event_type, raw_strength)
    jb.gate('strength', _strength_ok, value=raw_strength,
            threshold=MIN_CONFIRMED_SHORT_STRENGTH)
    if not _strength_ok:
        _log(NAME, f'{symbol} {event_type} strength={raw_strength:.0f} < '
                   '60，短空确认不足，跳过')
        _journal_finish(jb, action='REJECT', reason='strength_below_minimum')
        return state

    _stale = event_is_stale(evt)
    jb.gate('fresh', not _stale, value=not _stale)
    if _stale:
        _log(NAME, f'{symbol} {event_type} 事件已过期，拒绝追入')
        _journal_finish(jb, action='REJECT', reason='event_stale')
        return state

    # 冷却检查
    _sig_fresh = is_event_fresh(symbol, event_type, cooldown_s=180)
    jb.gate('signal_cooldown', _sig_fresh, value=_sig_fresh)
    if not _sig_fresh:
        _log(NAME, f'{symbol} {event_type} 冷却中，跳过')
        _journal_finish(jb, action='REJECT', reason='signal_cooldown')
        return state

    # 仓位上限
    pos_count = get_position_count(NAME)
    if drawdown_mode() == 'recovery' and pos_count >= 1:
        _replace_ok = maybe_replace_recovery_position(NAME, 'SHORT', symbol, evt.get('strength', 50))
        jb.gate('recovery_replace', _replace_ok, value=pos_count)
        if not _replace_ok:
            _log(NAME, '回撤恢复模式最多持有 1 个仓位，跳过')
            _journal_finish(jb, action='REJECT', reason='recovery_position_limit')
            return state
    _limit_ok = pos_count < MAX_POSITIONS
    jb.gate('position_limit', _limit_ok, value=pos_count, threshold=MAX_POSITIONS)
    if not _limit_ok:
        _log(NAME, f'已达仓位上限 {MAX_POSITIONS}/{MAX_POSITIONS}')
        _journal_finish(jb, action='REJECT', reason='position_limit')
        return state

    # 冷却期
    cooldowns = state.get('cooldowns', {})
    _sym_cd = symbol in cooldowns and time.time() < cooldowns[symbol]
    jb.gate('symbol_cooldown', not _sym_cd, value=bool(_sym_cd))
    if _sym_cd:
        _log(NAME, f'{symbol} 冷却中')
        _journal_finish(jb, action='REJECT', reason='symbol_cooldown')
        return state

    # 市场状态
    _mkt_ok = market_allows_trading(NAME, 'SHORT')
    jb.gate('market_allowed', _mkt_ok, value=_mkt_ok)
    if not _mkt_ok:
        _journal_finish(jb, action='REJECT', reason='market_disallowed')
        return state

    # 已有仓位
    _has_pos = has_any_position(symbol)
    jb.gate('existing_position', not _has_pos, value=_has_pos)
    if _has_pos:
        _log(NAME, f'{symbol} 已有持仓')
        _journal_finish(jb, action='REJECT', reason='existing_position')
        return state

    # 价格
    ticker = fapi_get(f'/fapi/v1/ticker/price?symbol={symbol}')
    _price_ok = bool(ticker and 'price' in ticker)
    jb.gate('price', _price_ok, value=float(ticker['price']) if _price_ok else None)
    if not _price_ok:
        release_event_fresh(symbol, event_type)
        _log(NAME, f'获取价格失败 {symbol}')
        _journal_finish(jb, action='REJECT', reason='price_unavailable')
        return state
    price = float(ticker['price'])

    # 获取市场数据（做空趋势过滤）
    market = read_s3_market_data()
    win_data = market.get(symbol, {})

    if event_type == 'PUMP_DOWN':
        _pump_ok = not pump_down_uptrend_guard(
            price, win_data.get('4h', {}), win_data.get('24h', {}))
        jb.gate('pump_guard', _pump_ok, value=_pump_ok)
        if not _pump_ok:
            _log(NAME, f'{symbol} PUMP_DOWN 但高周期仍上行，拒绝逆势追空')
            _journal_finish(jb, action='REJECT', reason='pump_down_uptrend')
            return state

    # 趋势过滤：做空只在价格 < 1h EMA20 时开仓
    _1h = win_data.get('1h', {})
    _ema20 = _1h.get('ema20', 0)
    _atr_abs = float(_1h.get('atr', 0))
    _flow = resolve_event_flow(evt, win_data)
    _of_bias = resolve_event_orderflow_bias(evt, win_data)
    _short_ratio = get_short_ratio(symbol)
    _rsi15 = float(win_data.get('15m', {}).get('rsi', 50))

    jb.set_market(symbol=symbol, price=price, ema20=_ema20 or None,
                  atr=_atr_abs or None, rsi=_rsi15, taker_buy_ratio=_flow)

    _entry_mode = classify_entry_mode(price, float(_ema20), _rsi15, _flow, 'SHORT')
    if _entry_mode == 'UNCONFIRMED':
        jb.gate('entry_mode', False, value=_entry_mode, reason='unconfirmed')
        _log(NAME, f'{symbol} 左右侧入场均未确认，跳过')
        _journal_finish(jb, action='REJECT', reason='entry_mode_unconfirmed')
        return state
    jb.gate('entry_mode', True, value=_entry_mode)
    jb.set_entry_mode(_entry_mode)
    _trend_fail = _entry_mode == 'RIGHT_MOMENTUM' and _ema20 > 0 and price > _ema20
    jb.gate('trend', not _trend_fail, value=price,
            threshold=_ema20 if (_entry_mode == 'RIGHT_MOMENTUM' and _ema20 > 0) else None)
    if _trend_fail:
        _log(NAME, f'{symbol} 价格 {price:.4f} > 1h EMA20 {_ema20:.4f}，不做空')
        _journal_finish(jb, action='REJECT', reason='trend_filter')
        return state

    max_extension = 1.25 if event_type == 'VIOLENT_BEARISH' else 2.0
    if _entry_mode == 'RIGHT_MOMENTUM':
        _ext_fail = price_is_overextended(price, float(_ema20), _atr_abs, 'SHORT', max_extension)
        jb.gate('extension', not _ext_fail, threshold=max_extension)
        if _ext_fail:
            _log(NAME, f'{symbol} 价格距离1h EMA20超过 {max_extension:.2f} ATR，拒绝追空')
            _journal_finish(jb, action='REJECT', reason='overextended')
            return state

    # 波动率过滤：1h ATR% > 6% 跳过
    _atr_pct = float(_1h.get('atr_pct', 0))
    _atr_ok = _atr_pct <= MAX_ATR_PCT
    jb.gate('atr', _atr_ok, value=_atr_pct, threshold=MAX_ATR_PCT)
    if not _atr_ok:
        _log(NAME, f'{symbol} 1h ATR={_atr_pct:.1f}% > {MAX_ATR_PCT:.0f}%，跳过')
        _journal_finish(jb, action='REJECT', reason='atr_exceeded')
        return state

    # 参数
    _atr_pct_val = _atr_pct
    _extension = ((float(_ema20) - price) / _atr_abs
                  if _ema20 and _atr_abs else 0)
    _event_age = event_age_sec(evt)
    _score = contract_score(evt.get('strength', 50), event_type, _atr_pct_val,
                             _extension, _flow, _event_age, 'SHORT', _short_ratio)
    leverage = leverage_for_score(event_type, _score, _atr_pct_val)
    margin = MARGIN_MODE.get(event_type, 'CROSSED')
    stop_pct = STOP_LOSS_PCT.get(event_type, 0.05)

    # ATR 自适应止损（ATR × 2，不低于固定止损）
    stop_pct = bounded_stop_pct(stop_pct, _atr_pct_val, MAX_STOP_LOSS_PCT)

    qty = calc_position_qty(NAME, state, symbol, price, event_type, _score, leverage,
                             atr_pct=_atr_pct_val, stop_pct=stop_pct)
    stop_price = round(price * (1 + stop_pct), 8)

    # ── Risk 输出记录（只记录现有结果，不重算） ──
    jb.set_strategy_score(_score)
    jb.set_risk(position_size=qty, leverage=leverage, stop_pct=stop_pct)

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
                           'taker_buy_ratio_15m': _flow,
                            'orderflow_bias_15m': _of_bias,
                           'global_short_ratio_1h': _short_ratio,
                       })
    _journal_finish(jb, action='OPEN', accepted=bool(ok),
                    reason=None if ok else 'open_position_rejected',
                    final_score=_score)
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
            events = read_all_signals()
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
