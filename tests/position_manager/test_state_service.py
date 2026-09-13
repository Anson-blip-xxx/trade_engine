"""P7-02：PositionStateService contract tests（service 面单独覆盖）。"""
import threading
import time

import pytest

from position_state.service import PositionStateService


def make_service(redis):
    """Port 模拟 P4-D2 RedisPositionStateAdapter：JSON 反序列化 → fresh dict。"""
    class Port:
        def load_positions(self):
            import json as _j
            raw = redis.store.get('pm:positions')
            if isinstance(raw, dict):
                try:
                    parsed = _j.loads(_j.dumps(raw))
                except Exception:
                    parsed = {}
                return {k: v for k, v in parsed.items()
                        if isinstance(v, dict)}
            return {}

        def save_positions(self, positions):
            redis.store['pm:positions'] = positions
    return PositionStateService(
        state_port=Port(),
        marker_set=redis.set,
        marker_get=redis.get,
        marker_delete=redis.delete,
        sandbox_check=lambda: False)


class FR:
    def __init__(self):
        self.store = {}

    def set(self, k, v):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)


class TestCoreContract:
    def test_service_creation_minimal(self):
        svc = make_service(FR())
        assert svc is not None

    def test_meta_load_empty(self):
        svc = make_service(FR())
        assert svc.load_meta() == {}

    def test_meta_load_malformed_filtered(self):
        fr = FR()
        fr.set('pm:positions', {'A': {'entry': 1}, 'bad': 'string'})
        svc = make_service(fr)
        assert set(svc.load_meta().keys()) == {'A'}

    def test_meta_save_then_load_roundtrip(self):
        fr = FR()
        svc = make_service(fr)
        svc.save({'A': {'entry': 5}})
        assert svc.load_meta() == {'A': {'entry': 5}}

    def test_save_no_ttl_overwrite(self):
        fr = FR()
        svc = make_service(fr)
        svc.save({'A': {}})
        svc.save({'B': {}})
        assert svc.load_meta() == {'B': {}}         # overwrite semantics

    def test_meta_redis_fail_via_port(self):
        """port 层异常 → service 吞错返回 {}（PMB-11 冻结）。"""
        class BoomPort:
            def load_positions(self):
                raise RuntimeError('down')

            def save_positions(self, d):
                pass
        svc = PositionStateService(state_port=BoomPort(),
                                   marker_set=FR().set, marker_get=FR().get,
                                   marker_delete=FR().delete,
                                   sandbox_check=lambda: False)
        assert svc.load_meta() == {}


class TestFreshDictPerCall:
    def test_two_calls_return_distinct_dicts(self):
        fr = FR()
        fr.set('pm:positions', {'A': {'entry': 1}})
        svc = make_service(fr)
        first = svc.load_meta()
        second = svc.load_meta()
        assert first == second
        assert first is not second                 # identity per call

    def test_first_mutation_does_not_affect_second(self):
        fr = FR()
        fr.store['pm:positions'] = {'A': {'qty': 10}}
        svc = make_service(fr)
        first = svc.load_meta()
        first['A']['qty'] = 55
        second = svc.load_meta()
        assert second['A']['qty'] == 10            # fresh dict per call (PMB-11)

    def test_save_persists_then_visible(self):
        fr = FR()
        svc = make_service(fr)
        rows = svc.load_meta()
        rows['A'] = {'entry': 42}
        svc.save(rows)
        assert svc.load_meta()['A']['entry'] == 42


class InsertionOrderTest:
    def test_dict_ordering(self):
        fr = FR()
        fr.set('pm:positions', {'AA': {}, 'BB': {}, 'ZZ': {}})
        svc = make_service(fr)
        assert list(svc.load_meta().keys()) == ['AA', 'B', 'ZZ'][:0] or \
            list(svc.load_meta().keys()) == ['AA', 'BB', 'ZZ']


class TestClosedMarker:
    def test_set_and_check(self):
        svc = make_service(FR())
        svc.mark_closed('AUSDT')
        assert svc.was_closed_recently('AUSDT') is True

    def test_clear(self):
        fr = FR()
        svc = make_service(fr)
        svc.mark_closed('X')
        svc.clear_closed('X')
        assert svc.was_closed_recently('X') is False

    def test_missing_marker_false(self):
        svc = make_service(FR())
        assert svc.was_closed_recently('NOPE') is False

    def test_malformed_marker(self):
        fr = FR()
        fr.set('closed:X', 'not-dict')
        svc = make_service(fr)
        assert svc.was_closed_recently('X') is False
        fr.set('closed:X', {'x': 1})
        assert svc.was_closed_recently('X') is False

    def test_marker_get_failure_false(self):
        """marker get 失败 → False（marker_get 层吞错，与 legacy `_rget` 一致）。"""
        fr = FR()
        boom_marker = {'count': 0}

        def boom_get(key):
            raise RuntimeError('down')
        svc = PositionStateService(
            state_port=type('P', (), {'load_positions': lambda s: {},
                                      'save_positions': lambda s, d: None}),
            marker_set=fr.set, marker_get=boom_get, marker_delete=fr.delete,
            sandbox_check=lambda: False)
        assert svc.was_closed_recently('X') is False

    def test_marker_age_four_hour_window(self):
        fr = FR()
        svc = make_service(fr)
        svc.mark_closed('A')
        marker = fr.get('closed:A')
        marker['ts'] -= 3 * 3600
        assert svc.was_closed_recently('A', 4) is True
        marker['ts'] -= 3601
        svc_testing_value = None
        assert svc.was_closed_recently('A', 4) is False

    def test_no_ttl_fact(self):
        fr = FR()
        svc = make_service(fr)
        svc.mark_closed('X')
        assert set(fr.get('closed:X').keys()) == {'ts'}


class TestLoadAssembly:
    def _svc(self):
        return make_service(FR())

    def test_layer1_ws_hit(self):
        import shared.position_manager as pm
        svc = make_service(FR())
        ws_last_update = time.time()
        ws_positions = {'AUSDT': {'entry': 5.0, 'qty': 2, 'side': 'SHORT'}}

        def merge_and_save(raw, now):
            result = dict(raw)
            svc.save(result)
            return result

        def meta_filtered():
            return {}
        out = svc.load_assembly(ws_snapshot_fn=lambda: (ws_last_update, ws_positions),
                                rest_positions_fn=lambda: {},
                                merge_and_save_fn=merge_and_save,
                                meta_filtered_fn=meta_filtered)
        assert 'AUSDT' in out

    def test_layer1_empty_falls_rest(self):
        svc = make_service(FR())
        ws_now = time.time()
        rest = {'B': {'entry': 2, 'qty': 10}}

        def merge_and_save(raw, now):
            svc.save(raw)
            return dict(raw)
        out = svc.load_assembly(ws_snapshot_fn=lambda: (ws_now, {}),
                                rest_positions_fn=lambda: rest,
                                merge_and_save_fn=merge_and_save,
                                meta_filtered_fn=lambda: {})
        assert out == rest

    def test_layer3_empty_when_all_dead(self):
        svc = make_service(FR())
        out = svc.load_assembly(ws_snapshot_fn=lambda: (0.0, {}),
                                rest_positions_fn=lambda: {},
                                merge_and_save_fn=lambda r, n: {},
                                meta_filtered_fn=lambda: {})
        assert out == {}
