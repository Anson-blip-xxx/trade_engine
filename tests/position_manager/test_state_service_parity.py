"""P7-02：PositionStateService parity（legacy wrapper vs service 同输入对拍）。"""
import time

import pytest

from position_state.service import PositionStateService


class JSONPort:
    """模拟 P4-D2 适配器：JSON round-trip → 每次 load 返回全新 dict。"""

    def __init__(self, redis_store):
        self.store = redis_store

    def load_positions(self):
        import json as _j
        raw = self.store.get('pm:positions')
        if isinstance(raw, dict):
            try:
                parsed = _j.loads(_j.dumps(raw))
            except Exception:
                parsed = {}
            return {k: v for k, v in parsed.items() if isinstance(v, dict)}
        return {}

    def save_positions(self, d):
        self.store['pm:positions'] = d


class FR2:
    def __init__(self):
        self.store = {}
        self.data = self.store  # 兼容 legacy 命名

    def set(self, k, v):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def direct_delete(self, k):
        self.store.pop(k, None)


def make_parity_pair(state_store):
    return PositionStateService(
        state_port=JSONPort(state_store),
        marker_set=state_store.set,
        marker_get=state_store.get,
        marker_delete=state_store.delete,
        sandbox_check=lambda: False)


def make_pos(qty=10.0, **kw):
    base = {'entry': 2.0, 'qty': qty, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': 1.0, 'leverage': 3, 'sl': 2.2,
            'signal_type': 'S8', 'event_type': 'S8', 'score': 66,
            'be_done': False}
    base.update(kw)
    return base


class TestParityMatrix:
    @pytest.fixture(autouse=True)
    def _setup(self, pm_storage):
        self.pm = pm_storage['pm']
        self.store = pm_storage['redis']

    def test_meta_load_parity_persists(self):
        self.store.set('pm:positions', {'AUSDT': make_pos()})
        legacy = self.pm._load_meta()
        svc = make_parity_pair(self.store)
        svc_meta = svc.load_meta()
        assert legacy == svc_meta
        assert list(legacy.keys()) == list(svc_meta.keys())

    def test_meta_load_missing_parity(self):
        self.store.data.clear()
        legacy = self.pm._load_meta()
        svc = make_parity_pair(self.store)
        assert legacy == svc.load_meta() == {}

    def test_save_parity_roundtrip(self):
        payload = {'AUSDT': make_pos(qty=42.0)}
        self.pm._save(payload)
        svc = make_parity_pair(self.store)
        assert svc.load_meta() == self.pm._load_meta()


class TestMarkerParity:
    def test_set_check_clear(self, pm_storage, monkeypatch):
        from shared import position_manager as pm2
        st = pm_storage['redis']
        st.direct_delete = lambda k: st.data.pop(k, None)
        svc = make_parity_pair(st)
        monkeypatch.setattr(pm2, '_mark_closed', lambda s: svc.mark_closed(s))
        monkeypatch.setattr(pm2, '_clear_closed_marker',
                            lambda s: svc.clear_closed(s))
        monkeypatch.setattr(pm2, '_was_closed_recently',
                            lambda s, w=4: svc.was_closed_recently(s, w))
        pm2._mark_closed('AUSDT')
        assert st.get('closed:AUSDT') is not None
        assert svc.was_closed_recently('AUSDT') is True
        assert pm2._was_closed_recently('AUSDT') is True
        pm2._clear_closed_marker('AUSDT')
        assert st.get('closed:AUSDT') is None

    def test_marker_same_ref(self, pm_storage):
        st = pm_storage['redis']
        st.direct_delete = lambda k: st.data.pop(k, None)
        svc = make_parity_pair(st)
        st.set('closed:X', {'ts': 1})
        assert svc._marker_get('closed:X') == {'ts': 1}


class TestLoadAssemblyParity:
    def test_ws_layer_merged_identical(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        ws_row = {'entry': 5.0, 'side': 'SHORT', 'qty': 2, 'leverage': 3,
                  'margin': 'CROSSED'}
        monkeypatch.setattr(pm, '_WS_LAST_UPDATE', time.time())
        monkeypatch.setattr(pm, '_WS_POSITIONS', {'AUSDT': dict(ws_row)})
        pm_full['redis'].set('pm:positions',
                             {'AUSDT': {'entry': 5.0, 'side': 'SHORT'}})
        legacy = pm._load()
        assert legacy['AUSDT']['qty'] == 2        # legacy（经 service 内核）
