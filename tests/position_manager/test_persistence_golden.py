"""Golden：平均入场 / 多持仓隔离 / 持久化 / closed 标记（P1-04）。

关键冻结点：PM 自身**不计算**平均入场价——合并后的 entry 永远来自交易所
positionRisk 快照；本地元数据不参与 entry 计算。
"""
import pytest
from pm_golden_helpers import make_position, position_snapshot, seed_positions

from shared.position_manager import _merge_meta

NOW = 1788000000.0


# ── Average Entry（当前实现：entry 来自交易所，不做本地加权） ─────────────

def test_average_entry_comes_from_exchange_snapshot():
    """增仓后 entry = 交易所快照的 entryPrice（Binance 已加权），PM 不重算。"""
    # 本地记录 entry=100；交易所合并时快照 entryPrice 已经是加权后的 110
    meta = {'AUSDT': make_position(entry=100.0, qty=1.0)}
    merged = _merge_meta({'AUSDT': {'entry': 110.0, 'side': 'LONG', 'qty': 2.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['entry'] == 110.0        # 直接采用交易所值


def test_average_entry_short_same_semantics():
    """空头同样：entry 直接采用交易所快照。"""
    meta = {'AUSDT': make_position(entry=100.0, qty=1.0, side='SHORT')}
    merged = _merge_meta({'AUSDT': {'entry': 90.0, 'side': 'SHORT', 'qty': 3.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['entry'] == 90.0


def test_average_entry_multiple_merges_still_exchange_value():
    """多次增仓：每次合并都直接取交易所最新快照的 entry。"""
    meta = {'AUSDT': make_position(entry=100.0, qty=1.0)}
    merged = _merge_meta({'AUSDT': {'entry': 105.0, 'side': 'LONG', 'qty': 2.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['entry'] == 105.0
    merged = _merge_meta({'AUSDT': {'entry': 112.0, 'side': 'LONG', 'qty': 4.0,
                                    'leverage': 3, 'margin': 'CROSSED'}},
                         dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['entry'] == 112.0


# ── Multiple Positions 隔离 ─────────────────────────────────────────────

def test_merge_one_symbol_does_not_affect_another():
    """合并 A 的元数据不影响 B 的记录。"""
    meta = {'AUSDT': make_position(system='S8', sl=0.5)}
    raw = {'AUSDT': {'entry': 1.0, 'side': 'LONG', 'qty': 5.0,
                     'leverage': 3, 'margin': 'CROSSED'},
           'BUSDT': {'entry': 2.0, 'side': 'SHORT', 'qty': 3.0,
                     'leverage': 5, 'margin': 'ISOLATED'}}
    merged = _merge_meta(raw, dict(meta), NOW, alert_external=False)
    assert merged['AUSDT']['sl'] == 0.5 and merged['AUSDT']['system'] == 'S8'
    assert merged['BUSDT']['system'] == 'S8'        # SHORT 默认
    assert merged['BUSDT']['sl'] == pytest.approx(2.0 * 1.08)  # B 无 meta → 默认 sl


def test_close_one_symbol_keeps_the_other_in_redis(pm_full):
    """平 A 后 B 在 Redis 中保持不变。"""
    pm_full['set_sandbox'](True)
    a = make_position(entry=1.0, symbol='AUSDT')
    b = make_position(entry=2.0, symbol='BUSDT', qty=5.0, original_qty=5.0)
    seed_positions(pm_full['redis'], {'AUSDT': a, 'BUSDT': b})
    positions = {'AUSDT': a, 'BUSDT': b}
    pm_full['pm']._close('AUSDT', a, 1.1, '手动平仓', positions)
    stored = pm_full['redis'].get('pm:positions')
    assert 'AUSDT' not in stored
    assert stored['BUSDT']['entry'] == 2.0 and stored['BUSDT']['qty'] == 5.0


# ── Persistence（真实 _save/_load/_mark_closed/_clear_closed_marker） ────

def test_open_persists_to_redis_positions_key(pm_full):
    """open → 持久化到 pm:positions。"""
    pm_full['set_sandbox'](True)
    pm_full['pm'].open_position('AUSDT', 'LONG', 1.0, 100.0, 3, 0.92, system='S6')
    stored = pm_full['redis'].get('pm:positions')
    assert stored['AUSDT']['entry'] == 1.0


def test_partial_close_persists_qty(pm_full):
    """部分平仓 → 数量变化持久化到 pm:positions。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=1.0, qty=10.0, original_qty=10.0)
    seed_positions(pm_full['redis'], {'AUSDT': pos})
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 1.1, 4.0, 5, positions)
    stored = pm_full['redis'].get('pm:positions')
    assert stored['AUSDT']['qty'] == 6.0


def test_close_persists_removal(pm_full):
    """全平 → pm:positions 中该 symbol 被移除。"""
    pm_full['set_sandbox'](True)
    pos = make_position()
    seed_positions(pm_full['redis'], {'AUSDT': pos})
    positions = {'AUSDT': pos}
    pm_full['pm']._close('AUSDT', pos, 1.1, '手动平仓', positions)
    stored = pm_full['redis'].get('pm:positions')
    assert stored == {} or 'AUSDT' not in stored


def test_load_roundtrip_preserves_fields(pm_full):
    """save → load 往返后字段完全一致。"""
    pm_full['set_sandbox'](True)
    pos = make_position(qty=7.77, sl=0.88, score=77)
    seed_positions(pm_full['redis'], {'AUSDT': pos})
    loaded = pm_full['pm']._load()
    assert position_snapshot(loaded['AUSDT']) == position_snapshot(pos)


def test_save_swallows_redis_errors(pm_storage, monkeypatch):
    """Observed Current Behavior：Redis 写入异常 → _save 静默吞掉，
    持久化失败不影响调用方。
    Potential concern: 极端情况下持仓变更可能只存在于内存。
    Future phase: Phase 7 PM decomposition（storage adapter）。"""
    def _raise(key, data):
        raise RuntimeError('redis down')
    monkeypatch.setattr(pm_storage['pm'], '_rset', _raise)
    pm_storage['pm']._save({'AUSDT': make_position()})   # 不应抛异常


def test_load_meta_redis_error_returns_empty(pm_storage, monkeypatch):
    """Redis 读取异常 → _load_meta 返回空 dict（当前行为）。"""
    def _raise(key):
        raise RuntimeError('redis down')
    monkeypatch.setattr(pm_storage['pm'], '_rget', _raise)
    assert pm_storage['pm']._load_meta() == {}


# ── closed 标记（真实 _mark_closed / _was_closed_recently / _clear） ─────

def test_closed_marker_lifecycle(pm_storage):
    """mark → was_closed True → clear → was_closed False。"""
    pm = pm_storage['pm']
    assert pm._was_closed_recently('XUSDT') is False
    pm._mark_closed('XUSDT')
    assert pm_storage['redis'].get('closed:XUSDT') is not None
    assert pm._was_closed_recently('XUSDT') is True
    pm._clear_closed_marker('XUSDT')
    assert pm._was_closed_recently('XUSDT') is False


def test_closed_marker_expires_after_4h(pm_storage):
    """标记超过 4h → 视为未关闭；4h 内 → 视为已关闭。"""
    import time
    pm = pm_storage['pm']
    pm_storage['redis'].set('closed:XUSDT', {'ts': time.time() - 4 * 3600 - 1})
    assert pm._was_closed_recently('XUSDT') is False
    pm_storage['redis'].set('closed:XUSDT', {'ts': time.time() - 4 * 3600 + 60})
    assert pm._was_closed_recently('XUSDT') is True
