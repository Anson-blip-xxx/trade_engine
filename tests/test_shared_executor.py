"""shared_executor 风控回归测试：1% 风险硬约束 / 回撤熔断 / R:R 过滤。"""
import time

import pytest


def test_position_qty_risk_cap(patch_executor):
    """止损亏损 = 名义×stop_pct ≤ 账户 1%：高仓位被砍到风险上限。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)  # 账户 4000 USDT

    # 无风险约束时：池=4000×0.8=3200，已用=0，分配=15%（score=100），ATR 不衰减
    qty_free = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=2, stop_pct=0)
    # 池 3200 × 15% = 480 USDT → qty = 480/1.0×3 = 1440
    assert qty_free == pytest.approx(1440.0)

    # 加风险约束 stop=4%：保证金上限 = 4000×0.01/(3×0.04) = 333.33 USDT
    # qty = 保证金/price×leverage = 333.33×3 = 1000
    qty_capped = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                      strength=100, leverage=3, atr_pct=2, stop_pct=0.04)
    assert qty_capped == pytest.approx(1000.0)
    # 验证止损亏损 = 名义×stop_pct = qty×0.04 ≤ 账户1%：1000×0.04=40 = 4000×1%
    assert qty_capped * 0.04 <= 4000 * 0.01 + 1e-9


def test_atr_decay_reduces_size(patch_executor):
    """ATR 高波动衰减：ATR 8% → 因子 0.5，仓位减半。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)

    qty_atr4 = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=4, stop_pct=0)
    qty_atr8 = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=8, stop_pct=0)
    assert qty_atr8 == pytest.approx(qty_atr4 * 0.5)


def test_drawdown_half_size(patch_executor):
    """回撤 ≥8% → 仓位系数 0.5。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4400, 'ts': time.time()})  # 峰 4400，回撤 9.09%

    coeff, dd = se._drawdown_status()
    assert coeff == 0.5
    assert dd > 8


def test_drawdown_pause(patch_executor):
    """回撤 ≥15% → 暂停（系数 0）。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4800, 'ts': time.time()})  # 回撤 16.7%

    coeff, dd = se._drawdown_status()
    assert coeff == 0.0
    assert dd >= 15


def test_drawdown_recovery_mode_after_pause(patch_executor):
    """暂停观察期结束后进入 25% 仓位恢复模式，而非永久停机。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4800, 'ts': time.time()})
    se._rset('account:dd_pause', {'ts': time.time() - se._DD_RECOVERY_DELAY - 1})

    coeff, dd = se._drawdown_status()

    assert coeff == se._DD_RECOVERY_FACTOR
    assert dd >= 15
    assert se.drawdown_mode() == 'recovery'


def test_drawdown_recovery_loss_budget_rehalts(patch_executor):
    """恢复模式再次亏损 2% 后重新硬暂停，防止连续亏损。"""
    se = patch_executor['se']
    patch_executor['set_balance'](3920)
    se._rset('account:peak', {'bal': 4800, 'ts': time.time()})
    se._rset('account:dd_pause', {
        'ts': time.time() - se._DD_RECOVERY_DELAY - 1,
        'base_balance': 4000,
        'loss_lock': False,
    })

    coeff, _ = se._drawdown_status()

    assert coeff == 0.0
    assert se._rget('account:dd_pause')['loss_lock'] is True


def test_drawdown_normal(patch_executor):
    """回撤 <8% → 正常系数 1。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4050, 'ts': time.time()})  # 回撤 1.2%

    coeff, dd = se._drawdown_status()
    assert coeff == 1.0


def test_event_expected_move_rr_support():
    """R:R 预判：PULSE_UP 用 15m 涨幅，TREND 用 1h 涨幅，无数据返回 0（不拦截）。"""
    from strategies.shared_executor import _event_expected_move

    assert _event_expected_move({'type': 'PULSE_UP', 'chg_15m': 3.2}) == pytest.approx(3.2)
    assert _event_expected_move({'type': 'TREND_DOWN', 'chg_1h': -2.5}) == pytest.approx(2.5)
    assert _event_expected_move({'type': 'PUMP_UP', 'chg_15m': None}) == 0.0
    assert _event_expected_move({'type': 'UNKNOWN'}) == 0.0


