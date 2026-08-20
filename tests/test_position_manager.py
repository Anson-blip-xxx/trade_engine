"""position_manager 回归测试 — 覆盖 2026-07-31 修复的 bug。"""
import time

import pytest


def test_round_qty_respects_precision(patch_pm):
    """KOMAUSDT 分层止盈：close_qty=17508.9 带小数被拒(-1111) → 必须对齐 LOT_SIZE 精度(step=1)。"""
    pm = patch_pm['pm']
    patch_pm['set_s6api'](get_symbol_info=lambda sym: (0, 7))  # quantityPrecision=0

    assert pm._round_qty('KOMAUSDT', 58363.0 * 0.3) == 17509.0


def test_round_qty_falls_back_to_6dp(patch_pm):
    """symbol_info 不可用时回退 6 位小数，不抛异常。"""
    pm = patch_pm['pm']
    patch_pm['set_s6api'](get_symbol_info=lambda sym: None)
    assert pm._round_qty('XXXUSDT', 1.23456789) == pytest.approx(1.234568)


def test_partial_close_rejected_no_state_change(patch_pm):
    """交易所拒绝（-1111）→ 不扣 qty、不落库、不记账。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    calls['post'].clear()
    patch_pm['set_s6api'](fapi_post=lambda *a, **k: {'code': -1111, 'msg': 'Precision is over'})

    pos = {'qty': 58363.0, 'entry': 0.01354, 'side': 'LONG', 'open_time': time.time(),
           'system': 'S6', 'leverage': 3}
    pm._partial_close('KOMAUSDT', pos, 0.02074, 17508.9, 5, {})

    assert pos['qty'] == 58363.0  # 未减少
    assert calls['save'] == []    # 未落库
    assert calls['record_trade'] == []  # 未记账


def test_partial_close_success_reduces_and_saves(patch_pm):
    """下单成功 → 扣 qty 并落库；不记独立交易（全平时会用 REALIZED_PNL 汇总）。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    patch_pm['set_s6api'](fapi_post=lambda *a, **k: {'status': 'FILLED', 'executedQty': '17509'})

    positions = {}
    pos = {'qty': 58363.0, 'entry': 0.01354, 'side': 'LONG', 'open_time': time.time(),
           'system': 'S6', 'leverage': 3}
    pm._partial_close('KOMAUSDT', pos, 0.02074, 17509.0, 5, positions)

    assert pos['qty'] == pytest.approx(40854.0)
    assert len(calls['save']) == 1
    assert calls['record_trade'] == []  # 分层止盈不单独记账


def test_close_reenrtry_guard_skips_recently_closed(patch_pm):
    """_close 防重入：4h 内已处理过的币直接跳过，不重复下单/记账。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    now = time.time()
    # 模拟 closed:{symbol} 标记（10 分钟前）
    patch_pm['set_s6api']()
    pm._rset(f'closed:RIFUSDT', {'ts': now - 600})

    posts = []
    patch_pm['set_s6api'](fapi_post=lambda *a, **k: (posts.append(a), {'status': 'FILLED'})[1])

    pos = {'qty': 100.0, 'entry': 1.0, 'side': 'LONG', 'open_time': now, 'system': 'S6'}
    pm._close('RIFUSDT', pos, 1.1, '时间止损', {})

    assert posts == []           # 未下单
    assert calls['record_trade'] == []  # 未记账
    assert calls['save'] == []   # 未落库


def test_close_force_bypasses_guard(patch_pm):
    """手动平仓 force=True → 无视 closed 标记，正常执行。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    now = time.time()
    pm._rset(f'closed:XUSDT', {'ts': now - 600})

    posts = []
    patch_pm['set_s6api'](fapi_post=lambda *a, **k: (posts.append(a), {'status': 'FILLED'})[1])

    pos = {'qty': 100.0, 'entry': 1.0, 'side': 'LONG', 'open_time': now, 'system': 'S6'}
    pm._close('XUSDT', pos, 1.1, '手动平仓', {}, force=True)

    assert len(posts) == 1  # 正常下单


