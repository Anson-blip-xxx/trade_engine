"""Golden：PM 持仓加载（_load_meta / _load 沙盘路径）。

Golden 原则：锁定当前行为，不测试"应该"。
"""
import time

from pm_golden_helpers import make_position


def test_load_meta_empty_returns_empty(pm_storage):
    """无持仓状态 → _load_meta 返回空 dict。"""
    assert pm_storage['pm']._load_meta() == {}


def test_load_meta_filters_non_dict_entries(pm_storage):
    """malformed state（非 dict 条目）→ 被过滤，正常条目保留。"""
    good = make_position()
    pm_storage['redis'].set('pm:positions',
                            {'AUSDT': good, 'bad': 'string-value', 'bad2': 123})
    meta = pm_storage['pm']._load_meta()
    assert set(meta.keys()) == {'AUSDT'}
    assert meta['AUSDT']['entry'] == 1.0


def test_load_meta_passes_normal_entries_unchanged(pm_storage):
    """正常条目原样返回（load 不修改字段值）。"""
    good = make_position(qty=55.5, sl=0.9)
    pm_storage['redis'].set('pm:positions', {'AUSDT': good})
    meta = pm_storage['pm']._load_meta()
    assert meta['AUSDT'] == good


def test_sandbox_load_returns_meta_only(pm_full):
    """沙盘模式：_load 只返回本地元数据（跳过 WS/REST 层）。"""
    pm_full['set_sandbox'](True)
    pos = make_position()
    pm_full['redis'].set('pm:positions', {'AUSDT': pos})
    loaded = pm_full['pm']._load()
    assert loaded == {'AUSDT': pos}


def test_sandbox_load_excludes_recently_closed(pm_full):
    """沙盘模式：4h 内被 close 处理过的 symbol 被排除。"""
    pm_full['set_sandbox'](True)
    pm_full['redis'].set('pm:positions', {'AUSDT': make_position()})
    pm_full['redis'].set('closed:AUSDT', {'ts': time.time()})
    assert pm_full['pm']._load() == {}


def test_sandbox_load_keeps_position_when_closed_marker_expired(pm_full):
    """closed 标记超过 4h（过期）→ 持仓重新可见。"""
    pm_full['set_sandbox'](True)
    pos = make_position()
    pm_full['redis'].set('pm:positions', {'AUSDT': pos})
    pm_full['redis'].set('closed:AUSDT', {'ts': time.time() - 5 * 3600})
    loaded = pm_full['pm']._load()
    assert loaded == {'AUSDT': pos}


def test_sandbox_load_multiple_positions(pm_full):
    """多持仓：全部返回，互相隔离。"""
    pm_full['set_sandbox'](True)
    a, b = make_position(symbol='AUSDT'), make_position()
    pm_full['redis'].set('pm:positions', {'AUSDT': a, 'BUSDT': b})
    loaded = pm_full['pm']._load()
    assert loaded == {'AUSDT': a, 'BUSDT': b}
