"""Golden：PM 边缘与异常场景（全部以当前实现为准）。"""
import time

from pm_golden_helpers import make_position, position_snapshot

from shared.position_manager import (
    SYSTEM_CFG,
    _get_cfg,
    _merge_meta,
    _position_id,
    _was_closed_recently,
)

NOW = 1788000000.0


# ── position_id ──────────────────────────────────────────────────────────

def test_position_id_format_frozen():
    """格式冻结：system:symbol:entry(.12g):open_time(.6f)。"""
    pid = _position_id('AUSDT', {'system': 'S8', 'entry': 0.04547,
                                 'open_time': 1788000000.5})
    assert pid == 'S8:AUSDT:0.04547:1788000000.500000'


def test_position_id_prefers_explicit_field():
    """pos 中已有 position_id → 原样返回。"""
    assert _position_id('AUSDT', {'position_id': 'custom'}) == 'custom'


def test_position_id_zero_defaults():
    """空 pos → ':AUSDT:0:0.000000'（system 缺省为空串；symbol 来自参数）。"""
    assert _position_id('AUSDT', {}) == ':AUSDT:0:0.000000'


def test_position_id_entry_uses_12g_format():
    """entry 用 .12g 格式化（长小数被截断）。"""
    pid = _position_id('AUSDT', {'system': 'S6', 'entry': 0.1234567890123456,
                                 'open_time': 0})
    assert pid.startswith('S6:AUSDT:0.123456789012:')


# ── closed 标记 ──────────────────────────────────────────────────────────

def test_was_closed_recently_false_when_missing(pm_storage):
    assert pm_storage['pm']._was_closed_recently('NOPEUSDT') is False


def test_was_closed_recently_within_window(pm_storage):
    pm_storage['redis'].set('closed:AUSDT', {'ts': time.time() - 60})
    assert _was_closed_recently('AUSDT') is True


def test_was_closed_recently_expired(pm_storage):
    pm_storage['redis'].set('closed:AUSDT', {'ts': time.time() - 5 * 3600})
    assert _was_closed_recently('AUSDT') is False


def test_was_closed_recently_malformed_marker(pm_storage):
    """malformed 标记（缺 ts）→ 当前行为：视为未关闭。"""
    pm_storage['redis'].set('closed:AUSDT', {'no_ts': 1})
    assert _was_closed_recently('AUSDT') is False


# ── _round_qty ───────────────────────────────────────────────────────────

def test_round_qty_tuple_info_uses_first_precision(pm_full):
    """symbol_info 为 tuple → 用第一个元素（qty_precision）。"""
    pm_full['set_s6api'](get_symbol_info=lambda sym: (2, 6))
    assert pm_full['pm']._round_qty('XUSDT', 1.234567) == 1.23


def test_round_qty_dict_info_uses_quantity_precision(pm_full):
    """symbol_info 为 dict → 用 quantity_precision。"""
    pm_full['set_s6api'](get_symbol_info=lambda sym: {'quantity_precision': 0})
    assert pm_full['pm']._round_qty('XUSDT', 17508.9) == 17509.0


def test_round_qty_none_falls_back_6dp(pm_full):
    """symbol_info 不可用 → 回退 6 位小数。"""
    pm_full['set_s6api'](get_symbol_info=lambda sym: None)
    assert pm_full['pm']._round_qty('XUSDT', 1.23456789) == 1.234568


# ── _get_cfg 系统配置映射 ────────────────────────────────────────────────

def test_get_cfg_maps_system_names():
    from shared.position_manager import SYSTEM_CFG
    assert _get_cfg({'system': 'S6A'}) is SYSTEM_CFG['S6A']
    assert _get_cfg({'system': 'S6B'}) is SYSTEM_CFG['S6B']
    assert _get_cfg({'system': 'S8B'}) is SYSTEM_CFG['S8B']


def test_get_cfg_plain_s8_falls_back_to_s8a():
    """system='S8' 不含任何已知 key → 兜底 S8A 配置（当前行为）。"""
    from shared.position_manager import SYSTEM_CFG
    assert _get_cfg({'system': 'S8'}) is SYSTEM_CFG['S8A']


def test_get_cfg_empty_system_falls_back_to_s8a():
    assert _get_cfg({'system': ''}) is SYSTEM_CFG['S8A']
    assert _get_cfg({}) is SYSTEM_CFG['S8A']


# ── _partial_close 边缘 ─────────────────────────────────────────────────

def test_partial_close_zero_qty_freezes_current_behavior(pm_full):
    """Observed Current Behavior：close_qty=0 → 仍会向交易所发 qty=0 的订单
    （真实交易所会拒绝；fake 成功路径下 qty 不变、照常 _save）。
    Potential concern: 缺少 close_qty 有效性校验。
    Future phase: Phase 7 PM decomposition。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=1.0, qty=10.0, original_qty=10.0)
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 1.1, 0, 5, positions)
    assert pos['qty'] == 10.0
    assert pm_full['calls']['post'][0]['params']['quantity'] == 0


def test_partial_close_negative_qty_increases_qty(pm_full):
    """Observed Current Behavior：close_qty<0 → pos['qty'] = qty - (-x) 会**增加**，
    且订单以负数量发出（真实交易所会拒绝；fake 成功路径下冻结此行为）。
    Potential concern: 缺少 close_qty 有效性校验。
    Future phase: Phase 7 PM decomposition。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=1.0, qty=10.0, original_qty=10.0)
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 1.1, -100.0, 5, positions)
    assert pos['qty'] == 110.0                            # 10 - (-100)
    assert pm_full['calls']['post'][0]['params']['quantity'] == -100.0


# ── _merge_meta 边缘 ────────────────────────────────────────────────────

def test_merge_meta_zero_exchange_qty_enters_merged():
    """Observed Current Behavior：交易所快照 qty=0 仍进入合并结果
    （qty>0.001 过滤在上游 REST 层，不在 _merge_meta）。"""
    merged = _merge_meta({'AUSDT': {'entry': 1.0, 'side': 'LONG', 'qty': 0.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         {}, NOW, alert_external=False)
    assert merged['AUSDT']['qty'] == 0.0


def test_merge_meta_meta_missing_fields_use_defaults():
    """meta 只有部分字段 → 其余全部用默认值（不抛异常）。"""
    meta = {'AUSDT': {'system': 'S8'}}
    merged = _merge_meta({'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         meta, NOW, alert_external=False)
    assert merged['AUSDT']['system'] == 'S8'
    assert merged['AUSDT']['be_done'] is False
    assert merged['AUSDT']['event_type'] == ''
    assert merged['AUSDT']['strength'] == 50
    assert position_snapshot(merged['AUSDT'])['entry'] == 1.0
