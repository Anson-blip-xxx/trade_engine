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
