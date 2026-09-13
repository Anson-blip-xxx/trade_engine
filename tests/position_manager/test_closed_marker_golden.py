"""P7-01：closed marker（closed:{sym}）golden —— S0-9 家族/4h ts 比较/无 TTL。"""
import time

import pytest



class TestClosedMarker:
    def test_set_and_check(self, pm_storage):
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')
        assert pm._was_closed_recently('AUSDT') is True

    def test_missing_marker(self, pm_storage):
        pm = pm_storage['pm']
        assert pm._was_closed_recently('NEVER-CLOSED') is False

    def test_clear_marker(self, pm_storage):
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')
        pm._clear_closed_marker('AUSDT')
        assert pm._was_closed_recently('AUSDT') is False

    def test_ts_age_four_hours_frozen(self, pm_storage):
        """4h 系 ts 比较而非 TTL（PMB-4 / PM OBS-5）。"""
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')
        # fake 时钟位移 <= 4h - 1s
        state = pm_storage['redis'].get('closed:AUSDT')
        state['ts'] -= 3 * 3600           # 3h 前
        assert pm._was_closed_recently('AUSDT', within_hours=4) is True
        # ts 改动必须重新写入（FakeRedis get 会回传 copy 吗？——验证引用一致）
        pm_storage['redis'].set('closed:AUSDT', state)
        state2 = pm_storage['redis'].get('closed:AUSDT')
        state2['ts'] -= 3601              # 共 4h+1s 前
        pm_storage['redis'].set('closed:AUSDT', state2)
        assert pm._was_closed_recently('AUSDT', within_hours=4) is False

    def test_was_closed_window_overlapping_statutory(self, pm_storage):
        """within_hours=8 与 4h 等 不同时段相对传入参数（f(a=4h))."""
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')
        st = pm_storage['redis'].get('closed:AUSDT')
        st['ts'] -= 5 * 3600
        pm_storage['redis'].set('closed:AUSDT', st)
        assert pm._was_closed_recently('AUSDT') is False
        assert pm._was_closed_recently('AUSDT', within_hours=8) is True

    def test_malformed_marker(self, pm_storage):
        pm = pm_storage['pm']
        pm_storage['redis'].set('closed:AUSDT', 'not-dict')
        assert pm._was_closed_recently('AUSDT') is False     # 静默吞错
        # 缺 'ts' 键
        pm_storage['redis'].set('closed:AUSDT', {'x': 1})
        assert pm._was_closed_recently('AUSDT') is False

    def test_redis_read_failure_false(self, pm_storage, monkeypatch):
        pm = pm_storage['pm']

        def boom(k):
            raise RuntimeError('down')
        monkeypatch.setattr(pm, '_rget', boom)
        assert pm._was_closed_recently('AUSDT') is False

    def test_clear_failure_silent(self, pm_storage, monkeypatch):
        import shared.redis_store as rs
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')

        def boom(k):
            raise RuntimeError('down')
        monkeypatch.setattr(rs, 'delete', boom)
        try:
            pm._clear_closed_marker('AUSDT')  # 不抛
        finally:
            pass

    def test_marker_no_ttl_fact(self, pm_storage):
        """确认 marker 不带 TTL：仅 {'ts'} 字段，Redis 值结构无 expire。"""
        pm = pm_storage['pm']
        pm._mark_closed('AUSDT')
        marker = pm_storage['redis'].get('closed:AUSDT')
        assert set(marker.keys()) == {'ts'}
        assert set(marker.keys()) == {'ts'} and isinstance(marker['ts'], float)




def test_marker_first_close_debug(pm_storage, monkeypatch):
    pm = pm_storage['pm']
    events = []
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S8', 'open_time': 1.0, 'leverage': 3, 'sl': 2.2,
           'signal_type': 'S', 'event_type': 'S', 'score': 66, 'be_done': False}
    positions = {'AUSDT': pos}
    monkeypatch.setattr(pm, '_sandbox_active',
                        lambda: events.append('sandbox') or False)
    monkeypatch.setattr(pm, '_s6api',
                        lambda: (lambda p, q=None: (_ for _ in ()).throw(
                            RuntimeError('x')), lambda p, q=None: {},
                                 lambda p, q=None: None, lambda s: 1.0,
                                 lambda s: (6, 6), lambda *a, **k: (0, 0, 0),
                                 lambda *a, **k: 50.0, lambda *a, **kw: None))
    from shared import position_manager as pm2
    monkeypatch.setattr(pm2, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm2, '_mark_closed', lambda s: events.append('mark'))
    monkeypatch.setattr(pm2, '_clear_closed_marker',
                        lambda s: events.append('clear'))
    out = pm._close('AUSDT', pos, 2.0, '手动平仓', positions)
    print('DEBUG out=', out, 'events=', events)
    assert out is False


class TestMarkerFirstClose:
    def test_marker_before_exchange_close(self, pm_full, monkeypatch):
        """_close 先 mark → 实盘 REST 异常 → outer except → _clear + False
        （E-OBS-6 顺序 + marker-before-exchange 冻结；pm_full 全隔离）。"""
        pm = pm_full['pm']
        events = []
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        monkeypatch.setattr(pm, '_mark_closed', lambda s: events.append('mark'))
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: events.append('clear'))

        def fapi_get_raise(path, params=None):
            raise RuntimeError('exchange down')
        pm_full['set_s6api'](fapi_get=fapi_get_raise)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
               'system': 'S8', 'open_time': 1.0, 'leverage': 3, 'sl': 2.2,
               'signal_type': 'S', 'event_type': 'S', 'score': 66,
               'be_done': False}
        positions = {'AUSDT': pos}
        out = pm._close('AUSDT', pos, 2.0, '手动平仓', positions)
        assert events == ['mark', 'clear']         # 顺序固定
        assert out is False
        assert 'AUSDT' in positions                # position 保留