def test_bounded_stop_pct_caps_atr_expansion():
    """ATR 放大止损时仍必须受尾部风险上限约束。"""
    from strategies.shared_executor import bounded_stop_pct

    assert bounded_stop_pct(0.04, 2) == pytest.approx(0.04)
    assert bounded_stop_pct(0.04, 5) == pytest.approx(0.08)
    assert bounded_stop_pct(0.10, 0) == pytest.approx(0.08)


def test_strength_filter_below_30_blocks_open(patch_executor):
    """strength<30 信号直接拒绝开仓（函数第一步，无副作用）。"""
    se = patch_executor['se']

    result = se.open_position('S6', 'XXX', 'LONG', 1.0, 0.95, 100, 'CROSSED', 3,
                              'PULSE_UP', 25)  # strength=25 < 30
    assert result is False


def test_short_signal_strength_gate():
    from strategies.shared_executor import short_signal_allows_open

    assert short_signal_allows_open('TREND_DOWN', 59) is False
    assert short_signal_allows_open('TREND_DOWN', 60) is True
    assert short_signal_allows_open('PANIC_SELL', 30) is True


def test_violent_bullish_blocked_in_bearish_regime():
    from strategies.shared_executor import long_signal_allows_open

    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'risk-off'}) is False
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'weak_bear'}) is False
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'range'}) is True
    assert long_signal_allows_open('TREND_UP', {'regime': 'risk-off'}) is True


def test_analysis_filter_blocks_open(patch_executor, monkeypatch):
    """分析统计劣化时，开仓在早期直接被拒绝。"""
    se = patch_executor['se']
    monkeypatch.setattr(se, '_analysis_gate', lambda *a, **k: (False, 100.0, 'bad history'))

    result = se.open_position('S6', 'XXX', 'LONG', 1.0, 0.95, 100, 'CROSSED', 3,
                              'PULSE_UP', 80)
    assert result is False


def test_analysis_gate_hard_mode_blocks(monkeypatch, patch_executor):
    se = patch_executor['se']
    monkeypatch.delenv('ANALYSIS_FILTER_MODE', raising=False)
    monkeypatch.setattr(se, '_analysis_allows_open', lambda *a, **k: (False, 'low quality', 0.5))
    monkeypatch.setattr(se, '_record_analysis_decision', lambda *a, **k: None)

    ok, qty, reason = se._analysis_gate('S6', 'BTCUSDT', 'TREND_UP', 100.0)
    assert ok is False
    assert qty == 100.0
    assert 'low quality' in reason


def test_analysis_gate_soft_mode_scales_qty(monkeypatch, patch_executor):
    se = patch_executor['se']
    monkeypatch.setenv('ANALYSIS_FILTER_MODE', 'soft')
    monkeypatch.setattr(se, '_analysis_allows_open', lambda *a, **k: (False, 'bad follow', 0.4))
    monkeypatch.setattr(se, '_record_analysis_decision', lambda *a, **k: None)

    ok, qty, reason = se._analysis_gate('S8', 'ETHUSDT', 'TREND_DOWN', 100.0)
    assert ok is True
    assert qty == pytest.approx(40.0)
    assert 'soft降权' in reason


def test_analysis_reject_summary_aggregates_recent_items(patch_executor):
    se = patch_executor['se']
    now = time.time()
    se._rset('event:analysis_reject', {
        'items': [
            {'ts': now - 60, 'system': 'S6', 'event_type': 'TREND_UP', 'action': 'block'},
            {'ts': now - 30, 'system': 'S6', 'event_type': 'TREND_UP', 'action': 'soft'},
            {'ts': now - 10, 'system': 'S8', 'event_type': 'TREND_DOWN', 'action': 'block'},
            {'ts': now - 7200, 'system': 'S8', 'event_type': 'PULSE_DOWN', 'action': 'block'},  # 过窗
        ]
    })

    s = se.get_analysis_reject_summary(window_sec=3600)
    assert s['total'] == 3
    assert s['by_action']['block'] == 2
    assert s['by_action']['soft'] == 1
    assert s['by_system']['S6'] == 2
    assert s['by_event']['TREND_UP'] == 2


