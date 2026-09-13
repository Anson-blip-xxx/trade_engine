"""P7-01：PM 三层 load chain golden（WS→REST→meta / fallback / marker 过滤）。

补 P7-02 StateService 拆分最可能破坏的 state 语义；与 P1-04 `test_load_golden`
互补（不覆盖 _load_meta 已冻结条目）。
"""
import threading
import time

import pytest

SYM = 'AUSDT'
WS_ROW = {'entry': 5.0, 'side': 'SHORT', 'qty': 2, 'leverage': 3,
          'margin': 'CROSSED'}


@pytest.fixture
def pm_env(pm_storage, monkeypatch):
    """三层隔离：light_fapi spy + ws 状态 monkeypatch。"""
    pm = pm_storage['pm']
    monkeypatch.setattr(pm, '_WS_LAST_UPDATE', 0.0)
    monkeypatch.setattr(pm, '_WS_POSITIONS', {})
    monkeypatch.setattr(pm, '_WS_LOCK', threading.Lock())
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    calls = {'fapi': [], 'fapi_return': []}
    monkeypatch.setattr(pm, '_light_fapi_get',
                        lambda path, params=None: calls['fapi_return'])
    return pm, pm_storage['redis'], calls


class TestLayer1WS:
    def test_ws_fresh_hit_merges_and_saves(self, pm_env):
        pm, redis, calls = pm_env
        pm._WS_LAST_UPDATE = time.time()      # ws_fresh 要求贴近 wall-clock
        pm._WS_POSITIONS['AUSDT'] = dict(WS_ROW)
        redis.set('pm:positions', {'AUSDT': {'entry': 5.0, 'side': 'SHORT',
                                             'open_time': 0.0}})
        out = pm._load()
        assert out['AUSDT']['qty'] == 2       # min(WS qty, meta qty) → 2
        assert redis.get('pm:positions')['AUSDT']['qty'] == 2  # merged+saved

    def test_ws_fresh_only_within_30s(self, pm_env):
        pm, redis, calls = pm_env
        pm._WS_LAST_UPDATE = time.time() - 31  # > 30s → 非 fresh
        pm._WS_POSITIONS['AUSDT'] = dict(WS_ROW)
        # REST 返回空 → 落到 meta 层（meta 里无 qty）
        redis.set('pm:positions', {'AUSDT': {'entry': 5.0}})
        out = pm._load()
        assert out['AUSDT'] == {'entry': 5.0}     # WS 层未生效（非 fresh）


import time  # 供 WS fresh 校验


class TestWSFallback:
    def test_ws_fresh_but_empty_falls_to_rest(self, pm_env):
        pm, redis, calls = pm_env
        pm._WS_LAST_UPDATE = time.time()
        pm._WS_POSITIONS.clear()
        calls['fapi_return'] = [{'symbol': 'AUSDT', 'positionAmt': '-10',
                                 'entryPrice': 5.0, 'leverage': 3,
                                 'marginType': 'CROSSED'}]
        out = pm._load()
        assert out[SYM]['qty'] == 10.0

    def test_ws_not_fresh_falls_to_rest_directly(self, pm_env):
        pm, redis, calls = pm_env
        pm._WS_LAST_UPDATE = 0.0
        calls['fapi_return'] = [{'symbol': 'AUSDT', 'positionAmt': '-10',
                                 'entryPrice': 5.0, 'leverage': 3,
                                 'marginType': 'CROSSED'}]
        out = pm._load()
        assert out[SYM]['qty'] == 10.0

    def test_rest_error_falls_to_meta(self, pm_env):
        pm, redis, calls = pm_env

        def boom(path, params=None):
            raise RuntimeError('rest down')
        pm._light_fapi_get = boom
        redis.set('pm:positions', {SYM: {'entry': 5.0, 'side': 'SHORT'}})
        out = pm._load()
        assert out[SYM]['entry'] == 5.0

    def test_all_fail_empty(self, pm_env):
        pm, redis, calls = pm_env

        def boom(path, params=None):
            raise RuntimeError('x')
        pm._light_fapi_get = boom
        redis.data.clear()
        assert pm._load() == {}

    def test_meta_layer_filtered_by_closed_marker(self, pm_env):
        pm, redis, calls = pm_env
        pm._was_closed_recently = lambda s: s == 'AUSDT1'
        redis.set('pm:positions', {'AUSDT1': {'entry': 5.0},
                                   'AUSDT2': {'entry': 6.0}})
        out = pm._load()
        assert out == {'AUSDT2': {'entry': 6.0}}


class TestAliasingAndMerge:
    def test_rest_merge_alias_not_removed(self, pm_env):
        """ws merge 后 qty=min(ws_qty, meta_qty) 语义（merge 规则）。"""
        pm, redis, calls = pm_env
        redis.set('pm:positions', {'AUSDT': {'entry': 5.0, 'side': 'SHORT',
                                             'qty': 10.0}})
        pm._WS_LAST_UPDATE = time.time()
        pm._WS_POSITIONS['AUSDT'] = dict(WS_ROW)
        out = pm._load()
        assert out['AUSDT']['qty'] == 2

    def test_layer1_ws_positions_read_not_alias(self, pm_env):
        """"_load 时 raw=dict(_WS_POSITIONS) —— 结果不引用全局对象。"""
        pm, redis, calls = pm_env
        pm._WS_LAST_UPDATE = time.time()
        pm._WS_POSITIONS['AUSDT'] = dict(WS_ROW)
        out = pm._load()
        assert out['AUSDT'] is not pm._WS_POSITIONS['AUSDT']