def test_close_failure_clears_marker(patch_pm):
    """下单失败 → 清除 closed 标记，允许后续重试。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    patch_pm['set_s6api'](
        fapi_get=lambda *a, **k: [{'symbol': 'YUSDT', 'positionAmt': '100', 'entryPrice': '1.0'}],
        fapi_post=lambda *a, **k: {'code': -2019, 'msg': 'insufficient'},
    )

    pos = {'qty': 100.0, 'entry': 1.0, 'side': 'LONG', 'open_time': time.time(), 'system': 'S6'}
    ok = pm._close('YUSDT', pos, 1.1, '时间止损', {})

    assert ok is False
    assert 'YUSDT' in calls['clear_closed']  # 失败后清标记


def test_close_failure_keeps_protective_algo(patch_pm, monkeypatch):
    """市价平仓被 PERCENT_PRICE 拒绝时，不得先取消原止损条件单。"""
    pm = patch_pm['pm']
    cancelled = []
    monkeypatch.setattr(pm, '_cancel_all_algo', lambda symbol: cancelled.append(symbol))
    patch_pm['set_s6api'](
        fapi_get=lambda *a, **k: [{'symbol': 'KOMAUSDT', 'positionAmt': '100', 'entryPrice': '1.0'}],
        fapi_post=lambda *a, **k: {'code': -4016, 'msg': 'The counterparty\'s best price does not meet the PERCENT_PRICE filter limit'},
    )

    pos = {'qty': 100.0, 'entry': 1.0, 'side': 'LONG',
           'open_time': time.time(), 'system': 'S6'}
    assert pm._close('KOMAUSDT', pos, 0.9, '硬止损', {}) is False
    assert cancelled == []


def test_close_error_is_rate_limited(patch_pm, monkeypatch):
    """同一币种持续被交易所拒绝时，错误日志按周期限频。"""
    pm = patch_pm['pm']
    messages = []
    monkeypatch.setattr(pm, '_pmlog', lambda message: messages.append(message))
    monkeypatch.setattr(pm, '_CLOSE_ERROR_LOG_TS', {})

    pm._log_close_error('COTIUSDT', 'PERCENT_PRICE', interval=60)
    pm._log_close_error('COTIUSDT', 'PERCENT_PRICE', interval=60)

    assert len(messages) == 1


def test_monitor_one_does_not_report_closed_when_close_fails(patch_pm, monkeypatch):
    """硬止损触发但交易所拒绝平仓 → 不返回 closed，避免 S8/TG 虚假平仓刷屏。"""
    pm = patch_pm['pm']
    monkeypatch.setattr(pm, '_get_funding_rate', lambda sym: 0.0)
    patch_pm['set_s6api'](
        fapi_get=lambda *a, **k: [{'symbol': 'DEXEUSDT', 'positionAmt': '-10', 'entryPrice': '2.48'}],
        fapi_post=lambda *a, **k: {'code': -4016, 'msg': 'PERCENT_PRICE filter limit'},
        record_trade=lambda *a, **k: None,
    )

    pos = {'qty': 10.0, 'original_qty': 10.0, 'entry': 2.48, 'side': 'SHORT',
           'open_time': time.time() - 3600, 'system': 'S8', 'sl': 2.68}

    assert pm._monitor_one('DEXEUSDT', pos, {'DEXEUSDT': pos}) is None


def test_close_matches_positionrisk_symbol(patch_pm):
    """positionRisk 必须按 symbol 匹配，不能拿别的同方向仓位误判/误取 qty。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    posts = []
    patch_pm['set_s6api'](
        fapi_get=lambda *a, **k: [{'symbol': 'OTHERUSDT', 'positionAmt': '-99', 'entryPrice': '1.0'}],
        fapi_post=lambda *a, **k: (posts.append(a), {'status': 'FILLED'})[1],
    )

    pos = {'qty': 10.0, 'original_qty': 10.0, 'entry': 2.48, 'side': 'SHORT',
           'open_time': time.time() - 3600, 'system': 'S8'}
    ok = pm._close('DEXEUSDT', pos, 2.7, '硬止损', {'DEXEUSDT': pos})

    assert ok is True                    # DEXE 已不在交易所 → 只记录并清本地
    assert posts == []                    # 不用 OTHERUSDT 的 qty 去给 DEXE 下单
    assert len(calls['record_trade']) == 1