def test_maybe_log_analysis_panel_throttled(monkeypatch, patch_executor):
    se = patch_executor['se']
    now = time.time()
    se._analysis_panel_last_log.clear()
    se._rset('event:analysis_reject', {
        'items': [{'ts': now - 10, 'system': 'S6', 'event_type': 'TREND_UP', 'action': 'block'}]
    })

    logs = []
    monkeypatch.setattr(se, '_log', lambda n, m: logs.append((n, m)))

    se.maybe_log_analysis_panel('S6', interval_sec=300, window_sec=3600)
    se.maybe_log_analysis_panel('S6', interval_sec=300, window_sec=3600)
    assert len(logs) == 1
    assert '分析过滤面板' in logs[0][1]


def test_close_notify_dedup_blocks_same_trade(patch_executor):
    se = patch_executor['se']

    assert se._should_notify_close('S6', 'COTIUSDT', '硬止损', 0.014, 1000, 'LONG') is True
    assert se._should_notify_close('S6', 'COTIUSDT', '硬止损', 0.014, 1000, 'LONG') is False
    assert se._should_notify_close('S6', 'COTIUSDT', '时间止损', 0.014, 1000, 'LONG') is True


def test_close_notify_dedup_expires(patch_executor, monkeypatch):
    se = patch_executor['se']
    t = [1000.0]
    monkeypatch.setattr(se.time, 'time', lambda: t[0])

    assert se._should_notify_close('S6', 'COTIUSDT', '硬止损', 0.014, 1000, 'LONG', window_sec=10) is True
    t[0] = 1005.0
    assert se._should_notify_close('S6', 'COTIUSDT', '硬止损', 0.014, 1000, 'LONG', window_sec=10) is False
    t[0] = 1011.0
    assert se._should_notify_close('S6', 'COTIUSDT', '硬止损', 0.014, 1000, 'LONG', window_sec=10) is True


def test_pm_monitor_does_not_send_cache_based_close_message(patch_executor, monkeypatch):
    """PM 只记录平仓，详细通知由 trade_recorder 单一发送。"""
    se = patch_executor['se']
    logs = []
    sent = []
    monkeypatch.setattr(se, 'monitor_all', lambda system_filter='': [
        ('SKYAIUSDT', '手动平仓', 0.10089, 0.0909, 1875.0, 'LONG')
    ])
    monkeypatch.setattr(se, '_refresh_positions', lambda: {})
    monkeypatch.setattr(se, 'save_state', lambda *args: None)
    monkeypatch.setattr(se, '_log', lambda *args: logs.append(args))

    se.pm_monitor('S6', {'cooldowns': {}}, tg_fn=sent.append)

    assert sent == []
    assert any('平仓 SKYAIUSDT' in msg for _, msg in logs)


def test_recovery_replaces_only_when_candidate_is_stronger(patch_executor, monkeypatch):
    """恢复模式替换弱仓，不增加总仓位。"""
    se = patch_executor['se']
    pos = {'system': 'S6', 'side': 'LONG', 'entry': 1.0,
           'open_time': time.time() - 3600, 'score': 70}
    monkeypatch.setattr(se, '_pm_load', lambda: {'WEAKUSDT': pos})
    monkeypatch.setattr(se, 'close_position', lambda *a, **k: True)
    monkeypatch.setattr(se, '_log', lambda *a, **k: None)
    monkeypatch.setattr('shared.position_score.calc_position_live_score', lambda *a, **k: 70)

    assert se.maybe_replace_recovery_position('S6', 'LONG', 'NEWUSDT', 80, margin=10) is True


def test_has_any_position_blocks_cross_strategy_symbol_conflict(patch_executor, monkeypatch):
    se = patch_executor['se']
    monkeypatch.setattr(se, '_pm_load', lambda: {
        'VELVETUSDT': {'qty': 10, 'system': 'S6', 'side': 'LONG'}
    })

    assert se.has_any_position('VELVETUSDT') is True


