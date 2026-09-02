"""Golden：PM 合并逻辑（_merge_meta / _merge_meta_preserving_missing）。

冻结当前行为：entry 永远来自交易所快照（PM 不自行计算均价）；
本地元数据只做 enrich 与部分字段覆盖。
"""
import time

import pytest
from pm_golden_helpers import make_position

from shared.position_manager import _merge_meta, _merge_meta_preserving_missing

NOW = 1788000000.0


def _bp(entry=1.0, side='LONG', qty=5.0, leverage=3, margin='CROSSED'):
    """模拟交易所 positionRisk 转换后的原始持仓。"""
    return {'entry': entry, 'side': side, 'qty': qty,
            'leverage': leverage, 'margin': margin}


def test_merge_long_defaults_frozen(pm_storage, monkeypatch):
    """LONG 无本地元数据 → 全部默认字段冻结（system/sl/event_type/stop 等）。"""
    monkeypatch.setattr('shared.position_manager.time.time', lambda: NOW)
    merged = _merge_meta({'AUSDT': _bp()}, {}, NOW, alert_external=False)
    pos = merged['AUSDT']
    assert pos['entry'] == 1.0                       # entry 来自交易所
    assert pos['qty'] == 5.0
    assert pos['side'] == 'LONG'
    assert pos['system'] == 'S6'                     # LONG 默认系统
    assert pos['sl'] == 0.92                         # 无 meta sl → entry*0.92
    assert pos['open_time'] == NOW                   # 无 meta → 默认当前时间
    assert pos['event_type'] == '' and pos['strength'] == 50 and pos['score'] == 50
    assert pos['be_done'] is False and pos['trail'] is False
    assert pos['atr'] == 0 and pos['algo_sl_id'] == 0
    assert pos['stop'] == 1.0                        # stop 默认 = entry
    assert pos['tp_done'] == [] and pos['highest'] == 1.0 and pos['lowest'] == 1.0
    assert pos['trend_reversal_warned'] is False
    # Observed: _merge_meta 内联生成三段式 id（与 _position_id 四段式不同）
    assert pos['position_id'] == 'S6:AUSDT:1788000000.000000'


def test_merge_short_defaults_frozen(pm_storage, monkeypatch):
    """SHORT 无本地元数据 → system=S8，sl=entry*1.08。"""
    merged = _merge_meta({'AUSDT': _bp(side='SHORT', entry=2.0)}, {}, NOW,
                         alert_external=False)
    pos = merged['AUSDT']
    assert pos['system'] == 'S8'
    assert pos['sl'] == pytest.approx(2.0 * 1.08)
    assert pos['side'] == 'SHORT'


def test_merge_meta_overrides_win_when_present(pm_storage):
    """本地元数据存在 → system/sl/open_time/event_type 等以 meta 为准。"""
    meta = {'AUSDT': make_position(system='S8', sl=0.5, open_time=123.0,
                                   event_type='TREND_UP', strength=80,
                                   position_id='custom-id', atr=0.01,
                                   be_done=True)}
    merged = _merge_meta({'AUSDT': _bp()}, dict(meta), NOW, alert_external=False)
    pos = merged['AUSDT']
    assert pos['system'] == 'S8'
    assert pos['sl'] == 0.5
    assert pos['open_time'] == 123.0
    assert pos['event_type'] == 'TREND_UP'
    assert pos['strength'] == 80
    assert pos['be_done'] is True
    assert pos['atr'] == 0.01
    assert pos['position_id'] == 'custom-id'


def test_merge_qty_capped_by_meta_qty(pm_storage):
    """qty = min(交易所数量, 本地元数据数量)。"""
    meta = {'AUSDT': make_position(qty=4.0)}
    merged = _merge_meta({'AUSDT': _bp(qty=10.0)}, dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['qty'] == 4.0


def test_merge_meta_qty_absent_uses_exchange_qty(pm_storage):
    """本地元数据无 qty → 用交易所数量。"""
    meta = {'AUSDT': {'system': 'S8', 'sl': 0.5}}
    merged = _merge_meta({'AUSDT': _bp(qty=7.5)}, dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['qty'] == 7.5


def test_merge_meta_qty_zero_from_exchange_enters_merged(pm_storage):
    """Observed Current Behavior：交易所快照 qty=0 时合并结果 qty=0，
    仍进入 merged 输出（qty>0.001 过滤在上游 REST 层，不在 _merge_meta）。"""
    merged = _merge_meta({'AUSDT': _bp(qty=0.0)}, {}, NOW, alert_external=False)
    assert merged['AUSDT']['qty'] == 0.0


def test_merge_multiple_positions_independent(pm_storage):
    """多持仓合并互相隔离。"""
    raw = {'AUSDT': _bp(entry=1.0), 'BUSDT': _bp(entry=2.0, side='SHORT', qty=3.0)}
    merged = _merge_meta(raw, {}, NOW, alert_external=False)
    assert merged['AUSDT']['entry'] == 1.0 and merged['AUSDT']['system'] == 'S6'
    assert merged['BUSDT']['entry'] == 2.0 and merged['BUSDT']['system'] == 'S8'


def test_merge_preserving_missing_does_not_mutate_input_meta(pm_storage):
    """Observed Current Behavior：_merge_meta_preserving_missing 内部对 meta
    做拷贝，调用方的原始 meta dict 不被 pop 修改。"""
    meta = {'AUSDT': make_position()}
    snapshot_before = dict(meta['AUSDT'])
    _merge_meta_preserving_missing({'AUSDT': _bp()}, meta, NOW)
    assert meta['AUSDT'] == snapshot_before


def test_merge_preserving_missing_keeps_local_not_on_exchange(pm_storage):
    """交易所快照缺失的本地持仓被保留（交给幽灵清理流程核验）。"""
    local = make_position()
    merged = _merge_meta_preserving_missing({}, {'AUSDT': local}, NOW)
    assert merged['AUSDT'] == local


def test_merge_preserving_missing_skips_recently_closed(pm_storage):
    """被 close 标记过的缺失持仓不补回（防重复清理）。"""
    pm_storage['redis'].set('closed:AUSDT', {'ts': time.time()})
    local = make_position()
    merged = _merge_meta_preserving_missing({}, {'AUSDT': local}, NOW)
    assert 'AUSDT' not in merged


def test_merge_position_id_format_frozen(pm_storage):
    """Observed: _merge_meta 生成三段式 id system:symbol:open_time(.6f)，
    与 _position_id() 的四段式（含 entry）不一致——两种格式并存。"""
    merged = _merge_meta({'AUSDT': _bp(entry=0.04547)},
                         {'AUSDT': make_position(system='S8', open_time=1788000000.5)},
                         NOW, alert_external=False)
    assert merged['AUSDT']['position_id'] == 'S8:AUSDT:1788000000.500000'