def test_ghost_cleanup_marks_closed_prevents_repeat(patch_pm):
    """幽灵清理后标记 closed，避免下一轮从 meta 重新读到再次清理/重复记账。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    patch_pm['set_s6api']()

    positions = {'RIFUSDT': {'qty': 11829.0, 'original_qty': 11829.0, 'entry': 0.09314,
                             'side': 'LONG', 'open_time': time.time(), 'system': 'S6'}}
    # 交易所无此持仓
    pm._light_fapi_get = lambda *a, **k: []
    pm._light_get_price = lambda *a, **k: 0.088

    closed = pm._ghost_cleanup(positions, system_filter='S6')

    assert len(closed) == 1
    assert 'RIFUSDT' not in positions            # 已从内存移除
    assert 'RIFUSDT' in calls['mark_closed']     # 已标记 → 下一轮 _load 会过滤
    assert len(calls['record_trade']) == 1       # 只记一次


def test_ghost_cleanup_records_current_exit_price(patch_pm):
    """幽灵仓必须用实际现价落库，不能把出场价写成入场价。"""
    pm = patch_pm['pm']
    recorded = []
    patch_pm['set_s6api'](record_trade=lambda *args, **kwargs: recorded.append((args, kwargs)))
    positions = {'RIFUSDT': {'qty': 100.0, 'entry': 1.0, 'side': 'LONG',
                             'open_time': time.time(), 'system': 'S6'}}
    pm._light_fapi_get = lambda *a, **k: []
    pm._light_get_price = lambda *a, **k: 0.8

    pm._ghost_cleanup(positions, system_filter='S6')

    assert recorded[0][0][2] == pytest.approx(0.8)
    assert recorded[0][1]['exit_reason'] == '手动平仓'


def test_ghost_cleanup_skips_closed_recently(patch_pm):
    """WS 领导者已标记 closed 的幽灵仓 → 不重复记账，直接移除。"""
    pm, calls = patch_pm['pm'], patch_pm['calls']
    patch_pm['set_s6api']()
    pm._rset('closed:RIFUSDT', {'ts': time.time() - 60})

    positions = {'RIFUSDT': {'qty': 11829.0, 'entry': 0.09314, 'side': 'LONG',
                             'open_time': time.time(), 'system': 'S6'}}
    pm._light_fapi_get = lambda *a, **k: []

    closed = pm._ghost_cleanup(positions, system_filter='S6')

    assert closed == []                  # 不重复清理
    assert 'RIFUSDT' not in positions
    assert calls['record_trade'] == []   # 不重复记账


def test_merge_meta_preserves_tp_done_highest_lowest():
    """_merge_meta 必须保留 tp_done/highest/lowest，且 qty 取交易所与 meta 的较小值。"""
    from shared.position_manager import _merge_meta

    raw = {'KOMAUSDT': {'entry': 0.01354, 'side': 'LONG', 'qty': 58363.0, 'leverage': 3}}
    meta = {'KOMAUSDT': {'qty': 40854.0, 'tp_done': [5], 'highest': 0.02074,
                         'lowest': 0.0133, 'be_done': True, 'trail': False, 'atr': 0.1}}
    merged = _merge_meta(raw, dict(meta), time.time())

    m = merged['KOMAUSDT']
    assert m['tp_done'] == [5]                 # 分层止盈状态保留 → 不会重复触发
    assert m['highest'] == 0.02074             # 吊灯锚点保留
    assert m['lowest'] == 0.0133
    assert m['qty'] == 40854.0                 # min(交易所58363, meta40854)


def test_merge_meta_exchange_qty_wins_when_meta_empty():
    """meta 无 qty 时以交易所为准。"""
    from shared.position_manager import _merge_meta

    raw = {'BTCUSDT': {'entry': 100.0, 'side': 'SHORT', 'qty': 5.0, 'leverage': 3}}
    merged = _merge_meta(raw, {}, time.time())
    assert merged['BTCUSDT']['qty'] == 5.0
    assert merged['BTCUSDT']['tp_done'] == []


def test_1h_reversal_only_exits_at_breakeven_or_profit():
    """亏损仓遇到 1h 反转先观察，避免回本途中被提前平掉。"""
    from shared.position_manager import _should_exit_1h_reversal

    assert _should_exit_1h_reversal(-0.1) is False
    assert _should_exit_1h_reversal(0.0) is True
    assert _should_exit_1h_reversal(2.5) is True


def test_early_loss_momentum_weak():
    """15m 仍逆向运行时触发早期亏损保护，反向恢复时不触发。"""
    from shared.position_manager import _early_loss_momentum_weak

    down = [[0, 0, 0, 0, p] for p in (10.0, 9.9, 9.8, 9.7)]
    up = [[0, 0, 0, 0, p] for p in (10.0, 9.9, 10.0, 10.1)]
    assert _early_loss_momentum_weak(down, 'LONG') is True
    assert _early_loss_momentum_weak(down, 'SHORT') is False
    assert _early_loss_momentum_weak(up, 'LONG') is False


def test_peak_pullback_guard_triggers_on_retrace():
    """SHORT 浮盈达标后实时上移锁利止损，从峰值回踩超阈值则立即平仓。"""
    from shared.position_manager import _peak_pullback_check

    cfg = {'peak_guard': {'trigger_pct': 3.0, 'drawdown_pct': 2.0}}
    pos = {'entry': 1.0, 'side': 'SHORT', 'qty': 10.0}
    # 未达触发阈值：不平
    assert _peak_pullback_check(pos, 0.975, cfg) is None
    # 达标（+4%）→ 武装，返回应上移的锁利止损价（极值×1.02）
    sl = _peak_pullback_check(pos, 0.96, cfg)
    assert sl is not None and not isinstance(sl, str)
    assert pos['lowest'] == 0.96
    assert sl == pytest.approx(0.9792)
    # 极值下移 → 止损跟随下移（更紧）
    sl2 = _peak_pullback_check(pos, 0.95, cfg)
    assert sl2 == pytest.approx(0.969)
    # 从极值回踩 +2.1% → 触发直接平仓
    reason = _peak_pullback_check(pos, 0.97, cfg)
    assert isinstance(reason, str) and '峰值回撤保护' in reason


def test_peak_pullback_guard_long_mirror():
    """LONG 从峰值回跌触发；未达阈值不动作。"""
    from shared.position_manager import _peak_pullback_check

    cfg = {'peak_guard': {'trigger_pct': 3.0, 'drawdown_pct': 2.0}}
    pos = {'entry': 1.0, 'side': 'LONG', 'qty': 10.0}
    assert _peak_pullback_check(pos, 1.02, cfg) is None   # pnl=2% < 3%
    sl = _peak_pullback_check(pos, 1.06, cfg)             # pnl=6% → 武装
    assert not isinstance(sl, str) and sl is not None
    assert pos['highest'] == 1.06
    assert sl == pytest.approx(1.0388)                    # 极值×0.98
    reason = _peak_pullback_check(pos, 1.025, cfg)        # 回跌 3.3% > 2%
    assert isinstance(reason, str) and '峰值回撤保护' in reason


def test_external_position_alert_has_grace_period(patch_pm, monkeypatch):
    """开仓写入 PM 元数据前的短暂竞态不能立即报警。"""
    pm = patch_pm['pm']
    sent = []
    monkeypatch.setattr(pm, '_pmlog', lambda message: sent.append(message))
    monkeypatch.setattr(pm, '_TG_TOKEN', '')
    monkeypatch.setattr(pm, '_pg_record_event', lambda *a, **k: None)

    raw = {'entry': 1.0, 'qty': 10.0, 'side': 'LONG'}
    pm._notify_external_position('TESTUSDT', raw, 'S6')

    assert sent == []
    assert pm._rget('alert:external_position:pending:TESTUSDT')['fingerprint'] == 'LONG:1:10'


def test_stagnant_profit_rule():
    """长时间只赚不到 1U 时释放仓位，避免资金费吞掉收益。"""
    from shared.position_manager import _is_stagnant_profit

    assert _is_stagnant_profit(0.5, 90) is True
    assert _is_stagnant_profit(1.0, 90) is False
    assert _is_stagnant_profit(0.5, 89) is False
    assert _is_stagnant_profit(-0.5, 120) is False


def test_merge_meta_preserves_missing_position_for_ghost_cleanup():
    """交易所快照缺币时不能静默删本地仓位，须交给幽灵清理记录。"""
    from shared.position_manager import _merge_meta_preserving_missing
    now = time.time()
    meta = {
        'KOMAUSDT': {
            'entry': 0.0239, 'qty': 18146.0, 'side': 'LONG',
            'system': 'S6', 'open_time': now,
        },
    }

    merged = _merge_meta_preserving_missing({}, meta, now)

    assert merged['KOMAUSDT'] == meta['KOMAUSDT']


def test_merge_meta_skips_legacy_perp_symbols():
    """遗留 *USD_PERP 合约不进 PM 监控/告警，也不从本地元数据补回。"""
    from shared.position_manager import _merge_meta_preserving_missing
    now = time.time()
    raw = {
        'LTCUSD_PERP': {'entry': 55.0, 'qty': 26.0, 'side': 'LONG'},
        'BTCUSDT': {'entry': 100.0, 'qty': 1.0, 'side': 'LONG'},
    }
    meta = {
        'LTCUSD_PERP': {'entry': 55.0, 'qty': 26.0, 'side': 'LONG',
                        'system': 'S6', 'open_time': now},
    }

    merged = _merge_meta_preserving_missing(raw, meta, now)

    assert 'LTCUSD_PERP' not in merged
    assert merged['BTCUSDT']['entry'] == 100.0