def test_entry_timing_filters():
    from strategies.shared_executor import event_is_stale, event_age_sec, price_is_overextended, classify_entry_mode, pump_down_uptrend_guard

    assert event_is_stale({'since': time.time() - 121}) is True
    assert event_is_stale({'since': time.time() - 1000, '_snapshot_ts': time.time()}) is False
    assert event_age_sec({'since': time.time() - 1000, '_snapshot_ts': time.time()}) < 5
    assert event_is_stale({'since': time.time() - 60}) is False
    assert price_is_overextended(115, 100, 5, 'LONG', 2) is True
    assert price_is_overextended(108, 100, 5, 'LONG', 2) is False
    assert price_is_overextended(85, 100, 5, 'SHORT', 2) is True
    assert classify_entry_mode(90, 100, 30, 0.6, 'LONG') == 'LEFT_REVERSAL'
    assert classify_entry_mode(110, 100, 60, 0.6, 'LONG') == 'RIGHT_MOMENTUM'
    assert classify_entry_mode(110, 100, 70, 0.4, 'SHORT') == 'LEFT_REVERSAL'
    assert classify_entry_mode(90, 100, 40, 0.4, 'SHORT') == 'RIGHT_MOMENTUM'
    assert pump_down_uptrend_guard(120, {'ema20': 100, 'chg': 5}, {'chg': 20}) is True
    assert pump_down_uptrend_guard(90, {'ema20': 100, 'chg': 5}, {'chg': 20}) is False
    assert pump_down_uptrend_guard(120, {'ema20': 100, 'chg': -5}, {'chg': 20}) is False


def test_release_event_fresh_allows_retry_after_price_failure():
    from strategies.shared_executor import is_event_fresh, release_event_fresh

    assert is_event_fresh('FAILUSDT', 'TREND_DOWN', cooldown_s=180) is True
    release_event_fresh('FAILUSDT', 'TREND_DOWN')
    assert is_event_fresh('FAILUSDT', 'TREND_DOWN', cooldown_s=180) is True


def test_contract_score_and_leverage_are_risk_adjusted():
    from strategies.shared_executor import contract_score, leverage_for_score

    strong = contract_score(99, 'VIOLENT_BULLISH', atr_pct=2,
                            taker_buy_ratio=0.6, side='LONG')
    weak = contract_score(99, 'VIOLENT_BULLISH', atr_pct=7,
                          extension_atr=2, taker_buy_ratio=0.4, side='LONG')
    assert strong > weak
    assert leverage_for_score('PULSE_UP', 55, 2) == 2
    assert leverage_for_score('PULSE_UP', 75, 2) == 3
    assert leverage_for_score('PULSE_UP', 95, 5) == 3
    assert contract_score(80, 'VIOLENT_BULLISH', side='LONG', short_ratio=0.65) > \
        contract_score(80, 'VIOLENT_BULLISH', side='LONG', short_ratio=0.35)


def test_long_trend_takeover_requires_pullback_and_flow():
    from strategies.shared_executor import long_trend_takeover_ready

    market = {
        '15m': {'ema20': 100, 'atr': 2, 'taker_buy_ratio': 0.6},
        '4h': {'chg': 8},
        '24h': {'ema20': 90, 'ema60': 80, 'chg': 30},
    }
    assert long_trend_takeover_ready(101, market) is True
    assert long_trend_takeover_ready(115, market) is False
    market['15m']['taker_buy_ratio'] = 0.45
    assert long_trend_takeover_ready(101, market) is False


def test_atr_position_model_matches_risk_cap():
    from strategies.position_models import AtrRiskPositionSizer

    model = AtrRiskPositionSizer()
    assert model.score_fraction(100) == pytest.approx(0.15)
    budget = model.budget(4000, 3200, 100, 3, atr_pct=2, stop_pct=0.04)
    assert budget == pytest.approx(333.333333, rel=1e-5)
