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
    long_trend_takeover_ready,
    get_market_state,
    long_signal_allows_open,
    resolve_event_flow, resolve_event_orderflow_bias,
)
from journal.builder import DecisionJournalBuilder, signal_source_from_event
from journal.recorder import get_default_recorder, safe_record

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

def _open_long(state: dict, evt: dict, market: dict) -> dict:
    """根据 S3 事件开做多仓位"""
    symbol = evt['symbol']
    event_type = evt['type']
    system_tag = SYSTEM_TAG.get(event_type, NAME)

    # ── Decision Journal 旁路（观察者，不参与决策） ──
    jb = DecisionJournalBuilder(
        signal_source=signal_source_from_event(evt),
        signal_type=event_type,
        symbol=symbol,
        side='LONG',
        strength=evt.get('strength'),
        event_id=evt.get('event_id'),
        raw=evt,
        strategy=NAME,
        process=NAME,
        pid=os.getpid(),
    )

    _ms = get_market_state()
    jb.set_regime(regime=_ms.get('regime'), source='S0', timestamp=_ms.get('timestamp'))
    _regime_ok = long_signal_allows_open(event_type, _ms)
    jb.gate('regime', _regime_ok, value=_ms.get('regime'))
    if not _regime_ok:
        _log(NAME, f'{symbol} {event_type} 与当前 S0 空头 regime 冲突，跳过')
        _journal_finish(jb, action='REJECT', reason='regime_conflict')
        return state

    _stale = event_is_stale(evt)
    jb.gate('fresh', not _stale, value=not _stale)
    if _stale:
        _log(NAME, f'{symbol} {event_type} 事件已过期，拒绝追入')
        _journal_finish(jb, action='REJECT', reason='event_stale')
        return state

    # ── 信号冷却检查 ──
    _sig_fresh = is_event_fresh(symbol, event_type, cooldown_s=180)
    jb.gate('signal_cooldown', _sig_fresh, value=_sig_fresh)
    if not _sig_fresh:
        _log(NAME, f'{symbol} {event_type} 冷却中，跳过')
        _journal_finish(jb, action='REJECT', reason='signal_cooldown')
        return state

    # ── 仓位上限 ──
    pos_count = get_position_count(NAME)
    if drawdown_mode() == 'recovery' and pos_count >= 1:
        _replace_ok = maybe_replace_recovery_position(NAME, 'LONG', symbol, evt.get('strength', 50))
        jb.gate('recovery_replace', _replace_ok, value=pos_count)
        if not _replace_ok:
            _log(NAME, f'回撤恢复模式最多持有 1 个仓位，跳过 {symbol}')
            _journal_finish(jb, action='REJECT', reason='recovery_position_limit')
            return state
    _limit_ok = pos_count < MAX_POSITIONS
    jb.gate('position_limit', _limit_ok, value=pos_count, threshold=MAX_POSITIONS)
    if not _limit_ok:
        _log(NAME, f'已达仓位上限 {MAX_POSITIONS}/{MAX_POSITIONS}，跳过 {symbol}')
        _journal_finish(jb, action='REJECT', reason='position_limit')
        return state

    # ── 冷却期检查 ──
    cooldowns = state.get('cooldowns', {})
    _sym_cd = symbol in cooldowns and time.time() < cooldowns[symbol]
    jb.gate('symbol_cooldown', not _sym_cd, value=bool(_sym_cd))
    if _sym_cd:
        _log(NAME, f'{symbol} 在冷却期中，跳过')
        _journal_finish(jb, action='REJECT', reason='symbol_cooldown')
        return state

    # ── 市场状态 ──
    _mkt_ok = market_allows_trading(NAME, 'LONG')
    jb.gate('market_allowed', _mkt_ok, value=_mkt_ok)
    if not _mkt_ok:
        _journal_finish(jb, action='REJECT', reason='market_disallowed')
        return state

    # ── 已有仓位检查 ──
    _has_pos = has_any_position(symbol)
    jb.gate('existing_position', not _has_pos, value=_has_pos)
    if _has_pos:
        _log(NAME, f'{symbol} 已有持仓，跳过')
        _journal_finish(jb, action='REJECT', reason='existing_position')
        return state

    # ── 获取价格 ──
    ticker = fapi_get(f'/fapi/v1/ticker/price?symbol={symbol}')
    _price_ok = bool(ticker and 'price' in ticker)
    jb.gate('price', _price_ok, value=float(ticker['price']) if _price_ok else None)
    if not _price_ok:
        release_event_fresh(symbol, event_type)
        _log(NAME, f'获取价格失败 {symbol}')
        _journal_finish(jb, action='REJECT', reason='price_unavailable')
        return state
    price = float(ticker['price'])

    # ── 获取窗口数据 ──
    win_data = market.get(symbol, {})

    # ── 趋势过滤：多头只在价格 > 1h EMA20 时开仓 ──
    _1h = win_data.get('1h', {})
    _ema20 = _1h.get('ema20', 0)
    _atr_abs = float(_1h.get('atr', 0))
    _flow = resolve_event_flow(evt, win_data)
    _of_bias = resolve_event_orderflow_bias(evt, win_data)
    _short_ratio = get_short_ratio(symbol)
    _rsi15 = float(win_data.get('15m', {}).get('rsi', 50))

    jb.set_market(symbol=symbol, price=price, ema20=_ema20 or None,
                  atr=_atr_abs or None, rsi=_rsi15, taker_buy_ratio=_flow)

    _entry_mode = classify_entry_mode(price, float(_ema20), _rsi15, _flow, 'LONG')
    takeover = event_type == 'PUMP_UP' or (
        event_type == 'VIOLENT_BULLISH'
        and float(win_data.get('4h', {}).get('chg', 0) or 0) > 3
        and float(win_data.get('24h', {}).get('chg', 0) or 0) > 10
    ) or bool(evt.get('breakout_confirmed'))
    if takeover:
        _takeover_ok = long_trend_takeover_ready(price, win_data)
        jb.gate('takeover', _takeover_ok, value=_takeover_ok)
        if not _takeover_ok:
            _log(NAME, f'{symbol} S6B 趋势接管条件未满足，等待回踩/趋势确认')
            _journal_finish(jb, action='REJECT', reason='takeover_not_ready')
            return state
        _entry_mode = 'S6B_TREND_TAKEOVER'
        system_tag = 'S6B'
    elif _entry_mode == 'UNCONFIRMED':
        jb.gate('entry_mode', False, value=_entry_mode, reason='unconfirmed')
        _log(NAME, f'{symbol} 左右侧入场均未确认，跳过')
        _journal_finish(jb, action='REJECT', reason='entry_mode_unconfirmed')
        return state
    jb.gate('entry_mode', True, value=_entry_mode)
    jb.set_entry_mode(_entry_mode)

    _trend_fail = (not takeover and _entry_mode == 'RIGHT_MOMENTUM'
                   and _ema20 > 0 and price < _ema20)
    jb.gate('trend', not _trend_fail, value=price,
            threshold=_ema20 if (_entry_mode == 'RIGHT_MOMENTUM' and _ema20 > 0) else None)
    if _trend_fail:
        _log(NAME, f'{symbol} 价格 {price:.4f} < 1h EMA20 {_ema20:.4f}，不做多')
        _journal_finish(jb, action='REJECT', reason='trend_filter')
        return state

    max_extension = 1.25 if event_type == 'VIOLENT_BULLISH' else 2.0
    _ext_fail = (not takeover and _entry_mode == 'RIGHT_MOMENTUM'
                 and price_is_overextended(price, float(_ema20), _atr_abs, 'LONG', max_extension))
    jb.gate('extension', not _ext_fail, threshold=max_extension)
    if _ext_fail:
        _log(NAME, f'{symbol} 价格距离1h EMA20超过 {max_extension:.2f} ATR，拒绝追多')
        _journal_finish(jb, action='REJECT', reason='overextended')
        return state

    # ── 波动率过滤：1h ATR% > 6% 跳过（波动太大止损易被扫） ──
    _atr_pct = float(_1h.get('atr_pct', 0))
    takeover_atr_max = 12.0 if takeover else MAX_ATR_PCT
    _atr_ok = _atr_pct <= takeover_atr_max
    jb.gate('atr', _atr_ok, value=_atr_pct, threshold=takeover_atr_max)
    if not _atr_ok:
        _log(NAME, f'{symbol} 1h ATR={_atr_pct:.1f}% > {MAX_ATR_PCT:.0f}%，跳过')
        _journal_finish(jb, action='REJECT', reason='atr_exceeded')
        return state

    # ── 计算仓位 ──
    _atr_pct_val = float(_1h.get('atr_pct', 0))
    _extension = ((price - float(_ema20)) / _atr_abs
                  if _ema20 and _atr_abs else 0)
    _event_age = event_age_sec(evt)
    _score = contract_score(evt.get('strength', 50), event_type, _atr_pct_val,
                             _extension, _flow, _event_age, 'LONG', _short_ratio)
    leverage = 2 if takeover else leverage_for_score(event_type, _score, _atr_pct_val)
    margin = MARGIN_MODE.get(event_type, 'CROSSED')
    stop_pct = 0.10 if takeover else STOP_LOSS_PCT.get(event_type, 0.06)

    # ATR 自适应止损（ATR × 2，但受止损上限约束）
    _atr_pct_val = float(_1h.get('atr_pct', 0))
    stop_pct = bounded_stop_pct(stop_pct, _atr_pct_val, 0.12 if takeover else MAX_STOP_LOSS_PCT)

    qty = calc_position_qty(NAME, state, symbol, price, event_type, _score, leverage,
                            atr_pct=_atr_pct_val, stop_pct=stop_pct)
    stop_price = round(price * (1 - stop_pct), 8)

    # ── Risk 输出记录（只记录现有结果，不重算） ──
    jb.set_strategy_score(_score)
    jb.set_risk(position_size=qty, leverage=leverage, stop_pct=stop_pct)

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
                            'taker_buy_ratio_15m': _flow,
                            'orderflow_bias_15m': _of_bias,
                            'global_short_ratio_1h': _short_ratio,
                       })
    _journal_finish(jb, action='OPEN', accepted=bool(ok),
                    reason=None if ok else 'open_position_rejected',
                    final_score=_score)
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
            events = read_all_signals()
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
